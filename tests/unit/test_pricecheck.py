"""STP-2300 시세 점검: the closes the books took, checked against each other and the day before."""
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


def test_days_list_newest_first_with_counts_and_a_day_not_valued_yet(ledger, calendar):
    held(ledger)
    close(ledger, "11-02", {"A": 10_000, "E": 10_000})
    close(ledger, "11-03", {"A": 12_000, "E": 10_003})
    rows = q.price_check_days(ledger, calendar, d("11-03")).records()
    assert [(r["date"], r["symbols"], r["flagged"], r["warned"], r["result"]) for r in rows] == [
        (d("11-03"), 2, 1, 1, BAD), (d("11-02"), 2, 0, 0, OK)]
    # the next business day has holdings and no close yet
    rows = q.price_check_days(ledger, calendar, d("11-04")).records()
    assert (rows[0]["date"], rows[0]["symbols"], rows[0]["flagged"], rows[0]["result"]) == (d("11-04"), 2, None, "평가 전")
    assert len(q.price_check_days(ledger, calendar, d("11-07")).records()) == 2       # a Saturday is not waited for
    assert [r["date"] for r in q.price_check_days(ledger, calendar, d("11-04"), d("11-03"), d("11-03")).records()] \
        == [d("11-03")]


def test_a_days_rows_put_the_worst_first_and_an_unvalued_day_shows_what_will_be_valued(ledger):
    held(ledger)
    close(ledger, "11-02", {"A": 10_000, "E": 10_000})
    close(ledger, "11-03", {"A": 12_000, "E": 10_003})
    rows = q.price_check_rows(ledger, d("11-03")).records()
    assert [(r["symbol"], r["level"]) for r in rows] == [("E", BAD), ("A", WARN)]
    assert (rows[1]["instrument_name"], rows[1]["asset_type"], rows[1]["change"]) == ("가상종목A", "STOCK", 20.0)
    assert rows[1]["prev_date"] == d("11-02") and rows[1]["prev_close"] == 10_000
    waiting = q.price_check_rows(ledger, d("11-04")).records()
    assert [(r["symbol"], r["close"], r["prev_close"], r["level"]) for r in waiting] == [
        ("A", None, 12_000, "평가 전"), ("E", None, 10_003, "평가 전")]
