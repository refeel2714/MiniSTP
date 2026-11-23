"""R-68 장중 현재가: 평가 가격은 가장 최근 종가와 오늘 현재가 가운데 더 늦은 것."""
import datetime as dt

from ministp.quotes import Mark, Quote, marks, valid_price

D = dt.date(2026, 11, 23)
T = dt.datetime(2026, 11, 23, 10, 30)


def test_todays_quote_wins_over_yesterdays_close():
    m = marks(dt.date(2026, 11, 21), {"069500": 80_000}, {"069500": Quote("069500", 80_500, T)}, T)
    assert m["069500"] == Mark(80_500, "현재가", T, False)
    assert m["069500"].label == "현재가 10:30"


def test_close_of_the_same_day_wins_over_its_quotes():
    m = marks(D, {"069500": 80_300}, {"069500": Quote("069500", 80_500, T)}, T + dt.timedelta(hours=6))
    assert m["069500"] == Mark(80_300, "종가", D)
    assert m["069500"].label == "종가 11-23"


def test_quote_older_than_five_minutes_is_marked_stale_but_still_used():
    now = T + dt.timedelta(minutes=5, seconds=1)
    m = marks(dt.date(2026, 11, 21), {"069500": 80_000}, {"069500": Quote("069500", 80_500, T)}, now)
    assert m["069500"].price == 80_500 and m["069500"].stale
    assert m["069500"].label == "현재가 10:30 (갱신 멈춤)"
    assert not marks(None, {}, {"069500": Quote("069500", 80_500, T)}, T + dt.timedelta(minutes=5))["069500"].stale


def test_symbols_without_a_quote_keep_the_close_and_new_symbols_get_the_quote():
    m = marks(dt.date(2026, 11, 21), {"069500": 80_000}, {"005930": Quote("005930", 61_000, T)}, T)
    assert m["069500"].source == "종가" and m["005930"].source == "현재가"
    assert marks(None, {}, {}, T) == {}


def test_valid_price():
    assert valid_price(80_500)
    assert not valid_price(0) and not valid_price(-5) and not valid_price(True) and not valid_price("80500")
