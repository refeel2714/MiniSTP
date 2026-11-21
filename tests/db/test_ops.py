"""Daily operation on a persistent ledger, against a simulated paper account.

The scenario follows the real paper account: one KODEX 200 share bought on
2026-11-18 (80,540 won, fee 10 won) and settled on 11-20 before MiniSTP opens
its books.
"""
import datetime as dt
import os

import pytest

from ministp import __main__ as cli
from ministp.broker import BrokerBalance, BrokerOrder, BrokerPosition, OrderAck
from ministp.cli import krx_calendar
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Side
from ministp.ops import (OpsError, add_instrument, clean_streak, daily_close, load_books,
                         open_books, place_order, resolve_break)
from ministp.recon import Cause, Status

psycopg = pytest.importorskip("psycopg")
URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db,
              pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

KODEX = Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF)
CAL = krx_calendar()
FEES = FeeSchedule.kis_paper()
D = dt.date.fromisoformat


class SimBroker:
    """Paper account: positions on trade date, cash on T+2, fee 0.015% truncated to 10 won."""

    def __init__(self, cash=100_000_000):
        self.cash = cash
        self.pending: list[tuple[dt.date, int]] = []  # (settle date, signed cash)
        self.positions: dict[str, list[int]] = {}
        self.orders: dict[dt.date, dict[str, BrokerOrder]] = {}
        self.prices = {"069500": 80_540}
        self.today = D("2026-11-18")
        self.cash_skew = 0
        self.calls = []

    # --- scenario helpers ---
    def set_day(self, day):
        self.today = day
        for sd, amt in [p for p in self.pending if p[0] <= day]:
            self.cash += amt
            self.pending.remove((sd, amt))

    def fill(self, order_no, price):
        o = self.orders[self.today][order_no]
        gross = o.qty * price
        fee = FEES.costs(KODEX, o.side == "SELL", gross)
        pos = self.positions.setdefault(o.symbol, [0, 0])
        if o.side == "BUY":
            pos[0] += o.qty
            pos[1] += gross
            signed = -(gross + fee.commission)
        else:
            cost = pos[1] * o.qty // pos[0]
            pos[0] -= o.qty
            pos[1] -= cost
            signed = gross - fee.commission - fee.taxes
        self.pending.append((CAL.settlement_date(self.today, _cycle()), signed))
        self.orders[self.today][order_no] = BrokerOrder(o.order_no, o.symbol, o.side, o.qty,
                                                        o.qty, gross, 0, 0)

    # --- gateway ---
    def get_balance(self):
        proj = self.cash + sum(a for _, a in self.pending)
        pos = {s: BrokerPosition(s, q, c) for s, (q, c) in self.positions.items() if q}
        return BrokerBalance(pos, self.cash + self.cash_skew, proj + self.cash_skew,
                             ({"output2": [{"dnca_tot_amt": str(self.cash)}]},))

    def get_price(self, symbol):
        return self.prices[symbol]

    def place_order(self, symbol, side, qty, limit_price):
        day = self.orders.setdefault(self.today, {})
        no = f"{len(day) + 1:010d}"
        day[no] = BrokerOrder(no, symbol, side, qty, 0, 0, 0, 0)
        self.calls.append(("order", symbol, side, qty))
        return OrderAck(no, "00950")

    def cancel_order(self, ack):
        raise AssertionError("not used")

    def daily_orders(self, day):
        return list(self.orders.get(day, {}).values())


def _cycle():
    from ministp.calendar import SettlementCycle
    return SettlementCycle.t_plus(2)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        from ministp.store import PostgresLedgerStore
        PostgresLedgerStore(c).init_schema()
        add_instrument(c, KODEX)
        yield c


@pytest.fixture
def broker():
    """The real account's state: 1 share bought 11-18 outside MiniSTP, settles 11-20."""
    b = SimBroker()
    no = b.place_order("069500", "BUY", 1, None).order_no
    b.fill(no, 80_540)
    b.calls.clear()
    return b


def books(conn):
    return load_books(conn, CAL, FEES)


def test_open_waits_for_settlement_then_starts_from_broker(conn, broker):
    broker.set_day(D("2026-11-19"))
    with pytest.raises(OpsError, match="미결제"):
        open_books(books(conn), broker, D("2026-11-19"))

    broker.set_day(D("2026-11-20"))
    assert broker.cash == 99_919_450
    open_books(books(conn), broker, D("2026-11-20"))
    led = books(conn).ledger  # reloaded from the DB
    assert led.cash_balance() == 99_919_450
    assert led.position("069500").qty == 1 and led.position("069500").book_cost == 80_540
    assert led.units == 99_919_450 + 80_540
    with pytest.raises(OpsError, match="이미"):
        open_books(books(conn), broker, D("2026-11-20"))


