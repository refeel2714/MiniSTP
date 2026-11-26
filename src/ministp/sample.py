"""Sample database: simulated trading days for demos and screen development.

Every record goes through the same code as real operation (open_books,
place_order, daily_close, resolve_break) against a simulated paper account, so
the sample is shaped exactly like the owner's real database without containing
any of it. The run is deterministic for a given seed.

`accounts=3` adds a second account of the default customer and one account of a
second customer (다계좌, 스펙 D7). They trade the same market on the same days, each
against its own simulated brokerage account; the default account's own history is
the same with or without them.
"""
import datetime as dt
import random
from dataclasses import dataclass, field

import psycopg

from .broker.sim import SimBroker
from .calendar import BusinessCalendar
from .fees import FeeSchedule
from .instruments import AssetType, Instrument, Market
from .ledger import Side
from .accounts import add_account, add_customer, set_status
from .ops import Books, add_instrument, clean_streak, daily_close, load_books, open_books, place_order, resolve_break
from .recon import Cause, Status
from .store import DEFAULT_ACCOUNT, PostgresLedgerStore
from .ticks import round_to_tick

SAMPLE_INSTRUMENTS = [
    (Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF), 80_000),
    (Instrument("360750", "TIGER 미국S&P500", Market.KOSPI, AssetType.ETF), 21_000),
    (Instrument("005930", "삼성전자", Market.KOSPI, AssetType.STOCK), 60_000),
]
OPENING_CASH = 100_000_000


@dataclass(frozen=True)
class SampleAccount:
    """An extra account of the sample. Day numbers are fractions of the run, so short runs have them too."""
    account_id: str
    customer_id: str
    alias: str
    credential_ref: str                 # a name only; the sample has no keys and calls no broker
    cash: int
    opens_at: float = 0.0               # registered and opened this far into the run
    holdings: tuple[tuple[str, int], ...] = ()   # shares already held at the broker when the books open
    order_size: tuple[int, int] = (500_000, 2_000_000)
    skew_at: float | None = None        # a day the broker's cash differs by a few won (a break, then judged)
    suspend_at: float | None = None     # suspended for two business days from here (R-39)


SAMPLE_CUSTOMERS = {"C0002": "두 번째 고객"}
SAMPLE_ACCOUNTS = [
    SampleAccount("A0002", "C0001", "두 번째 계좌", "KIS_2", 30_000_000, opens_at=0.25, skew_at=0.6),
    SampleAccount("A0003", "C0002", "연습 계좌", "KIS_3", 50_000_000, holdings=(("005930", 100),),
                  order_size=(1_000_000, 3_000_000), suspend_at=0.5),
]
SUSPENDED_DAYS = 2


class SampleError(RuntimeError):
    pass


@dataclass
class AccountSummary:
    """What one extra account did in the sample."""
    account_id: str
    alias: str
    opened_on: dt.date | None = None
    orders: int = 0
    fills: int = 0
    refused: int = 0
    breaks_resolved: int = 0
    streak: int = 0
    status: str = "OPEN"
    notes: list[str] = field(default_factory=list)


@dataclass
class SampleSummary:
    """Totals of the default account (as before 다계좌), and one line per extra account."""
    first_day: dt.date
    last_day: dt.date
    days: int = 0
    orders: int = 0
    fills: int = 0
    canceled: int = 0
    breaks_resolved: int = 0
    streak: int = 0
    notes: list[str] = field(default_factory=list)
    others: list[AccountSummary] = field(default_factory=list)


def guard_sample_db(conn: psycopg.Connection) -> str:
    """Refuse anything but a database whose name says it is a sample or a test."""
    name = conn.info.dbname
    if "sample" not in name and "test" not in name:
        raise SampleError(f"샘플 DB 이름에는 'sample'이 들어가야 해요 (지금: {name}). 운영 DB는 건드리지 않아요.")
    return name


