"""TradingSession against a fake broker: fills, cancels, anomalies, recon."""
import pytest

from conftest import d
from ministp.broker import BrokerBalance, BrokerOrder, BrokerPosition, OrderAck
from ministp.ledger import Side
from ministp.oms import OrderStatus, OverfillError
from ministp.recon import Status
from ministp.session import TradingSession

DAY = d("11-02")


class FakeBroker:
    def __init__(self, price=50_000, reject=False):
        self.price, self.reject = price, reject
        self.orders: dict[str, BrokerOrder] = {}
        self.placed, self.canceled = [], []
        self.cash_d0 = 10_000_000
        self.cash_projected = 10_000_000
        self.positions = {}

    def get_price(self, symbol):
        return self.price

    def place_order(self, symbol, side, qty, limit_price):
        if self.reject:
            raise RuntimeError("APBK0919 주문가능금액 초과")
        no = f"{len(self.placed) + 1:010d}"
        self.placed.append((symbol, side, qty, limit_price))
        self.orders[no] = BrokerOrder(no, symbol, side, qty, 0, 0, 0, 0)
        return OrderAck(no, "00950")

    def cancel_order(self, ack):
        self.canceled.append(ack.order_no)

    def daily_orders(self, day):
        return list(self.orders.values())

    def get_balance(self):
        return BrokerBalance(self.positions, self.cash_d0, self.cash_projected)

    # test helpers
    def fill(self, no, qty, price, canceled=0):
        o = self.orders[no]
        self.orders[no] = BrokerOrder(no, o.symbol, o.side, o.qty, o.filled_qty + qty,
                                      o.filled_amount + qty * price, canceled, 0)


@pytest.fixture
def session(ledger):
    ledger.deposit(DAY, 10_000_000)
    return TradingSession(ledger, FakeBroker())


def test_market_order_fill_is_booked_once(session):
    cid = session.submit(DAY, "A", Side.BUY, 10)
    assert session.broker.placed == [("A", "BUY", 10, None)]
    assert session.oms.orders[cid].status is OrderStatus.SUBMITTED
    session.broker.fill("0000000001", 10, 50_100)
    assert session.sync(DAY) == ["0000000001-1"]
    assert session.sync(DAY) == []  # idempotent
    p = session.ledger.position("A")
    assert (p.qty, p.book_cost) == (10, 501_000)
    assert session.oms.orders[cid].status is OrderStatus.FILLED


def test_partial_fills_at_different_prices_are_booked_separately(session):
    session.submit(DAY, "A", Side.BUY, 10, limit_price=50_000)
    session.broker.fill("0000000001", 4, 50_000)
    session.sync(DAY)
    session.broker.fill("0000000001", 6, 49_950)
    assert session.sync(DAY) == ["0000000001-2"]
    assert session.ledger.position("A").book_cost == 4 * 50_000 + 6 * 49_950


def test_mixed_price_increase_waits_and_is_flagged(session):
    session.submit(DAY, "A", Side.BUY, 3)
    b = session.broker
    o = b.orders["0000000001"]
    b.orders["0000000001"] = type(o)(o.order_no, "A", "BUY", 3, 3, 150_001, 0, 0)
    assert session.sync(DAY) == []
    assert [a.kind for a in session.anomalies] == ["ODD_FILL_PRICE"]
    assert session.ledger.position("A").qty == 0


def test_cancel_after_partial_fill(session):
    cid = session.submit(DAY, "A", Side.BUY, 10, limit_price=50_000)
    session.broker.fill("0000000001", 3, 50_000)
    session.sync(DAY)
    session.cancel(cid)
    assert session.broker.canceled == ["0000000001"]
    assert session.oms.orders[cid].status is OrderStatus.CANCEL_PENDING
    session.broker.fill("0000000001", 0, 0, canceled=7)
    session.sync(DAY)
    assert session.oms.orders[cid].status is OrderStatus.CANCELED
    assert session.oms.orders[cid].filled_qty == 3


def test_risk_rejects_before_reaching_broker(session):
    assert session.submit(DAY, "A", Side.SELL, 1) is None
    assert session.broker.placed == []
    assert session.anomalies[-1].detail == "risk: SHORT_SELL"
    assert session.submit(DAY, "A", Side.BUY, 1_000) is None  # 50,000,000 > cash
    assert session.anomalies[-1].detail == "risk: INSUFFICIENT_CASH"


def test_open_buy_orders_count_against_buying_power(session):
    assert session.submit(DAY, "A", Side.BUY, 150, limit_price=50_000)   # 7.5M
    assert session.submit(DAY, "A", Side.BUY, 60, limit_price=50_000) is None  # +3M > 10M
    assert session.anomalies[-1].detail == "risk: INSUFFICIENT_CASH"


def test_broker_refusal_marks_order_rejected(ledger):
    ledger.deposit(DAY, 10_000_000)
    s = TradingSession(ledger, FakeBroker(reject=True))
    assert s.submit(DAY, "A", Side.BUY, 1) is None
    assert list(s.oms.orders.values())[0].status is OrderStatus.REJECTED
    assert "APBK0919" in s.anomalies[-1].detail


def test_order_placed_outside_the_system_is_flagged_once(session):
    session.broker.orders["9999999999"] = BrokerOrder("9999999999", "A", "BUY", 1, 1, 50_000, 0, 0)
    session.sync(DAY)
    session.sync(DAY)
    assert [(a.kind, a.ref) for a in session.anomalies] == [("UNKNOWN_ORDER", "9999999999")]
    assert session.ledger.position("A").qty == 0


def test_broker_overfill_engages_kill_switch(session):
    session.submit(DAY, "A", Side.BUY, 5)
    session.broker.fill("0000000001", 6, 50_000)
    with pytest.raises(OverfillError):
        session.sync(DAY)
    assert session.oms.halted
    assert session.submit(DAY, "A", Side.BUY, 1) is None


def test_reconcile_includes_today_exec_qty(session):
    session.submit(DAY, "A", Side.BUY, 10)
    session.broker.fill("0000000001", 10, 50_000)
    session.sync(DAY)
    r = session.ledger.execution_result("0000000001-1")
    b = session.broker
    b.positions = {"A": BrokerPosition("A", 10, 500_000)}
    b.cash_projected = 10_000_000 - r.net
    results = session.reconcile(DAY)
    assert all(x.status is Status.MATCH for x in results), [x for x in results if x.status is Status.BREAK]
    assert ("EXEC_QTY", 10, 10) in [(x.item, x.internal, x.broker) for x in results]
