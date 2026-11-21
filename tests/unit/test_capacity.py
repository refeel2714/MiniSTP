"""주문가능: the numbers a screen shows are the numbers the order check uses."""
from decimal import Decimal

from conftest import d
from ministp.capacity import OpenOrder, capacity, check, pending_buy_value
from ministp.ledger import Side
from ministp.risk import RiskLimits

DAY = d("11-02")


def test_reservations_and_sellable(ledger):
    ledger.deposit(DAY, 10_000_000)
    ledger.execute("E1", DAY, "A", Side.BUY, 100, 50_000)
    orders = [OpenOrder("C1", "A", Side.BUY, 10, 49_000),
              OpenOrder("C2", "B", Side.BUY, 5, None),      # market, priced from ref_prices
              OpenOrder("C3", "A", Side.SELL, 30, None)]
    cap = capacity(ledger, orders, "A", 50_000, ref_prices={"B": 20_000})
    assert cap.pending_buy_value == 10 * 49_000 + 5 * 20_000
    assert cap.unpriced_buy_orders == 0
    assert cap.available_cash == ledger.projected_cash() - cap.pending_buy_value
    assert (cap.held_qty, cap.pending_sell_qty, cap.sellable_qty) == (100, 30, 70)


def test_unpriced_market_buy_is_reported_not_hidden(ledger):
    ledger.deposit(DAY, 1_000_000)
    reserved, unpriced = pending_buy_value([OpenOrder("C1", "B", Side.BUY, 5, None)], {})
    assert (reserved, unpriced) == (0, 1)


def test_max_buy_qty_is_exactly_what_the_check_allows(ledger):
    ledger.deposit(DAY, 1_000_000)
    for price in (1, 7, 999, 80_540, 333_333, 1_000_000, 2_000_000):
        cap = capacity(ledger, [], "A", price)
        if cap.max_buy_qty:
            assert check(cap, Side.BUY, cap.max_buy_qty).ok, price
        assert check(cap, Side.BUY, cap.max_buy_qty + 1).reason == "INSUFFICIENT_CASH", price


def test_max_buy_qty_respects_order_value_limit(ledger):
    ledger.deposit(DAY, 10_000_000)
    limits = RiskLimits(max_order_value=1_000_000, cash_buffer_rate=Decimal("0"))
    cap = capacity(ledger, [], "A", 30_000, limits)
    assert cap.max_buy_qty == 33
    assert check(cap, Side.BUY, 34, limits).reason == "MAX_ORDER_VALUE"


def test_sell_check_uses_sellable_qty(ledger):
    ledger.deposit(DAY, 10_000_000)
    ledger.execute("E1", DAY, "A", Side.BUY, 10, 50_000)
    cap = capacity(ledger, [OpenOrder("C1", "A", Side.SELL, 4, None)], "A", 50_000)
    assert check(cap, Side.SELL, 6).ok
    assert check(cap, Side.SELL, 7).reason == "SHORT_SELL"
