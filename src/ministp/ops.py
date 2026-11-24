"""Day-to-day operation on a persistent ledger (Phase 2 paper trading).

Three jobs, each a separate process run by the owner:

* ``open_books``  – once: broker cash and holdings become the ledger's opening state.
* ``place_order`` – any time during market hours: risk -> OMS -> broker, intent and
  answer stored in ``oms_orders`` / ``oms_order_acks``, fills booked if they arrive.
* ``daily_close`` – once per business day after the close: settlements due, fills,
  valuation and NAV, then reconciliation stored in ``broker_snapshots`` /
  ``recon_results`` (spec R-70~74).

Everything a job needs is rebuilt from the database, so jobs can run on
different days and any of them can be re-run safely.
"""
import datetime as dt
import json
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from .broker.base import BrokerGateway, OrderAck
from .calendar import BusinessCalendar
from .capacity import Capacity, OpenOrder, capacity, pending_buy_value
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import ExecutionEvent, LateExecutionEvent, Ledger, NavEvent, NavRow, Side, book_date
from .oms import InvalidTransition, OrderStatus
from .quotes import KIND as QUOTE_KIND, Quote, latest_quotes, marks, valid_price
from .recon import Cause, ReconResult, Status
from . import audit
from .session import Anomaly, TradingSession, failure_text
from .store import DEFAULT_ACCOUNT, PostgresLedgerStore, migrate


class OpsError(RuntimeError):
    pass


# ---------- instruments ----------

def add_instrument(conn: psycopg.Connection, inst: Instrument) -> None:
    if conn.execute("select 1 from instruments where symbol = %s", (inst.symbol,)).fetchone():
        raise OpsError(f"이미 등록된 종목이에요: {inst.symbol}")
    with conn.transaction():
        conn.execute("insert into instruments (symbol, name, market, asset_type) values (%s, %s, %s, %s)",
                     (inst.symbol, inst.name, inst.market.value, inst.asset_type.value))
        audit.record(conn, "INSTRUMENT_ADD", target=inst.symbol,
                     detail={"name": inst.name, "market": inst.market.value, "asset_type": inst.asset_type.value})


def load_instruments(conn: psycopg.Connection) -> dict[str, Instrument]:
    return {s: Instrument(s, n, Market(m), AssetType(a)) for s, n, m, a in
            conn.execute("select symbol, name, market, asset_type from instruments")}


# ---------- single writer ----------

WRITER_LOCK = 0x4D53_5450  # "MSTP"


def acquire_writer(conn: psycopg.Connection) -> None:
    """One process appends to the ledger at a time (seq numbers come from the in-memory ledger).
    Held until the connection closes."""
    if not conn.execute("select pg_try_advisory_lock(%s)", (WRITER_LOCK,)).fetchone()[0]:
        raise OpsError("다른 MiniSTP 명령(run 등)이 원장을 쓰고 있어요. 그 창을 먼저 종료해 주세요.")


# ---------- context ----------

@dataclass
class Books:
    conn: psycopg.Connection
    store: PostgresLedgerStore
    ledger: Ledger
    instruments: dict[str, Instrument]
    account_id: str = DEFAULT_ACCOUNT   # the one account these books are (ERD v2.0)
    status: str = "OPEN"                # its current status (account_status_v) when the books were loaded


def require_current_schema(conn: psycopg.Connection) -> None:
    """The code is ahead of the database: never read or write a ledger of another shape."""
    todo = migrate.pending(conn, migrate.discover())
    if todo:
        raise OpsError(f"DB 스키마가 이 버전의 코드보다 오래됐어요. 남은 마이그레이션 {len(todo)}개를 "
                       "db-migrate로 적용해 주세요 (먼저 --dry-run으로 리허설).")


