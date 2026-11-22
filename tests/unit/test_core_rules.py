import datetime as dt
from decimal import Decimal

import pytest

from conftest import d
from ministp.accounting import Account, Cr, Dr, UnbalancedEntryError, entry
from ministp.calendar import BusinessCalendar, CalendarNotLoadedError, SettlementCycle
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Ledger, ShortSellError, Side


def test_settlement_skips_weekend_and_holiday():
    cal = BusinessCalendar({2026}, {dt.date(2026, 12, 31)})
    t2 = SettlementCycle.t_plus(2)
    assert cal.settlement_date(d("11-05"), t2) == d("11-09")  # Thu -> Mon
    assert cal.settlement_date(d("12-28"), t2) == d("12-30")


def test_unloaded_year_is_an_error():
    cal = BusinessCalendar({2026}, set())
    with pytest.raises(CalendarNotLoadedError):
        cal.settlement_date(d("12-30"), SettlementCycle.t_plus(2))  # T+2 crosses into 2027


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
    assert again.projected_cash() == ledger.projected_cash()


def test_later_deposit_issues_units_at_last_nav(ledger):
    ledger.deposit(d("11-02"), 20_000_000)
    ledger.execute("N1", d("11-02"), "A", Side.BUY, 100, 50_000)
    nav = ledger.nav(d("11-02"), {"A": 50_500})
    assert nav.nav_per_1000 == Decimal("1002.46")
    ledger.deposit(d("11-03"), 1_000_000)
    assert ledger.units == 20_000_000 + 997_546  # 1,000,000 / 1002.46 * 1000 = 997,546.04 -> 절사


def test_t_plus_1_cutover_is_chosen_by_trade_date(calendar, instruments):
    """Trades before the cutover keep T+2; trades from the cutover settle T+1."""
    cycle = SettlementCycle.t_plus(2).then(d("11-09"), 1)
    led = Ledger(calendar, FeeSchedule.spec_v1(), instruments, settlement_cycle=cycle)
    led.deposit(d("11-02"), 10_000_000)
    before = led.execute("C1", d("11-06"), "A", Side.BUY, 1, 50_000)  # Fri, T+2
    after = led.execute("C2", d("11-09"), "A", Side.BUY, 1, 50_000)   # Mon, T+1
    assert before.settle_date == after.settle_date == d("11-10")
    assert led.settle_due(d("11-10")) == ["C1", "C2"]


def test_settlement_cycle_validation():
    with pytest.raises(ValueError):
        SettlementCycle(())
    with pytest.raises(LookupError):
        SettlementCycle.t_plus(1, d("11-09")).days_for(d("11-06"))


def test_kis_paper_commission_truncates_below_10_won():
    """2026-11-18 KIS 모의 실측: 069500 1주 80,540원 매수 수수료 10원 (0.015% = 12.08원)."""
    from ministp.fees import FeeSchedule
    from ministp.instruments import AssetType, Instrument, Market
    etf = Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF)
    assert FeeSchedule.spec_v1().costs(etf, False, 80_540).commission == 12
    assert FeeSchedule.kis_paper().costs(etf, False, 80_540).commission == 10
    assert FeeSchedule.kis_paper().costs(etf, False, 1_000_000).commission == 150


def test_small_cash_break_is_fee_diff_candidate():
    import datetime as dt
    from ministp.recon import Cause, ReconItem, reconcile
    [r] = reconcile(dt.date(2026, 11, 18), [ReconItem("CASH_PROJECTED", "", 99_919_448, 99_919_450)])
    assert r.cause is Cause.FEE_DIFF
    [r] = reconcile(dt.date(2026, 11, 18), [ReconItem("CASH_PROJECTED", "", 99_919_400, 99_919_450)])
    assert r.cause is Cause.UNKNOWN


def test_opening_position_funds_capital_and_units(ledger):
    """R-05: holdings that predate the ledger enter at broker book cost."""
    from ministp.accounting import Account
    from ministp.ledger import Ledger, Side
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.open_position(d("11-02"), "E", 1, 80_540)
    assert ledger.position("E").book_cost == 80_540
    assert ledger.account_balance(Account.FVPL_STOCK) == 80_540
    assert ledger.account_balance(Account.CONTRIBUTED_CAPITAL) == 1_080_540
    assert ledger.units == 1_080_540
    assert ledger.nav(d("11-02"), {"E": 80_540}).nav_per_1000 == 1000
    with pytest.raises(ValueError):
        ledger.open_position(d("11-02"), "A", 1, 50_000)  # NAV already struck
    r = ledger.execute("X1", d("11-03"), "E", Side.SELL, 1, 81_000)
    assert r.cost_of_sale == 80_540
    again = Ledger.replay(ledger.events, ledger.calendar, ledger.fees, ledger.instruments)
    assert again.journal == ledger.journal


def test_business_days_between_counts_d_plus_n():
    from ministp.cli import krx_calendar
    cal = krx_calendar()
    fri = dt.date(2026, 12, 24)
    assert cal.business_days_between(fri, dt.date(2026, 12, 28)) == 1   # 12/25 휴장, 주말
    assert cal.business_days_between(fri, fri) == 0
    assert cal.add_business_days(fri, 2) == dt.date(2026, 12, 29)


def test_tick_table_agrees_with_tick_size():
    from ministp.instruments import AssetType
    from ministp.ticks import tick_size, tick_table
    for asset, lo, hi, tick in tick_table():
        a = AssetType(asset)
        assert tick_size(lo, a) == tick
        if hi is not None:
            assert tick_size(hi - 1, a) == tick


def test_dates_after_the_verified_holidays_raise_instead_of_guessing():
    """2027 is loaded so December trades can settle, but only January's closures are known:
    a February date must not be silently counted as a business day (설 연휴가 빠져 있음)."""
    from ministp.cli import KRX_VERIFIED_UNTIL, krx_calendar
    cal = krx_calendar()
    assert cal.settlement_date(dt.date(2026, 12, 30), SettlementCycle.t_plus(2)) == dt.date(2027, 1, 5)
    assert cal.is_business_day(KRX_VERIFIED_UNTIL) is False          # 1/31은 일요일, 기한 안이라 계산됨
    with pytest.raises(CalendarNotLoadedError, match="확인되지 않았어요"):
        cal.is_business_day(dt.date(2027, 2, 1))
    with pytest.raises(CalendarNotLoadedError):
        cal.settlement_date(dt.date(2027, 1, 28), SettlementCycle.t_plus(2))   # 결제일이 2/1로 넘어감


def test_calendar_without_a_verified_limit_is_unchanged():
    cal = BusinessCalendar({2027}, set())
    assert cal.verified_until is None and cal.is_business_day(dt.date(2027, 2, 1))
