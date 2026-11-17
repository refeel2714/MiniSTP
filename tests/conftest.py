import datetime as dt

import pytest

from ministp.calendar import BusinessCalendar
from ministp.fees import FeeSchedule
from ministp.instruments import AssetType, Instrument, Market
from ministp.ledger import Ledger


def d(s: str) -> dt.date:
    """'11-02' -> 2026-11-02"""
    return dt.date.fromisoformat(f"2026-{s}")


@pytest.fixture
def calendar() -> BusinessCalendar:
    # 2026-11 has no exchange holidays; weekends only (spec 04 전제).
    return BusinessCalendar(loaded_years={2026}, holidays=set())


@pytest.fixture
def instruments() -> dict[str, Instrument]:
    return {
        "A": Instrument("A", "가상종목A", Market.KOSPI, AssetType.STOCK),
        "B": Instrument("B", "가상종목B", Market.KOSPI, AssetType.STOCK),
        "E": Instrument("E", "가상ETF", Market.KOSPI, AssetType.ETF),
    }


@pytest.fixture
def ledger(calendar, instruments) -> Ledger:
    return Ledger(calendar=calendar, fees=FeeSchedule.spec_v1(), instruments=instruments)