def build_sample(conn: psycopg.Connection, calendar: BusinessCalendar, fees: FeeSchedule,
                 start: dt.date = dt.date(2026, 9, 1), days: int = 40, seed: int = 7,
                 accounts: int = 1) -> SampleSummary:
    """Wipe the sample database and fill it with `days` business days of trading.

    `accounts` is 1 (the default account only) up to 1 + len(SAMPLE_ACCOUNTS)."""
    guard_sample_db(conn)
    if not 1 <= accounts <= 1 + len(SAMPLE_ACCOUNTS):
        raise SampleError(f"샘플 계좌 수는 1~{1 + len(SAMPLE_ACCOUNTS)}개예요.")
    day = start if calendar.is_business_day(start) else calendar.add_business_days(start, 1)
    conn.execute("drop schema if exists public cascade; create schema public")
    PostgresLedgerStore(conn).init_schema(opened_on=day)
    for inst, _ in SAMPLE_INSTRUMENTS:
        add_instrument(conn, inst)

    rng = random.Random(seed)
    instruments = {i.symbol: i for i, _ in SAMPLE_INSTRUMENTS}
    broker = SimBroker(calendar, fees, instruments, OPENING_CASH,
                       {i.symbol: p for i, p in SAMPLE_INSTRUMENTS}, day)
    open_books(_books(conn, calendar, fees), broker, day)
    summary = SampleSummary(day, day)
    skew_day, cancel_day, partial_day = days // 3, days // 2, (2 * days) // 3
    others = [_Extra(spec, days, seed) for spec in SAMPLE_ACCOUNTS[:accounts - 1]]
    summary.others = [x.summary for x in others]

    for n in range(days):
        if n:
            day = calendar.add_business_days(day, 1)
            broker.set_day(day)
            _move_prices(broker, rng)
        books = _books(conn, calendar, fees)
        for _ in range(rng.choice((0, 1, 1, 2))):
            _random_trade(books, broker, day, rng, summary, partial=(n == partial_day))
        if n == cancel_day:
            _unfilled_then_canceled(books, broker, day, summary)
        broker.cash_skew = -5 if n == skew_day else 0  # one day the broker shows 5 won less
        report = daily_close(_books(conn, calendar, fees), broker, day)
        for r in report.results:
            if r.status is Status.BREAK:
                _judge(conn, day, r.item, DEFAULT_ACCOUNT)
                summary.breaks_resolved += 1
        for x in others:   # after the default account, so its history does not depend on them
            x.run_day(conn, calendar, fees, instruments, broker.prices, n, day)
        summary.days += 1
        summary.last_day = day
    summary.streak = clean_streak(conn, calendar, summary.last_day)
    for x in others:
        x.summary.streak = clean_streak(conn, calendar, summary.last_day, x.spec.account_id)
    return summary


def _books(conn, calendar, fees, account_id: str = DEFAULT_ACCOUNT) -> Books:
    return load_books(conn, calendar, fees, account_id)


def _judge(conn, day: dt.date, item: str, account_id: str) -> None:
    rid = conn.execute("select id from recon_results where date = %s and item = %s and status = 'BREAK'"
                       " and account_id = %s order by id desc limit 1", (day, item, account_id)).fetchone()[0]
    resolve_break(conn, rid, Cause.FEE_DIFF, "샘플: 증권사 수수료 절사 차이로 판단")


