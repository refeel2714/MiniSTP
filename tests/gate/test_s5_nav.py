"""Spec 04 S5 일별 기준가 and 평가분개."""
from decimal import Decimal

import pytest

from conftest import d
from gate.trades import INITIAL_DEPOSIT, run_day
from ministp.accounting import Account

pytestmark = pytest.mark.gate

CLOSES = {"11-02": {"A": 50_500}, "11-03": {"A": 52_000}}
# day: cash, payables, market value, net assets, units, nav
S5 = {
    "11-02": (20_000_000, 5_000_750, 5_050_000, 20_049_250, 20_000_000, "1002.46"),
    "11-03": (20_000_000, 7_651_147, 7_800_000, 20_148_853, 20_000_000, "1007.44"),
}
VALUATION = {"11-02": 50_000, "11-03": 150_000}


def test_s5_daily_nav_and_valuation(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in ["11-02", "11-03"]:
        run_day(ledger, day, d)
        ledger.revalue(d(day), CLOSES[day])
        nav = ledger.nav(d(day), CLOSES[day])
        cash, payables, mv, net, units, per1000 = S5[day]
        assert (nav.cash, nav.payables, nav.market_value) == (cash, payables, mv)
        assert (nav.net_assets, nav.units) == (net, units)
        assert nav.nav_per_1000 == Decimal(per1000)
        assert ledger.account_balance(Account.FVPL_VALUATION) == VALUATION[day]


def test_s5_previous_valuation_is_reversed(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in ["11-02", "11-03"]:
        run_day(ledger, day, d)
        ledger.revalue(d(day), CLOSES[day])
    kinds = [e.kind for e in ledger.journal if e.kind.startswith("VALUATION")]
    assert kinds == ["VALUATION", "VALUATION_REVERSAL", "VALUATION"]
