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
from dataclasses import dataclass, field

import psycopg
from psycopg.types.json import Jsonb

from .broker.base import BrokerGateway, OrderAck
from .calendar import BusinessCalendar
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import ExecutionEvent, Ledger, NavEvent, Side
from .oms import OrderStatus
from .recon import Cause, ReconResult, Status
from .session import Anomaly, TradingSession
from .store import PostgresLedgerStore


class OpsError(RuntimeError):
    pass


# ---------- instruments ----------

def add_instrument(conn: psycopg.Connection, inst: Instrument) -> None:
    conn.execute("insert into instruments (symbol, name, market, asset_type) values (%s, %s, %s, %s)",
                 (inst.symbol, inst.name, inst.market.value, inst.asset_type.value))


def load_instruments(conn: psycopg.Connection) -> dict[str, Instrument]:
    return {s: Instrument(s, n, Market(m), AssetType(a)) for s, n, m, a in
            conn.execute("select symbol, name, market, asset_type from instruments")}


# ---------- context ----------

@dataclass
class Books:
    conn: psycopg.Connection
    store: PostgresLedgerStore
    ledger: Ledger
    instruments: dict[str, Instrument]


def load_books(conn: psycopg.Connection, calendar: BusinessCalendar, fees: FeeSchedule) -> Books:
    store = PostgresLedgerStore(conn)
    instruments = load_instruments(conn)
    ledger = store.load(calendar, fees, instruments)
    problems = store.verify(ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")
    return Books(conn, store, ledger, instruments)


def _snapshot(conn, day: dt.date, kind: str, payload) -> int:
    return conn.execute("insert into broker_snapshots (date, kind, payload) values (%s, %s, %s)"
                        " returning snapshot_id", (day, kind, Jsonb(payload))).fetchone()[0]


# ---------- open ----------

def open_books(books: Books, broker: BrokerGateway, day: dt.date) -> None:
    """R-05: start the ledger from the broker's settled cash and holdings."""
    if books.ledger.events:
        raise OpsError("원장이 이미 시작됐어요. 시작값은 한 번만 기록해요.")
    bal = broker.get_balance()
    if bal.cash_d0 != bal.cash_projected:
        raise OpsError(f"미결제 거래가 있어요 (D+0 {bal.cash_d0:,} / D+2 {bal.cash_projected:,}). "
                       "결제가 끝난 뒤(다음 영업일 이후) 다시 실행해 주세요.")
    missing = sorted(set(bal.positions) - set(books.instruments))
    if missing:
        raise OpsError(f"종목 등록이 필요해요: {', '.join(missing)}")
    _snapshot(books.conn, day, "OPENING_BALANCE", list(bal.raw))
    books.ledger.deposit(day, bal.cash_d0)
    for sym in sorted(bal.positions):
        p = bal.positions[sym]
        books.ledger.open_position(day, sym, p.qty, p.purchase_amount)


# ---------- orders ----------

def _stored_orders(conn, day: dt.date):
    return conn.execute(
        "select o.client_id, o.symbol, o.side, o.qty, o.limit_price, a.outcome, a.order_no, a.org_no"
        " from oms_orders o left join oms_order_acks a using (client_id)"
        " where o.trade_date = %s order by o.client_id", (day,)).fetchall()


def session_for(books: Books, broker: BrokerGateway, day: dt.date) -> TradingSession:
    """A session that knows every order MiniSTP placed today and persists new ones."""
    session = TradingSession(books.ledger, broker)
    rows = _stored_orders(books.conn, day)
    for cid, sym, side, qty, limit, outcome, order_no, org_no in rows:
        if outcome == "ACCEPTED":
            session.restore(cid, sym, Side(side), qty, limit, OrderAck(order_no, org_no))
        elif outcome is None:
            # intent stored but no answer recorded: the process died around the broker call
            session.anomalies.append(Anomaly(day, "UNACKED_ORDER", cid,
                                             f"{side} {sym} x{qty}: 접수 결과 미기록, 증권사 주문내역 확인 필요"))
    session._seq = len(rows)

    def on_submit(cid, d, symbol, side, qty, limit_price):
        books.conn.execute("insert into oms_orders (client_id, trade_date, symbol, side, qty, limit_price)"
                           " values (%s, %s, %s, %s, %s, %s)", (cid, d, symbol, side.value, qty, limit_price))

    def on_ack(cid, ack, detail):
        if ack is None:
            books.conn.execute("insert into oms_order_acks (client_id, outcome, detail)"
                               " values (%s, 'REJECTED', %s)", (cid, detail))
        else:
            books.conn.execute("insert into oms_order_acks (client_id, outcome, order_no, org_no)"
                               " values (%s, 'ACCEPTED', %s, %s)", (cid, ack.order_no, ack.org_no))

    session.on_submit, session.on_ack = on_submit, on_ack
    return session


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
    })
    with books.conn.cursor() as cur:
        cur.executemany(
            "insert into recon_results (run_id, date, item, key, internal_value, broker_value, diff,"
            " status, cause_code) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(run_id, r.date, r.item, r.key, r.internal, r.broker, r.diff, r.status.value,
              r.cause.value if r.cause else None) for r in results])
    problems = books.store.verify(ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")
    return DailyReport(day, settled, booked, nav, results, session.anomalies, run_id, notes)


def clean_streak(conn: psycopg.Connection, calendar: BusinessCalendar, up_to: dt.date) -> int:
    """R-74 gate: consecutive business days, ending at `up_to`, whose latest run had no break
    (or whose breaks were all resolved by a later row)."""
    rows = conn.execute(
        "select date, run_id, status, id, supersedes_id from recon_results where date <= %s",
        (up_to,)).fetchall()
    latest_run: dict[dt.date, int] = {}
    for date, run_id, *_ in rows:
        latest_run[date] = max(run_id, latest_run.get(date, 0))
    resolved = {sup for *_, sup in rows if sup is not None}
    clean_days = set()
    for date, run_id in latest_run.items():
        mine = [(st, i) for d, r, st, i, sup in rows if d == date and r == run_id and sup is None]
        if all(st == "MATCH" or i in resolved for st, i in mine):
            clean_days.add(date)
    streak, d = 0, up_to
    while d in clean_days:
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
    row = conn.execute("select run_id, date, item, key, internal_value, broker_value, diff, status"
                       " from recon_results where id = %s", (result_id,)).fetchone()
    if row is None or row[7] != "BREAK":
        raise OpsError(f"불일치 행이 아니에요: {result_id}")
    return conn.execute(
        "insert into recon_results (run_id, date, item, key, internal_value, broker_value, diff,"
        " status, cause_code, note, supersedes_id) values (%s, %s, %s, %s, %s, %s, %s, 'RESOLVED',"
        " %s, %s, %s) returning id", (*row[:7], cause.value, note, result_id)).fetchone()[0]