class _Extra:
    """One extra account: its own simulated brokerage account and its own random stream."""

    def __init__(self, spec: SampleAccount, days: int, seed: int):
        self.spec, self.broker = spec, None
        self.rng = random.Random(f"{seed}-{spec.account_id}")
        self.first = min(int(days * spec.opens_at), days - 1)
        self.skew = None if spec.skew_at is None else max(int(days * spec.skew_at), self.first)
        self.suspend = None if spec.suspend_at is None else max(int(days * spec.suspend_at), self.first + 1)
        self.summary = AccountSummary(spec.account_id, spec.alias)

    def run_day(self, conn, calendar, fees, instruments, prices: dict[str, int], n: int, day: dt.date) -> None:
        spec, acct, out = self.spec, self.spec.account_id, self.summary
        if n < self.first:
            return
        if n == self.first:
            if not conn.execute("select 1 from customers where customer_id = %s", (spec.customer_id,)).fetchone():
                add_customer(conn, spec.customer_id, SAMPLE_CUSTOMERS[spec.customer_id])
            add_account(conn, acct, spec.customer_id, spec.alias, credential_ref=spec.credential_ref, opened_on=day)
            self.broker = SimBroker(calendar, fees, instruments, spec.cash, prices, day)
            for sym, qty in spec.holdings:     # bought before MiniSTP: the books open with them (R-05)
                self.broker.positions[sym] = [qty, qty * prices[sym]]
            open_books(_books(conn, calendar, fees, acct), self.broker, day)
            out.opened_on = day
        else:
            self.broker.set_day(day)
        self.broker.prices = dict(prices)      # one market for every account
        if self.suspend is not None and n == self.suspend:
            out.status = set_status(conn, acct, "SUSPENDED", day, "샘플: 본인 확인 서류 보완 요청").status
        elif self.suspend is not None and n == self.suspend + SUSPENDED_DAYS:
            out.status = set_status(conn, acct, "OPEN", day, "샘플: 서류 확인 완료").status

        books = _books(conn, calendar, fees, acct)
        trades = self.rng.choice((0, 1, 1))
        if books.status != "OPEN" and n == self.suspend:
            trades = 1                           # the customer tries once and is refused (ACCOUNT_NOT_OPEN)
        for _ in range(trades):
            before = len(out.notes)
            _random_trade(books, self.broker, day, self.rng, out, partial=False, size=spec.order_size)
            out.refused += len(out.notes) - before
        self.broker.cash_skew = -3 if n == self.skew else 0
        report = daily_close(_books(conn, calendar, fees, acct), self.broker, day)   # a suspended account still closes
        for r in report.results:
            if r.status is Status.BREAK:
                _judge(conn, day, r.item, acct)
                out.breaks_resolved += 1


def _move_prices(broker: SimBroker, rng: random.Random) -> None:
    for sym, p in broker.prices.items():
        moved = p * (1 + rng.gauss(0, 0.012))
        broker.prices[sym] = round_to_tick(int(moved), broker.instruments[sym].asset_type, up=False)


def _random_trade(books: Books, broker: SimBroker, day: dt.date, rng: random.Random,
                  summary, partial: bool, size: tuple[int, int] = (1_000_000, 4_000_000)) -> None:
    held = {s: p.qty for s, p in books.ledger.positions().items() if p.qty}
    sell = held and rng.random() < 0.35
    sym = rng.choice(sorted(held)) if sell else rng.choice(sorted(broker.prices))
    price = broker.prices[sym]
    if sell:
        qty = max(1, held[sym] // rng.choice((2, 3, 4)))
    else:
        qty = max(1, int(rng.uniform(*size) // price))
    limit = price if rng.random() < 0.3 else None
    fill_now = qty // 2 if partial and qty > 1 else qty

    calls = iter((True, False))

    def wait() -> bool:  # the order command polls: fill (part of) it, poll once more, stop
        no = f"{len(broker.orders.get(day, {})):010d}"
        if broker.orders[day][no].filled_qty == 0:
            broker.fill(no, fill_now, price)
        return next(calls)

    session, cid = place_order(books, broker, day, sym, Side.SELL if sell else Side.BUY, qty, limit, wait=wait)
    if cid is None:
        summary.notes.append(f"{day} 주문 거부: {session.anomalies[-1].detail}")
        return
    summary.orders += 1
    summary.fills += 1
    if fill_now < qty:  # the rest fills later in the day; daily_close books it
        no = f"{len(broker.orders[day]):010d}"
        broker.fill(no, qty - fill_now, price)
        summary.fills += 1


def _unfilled_then_canceled(books: Books, broker: SimBroker, day: dt.date, summary: SampleSummary) -> None:
    sym = "069500"
    low = round_to_tick(int(broker.prices[sym] * 0.97), books.instruments[sym].asset_type, up=False)
    session, cid = place_order(books, broker, day, sym, Side.BUY, 10, low)
    if cid is not None:
        session.cancel(cid)  # nobody sold that low; canceled before the close
        summary.orders += 1
        summary.canceled += 1
