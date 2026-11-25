"""STP-2300 시세 추이: the closes the books took over a period, and the checks on the closes themselves."""
from decimal import Decimal

from ministp import queries as q
from ministp.ledger import Side
from ministp.pricecheck import BAD, OK, WARN, PriceRules, closes_by_day, price_checks, worst

from conftest import d


def close(ledger, date, prices, nav=None):
    ledger.revalue(d(date), prices)
    ledger.nav(d(date), prices if nav is None else nav)


def check(ledger, date, symbol="A", rules=PriceRules()):
    return next(c for c in price_checks(ledger, rules) if (c.date, c.symbol) == (d(date), symbol))


def held(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.execute("X2", d("11-02"), "E", Side.BUY, 10, 10_000)


def test_ordinary_closes_pass_and_the_first_close_has_nothing_before_it(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_100, "E": 10_005})
    close(ledger, "11-03", {"A": 10_300, "E": 9_995})
    first, second = check(ledger, "11-02"), check(ledger, "11-03")
    assert (first.level, first.reasons, first.prev_date, first.prev_close, first.change) == (OK, (), None, None, None)
    assert first.exec_gap == Decimal(10_000) / 10_100 - 1         # bought at 10,000, closed at 10,100
    assert (second.level, second.prev_date, second.prev_close) == (OK, d("11-02"), 10_100)
    assert second.change == Decimal(10_300) / 10_100 - 1 and second.exec_gap is None
    assert check(ledger, "11-03", "E").level == OK                # an ETF moves in 5 won steps at any price


def test_a_close_off_the_tick_grid_cannot_be_a_closing_price(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_105, "E": 10_003})            # the stock needs 10 won steps, the ETF 5
    a, e = check(ledger, "11-02"), check(ledger, "11-02", "E")
    assert a.level == e.level == BAD
    assert "호가단위(10원)" in a.reasons[0] and "호가단위(5원)" in e.reasons[0]


def test_valuation_and_nav_must_take_the_same_close(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_100, "E": 10_000}, nav={"A": 10_200, "E": 10_000})
    a = check(ledger, "11-02")
    assert a.level == BAD and a.close == 10_200                   # the row shows what the NAV was struck at
    assert "평가에 쓴 종가 10,100" in a.reasons[0] and "기준가에 쓴 종가 10,200" in a.reasons[0]
    assert check(ledger, "11-02", "E").level == OK
    assert closes_by_day(ledger)[d("11-02")] == ({"A": 10_100, "E": 10_000}, {"A": 10_200, "E": 10_000})


def test_a_move_past_the_price_limit_is_a_split_or_a_wrong_close(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_000, "E": 10_000})
    close(ledger, "11-03", {"A": 12_000, "E": 13_000})            # +20% and +30%: inside the limit
    close(ledger, "11-04", {"A": 6_000, "E": 13_000})             # -50%: no stock does that in a day
    assert (check(ledger, "11-03").level, check(ledger, "11-03", "E").level) == (WARN, WARN)
    assert "15%" in check(ledger, "11-03").reasons[0]
    halved = check(ledger, "11-04")
    assert halved.level == BAD and "가격제한폭(30%)" in halved.reasons[0] and "액면분할" in halved.reasons[0]
    assert check(ledger, "11-04", "E").level == OK
    # the thresholds are the rules', not the code's
    loose = PriceRules(warn_change=Decimal("0.6"), limit_change=Decimal("0.7"))
    assert check(ledger, "11-04", rules=loose).level == OK


def test_a_close_far_from_the_days_executions_is_worth_a_look(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.execute("X2", d("11-02"), "A", Side.BUY, 10, 11_000)
    close(ledger, "11-02", {"A": 12_500})
    c = check(ledger, "11-02")
    assert c.level == WARN and "체결단가와 종가" in c.reasons[0]
    assert c.exec_gap == Decimal(10_000) / 12_500 - 1             # the execution furthest from the close
    assert worst([OK, WARN]) == WARN and worst([WARN, BAD, OK]) == BAD and worst([]) == OK


def test_a_day_closed_twice_is_checked_at_its_last_closes(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_105, "E": 10_000})
    close(ledger, "11-02", {"A": 10_100, "E": 10_000})
    assert [c.level for c in price_checks(ledger)] == [OK, OK]


def test_the_period_summary_reads_each_symbol_from_the_close_before_the_period(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_000, "E": 10_000})
    close(ledger, "11-03", {"A": 12_000, "E": 10_003})
    close(ledger, "11-04", {"A": 11_000, "E": 10_005})
    e, a = q.price_trend_symbols(ledger).records()                          # the worse level first (BAD, then WARN)
    assert (a["symbol"], a["instrument_name"], a["asset_type"], a["days"]) == ("A", "가상종목A", "STOCK", 3)
    assert (a["base_close"], a["last_close"], a["last_date"], a["change"]) == (10_000, 11_000, d("11-04"), 10.0)
    assert (a["high"], a["low"], a["trend"], a["level"]) == (12_000, 10_000, [10_000, 12_000, 11_000], WARN)
    assert (e["level"], e["trend"]) == (BAD, [10_000, 10_003, 10_005])      # the worst check of the period
    # one day reads as that day against the day before; the period travels with the row for the detail
    [a] = [r for r in q.price_trend_symbols(ledger, d("11-04"), d("11-04")).records() if r["symbol"] == "A"]
    assert (a["days"], a["base_close"], a["last_close"], a["change"], a["level"]) == (1, 12_000, 11_000, -8.33, OK)
    assert (a["date_from"], a["date_to"]) == (d("11-04"), d("11-04"))
    assert q.price_trend_symbols(ledger, d("11-05"), None).records() == []


def test_a_symbols_rows_list_the_period_newest_first_with_the_change_in_won_and_percent(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_000, "E": 10_000})
    close(ledger, "11-03", {"A": 12_000, "E": 10_003})
    close(ledger, "11-04", {"A": 11_000, "E": 10_005})
    rows = q.price_trend_rows(ledger, "A").records()
    assert [(r["date"], r["close"], r["diff"], r["change"], r["level"]) for r in rows] == [
        (d("11-04"), 11_000, -1_000, -8.33, OK), (d("11-03"), 12_000, 2_000, 20.0, WARN),
        (d("11-02"), 10_000, None, None, OK)]
    assert rows[2]["exec_gap"] == 0.0 and "15%" in rows[1]["reasons"]     # bought at 10,000 the day it closed there
    assert [r["date"] for r in q.price_trend_rows(ledger, "A", d("11-03"), d("11-03")).records()] == [d("11-03")]
    assert "호가단위" in q.price_trend_rows(ledger, "E", d("11-03"), d("11-03")).records()[0]["reasons"]
    assert q.price_trend_rows(ledger, "Z").records() == []
