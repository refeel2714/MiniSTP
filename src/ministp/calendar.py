"""Korean exchange business-day calendar (spec R-10, R-11)."""
import datetime as dt
from collections.abc import Iterable


class CalendarNotLoadedError(LookupError):
    pass


class BusinessCalendar:
    def __init__(self, loaded_years: Iterable[int], holidays: Iterable[dt.date]):
        self._years = frozenset(loaded_years)
        self._holidays = frozenset(holidays)

    def _check(self, day: dt.date) -> None:
        if day.year not in self._years:
            raise CalendarNotLoadedError(f"holiday calendar for {day.year} is not loaded")

    def is_business_day(self, day: dt.date) -> bool:
        self._check(day)
        return day.weekday() < 5 and day not in self._holidays

    def add_business_days(self, day: dt.date, n: int) -> dt.date:
        cur = day
        while n > 0:
            cur += dt.timedelta(days=1)
            if self.is_business_day(cur):
                n -= 1
        return cur

    def settlement_date(self, trade_date: dt.date) -> dt.date:
        """T+2 (R-11)."""
        return self.add_business_days(trade_date, 2)