def load_books(conn: psycopg.Connection, calendar: BusinessCalendar, fees: FeeSchedule,
               account_id: str = DEFAULT_ACCOUNT) -> Books:
    """The books of one account, replayed from its events and checked against the stored rows."""
    require_current_schema(conn)
    row = conn.execute("select status from account_status_v where account_id = %s", (account_id,)).fetchone()
    if row is None:
        raise OpsError(f"등록되지 않은 계좌예요: {account_id}. account list로 확인해 주세요.")
    store = PostgresLedgerStore(conn, account_id)
    instruments = load_instruments(conn)
    ledger = store.load(calendar, fees, instruments)
    problems = store.verify(ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")
    return Books(conn, store, ledger, instruments, account_id, row[0])


def _snapshot(conn, day: dt.date, kind: str, payload, account_id: str = DEFAULT_ACCOUNT) -> int:
    payload = json.loads(json.dumps(payload, default=str))
    return conn.execute("insert into broker_snapshots (date, kind, payload, account_id) values (%s, %s, %s, %s)"
                        " returning snapshot_id", (day, kind, Jsonb(payload), account_id)).fetchone()[0]


# ---------- open ----------

def is_opened(books: Books) -> bool:
    """`open` has run for this account. An account that was empty at the broker has no event to show for it,
    only the stored answer of the broker."""
    return bool(books.ledger.events) or books.conn.execute(
        "select 1 from broker_snapshots where kind = 'OPENING_BALANCE' and account_id = %s limit 1",
        (books.account_id,)).fetchone() is not None


def open_books(books: Books, broker: BrokerGateway, day: dt.date) -> None:
    """R-05: start the ledger from the broker's settled cash and holdings."""
    if is_opened(books):
        raise OpsError("원장이 이미 시작됐어요. 시작값은 한 번만 기록해요.")
    if books.status != "OPEN":
        raise OpsError(f"계좌 {books.account_id}가 정상 상태가 아니에요 ({books.status}).")
    bal = broker.get_balance()
    if bal.cash_d0 != bal.cash_projected:
        raise OpsError(f"미결제 거래가 있어요 (D+0 {bal.cash_d0:,} / D+2 {bal.cash_projected:,}). "
                       "결제가 끝난 뒤(다음 영업일 이후) 다시 실행해 주세요.")
    missing = sorted(set(bal.positions) - set(books.instruments))
    if missing:
        raise OpsError(f"종목 등록이 필요해요: {', '.join(missing)}")
    _snapshot(books.conn, day, "OPENING_BALANCE", list(bal.raw), books.account_id)
    if bal.cash_d0:   # an account that is still empty starts with no event: its first deposit opens the books
        books.ledger.deposit(day, bal.cash_d0)
    for sym in sorted(bal.positions):
        p = bal.positions[sym]
        books.ledger.open_position(day, sym, p.qty, p.purchase_amount)
    audit.record(books.conn, "OPEN", account_id=books.account_id, target=day.isoformat(),
                 detail={"cash": bal.cash_d0, "positions": len(bal.positions)})


# ---------- orders ----------

def _stored_orders(conn, day: dt.date, account_id: str = DEFAULT_ACCOUNT):
    return conn.execute(
        "select o.client_id, o.symbol, o.side, o.qty, o.limit_price, a.outcome, a.order_no, a.org_no"
        " from oms_orders o left join oms_order_acks a using (client_id)"
        " where o.trade_date = %s and o.account_id = %s order by o.client_id", (day, account_id)).fetchall()


CANCEL_KIND = "CANCEL_REQUEST"   # R-45: a cancel the broker took, so a restarted process knows it


def session_for(books: Books, broker: BrokerGateway, day: dt.date) -> TradingSession:
    """A session that knows every order MiniSTP placed today and persists new ones."""
    session = TradingSession(books.ledger, broker, account_open=books.status == "OPEN",
                             id_prefix="S" if books.account_id == DEFAULT_ACCOUNT else f"{books.account_id}-S")
    rows = _stored_orders(books.conn, day, books.account_id)
    cancel_requested = {r[0] for r in books.conn.execute(
        "select payload ->> 'client_id' from broker_snapshots where kind = %s and date = %s and account_id = %s",
        (CANCEL_KIND, day, books.account_id))}
    for cid, sym, side, qty, limit, outcome, order_no, org_no in rows:
        if outcome == "ACCEPTED":
            session.restore(day, cid, sym, Side(side), qty, limit, OrderAck(order_no, org_no),
                            cancel_requested=cid in cancel_requested)
        elif outcome is None:
            # R-103: intent stored but no answer recorded (the answer was lost, or the process died around
            # the broker call). Tracked again so its cash stays reserved and a sync can find it.
            session.restore_unacked(day, cid, sym, Side(side), qty, limit)
    # Next number after the highest stored one. Orders refused by the order check never reach the DB,
    # so counting rows would hand out a number already used today (S…-0003 twice).
    session._seq = max((int(r[0].rsplit("-", 1)[1]) for r in rows), default=0)

    def on_submit(cid, d, symbol, side, qty, limit_price):
        books.conn.execute(
            "insert into oms_orders (client_id, trade_date, symbol, side, qty, limit_price, account_id)"
            " values (%s, %s, %s, %s, %s, %s, %s)", (cid, d, symbol, side.value, qty, limit_price, books.account_id))

    def on_ack(cid, ack, detail):
        o = session.oms.orders[cid]
        asked = _order_detail(o.symbol, o.side, o.qty, o.limit_price)
        with books.conn.transaction():   # the answer and its audit row together
            if ack is None:
                books.conn.execute("insert into oms_order_acks (client_id, outcome, detail)"
                                   " values (%s, 'REJECTED', %s)", (cid, detail))
                audit.record(books.conn, "ORDER_CONFIRM" if session.confirming else "ORDER", "FAILED",
                             account_id=books.account_id, target=cid, detail=asked, message=detail)
            else:
                books.conn.execute("insert into oms_order_acks (client_id, outcome, order_no, org_no, detail)"
                                   " values (%s, 'ACCEPTED', %s, %s, %s)",
                                   (cid, ack.order_no, ack.org_no, detail or None))
                audit.record(books.conn, "ORDER_CONFIRM" if session.confirming else "ORDER",
                             account_id=books.account_id, target=cid,
                             detail=asked | {"order_no": ack.order_no}, message=detail or None)

    def on_unknown(cid, detail):
        # R-103: no ack row on purpose. The order stays "sent, answer unknown" until it is found.
        o = session.oms.orders[cid]
        audit.record(books.conn, "ORDER", "FAILED", account_id=books.account_id, target=cid,
                     detail=_order_detail(o.symbol, o.side, o.qty, o.limit_price) | {"unknown": True},
                     message=f"접수 결과 미확인: {detail}")

    def on_refused(d, symbol, side, qty, limit_price, detail):
        # R-85: an order the pre-check refused is in no order table. This row is all that is left of it.
        kind, _, code = detail.partition(": ")
        reason = code if kind == "risk" else "KILL_SWITCH" if kind == "kill switch engaged" else None
        audit.record(books.conn, "ORDER", "REFUSED" if reason else "FAILED",   # a rule said no / the price call broke
                     account_id=books.account_id, message=failure_text(detail),
                     detail=_order_detail(symbol, side, qty, limit_price) | {"date": d, "reason": reason})

    def on_cancel(cid, ack):
        o = session.oms.orders[cid]
        with books.conn.transaction():   # the request and its audit row together (R-45)
            _snapshot(books.conn, day, CANCEL_KIND, {"client_id": cid, "order_no": ack.order_no,
                                                     "requested_at": dt.datetime.now().isoformat(timespec="seconds")},
                      books.account_id)
            audit.record(books.conn, "ORDER_CANCEL", account_id=books.account_id, target=cid,
                         detail=_order_detail(o.symbol, o.side, o.qty, o.limit_price) | {"order_no": ack.order_no})

    session.on_submit, session.on_ack, session.on_refused, session.on_cancel = on_submit, on_ack, on_refused, on_cancel
    session.on_unknown = on_unknown
    return session


def _order_detail(symbol: str, side, qty: int, limit_price: int | None) -> dict:
    return {"symbol": symbol, "side": getattr(side, "value", side), "qty": qty, "limit_price": limit_price or None}


def cancel_order(books: Books, broker: BrokerGateway, day: dt.date, ref: str,
                 wait=None) -> tuple[TradingSession, str]:
    """R-45: cancel the unfilled rest of one of today's MiniSTP orders (`ref` as `TradingSession.find`).
    If `wait` is given (callable returning False to stop), poll until the broker confirms."""
    session = session_for(books, broker, day)
    session.sync(day)                     # fills that came in first are booked first (R-42)
    try:
        cid = session.find(ref)
    except KeyError:
        raise OpsError(f"오늘 MiniSTP가 낸 주문 가운데 {ref}을(를) 찾지 못했어요.") from None
    try:
        session.cancel(cid)
    except InvalidTransition as exc:
        raise OpsError(str(exc).split(": ", 1)[-1]) from None
    if wait is not None:
        while True:
            session.sync(day)
            if session.oms.orders[cid].status is not OrderStatus.CANCEL_PENDING or not wait():
                break
    return session, cid


def confirm_order(books: Books, broker: BrokerGateway, day: dt.date, ref: str,
                  order_no: str | None) -> tuple[TradingSession, str, str]:
    """R-103: settle an order of today that was sent with no answer. `order_no`: the broker's number for it
    (from the HTS or the broker's order list); None: the broker has no such order, so it is closed as
    refused and its request line counts as failed. A sync runs first, so one found there needs nothing.
    Returns (session, client id, how): "found" (the sync found it), "confirmed" or "not_placed"."""
    session = session_for(books, broker, day)
    waiting = set(session.unacked)
    session.sync(day)
    try:
        cid = session.find(ref)
    except KeyError:
        raise OpsError(f"오늘 MiniSTP가 낸 주문 가운데 {ref}을(를) 찾지 못했어요.") from None
    if cid not in session.unacked:
        if cid in waiting:                # the sync just found it in the broker's list
            return session, cid, "found"
        o = session.oms.orders[cid]
        raise OpsError(f"주문 {cid}는 결과를 기다리는 주문이 아니에요 (지금 {o.status.value}).")
    try:
        session.confirm_unacked(day, cid, order_no)
    except InvalidTransition as exc:
        raise OpsError(str(exc).split(": ", 1)[-1]) from None
    session.sync(day)                     # a found order may already have fills
    return session, cid, "not_placed" if order_no is None else "confirmed"


def stored_open_orders(books: Books, day: dt.date) -> list[OpenOrder]:
    """Today's live orders as the DB and ledger know them (주문 의도 + 접수 - 반영된 체결).

    Cancels made outside MiniSTP show up only after a broker sync, so this errs on
    the safe side: it may still reserve cash for an order the broker already dropped.
    """
    return session_for(books, None, day).open_orders()


def order_capacity(books: Books, day: dt.date, symbol: str, ref_price: int,
                   ref_prices: dict[str, int] | None = None) -> Capacity:
    """주문가능 for one symbol from the DB, for screens (조회 전용, no broker call)."""
    return capacity(books.ledger, stored_open_orders(books, day), symbol, ref_price,
                    ref_prices=ref_prices)


def place_order(books: Books, broker: BrokerGateway, day: dt.date, symbol: str, side: Side,
                qty: int, limit_price: int | None = None, wait=None) -> tuple[TradingSession, str | None]:
    """Submit one order; if `wait` is given (callable returning False to stop), poll for fills.

    결재를 거치지 않는 낮은 단계 경로라 샘플 DB(모의 증권사)만 써요. 실제 주문(`order`, `run` 창,
    결재 요청)은 `approval`을 거쳐 승인된 요청만 나가요 (R-100~101)."""
    if symbol not in books.instruments:
        raise OpsError(f"종목 등록이 필요해요: {symbol}")
    session = session_for(books, broker, day)
    cid = session.submit(day, symbol, side, qty, limit_price)
    if cid is not None and wait is not None:
        while True:
            session.sync(day)
            if session.oms.orders[cid].status is OrderStatus.FILLED or not wait():
                break
    return session, cid


# ---------- daily close ----------

@dataclass
class DailyReport:
    day: dt.date
    settled: list[str]
    booked: list[str]
    nav: object | None
    results: list[ReconResult]
    anomalies: list[Anomaly]
    run_id: int
    notes: list[str] = field(default_factory=list)
    closed: bool = False        # R-77: this run closed the day

    @property
    def clean(self) -> bool:
        """R-74: no UNKNOWN cause and no unresolved break."""
        return all(r.status is Status.MATCH for r in self.results)


# Why a day is not closed yet (ledger.CLOSE_BLOCKERS), for a person.
CLOSE_BLOCKER_TEXT = {
    "ALREADY_CLOSED": "이미 마감한 날이에요",
    "LATER_ENTRIES": "다음 날 장부가 이미 시작돼서 이 날만 따로 마감할 수 없어요. 다음 마감이 이 날까지 함께 닫아요",
    "NAV_NOT_LAST": "그날 기준가를 산출한 바로 뒤에만 마감해요. daily를 먼저 실행해 주세요",
    "UNSETTLED": "결제일이 지났는데 결제하지 않은 거래가 있어요. daily를 먼저 실행해 주세요",
}


def _nav_outdated(ledger: Ledger, day: dt.date) -> bool:
    """R-75: the day has a NAV, and something was booked on that same day after it."""
    events = ledger.events
    last = max(i for i, e in enumerate(events) if isinstance(e, NavEvent) and e.date == day)
    after = events[last + 1:]
    return bool(after) and all(book_date(e) == day for e in after)


def close_books(books: Books, day: dt.date) -> NavRow:
    """R-77: close the day by hand, once every break of its last daily recon has been judged.

    `daily` closes a day itself when its recon is clean. This is for the day that had a break:
    the owner judges it (`resolve`), then closes. The rule is the one the gate and the 대사 달력
    read (`recon_days`), so a day that counts as clean is a day that can be closed."""
    days = recon_days(books.conn, date_from=day, date_to=day, account_id=books.account_id)
    if day not in days:
        raise OpsError(f"{day} 일 마감 대사 기록이 없어요. daily를 먼저 실행해 주세요.")
    if not days[day].clean:
        raise OpsError(f"{day} 대사에 판단하지 않은 불일치가 {days[day].open_breaks}건 있어요. "
                       "resolve로 원인을 판단한 뒤 마감해 주세요.")
    reason = books.ledger.close_blocker(day)
    if reason:
        raise OpsError(f"{day} 장부를 마감할 수 없어요: {CLOSE_BLOCKER_TEXT[reason]}.")
    with books.conn.transaction():   # the close and its audit row together
        nav = books.ledger.close_day(day)
        problems = books.store.verify(books.ledger)
        if problems:
            raise OpsError(f"DB 원장 검증 실패: {problems}")
        audit.record(books.conn, "CLOSE", account_id=books.account_id, target=day.isoformat(),
                     detail={"net_assets": nav.net_assets, "nav_per_1000": nav.nav_per_1000})
    return nav


def daily_close(books: Books, broker: BrokerGateway, day: dt.date) -> DailyReport:
    ledger = books.ledger
    if not is_opened(books):
        raise OpsError("원장 시작값이 없어요. 먼저 open 명령을 실행해 주세요.")
    notes = []

    # 1. settlements due, each on its own settlement date (R-12)
    settled = []
    while (sd := ledger.next_settlement_date(day)) is not None:
        settled += ledger.settle_due(sd)

    # 2. fills of orders MiniSTP placed today
    session = session_for(books, broker, day)
    booked = session.sync(day)
    for e in ledger.events[-len(booked):] if booked else ():
        if isinstance(e, LateExecutionEvent):   # R-78: the day was closed, so the fill is a correction
            notes.append(f"마감 후 체결 {e.exec_id}: {e.trade_date}은 마감한 날이라 {e.booked_on} 장부에 정정으로 기록했어요.")

    # 2b. dividends from the register: recognised, paid, taken back (R-57~59). A closed day takes none.
    if not ledger.is_closed(day):
        from .rights import apply_due   # rights builds on this module
        dividends = apply_due(books, day)
        notes += dividends
    else:
        dividends = []

    # 3. valuation and NAV at the close (R-55, R-60). Until the day is closed it can be struck again (R-75).
    nav = None
    has_nav = any(isinstance(e, NavEvent) and e.date == day for e in ledger.events)
    if ledger.is_closed(day):
        notes.append(f"{day}은 이미 마감한 날이라 대사만 다시 실행했어요.")
    elif has_nav and not _nav_outdated(ledger, day):
        notes.append("오늘 NAV는 이미 기록돼 있어 대사만 다시 실행했어요.")
    else:
        if has_nav:
            notes.append("기준가를 산출한 뒤에 반영된 거래가 있어, 마감 전이라 기준가를 다시 산출했어요.")
        closes = {sym: broker.get_price(sym) for sym in sorted(ledger.positions())}
        ledger.revalue(day, closes)
        nav = ledger.nav(day, closes)

    # 4. reconciliation, broker answers stored first (R-70)
    results = session.reconcile(day)
    orders = broker.daily_orders(day)
    run_id = _snapshot(books.conn, day, "DAILY_RECON", {
        "balance": list(session.last_balance.raw),
        "orders": [o.__dict__ for o in orders],
        "anomalies": [a.__dict__ | {"day": a.day.isoformat()} for a in session.anomalies],
    }, books.account_id)
    _insert_results(books.conn, run_id, results, books.account_id)

    # 5. close the day when the recon is clean (R-77). A day with a break waits for the owner's judgement.
    closed = False
    if not ledger.is_closed(day):
        reason = ledger.close_blocker(day)
        if any(r.status is not Status.MATCH for r in results):
            notes.append("불일치가 있어 장부를 마감하지 않았어요. resolve로 원인을 판단한 뒤 close로 마감해 주세요.")
        elif reason:
            notes.append(f"장부를 마감하지 않았어요: {CLOSE_BLOCKER_TEXT[reason]}.")
        else:
            ledger.close_day(day)
            closed = True
    problems = books.store.verify(ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")
    breaks = sum(r.status is not Status.MATCH for r in results)
    audit.record(books.conn, "DAILY", account_id=books.account_id, target=day.isoformat(),
                 detail={"run_id": run_id, "settled": len(settled), "booked": len(booked), "breaks": breaks,
                         "dividends": len(dividends) or None,
                         "closed": closed, "nav_per_1000": nav.nav_per_1000 if nav else None})
    return DailyReport(day, settled, booked, nav, results, session.anomalies, run_id, notes, closed)


GATE_DAYS = 20   # R-74: this many clean business days in a row open the next stage


@dataclass(frozen=True)
class ReconDay:
    """One date's recon as its latest run left it."""
    run_id: int
    items: int          # rows the run compared
    breaks: int         # of those, not a match
    open_breaks: int    # breaks nobody has judged yet (no later row supersedes them)

    @property
    def clean(self) -> bool:
        """R-74: no break, or every break resolved by a later row."""
        return self.open_breaks == 0


def recon_days(conn: psycopg.Connection, kind: str = "DAILY_RECON", date_from: dt.date | None = None,
               date_to: dt.date | None = None, account_id: str = DEFAULT_ACCOUNT) -> dict[dt.date, ReconDay]:
    """Each date's latest run of `kind` for one account, summed up. The gate count and the 대사 달력
    both read this, so a day the calendar draws as clean is a day the gate counts."""
    rows = conn.execute(
        "select r.date, r.run_id, count(*), count(*) filter (where r.status <> 'MATCH'),"
        " count(*) filter (where r.status <> 'MATCH' and not exists"
        " (select 1 from recon_results x where x.supersedes_id = r.id))"
        " from recon_results r join broker_snapshots b on b.snapshot_id = r.run_id"
        " where b.kind = %(kind)s and r.account_id = %(acct)s and r.supersedes_id is null"
        " and (%(f)s::date is null or r.date >= %(f)s) and (%(t)s::date is null or r.date <= %(t)s)"
        " group by r.date, r.run_id order by r.date, r.run_id",
        {"kind": kind, "acct": account_id, "f": date_from, "t": date_to}).fetchall()
    return {date: ReconDay(run_id, items, breaks, open_breaks)   # a later run of the same date replaces the earlier
            for date, run_id, items, breaks, open_breaks in rows}


def clean_streak(conn: psycopg.Connection, calendar: BusinessCalendar, up_to: dt.date,
                 account_id: str = DEFAULT_ACCOUNT) -> int:
    """R-74 gate, per account: consecutive business days, ending at `up_to`, whose latest run
    had no break (or whose breaks were all resolved by a later row)."""
    days = recon_days(conn, date_to=up_to, account_id=account_id)
    streak, d = 0, up_to
    while d in days and days[d].clean:
        streak += 1
        d = _prev(calendar, d)
    return streak


def _prev(calendar: BusinessCalendar, d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while not calendar.is_business_day(d):
        d -= dt.timedelta(days=1)
    return d


def resolve_break(conn: psycopg.Connection, result_id: int, cause: Cause, note: str) -> int:
    """R-73: the owner's judgement on a break, added as a new row (never an update)."""
    row = conn.execute("select run_id, date, item, key, internal_value, broker_value, diff, account_id, status"
                       " from recon_results where id = %s", (result_id,)).fetchone()
    if row is None or row[8] != "BREAK":
        raise OpsError(f"불일치 행이 아니에요: {result_id}")
    with conn.transaction():
        new_id = conn.execute(   # the judgement belongs to the account of the row it judges
            "insert into recon_results (run_id, date, item, key, internal_value, broker_value, diff, account_id,"
            " status, cause_code, note, supersedes_id) values (%s, %s, %s, %s, %s, %s, %s, %s, 'RESOLVED',"
            " %s, %s, %s) returning id", (*row[:8], cause.value, note, result_id)).fetchone()[0]
        audit.record(conn, "RESOLVE", account_id=row[7], target=str(result_id),
                     detail={"cause": cause.value, "note": note, "date": row[1], "item": row[2], "key": row[3],
                             "row": new_id})
    return new_id


def _insert_results(conn: psycopg.Connection, run_id: int, results, account_id: str) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "insert into recon_results (run_id, date, item, key, internal_value, broker_value, diff,"
            " status, cause_code, account_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(run_id, r.date, r.item, r.key, r.internal, r.broker, r.diff, r.status.value,
              r.cause.value if r.cause else None, account_id) for r in results])


