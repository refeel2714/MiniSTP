import datetime as dt
from decimal import Decimal

import pytest

from conftest import d
from ministp.accounting import Account, Cr, Dr, UnbalancedEntryError, entry
from ministp.calendar import BusinessCalendar, CalendarNotLoadedError
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Ledger, ShortSellError, Side


def test_settlement_skips_weekend_and_holiday():
    cal = BusinessCalendar({2026}, {dt.date(2026, 12, 31)})
    assert cal.settlement_date(d("11-05")) == d("11-09")  # Thu -> Mon
    assert cal.settlement_date(d("12-28")) == d("12-30")


def test_unloaded_year_is_an_error():
    cal = BusinessCalendar({2026}, set())
    with pytest.raises(CalendarNotLoadedError):
        cal.settlement_date(d("12-30"))  # T+2 crosses into 2027


def test_kosdaq_tax_single_rate():
    inst = Instrument("K", "K", Market.KOSDAQ, AssetType.STOCK)
    c = FeeSchedule.spec_v1().costs(inst, True, 1_234_567)
    assert (c.commission, c.tx_tax, c.rural_tax) == (185, 2_469, 0)


def test_short_sell_rejected(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 1, 10_000)
    with pytest.raises(ShortSellError):
        ledger.execute("X2", d("11-02"), "A", Side.SELL, 2, 10_000)


def test_unbalanced_entry_rejected():
    with pytest.raises(UnbalancedEntryError):
        entry(d("11-02"), "BAD", "", Dr(Account.CASH, 10), Cr(Account.CONTRIBUTED_CAPITAL, 9))


def test_replay_rebuilds_identical_state(ledger, calendar, instruments):
    ledger.deposit(d("11-02"), 5_000_000)
    ledger.execute("R1", d("11-02"), "A", Side.BUY, 10, 50_000)
    ledger.revalue(d("11-02"), {"A": 51_000})
    ledger.nav(d("11-02"), {"A": 51_000})
    ledger.settle_due(d("11-04"))
    ledger.execute("R2", d("11-04"), "A", Side.SELL, 3, 52_000)
    again = Ledger.replay(ledger.events, calendar, FeeSchedule.spec_v1(), instruments)
    assert again.journal == ledger.journal
    assert again.positions() == ledger.positions()
    assert again.expected_cash_d2() == ledger.expected_cash_d2()


def test_later_deposit_issues_units_at_last_nav(ledger):
    ledger.deposit(d("11-02"), 20_000_000)
    ledger.execute("N1", d("11-02"), "A", Side.BUY, 100, 50_000)
    nav = ledger.nav(d("11-02"), {"A": 50_500})
    assert nav.nav_per_1000 == Decimal("1002.46")
    ledger.deposit(d("11-03"), 1_000_000)
    assert ledger.units == 20_000_000 + 997_546  # 1,000,000 / 1002.46 * 1000 = 997,546.04 -> 절사
