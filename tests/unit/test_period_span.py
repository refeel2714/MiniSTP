"""STP-2300·6100 조회 기간: 30일·60일·90일·1년·전체, 시작일을 넣으면 그 날이 우선."""
import datetime as dt
from types import SimpleNamespace

from ministp.ui.transactions import _span

TODAY = dt.date(2026, 11, 24)
c = SimpleNamespace(today=TODAY)


def test_a_period_counts_back_from_the_end_day_or_today():
    assert _span(c, {"period": "30일"}) == (dt.date(2026, 10, 26), TODAY)          # 오늘을 넣어 30일
    assert _span(c, {"period": "90일", "date_to": dt.date(2026, 11, 10)}) == (dt.date(2026, 8, 13), dt.date(2026, 11, 10))
    assert _span(c, {"period": "1년"}) == (dt.date(2025, 11, 25), TODAY)


def test_a_start_day_wins_and_all_means_from_the_first_day():
    start = dt.date(2026, 11, 1)
    assert _span(c, {"period": "30일", "date_from": start}) == (start, None)
    assert _span(c, {"period": "전체"}) == (None, None)
    assert _span(c, {}) == (None, None)