def store_recon(conn: psycopg.Connection, day: dt.date, kind: str, payload, results,
                account_id: str = DEFAULT_ACCOUNT) -> int:
    run_id = _snapshot(conn, day, kind, payload, account_id)
    _insert_results(conn, run_id, results, account_id)
    return run_id


# ---------- real-time IBOR (spec D6) ----------

@dataclass
class IntradayStats:
    notices: int = 0
    fills_booked: int = 0
    polls_booked: int = 0
    recons: int = 0
    recon_breaks: int = 0
    latencies_ms: list[int] = field(default_factory=list)
    feed_drops: int = 0
    quotes: int = 0            # 현재가를 받은 횟수 (R-68)
    quote_failures: int = 0


class IntradayRunner:
    """Main loop of `python -m ministp run`: the only ledger writer during market hours.

    Events arrive on one queue from the fill-notice thread and the console thread;
    everything that touches the ledger or the DB happens here, in order.
    """

    def __init__(self, books: Books, broker: BrokerGateway, day: dt.date, events, *,
                 now=dt.datetime.now, monotonic=None, out=print,
                 poll_every: float = 60.0, recon_every: float = 300.0, quote_every: float = 60.0,
                 send_every: float = 5.0, settle_wait=None):
        import time as _time
        self.books, self.broker, self.day, self.events = books, broker, day, events
        self.now, self.out = now, out
        self.monotonic = monotonic or _time.monotonic
        self.poll_every, self.recon_every, self.quote_every = poll_every, recon_every, quote_every
        self.send_every = send_every     # 승인된 주문 요청을 집어 보내는 간격 (R-101)
        self.settle_wait = settle_wait or (lambda: _time.sleep(3))
        self.session = session_for(books, broker, day)
        self.stats = IntradayStats()
        self.feed_up = False
        self.quotes: dict[str, Quote] = latest_quotes(books.conn, day)   # 다시 띄워도 오늘 받은 가격부터
        self._quote_failing: set[str] = set()
        self._last_poll = self._last_recon = self._last_quote = self._last_send = self.monotonic()
        self._paused_for: tuple[str, ...] = ()   # 답이 없는 주문 때문에 전송을 멈춘 주문들 (R-103)

    # --- handlers ---

    def on_notice(self, n, received_at: dt.datetime) -> None:
        self.stats.notices += 1
        if not (n.is_fill or n.rejected):
            self.out(f"[{received_at:%H:%M:%S}] 접수 {n.side} {n.symbol} x{n.qty} (주문 {n.order_no})")
            return
        t0 = self.monotonic()
        booked = self.session.sync(self.day)
        ms = int((self.monotonic() - t0) * 1000)
        self.stats.fills_booked += len(booked)
        if booked:
            self.stats.latencies_ms.append(ms)
        _snapshot(self.books.conn, self.day, "FILL_NOTICE", {
            "notice": n.__dict__, "received_at": received_at.isoformat(),
            "booked": booked, "booking_ms": ms}, self.books.account_id)
        what = "거부" if n.rejected else f"체결 {n.qty}주 @ {n.price:,}"
        self.out(f"[{received_at:%H:%M:%S}] {what} {n.side} {n.symbol} → 원장 반영 {len(booked)}건 ({ms}ms)")
        self._report_anomalies()

    def on_feed(self, state: str, detail: str, at: dt.datetime) -> None:
        if state == "up":
            self.out(f"[{at:%H:%M:%S}] 실시간 체결통보 연결됨")
            if not self.feed_up:
                self.poll("재연결 후 누락분 확인")
            self.feed_up = True
        else:
            self.feed_up = False
            self.stats.feed_drops += 1
            self.out(f"[{at:%H:%M:%S}] 체결통보 연결 끊김: {detail}. 1분마다 조회로 보완해요.")

    def settle(self) -> list[str]:
        """R-12 in IBOR: book the settlements due today as soon as the runner sees the day, the way the
        broker's 예수금 D+0 already includes them, instead of waiting for `daily`. Without this the
        intraday cash recon breaks on every settlement day until the close."""
        settled = []
        while (sd := self.books.ledger.next_settlement_date(self.day)) is not None:
            settled += self.books.ledger.settle_due(sd)
        if settled:
            self.out(f"결제 {len(settled)}건 반영, 예수금 D+0 {self.books.ledger.cash_balance():,}")
        return settled

    def poll(self, why: str = "") -> list[str]:
        self.settle()
        booked = self.session.sync(self.day)
        self._last_poll = self.monotonic()
        if booked:
            self.stats.polls_booked += len(booked)
            self.out(f"조회로 반영한 체결 {len(booked)}건{' (' + why + ')' if why else ''}")
        self._report_anomalies()
        return booked

    def recon(self, verbose: bool = False) -> list:
        """Intraday reconciliation (D6). A break is re-checked once to rule out timing."""
        self.poll()
        results = self.session.reconcile(self.day)
        if any(r.status is Status.BREAK for r in results):
            self.settle_wait()
            self.poll()
            results = self.session.reconcile(self.day)
        self._last_recon = self.monotonic()
        store_recon(self.books.conn, self.day, "INTRADAY_RECON",
                    {"balance": list(self.session.last_balance.raw)}, results, self.books.account_id)
        self.stats.recons += 1
        breaks = [r for r in results if r.status is Status.BREAK]
        self.stats.recon_breaks += len(breaks)
        if breaks or verbose:
            for r in results:
                mark = "OK   " if r.status is Status.MATCH else "BREAK"
                self.out(f"  {mark} {r.item:15} {r.key:10} 내부 {r.internal:>15,}  증권사 {r.broker:>15,}")
        if not breaks and verbose:
            self.out("장중 대사 일치")
        return results

    def quote(self) -> dict[str, Quote]:
        """현재가 (R-68): 보유 종목과 미체결 주문 종목의 현재가를 받아 증권사 기록에 남겨요.

        IBOR 평가에만 쓰고 원장에는 넣지 않아요. 한 종목이 실패해도 원장 기록자는 멈추지 않고,
        그 종목은 마지막으로 받은 가격(없으면 종가)으로 평가해요."""
        self._last_quote = self.monotonic()
        symbols = sorted(set(self.books.ledger.positions()) | {o.symbol for o in self.session._open_orders()})
        if not symbols:
            return {}
        at = self.now().replace(microsecond=0)
        prices, failed = {}, {}
        for sym in symbols:
            try:
                price = self.broker.get_price(sym)
            except Exception as exc:    # a quote must never stop the only ledger writer
                failed[sym] = str(exc) or type(exc).__name__
                continue
            if valid_price(price):
                prices[sym] = price
            else:
                failed[sym] = f"잘못된 가격 {price!r}"
        _snapshot(self.books.conn, self.day, QUOTE_KIND,
                  {"at": at.isoformat(), "prices": prices, "failed": failed})
        self.quotes.update({sym: Quote(sym, p, at) for sym, p in prices.items()})
        self.stats.quotes += 1
        self.stats.quote_failures += len(failed)
        for sym in sorted(set(failed) - self._quote_failing):
            self.out(f"[{at:%H:%M:%S}] 현재가를 받지 못했어요: {sym} ({failed[sym]}). 마지막 가격으로 평가해요.")
        for sym in sorted(self._quote_failing & set(prices)):
            self.out(f"[{at:%H:%M:%S}] 현재가 다시 받는 중: {sym}")
        self._quote_failing = set(failed)
        return prices

    def on_command(self, line: str) -> bool:
        """Returns False to stop."""
        parts = line.strip().split()
        if not parts:
            return True
        cmd = parts[0].lower()
        try:
            if cmd in ("quit", "exit", "q"):
                return False
            if cmd in ("buy", "sell") and len(parts) in (3, 4):
                symbol, qty = parts[1], int(parts[2])
                limit = int(parts[3]) if len(parts) == 4 else None
                self.order(symbol, Side(cmd.upper()), qty, limit)
                return True
            if cmd == "cancel" and len(parts) == 2:
                self.cancel(parts[1])
                return True
            if cmd == "confirm" and len(parts) == 3:
                self.confirm(parts[1], None if parts[2].lower() in ("none", "없음", "--not-placed") else parts[2])
                return True
            if cmd == "status":
                self.print_status()
                return True
            if cmd == "recon":
                self.recon(verbose=True)
                return True
        except (ValueError, OpsError) as exc:
            self.out(f"오류: {exc}")
            return True
        self.out("명령: buy 종목 수량 [지정가] | sell 종목 수량 [지정가] | cancel 주문 | confirm 주문 증권사번호|none"
                 " | status | recon | quit")
        return True

    def order(self, symbol: str, side: Side, qty: int, limit: int | None) -> None:
        """창에서 친 주문도 결재를 거쳐요(11/24): 요청을 올리고, 셀프 승인 허용이 있으면 승인해 바로 보내요."""
        from . import approval
        try:
            right = approval.rights_of(self.books.conn, approval.actor(), self.books.account_id,
                                       self.day).get("ORDER_APPROVE")
            self_ok = bool(right and right.self_approve)
            prices = {} if limit or symbol not in self.books.instruments else {symbol: self.broker.get_price(symbol)}
            (key,), _ = approval.submit(self.books, self.day, self.day, [approval.Line(symbol, side, qty, limit)],
                                        self_approve=self_ok, now=self.now(), prices=prices)
        except OpsError as exc:
            self.out(f"주문 요청 거부: {exc}")
            audit.record_refusal(self.books.conn, "ORDER", exc, account_id=self.books.account_id,
                                 detail=_order_detail(symbol, side.value, qty, limit) | {"date": self.day})
            return
        if not self_ok:
            self.out(f"결재 요청 {key.request_no}을 올렸어요. 승인되면 바로 보내요.")
            return
        self.send_requests()

    def send_requests(self) -> None:
        """승인된 오늘 요청을 보내요 (R-101). 보내기 직전에 묶음 검증을 다시 해요."""
        from . import approval
        self._last_send = self.monotonic()
        waiting = self.session.unacked
        if waiting:                       # R-103: 찾을 때까지 보내지 않아요. 같은 안내는 한 번만
            if tuple(waiting) != self._paused_for:
                self._paused_for = tuple(waiting)
                self.out(f"[{self.now():%H:%M:%S}] 접수 결과를 모르는 주문 {', '.join(waiting)} 때문에 전송을 멈췄어요. "
                         "조회로 증권사 주문내역에서 찾으면 이어서 보내요. 없으면 `confirm 주문ID --not-placed`")
            return
        if self._paused_for:
            self._paused_for = ()
            self.out(f"[{self.now():%H:%M:%S}] 답이 없던 주문을 모두 확인했어요. 승인된 요청을 이어서 보내요.")
        for s in approval.send_approved(self.books, self.session, self.day, now=self.now()):
            if s.unknown:
                self.out(f"[{self.now():%H:%M:%S}] {s} → 주문 {s.client_id} 증권사 답 없음. 다시 보내지 않고 "
                         "다음 조회에서 찾아요. 그때까지 다른 요청도 멈춰요.")
            elif s.client_id:
                self.out(f"[{self.now():%H:%M:%S}] {s} → 주문 {s.client_id} 접수. 체결되면 바로 원장에 반영해요.")
            else:
                self.out(f"[{self.now():%H:%M:%S}] {s} 전송 실패: {s.detail}")

    def confirm(self, ref: str, order_no: str | None) -> None:
        """R-103: `confirm 3 0000012345` (답이 없던 주문의 증권사 주문번호) or `confirm 3 none` (증권사에 없음)."""
        self.poll("확인 전 조회")          # often the order list already settles it
        try:
            cid = self.session.find(ref)
        except KeyError:
            self.out(f"오늘 MiniSTP가 낸 주문 가운데 {ref}을(를) 찾지 못했어요.")
            return
        if cid not in self.session.unacked:
            self.out(f"주문 {cid}는 결과를 기다리는 주문이 아니에요 (지금 {self.session.oms.orders[cid].status.value}).")
            return
        try:
            self.session.confirm_unacked(self.day, cid, order_no)
        except InvalidTransition as exc:
            self.out(f"확인하지 않았어요: {str(exc).split(': ', 1)[-1]}")
            return
        self.out(f"주문 {cid}: " + (f"증권사 주문 {order_no}로 확인했어요." if order_no else
                                     "증권사에 없는 주문으로 닫았어요. 그 줄은 전송 실패로 남아요. 필요하면 새로 요청해 주세요."))
        self.poll("확인 후 조회")

    def cancel(self, ref: str) -> None:
        """R-45: `cancel 3` (오늘 세 번째 주문), `cancel S20261124-0003` or the broker's order number."""
        self.poll("취소 전 확인")          # a fill that arrived first is booked first (R-42)
        try:
            cid = self.session.find(ref)
        except KeyError:
            self.out(f"오늘 MiniSTP가 낸 주문 가운데 {ref}을(를) 찾지 못했어요. status로 미체결 주문을 확인해 주세요.")
            return
        try:
            self.session.cancel(cid)
        except InvalidTransition as exc:
            self.out(f"취소하지 않았어요: {str(exc).split(': ', 1)[-1]}")
            return
        except Exception as exc:          # the broker refused or the network failed: nothing changed
            self.out(f"취소 요청 실패: {exc}. 주문은 그대로예요.")
            return
        self.out(f"주문 {cid} 취소 요청을 증권사가 받았어요. 취소 확인은 다음 조회 때 반영해요.")
        self.poll("취소 확인")

    def print_status(self) -> None:
        led = self.books.ledger
        reserved, unpriced = pending_buy_value(self.session.open_orders(), {})
        self.out(f"IBOR  예수금 D+0 {led.cash_balance():,}  결제 후 {led.projected_cash():,}  "
                 f"미체결 매수 예약 {reserved:,}  주문가능 {led.projected_cash() - reserved:,}"
                 + (f"  (시장가 매수 {unpriced}건은 가격 미정이라 예약에서 빠짐)" if unpriced else ""))
        from .queries import last_closes
        prices = marks(*last_closes(led), self.quotes, self.now())
        value = pnl = 0
        for sym, pos in led.positions().items():
            m = prices.get(sym)
            if m is None:
                self.out(f"  {sym}: {pos.qty}주, 매입금액 {pos.book_cost:,}, 평가 가격 없음")
                continue
            v = pos.qty * m.price
            value, pnl = value + v, pnl + v - pos.book_cost
            self.out(f"  {sym}: {pos.qty}주, 매입금액 {pos.book_cost:,}, {m.label} {m.price:,}, "
                     f"평가금액 {v:,}, 평가손익 {v - pos.book_cost:+,}")
        if led.positions():
            self.out(f"  평가금액 합계 {value:,}, 평가손익 합계 {pnl:+,} (IBOR, 기준가에는 쓰지 않아요)")
        open_orders = self.session._open_orders()
        for o in open_orders:
            self.out(f"  미체결 {o.order_id} {o.side} {o.symbol} {o.filled_qty}/{o.qty}"
                     + (" (취소 요청 중)" if o.status is OrderStatus.CANCEL_PENDING else ""))
        s = self.stats
        lat = f", 반영 지연 중앙값 {sorted(s.latencies_ms)[len(s.latencies_ms) // 2]}ms" if s.latencies_ms else ""
        self.out(f"통보 {s.notices}건, 통보로 반영 {s.fills_booked}건, 조회로 반영 {s.polls_booked}건, "
                 f"장중 대사 {s.recons}회(불일치 {s.recon_breaks}){lat}, 연결 끊김 {s.feed_drops}회, "
                 f"현재가 {s.quotes}회(실패 {s.quote_failures}종목)")

    def _report_anomalies(self) -> None:
        seen = getattr(self, "_seen_anomalies", 0)
        for a in self.session.anomalies[seen:]:
            self.out(f"  이상: {a.kind} {a.ref} {a.detail}")
        self._seen_anomalies = len(self.session.anomalies)

    # --- loop ---

    def step(self, timeout: float = 1.0) -> bool:
        import queue as _queue
        try:
            ev = self.events.get(timeout=timeout)
        except _queue.Empty:
            ev = None
        keep = True
        if ev is not None:
            kind = ev[0]
            if kind == "notice":
                self.on_notice(ev[1], ev[2])
            elif kind == "feed":
                self.on_feed(ev[1], ev[2], ev[3])
            elif kind == "command":
                keep = self.on_command(ev[1])
            elif kind == "stop":
                keep = False
        t = self.monotonic()
        if t - self._last_poll >= self.poll_every:
            self.poll("주기 조회")
        if t - self._last_recon >= self.recon_every:
            self.recon()
        if t - self._last_quote >= self.quote_every:
            self.quote()
        if t - self._last_send >= self.send_every:
            self.send_requests()
        return keep

    def run(self, until: dt.time) -> IntradayStats:
        self.out(f"[{self.now():%H:%M:%S}] 장중 실행 시작. 명령: buy/sell 종목 수량 [지정가], cancel 주문, status, recon, quit")
        self.poll("시작 시 확인")
        self.quote()
        self.send_requests()        # 장 전에 승인된 요청부터
        while self.now().time() < until and self.step():
            pass
        from . import approval
        closed = self.now().time() >= until     # quit으로 일찍 끝내면 승인된 요청은 그대로 둬요
        expired = approval.expire(self.books.conn, self.books.account_id, self.day, through_today=True) if closed else []
        if expired:
            self.out(f"오늘 나가지 않은 주문 요청 {len(expired)}건을 만료했어요: {', '.join(expired)}")
        self.recon(verbose=True)
        self.print_status()
        self.out("장중 실행 종료. 15:40 이후 daily를 실행해 주세요.")
        return self.stats
