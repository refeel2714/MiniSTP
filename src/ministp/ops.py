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
from .ledger import ExecutionEvent, Ledger, NavEvent, Side
from .oms import InvalidTransition, OrderStatus
from .quotes import KIND as QUOTE_KIND, Quote, latest_quotes, marks, valid_price
from .recon import Cause, ReconResult, Status
from .session import Anomaly, TradingSession, failure_text
from .store import DEFAULT_ACCOUNT, PostgresLedgerStore, migrate


class OpsError(RuntimeError):
    pass


# ---------- instruments ----------

def add_instrument(conn: psycopg.Connection, inst: Instrument) -> None:
    conn.execute("insert into instruments (symbol, name, market, asset_type) values (%s, %s, %s, %s)",
                 (inst.symbol, inst.name, inst.market.value, inst.asset_type.value))


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

def open_books(books: Books, broker: BrokerGateway, day: dt.date) -> None:
    """R-05: start the ledger from the broker's settled cash and holdings."""
    if books.ledger.events:
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
    books.ledger.deposit(day, bal.cash_d0)
    for sym in sorted(bal.positions):
        p = bal.positions[sym]
        books.ledger.open_position(day, sym, p.qty, p.purchase_amount)


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
            # intent stored but no answer recorded: the process died around the broker call
            session.anomalies.append(Anomaly(day, "UNACKED_ORDER", cid,
                                             f"{side} {sym} x{qty}: 접수 결과 미기록, 증권사 주문내역 확인 필요"))
    # Next number after the highest stored one. Orders refused by the order check never reach the DB,
    # so counting rows would hand out a number already used today (S…-0003 twice).
    session._seq = max((int(r[0].rsplit("-", 1)[1]) for r in rows), default=0)

    def on_submit(cid, d, symbol, side, qty, limit_price):
        books.conn.execute(
            "insert into oms_orders (client_id, trade_date, symbol, side, qty, limit_price, account_id)"
            " values (%s, %s, %s, %s, %s, %s, %s)", (cid, d, symbol, side.value, qty, limit_price, books.account_id))

    def on_ack(cid, ack, detail):
        if ack is None:
            books.conn.execute("insert into oms_order_acks (client_id, outcome, detail)"
                               " values (%s, 'REJECTED', %s)", (cid, detail))
        else:
            books.conn.execute("insert into oms_order_acks (client_id, outcome, order_no, org_no)"
                               " values (%s, 'ACCEPTED', %s, %s)", (cid, ack.order_no, ack.org_no))

    def on_cancel(cid, ack):
        _snapshot(books.conn, day, CANCEL_KIND, {"client_id": cid, "order_no": ack.order_no,
                                                 "requested_at": dt.datetime.now().isoformat(timespec="seconds")},
                  books.account_id)

    session.on_submit, session.on_ack, session.on_cancel = on_submit, on_ack, on_cancel
    return session


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
    """Submit one order; if `wait` is given (callable returning False to stop), poll for fills."""
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

    @property
    def clean(self) -> bool:
        """R-74: no UNKNOWN cause and no unresolved break."""
        return all(r.status is Status.MATCH for r in self.results)


def daily_close(books: Books, broker: BrokerGateway, day: dt.date) -> DailyReport:
    ledger = books.ledger
    if not ledger.events:
        raise OpsError("원장 시작값이 없어요. 먼저 open 명령을 실행해 주세요.")
    notes = []

    # 1. settlements due, each on its own settlement date (R-12)
    settled = []
    while (sd := ledger.next_settlement_date(day)) is not None:
        settled += ledger.settle_due(sd)

    # 2. fills of orders MiniSTP placed today
    session = session_for(books, broker, day)
    booked = session.sync(day)

    # 3. valuation and NAV at the close, once per day (R-55, R-60)
    nav = None
    if any(isinstance(e, NavEvent) and e.date == day for e in ledger.events):
        notes.append("오늘 NAV는 이미 기록돼 있어 대사만 다시 실행했어요.")
    else:
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
    problems = books.store.verify(ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")
    return DailyReport(day, settled, booked, nav, results, session.anomalies, run_id, notes)


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
    return conn.execute(   # the judgement belongs to the account of the row it judges
        "insert into recon_results (run_id, date, item, key, internal_value, broker_value, diff, account_id,"
        " status, cause_code, note, supersedes_id) values (%s, %s, %s, %s, %s, %s, %s, %s, 'RESOLVED',"
        " %s, %s, %s) returning id", (*row[:8], cause.value, note, result_id)).fetchone()[0]


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
                 settle_wait=None):
        import time as _time
        self.books, self.broker, self.day, self.events = books, broker, day, events
        self.now, self.out = now, out
        self.monotonic = monotonic or _time.monotonic
        self.poll_every, self.recon_every, self.quote_every = poll_every, recon_every, quote_every
        self.settle_wait = settle_wait or (lambda: _time.sleep(3))
        self.session = session_for(books, broker, day)
        self.stats = IntradayStats()
        self.feed_up = False
        self.quotes: dict[str, Quote] = latest_quotes(books.conn, day)   # 다시 띄워도 오늘 받은 가격부터
        self._quote_failing: set[str] = set()
        self._last_poll = self._last_recon = self._last_quote = self.monotonic()

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
                if symbol not in self.books.instruments:
                    self.out(f"종목 등록이 필요해요: {symbol}")
                    return True
                cid = self.session.submit(self.day, symbol, Side(cmd.upper()), qty, limit)
                if cid is None:
                    self.out(f"주문 실패: {failure_text(self.session.anomalies[-1].detail)}")
                else:
                    self.out(f"주문 {cid} 접수. 체결되면 바로 원장에 반영해요.")
                return True
            if cmd == "cancel" and len(parts) == 2:
                self.cancel(parts[1])
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
        self.out("명령: buy 종목 수량 [지정가] | sell 종목 수량 [지정가] | cancel 주문 | status | recon | quit")
        return True

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
        return keep

    def run(self, until: dt.time) -> IntradayStats:
        self.out(f"[{self.now():%H:%M:%S}] 장중 실행 시작. 명령: buy/sell 종목 수량 [지정가], cancel 주문, status, recon, quit")
        self.poll("시작 시 확인")
        self.quote()
        while self.now().time() < until and self.step():
            pass
        self.recon(verbose=True)
        self.print_status()
        self.out("장중 실행 종료. 15:40 이후 daily를 실행해 주세요.")
        return self.stats