def test_order_daily_settle_and_streak(conn, broker):
    broker.set_day(D("2026-11-20"))
    open_books(books(conn), broker, D("2026-11-20"))

    # 11-20: buy 2 more, filled while the command waits
    day = D("2026-11-20")
    waits = iter([True, False])

    def wait():
        if broker.orders[day]["0000000001"].filled_qty == 0:
            broker.fill("0000000001", 80_000)
        return next(waits)

    broker.orders[day] = {}
    session, cid = place_order(books(conn), broker, day, "069500", Side.BUY, 2, wait=wait)
    assert cid == "S20261120-0001"
    assert session.executions_of(cid) == ["20261120-0000000001-1"]
    assert conn.execute("select outcome, order_no from oms_order_acks").fetchall() == [
        ("ACCEPTED", "0000000001")]

    rep = daily_close(books(conn), broker, day)
    assert rep.booked == []          # already booked by the order command
    assert rep.clean, rep.results
    # cash 99,919,450 - payable (160,000 + fee 20) + 3 shares at the 80,540 close
    assert rep.nav.net_assets == 99_919_450 - 160_020 + 3 * 80_540
    assert clean_streak(conn, CAL, day) == 1

    # re-running the same day only re-reconciles
    rep2 = daily_close(books(conn), broker, day)
    assert rep2.nav is None and rep2.notes and rep2.clean

    # 11-23 (Mon): nothing settles yet; 11-24: the 11-20 buy settles
    broker.set_day(D("2026-11-23"))
    assert daily_close(books(conn), broker, D("2026-11-23")).settled == []
    broker.set_day(D("2026-11-24"))
    rep = daily_close(books(conn), broker, D("2026-11-24"))
    assert rep.settled == ["20261120-0000000001-1"] and rep.clean
    assert clean_streak(conn, CAL, D("2026-11-24")) == 3
    assert books(conn).store.verify(books(conn).ledger) == []


def test_fill_after_order_command_is_booked_by_daily(conn, broker):
    day = D("2026-11-20")
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}
    _, cid = place_order(books(conn), broker, day, "069500", Side.BUY, 1, limit_price=80_000)
    broker.fill("0000000001", 80_000)  # fills later in the day
    rep = daily_close(books(conn), broker, day)
    assert rep.booked == ["20261120-0000000001-1"] and rep.clean


def test_break_then_owner_resolves(conn, broker):
    day = D("2026-11-20")
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.cash_skew = -5
    rep = daily_close(books(conn), broker, day)
    breaks = [r for r in rep.results if r.status is Status.BREAK]
    assert {(r.item, r.cause) for r in breaks} == {("CASH_D0", Cause.FEE_DIFF),
                                                   ("CASH_PROJECTED", Cause.FEE_DIFF)}
    assert clean_streak(conn, CAL, day) == 0
    ids = [i for (i,) in conn.execute("select id from recon_results where status = 'BREAK'")]
    for i in ids:
        resolve_break(conn, i, Cause.FEE_DIFF, "모의계좌 수수료 확인")
    assert clean_streak(conn, CAL, day) == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("update recon_results set status = 'MATCH'")


def test_order_from_the_app_shows_up_as_anomaly_and_break(conn, broker):
    day = D("2026-11-20")
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}
    no = broker.place_order("069500", "BUY", 1, None).order_no  # placed in the KIS app
    broker.fill(no, 80_100)
    rep = daily_close(books(conn), broker, day)
    assert [a.kind for a in rep.anomalies] == ["UNKNOWN_ORDER"]
    assert not rep.clean


def test_cli_end_to_end(conn, broker, capsys):
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01"}
    broker.set_day(D("2026-11-20"))
    kw = dict(environ=env, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda h, m: (lambda: dt.datetime(2026, 11, 20, h, m))  # noqa: E731

    assert cli.run(["db-init"], now=at(8, 0), **kw) == 0  # idempotent on an existing DB
    assert cli.run(["open"], now=at(8, 0), **kw) == 0
    assert cli.run(["order", "069500", "buy", "1"], now=at(8, 30), **kw) == 2  # before open
    broker.orders[D("2026-11-20")] = {}
    assert cli.run(["order", "069500", "buy", "1"], now=at(10, 0), **kw) == 0
    broker.fill("0000000001", 80_000)
    assert cli.run(["daily"], now=at(15, 0), **kw) == 2  # too early
    assert cli.run(["daily"], now=at(16, 0), **kw) == 0
    assert cli.run(["status"], now=at(16, 5), **kw) == 0
    out = capsys.readouterr().out
    assert "원장 시작: 예수금 99,919,450" in out
    assert "무오류. 무오류 연속 영업일 1 / 20" in out
    assert "069500 KODEX 200: 2주" in out


def test_broker_reuses_order_numbers_on_the_next_day(conn, broker):
    """KIS order numbers restart each business day: day 2's 0000000001 is a new order."""
    d1, d2 = D("2026-11-20"), D("2026-11-23")
    broker.set_day(d1)
    open_books(books(conn), broker, d1)
    broker.orders[d1] = {}
    place_order(books(conn), broker, d1, "069500", Side.BUY, 1, limit_price=80_000)
    broker.fill("0000000001", 80_000)
    assert daily_close(books(conn), broker, d1).booked == ["20261120-0000000001-1"]

    broker.set_day(d2)
    broker.orders[d2] = {}
    session, cid = place_order(books(conn), broker, d2, "069500", Side.BUY, 2, limit_price=80_000)
    assert session.oms.orders[cid].filled_qty == 0       # day 1's fill is not this order's
    broker.fill("0000000001", 80_000)
    rep = daily_close(books(conn), broker, d2)
    assert rep.booked == ["20261123-0000000001-1"] and rep.clean, rep.results
    assert books(conn).ledger.position("069500").qty == 1 + 1 + 2
    assert conn.execute("select count(*) from oms_order_acks where order_no = '0000000001'").fetchone()[0] == 2


def test_db_init_upgrades_a_v1_0_database(conn):
    from ministp.store import PostgresLedgerStore
    conn.execute("alter table oms_order_acks add constraint oms_order_acks_order_no_key unique (order_no)")
    PostgresLedgerStore(conn).init_schema()          # what `db-init` runs
    names = {r[0] for r in conn.execute("select conname from pg_constraint"
                                        " where conrelid = 'oms_order_acks'::regclass")}
    assert "oms_order_acks_order_no_key" not in names
