import datetime as dt
from decimal import Decimal

import pytest

from conftest import d
from ministp.backtest import BacktestEngine, SlippageModel
from ministp.instruments import AssetType
from ministp.ledger import Side
from ministp.oms import OrderStatus
from ministp.risk import RiskLimits
from ministp.strategies.ma_cross import MovingAverageCross
from ministp.strategy import Bar
from ministp.ticks import round_to_tick, tick_size


def bar(day, sym, o, c=None):
    c = o if c is None else c
    return Bar(d(day), sym, o, max(o, c), min(o, c), c)


class Script:
    """Submits pre-planned orders on given days; records what it could see."""
    name = "script"

    def __init__(self, plan):
        self.plan = plan
        self.seen = []

    def on_bar(self, ctx, bars):
        self.seen.append((ctx.today, {s: b.close for s, b in bars.items()}))
        for sym, side, qty in self.plan.get(ctx.today, []):
            ctx.submit(sym, side, qty)


@pytest.mark.parametrize("price,asset,tick", [
    (1_999, AssetType.STOCK, 1), (2_000, AssetType.STOCK, 5), (19_990, AssetType.STOCK, 10),
    (50_000, AssetType.STOCK, 100), (499_500, AssetType.STOCK, 500), (500_000, AssetType.STOCK, 1_000),
    (52_345, AssetType.ETF, 5),
])
def test_tick_size(price, asset, tick):
    assert tick_size(price, asset) == tick


def test_tick_rounding_direction():
    assert round_to_tick(Decimal("50050"), AssetType.STOCK, up=True) == 50_100
    assert round_to_tick(Decimal("49975"), AssetType.STOCK, up=False) == 49_950
    assert round_to_tick(Decimal("49990"), AssetType.STOCK, up=True) == 50_000


def test_order_fills_next_open_with_slippage(ledger):
    """Hand-checked: buy 10 A on 11-02 close -> fill 11-03 open 50,000 * 1.001 = 50,050 -> tick 100 -> 50,100."""
    ledger.deposit(d("11-02"), 1_000_000)
    strat = Script({d("11-02"): [("A", Side.BUY, 10)]})
    res = BacktestEngine(ledger, strat).run([
        bar("11-02", "A", 50_000), bar("11-03", "A", 50_000, 51_000)])
    assert res.fills == [(d("11-03"), "BT000001", "A", Side.BUY, 10, 50_100)]
    r = ledger.execution_result("BT000001-1")
    assert (r.gross, r.commission, r.settle_date) == (501_000, 75, d("11-05"))
    nav = res.navs[-1]
    assert (nav.cash, nav.payables, nav.market_value, nav.net_assets) == (1_000_000, 501_075, 510_000, 1_008_925)
    assert nav.nav_per_1000 == Decimal("1008.93")
    assert res.navs[0].nav_per_1000 == Decimal("1000.00")


def test_strategy_never_sees_future_bars(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    strat = Script({})
    BacktestEngine(ledger, strat).run([bar("11-03", "A", 1, 2), bar("11-02", "A", 1, 1)])
    assert strat.seen == [(d("11-02"), {"A": 1}), (d("11-03"), {"A": 2})]


def test_insufficient_cash_is_rejected_before_oms_submission(ledger):
    ledger.deposit(d("11-02"), 100_000)
    strat = Script({d("11-02"): [("A", Side.BUY, 10)]})
    eng = BacktestEngine(ledger, strat)
    res = eng.run([bar("11-02", "A", 50_000), bar("11-03", "A", 50_000)])
    assert res.fills == []
    assert [r.reason for r in res.rejections] == ["INSUFFICIENT_CASH"]
    assert eng.oms.orders["BT000001"].status is OrderStatus.REJECTED


def test_gap_up_rechecks_cash_at_fill(ledger):
    ledger.deposit(d("11-02"), 505_000)
    strat = Script({d("11-02"): [("A", Side.BUY, 10)]})  # ok at 50,000 close
    res = BacktestEngine(ledger, strat).run([bar("11-02", "A", 50_000), bar("11-03", "A", 55_000)])
    assert res.fills == []
    assert [(r.date, r.reason) for r in res.rejections] == [(d("11-03"), "INSUFFICIENT_CASH")]


def test_sell_more_than_held_rejected(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    strat = Script({d("11-02"): [("A", Side.SELL, 1)]})
    res = BacktestEngine(ledger, strat).run([bar("11-02", "A", 50_000)])
    assert [r.reason for r in res.rejections] == ["SHORT_SELL"]


def test_max_order_value_limit(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    strat = Script({d("11-02"): [("A", Side.BUY, 100)]})
    res = BacktestEngine(ledger, strat, limits=RiskLimits(max_order_value=1_000_000)).run(
        [bar("11-02", "A", 50_000)])
    assert [r.reason for r in res.rejections] == ["MAX_ORDER_VALUE"]


def test_kill_switch_blocks_new_orders(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    eng = BacktestEngine(ledger, Script({d("11-02"): [("A", Side.BUY, 1)]}))
    eng.oms.halt("manual")
    res = eng.run([bar("11-02", "A", 50_000)])
    assert [r.reason for r in res.rejections] == ["KILL_SWITCH"]


def test_ma_cross_end_to_end_keeps_ledger_consistent(calendar, instruments):
    from ministp.fees import FeeSchedule
    from ministp.ledger import Ledger
    from ministp.accounting import Account

    cal = type(calendar)({2026, 2027}, set())
    led = Ledger(cal, FeeSchedule.spec_v1(), instruments)
    led.deposit(dt.date(2026, 9, 1), 5_000_000)
    # down, up, down: guarantees at least one buy and one sell
    path = [50_000 - 300 * i for i in range(25)] + [42_800 + 600 * i for i in range(25)] \
        + [57_200 - 700 * i for i in range(25)]
    days, cur = [], dt.date(2026, 9, 1)
    while len(days) < len(path):
        if cal.is_business_day(cur):
            days.append(cur)
        cur += dt.timedelta(days=1)
    bars = [Bar(day, "A", p, p, p, p) for day, p in zip(days, path)]
    res = BacktestEngine(led, MovingAverageCross("A", budget=2_000_000)).run(bars)

    sides = [f[3] for f in res.fills]
    assert Side.BUY in sides and Side.SELL in sides
    assert led.account_balance(Account.CASH) == led.cash_balance()
    assert led.account_balance(Account.FVPL_STOCK) == sum(p.book_cost for p in led.positions().values())
    assert led.account_balance(Account.SELL_RECEIVABLE) - led.account_balance(Account.BUY_PAYABLE) \
        == led.pending_settlement_net()
    assert res.max_drawdown <= 0
    again = Ledger.replay(led.events, cal, FeeSchedule.spec_v1(), instruments)
    assert again.journal == led.journal
