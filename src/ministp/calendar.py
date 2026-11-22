"""Korean exchange business-day calendar and settlement cycle (spec R-10, R-11)."""
import bisect
import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass


class CalendarNotLoadedError(LookupError):
    pass


class BusinessCalendar:
    def __init__(self, loaded_years: Iterable[int], holidays: Iterable[dt.date],
                 verified_until: dt.date | None = None):
        """`verified_until`: the last date whose holidays are known to be complete.

        A year can be loaded before the exchange has published all of its closures
        (KRX announces next year's in December). Dates after `verified_until` raise
        instead of being counted as business days by default (R-10)."""
        self._years = frozenset(loaded_years)
        self._holidays = frozenset(holidays)
        self.verified_until = verified_until

    @property
    def loaded_years(self) -> tuple[int, ...]:
        return tuple(sorted(self._years))

    @property
    def holidays(self) -> tuple[dt.date, ...]:
        """Registered weekday closures (weekends are closed without being listed)."""
        return tuple(sorted(self._holidays))

    def business_days_between(self, start: dt.date, end: dt.date) -> int:
        """Business days after `start` up to and including `end` (D+n of `end` seen from `start`)."""
        n, cur = 0, start
        while cur < end:
            cur += dt.timedelta(days=1)
            n += self.is_business_day(cur)
        return n

    def _check(self, day: dt.date) -> None:
        if day.year not in self._years:
            raise CalendarNotLoadedError(f"holiday calendar for {day.year} is not loaded")
        if self.verified_until is not None and day > self.verified_until:
            raise CalendarNotLoadedError(
                f"{day}의 휴장일은 아직 확인되지 않았어요 (확인된 기한: {self.verified_until}). "
                "거래소 휴장일 공고를 반영한 뒤 기한을 늘려 주세요.")

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

    def settlement_date(self, trade_date: dt.date, cycle: "SettlementCycle") -> dt.date:
        """T+n where n is the cycle in force on the trade date (R-11)."""
        return self.add_business_days(trade_date, cycle.days_for(trade_date))


@dataclass(frozen=True)
class SettlementCycle:
    """Settlement lag as data with effective dates (R-11).

    The lag is chosen by the *trade date*, so trades booked before a cutover keep
    their original T+2 even if they settle after it. Changing to T+1 is a new rule
    row, not a code change.
    """

    rules: tuple[tuple[dt.date, int], ...]  # (effective_from, business days), sorted

    def __post_init__(self):
        if not self.rules or list(self.rules) != sorted(self.rules):
            raise ValueError("rules must be non-empty and sorted by effective_from")
        if any(n < 0 for _, n in self.rules):
            raise ValueError("settlement lag cannot be negative")

    @classmethod
    def t_plus(cls, days: int, effective_from: dt.date = dt.date.min) -> "SettlementCycle":
        return cls(((effective_from, days),))

    def then(self, effective_from: dt.date, days: int) -> "SettlementCycle":
        """Add a cutover, e.g. `SettlementCycle.t_plus(2).then(date(2027, x, y), 1)`."""
        return SettlementCycle(tuple(sorted(self.rules + ((effective_from, days),))))

    def days_for(self, trade_date: dt.date) -> int:
        i = bisect.bisect_right([d for d, _ in self.rules], trade_date) - 1
        if i < 0:
            raise LookupError(f"no settlement rule in force on {trade_date}")
        return self.rules[i][1]
