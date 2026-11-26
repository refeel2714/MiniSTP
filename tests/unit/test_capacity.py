"""주문가능: the numbers a screen shows are the numbers the order check uses."""
from decimal import Decimal

from conftest import d
from ministp.capacity import OpenOrder, capacity, check, pending_buy_value
from ministp.instruments import AssetType
from ministp.ledger import Side
from ministp.risk import REASON_TEXT, RiskLimits, explain

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


# ---------- R-38 주문 형식: 호가단위와 영업일 ----------

def test_limit_price_must_be_on_a_tick(ledger):
    ledger.deposit(DAY, 10_000_000)
    stock = dict(asset_type=AssetType.STOCK)
    assert check(capacity(ledger, [], "A", 60_000), Side.BUY, 1, limit_price=60_000, **stock).ok
    assert check(capacity(ledger, [], "A", 60_050), Side.BUY, 1, limit_price=60_050, **stock).reason == "BAD_TICK"
    assert check(capacity(ledger, [], "A", 1_999), Side.BUY, 1, limit_price=1_999, **stock).ok       # 1원 단위
    assert check(capacity(ledger, [], "A", 2_001), Side.BUY, 1, limit_price=2_001, **stock).reason == "BAD_TICK"
    # 구간 경계: 50,000원부터 100원 단위
    assert check(capacity(ledger, [], "A", 49_950), Side.BUY, 1, limit_price=49_950, **stock).ok
    assert check(capacity(ledger, [], "A", 50_050), Side.BUY, 1, limit_price=50_050, **stock).reason == "BAD_TICK"


def test_etf_tick_is_five_won_at_any_price(ledger):
    ledger.deposit(DAY, 10_000_000)
    etf = dict(asset_type=AssetType.ETF)
    assert check(capacity(ledger, [], "E", 80_545), Side.BUY, 1, limit_price=80_545, **etf).ok
    assert check(capacity(ledger, [], "E", 80_543), Side.BUY, 1, limit_price=80_543, **etf).reason == "BAD_TICK"


def test_market_order_has_no_price_to_check(ledger):
    ledger.deposit(DAY, 10_000_000)
    cap = capacity(ledger, [], "A", 60_050)          # 기준 단가는 현재가라 호가단위와 무관
    assert check(cap, Side.BUY, 1, limit_price=None, asset_type=AssetType.STOCK).ok


def test_no_order_on_a_closed_day(ledger):
    ledger.deposit(DAY, 10_000_000)
    cap = capacity(ledger, [], "A", 60_000)
    assert check(cap, Side.BUY, 1, business_day=False).reason == "NOT_BUSINESS_DAY"


def test_a_mistyped_order_is_reported_as_mistyped_not_as_short_of_cash(ledger):
    ledger.deposit(DAY, 100_000)
    cap = capacity(ledger, [], "A", 60_050)
    over = cap.max_buy_qty + 1
    assert check(cap, Side.BUY, over).reason == "INSUFFICIENT_CASH"
    assert check(cap, Side.BUY, over, limit_price=60_050, asset_type=AssetType.STOCK).reason == "BAD_TICK"
    assert check(cap, Side.BUY, over, limit_price=60_050, asset_type=AssetType.STOCK,
                 business_day=False).reason == "NOT_BUSINESS_DAY"


def test_no_order_on_an_account_that_is_not_open(ledger):
    """R-39: asked before the order form, so the answer is about the account and not the typing."""
    ledger.deposit(DAY, 10_000_000)
    cap = capacity(ledger, [], "A", 60_000)
    assert check(cap, Side.BUY, 1, account_open=False).reason == "ACCOUNT_NOT_OPEN"
    assert check(cap, Side.BUY, 0, account_open=False, business_day=False).reason == "ACCOUNT_NOT_OPEN"
    assert check(cap, Side.BUY, 1, halted=True, account_open=False).reason == "KILL_SWITCH"


def test_every_refusal_has_a_wording():
    for reason in ("KILL_SWITCH", "ACCOUNT_NOT_OPEN", "NOT_BUSINESS_DAY", "BAD_QTY", "BAD_TICK", "MAX_ORDER_VALUE",
                   "SHORT_SELL", "INSUFFICIENT_CASH"):
        assert REASON_TEXT[reason] and explain(reason).endswith(f"[{reason}]")
    assert explain("SOMETHING_NEW") == "SOMETHING_NEW"
