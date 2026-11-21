"""Sample database: simulated trading days for demos and screen development.

Every record goes through the same code as real operation (open_books,
place_order, daily_close, resolve_break) against a simulated paper account, so
the sample is shaped exactly like the owner's real database without containing
any of it. The run is deterministic for a given seed.
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
from .ops import Books, add_instrument, clean_streak, daily_close, load_books, open_books, place_order, resolve_break
from .recon import Cause, Status
from .store import PostgresLedgerStore
from .ticks import round_to_tick

SAMPLE_INSTRUMENTS = [
    (Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF), 80_000),
    (Instrument("360750", "TIGER 미국S&P500", Market.KOSPI, AssetType.ETF), 21_000),
    (Instrument("005930", "삼성전자", Market.KOSPI, AssetType.STOCK), 60_000),
]
OPENING_CASH = 100_000_000


class SampleError(RuntimeError):
    pass


@dataclass
class SampleSummary:
    first_day: dt.date
    last_day: dt.date
    days: int = 0
    orders: int = 0
    fills: int = 0
    canceled: int = 0
    breaks_resolved: int = 0
    streak: int = 0
    notes: list[str] = field(default_factory=list)


def guard_sample_db(conn: psycopg.Connection) -> str:
    """Refuse anything but a database whose name says it is a sample or a test."""
    name = conn.info.dbname
    if "sample" not in name and "test" not in name:
        raise SampleError(f"샘플 DB 이름에는 'sample'이 들어가야 해요 (지금: {name}). 운영 DB는 건드리지 않아요.")
    return name


def build_sample(conn: psycopg.Connection, calendar: BusinessCalendar, fees: FeeSchedule,
                 start: dt.date = dt.date(2026, 9, 1), days: int = 40, seed: int = 7) -> SampleSummary:
    """Wipe the sample database and fill it with `days` business days of trading."""
    guard_sample_db(conn)
    conn.execute("drop schema if exists public cascade; create schema public")
    PostgresLedgerStore(conn).init_schema()
    for inst, _ in SAMPLE_INSTRUMENTS:
        add_instrument(conn, inst)

    rng = random.Random(seed)
    day = start if calendar.is_business_day(start) else calendar.add_business_days(start, 1)
    instruments = {i.symbol: i for i, _ in SAMPLE_INSTRUMENTS}
    broker = SimBroker(calendar, fees, instruments, OPENING_CASH,
                       {i.symbol: p for i, p in SAMPLE_INSTRUMENTS}, day)
    open_books(_books(conn, calendar, fees), broker, day)
    summary = SampleSummary(day, day)
    skew_day, cancel_day, partial_day = days // 3, days // 2, (2 * days) // 3

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
                rid = conn.execute("select id from recon_results where date = %s and item = %s and status = 'BREAK'"
                                   " order by id desc limit 1", (day, r.item)).fetchone()[0]
                resolve_break(conn, rid, Cause.FEE_DIFF, "샘플: 증권사 수수료 절사 차이로 판단")
                summary.breaks_resolved += 1
        summary.days += 1
        summary.last_day = day
    summary.streak = clean_streak(conn, calendar, summary.last_day)
    return summary


def _books(conn, calendar, fees) -> Books:
    return load_books(conn, calendar, fees)


def _move_prices(broker: SimBroker, rng: random.Random) -> None:
    for sym, p in broker.prices.items():
        moved = p * (1 + rng.gauss(0, 0.012))
        broker.prices[sym] = round_to_tick(int(moved), broker.instruments[sym].asset_type, up=False)


def _random_trade(books: Books, broker: SimBroker, day: dt.date, rng: random.Random,
                  summary: SampleSummary, partial: bool) -> None:
    held = {s: p.qty for s, p in books.ledger.positions().items() if p.qty}
    sell = held and rng.random() < 0.35
    sym = rng.choice(sorted(held)) if sell else rng.choice(sorted(broker.prices))
    price = broker.prices[sym]
    if sell:
        qty = max(1, held[sym] // rng.choice((2, 3, 4)))
    else:
        qty = max(1, int(rng.uniform(1_000_000, 4_000_000) // price))
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
