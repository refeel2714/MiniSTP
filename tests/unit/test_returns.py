"""STP-6100 수익률: the NAV's return is the return on what was invested, deposits do not move it (R-67)."""
import datetime as dt
from decimal import Decimal

import pytest

from ministp import queries as q
from ministp.ledger import Side

from conftest import d


def close(ledger, date, prices):
    ledger.revalue(d(date), prices)
    return ledger.nav(d(date), prices)


def pct(end, base):
    return float(round((Decimal(end) / Decimal(base) - 1) * 100, 2))


@pytest.fixture
def books(ledger):
    ledger.deposit(d("10-30"), 1_000_000)
    oct30 = ledger.nav(d("10-30"), {})
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    nov2 = close(ledger, "11-02", {"A": 11_000})
    ledger.deposit(d("11-03"), 500_000)                     # buys units at the 11-02 NAV
    nov3 = close(ledger, "11-03", {"A": 11_000})
    close(ledger, "12-04", {"A": 9_000})                    # struck twice: the last one counts
    dec4 = close(ledger, "12-04", {"A": 12_100})
    return ledger, oct30, nov2, nov3, dec4


def test_money_put_in_moves_net_assets_but_not_the_return(books):
    ledger, oct30, nov2, nov3, _ = books
    assert oct30.nav_per_1000 == Decimal("1000.00")
    days = {r.date: r for r in q.return_days(ledger)}
    dep = days[d("11-03")]
    assert dep.flows == 500_000 and dep.net_assets == nov2.net_assets + 500_000 + dep.profit
    assert dep.nav == nov2.nav_per_1000 and dep.day_return == 0 and dep.profit == 0
    first = days[d("10-30")]
    assert (first.base_nav, first.flows, first.profit) == (Decimal("1000.00"), 1_000_000, 0)
    gain = days[d("11-02")]
    assert gain.profit == nov2.net_assets - oct30.net_assets and gain.flows == 0   # 평가이익 less the fee


def test_each_month_is_its_last_nav_against_the_month_end_before_it(books):
    ledger, oct30, nov2, nov3, dec4 = books
    rows = q.return_months(ledger).records()
    assert [r["month"] for r in rows] == ["2026-12", "2026-11", "2026-10"]
    dec, nov, oct_ = rows
    assert (oct_["month_return"], oct_["flows"], oct_["profit"]) == (0.0, 1_000_000, 0)
    assert nov["month_return"] == pct(nov3.nav_per_1000, oct30.nav_per_1000) and nov["flows"] == 500_000
    assert nov["profit"] == nov3.net_assets - oct30.net_assets - 500_000
    assert dec["month_return"] == pct(dec4.nav_per_1000, nov3.nav_per_1000)
    assert dec["cum_return"] == pct(dec4.nav_per_1000, 1000) and dec["days"] == 1
    assert dec["trend"] == [float(dec4.nav_per_1000)] and nov["trend"] == [float(nov2.nav_per_1000),
                                                                           float(nov3.nav_per_1000)]


def test_the_periods_run_to_the_last_nav(books):
    ledger, oct30, nov2, nov3, dec4 = books
    p = q.return_periods(ledger)
    assert p["last_date"] == d("12-04")
    assert p["since_start"] == p["year_to_date"] == pct(dec4.nav_per_1000, 1000)   # the books started this year
    assert p["one_month"] == pct(dec4.nav_per_1000, nov3.nav_per_1000)              # from the NAV on or before 11-04
    # a period counts only its own days, and its 누적 starts at the NAV before it: the newest month's
    # 누적 is the period's return
    assert q.return_months(ledger, d("11-03"), d("11-30")).records()[0]["cum_return"] == 0.0   # 11-03 vs 11-02
    rows = q.return_months(ledger, d("11-03"), None).records()
    assert [r["month"] for r in rows] == ["2026-12", "2026-11"]
    assert rows[1]["month_return"] == 0.0 and rows[0]["cum_return"] == pct(dec4.nav_per_1000, nov2.nav_per_1000)
    assert rows[0]["date_from"] == d("11-03")
    assert q.return_periods(type(ledger)(ledger.calendar, ledger.fees, ledger.instruments)) == {}


def test_a_months_days_read_newest_first_with_the_month_so_far(books):
    ledger, oct30, nov2, nov3, _ = books
    rows = q.return_month_days(ledger, "2026-11").records()
    assert [r["date"] for r in rows] == [d("11-03"), d("11-02")]
    assert rows[1]["day_return"] == rows[1]["month_to_date"] == pct(nov2.nav_per_1000, 1000)
    assert rows[0]["month_to_date"] == pct(nov3.nav_per_1000, oct30.nav_per_1000)
    assert rows[0]["flows"] == 500_000
    assert q.return_month_days(ledger, "2026-09").records() == []
    with pytest.raises(ValueError, match="YYYY-MM"):
        q.return_month_days(ledger, "11월")


def test_a_month_back_lands_on_the_shorter_months_last_day():
    assert q._month_back(dt.date(2026, 3, 31)) == dt.date(2026, 2, 28)
    assert q._month_back(dt.date(2026, 1, 15)) == dt.date(2025, 12, 15)
