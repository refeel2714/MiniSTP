"""일 마감 (spec D6, R-77~79): the close event, the lock on closed days, late fills as corrections, ABOR."""
import pytest

from conftest import d
from ministp.accounting import Account
from ministp.ledger import (CLOSE_BLOCKERS, CannotCloseError, ClosedDayError, DayClosedEvent, DepositEvent,
                            LateExecutionEvent, Ledger, NavEvent, Side, book_date)
from ministp.store import from_payload, to_payload
from ministp.store.postgres import close_row, position_row


def day_end(ledger: Ledger, day, closes):
    """What `daily` does to the ledger before the recon: settle what is due, value, strike the NAV."""
    while (sd := ledger.next_settlement_date(day)) is not None:
        ledger.settle_due(sd)
    ledger.revalue(day, closes)
    return ledger.nav(day, closes)


@pytest.fixture
def traded(ledger) -> Ledger:
    """11-02 (Mon): 10 shares of A bought at 10,000, the day valued at 10,100. Not closed yet."""
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    day_end(ledger, d("11-02"), {"A": 10_100})
    return ledger


def test_a_close_right_after_the_nav_fixes_the_books_of_that_day(traded):
    nav = traded.nav_history()[-1]
    assert traded.closed_through is None and traded.abor() is None and traded.open_day() is None
    assert traded.close_day(d("11-02")) == nav
    assert traded.closed_through == d("11-02") and traded.closed_days() == {d("11-02"): nav}
    assert isinstance(traded.events[-1], DayClosedEvent) and traded.unclosed_events() == ()
    assert traded.is_closed(d("11-02")) and traded.is_closed(d("10-30")) and not traded.is_closed(d("11-03"))
    journal = traded.journal

    traded.execute("X2", d("11-03"), "A", Side.BUY, 5, 10_200)        # the next day's trading: IBOR moves on
    abor = traded.abor()
    assert (traded.position("A").qty, abor.position("A").qty) == (15, 10)
    assert abor.journal == journal and abor.nav_history() == [nav]    # ABOR is the books as the close left them
    assert [type(e).__name__ for e in traded.unclosed_events()] == ["ExecutionEvent"]   # and this is all the difference
    assert close_row(traded, traded.events[-2]) == (d("11-02"), nav.net_assets, nav.units, nav.nav_per_1000)


def test_a_day_is_closed_only_as_the_last_word_on_it(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    assert ledger.close_blocker(d("11-02")) == "NAV_NOT_LAST"         # no NAV yet
    with pytest.raises(CannotCloseError) as e:
        ledger.close_day(d("11-02"))
    assert e.value.reason == "NAV_NOT_LAST" and e.value.reason in CLOSE_BLOCKERS
    first = day_end(ledger, d("11-02"), {"A": 10_100})
    ledger.execute("X2", d("11-02"), "A", Side.BUY, 1, 10_100)        # booked after the NAV: the NAV is out of date
    assert ledger.close_blocker(d("11-02")) == "NAV_NOT_LAST"
    again = day_end(ledger, d("11-02"), {"A": 10_100})                # R-75: struck again while the day is open
    assert again.market_value == first.market_value + 10_100
    assert ledger.nav_history() == [again]                            # the last NAV is the day's NAV
    assert ledger.close_blocker(d("11-02")) is None

    ledger.execute("X3", d("11-03"), "A", Side.BUY, 1, 10_100)
    day_end(ledger, d("11-02"), {"A": 10_100})                        # even with a fresh NAV of 11-02 ...
    assert ledger.close_blocker(d("11-02")) == "LATER_ENTRIES"        # ... 11-03 has begun: its close covers 11-02
    day_end(ledger, d("11-03"), {"A": 10_100})
    ledger.close_day(d("11-03"))
    assert ledger.closed_through == d("11-03") and list(ledger.closed_days()) == [d("11-03")]
    assert ledger.close_blocker(d("11-02")) == ledger.close_blocker(d("11-03")) == "ALREADY_CLOSED"

    ledger.revalue(d("11-04"), {"A": 10_100})                         # 11-04: X1 and X2 are due, not settled
    ledger.nav(d("11-04"), {"A": 10_100})
    assert ledger.close_blocker(d("11-04")) == "UNSETTLED"
    day_end(ledger, d("11-04"), {"A": 10_100})
    assert ledger.close_blocker(d("11-04")) is None


def test_nothing_more_is_booked_on_a_closed_day(traded, calendar, instruments):
    traded.close_day(d("11-02"))
    n = len(traded.events)
    for booking in (lambda: traded.deposit(d("11-02"), 1), lambda: traded.revalue(d("11-02"), {"A": 9_000}),
                    lambda: traded.nav(d("10-30"), {"A": 9_000}), lambda: traded.close_day(d("11-02"))):
        with pytest.raises((ClosedDayError, CannotCloseError)):
            booking()
    assert len(traded.events) == n
    traded.deposit(d("11-03"), 1)                                     # refusing is not halting: the open day goes on
    # a stored stream that books into a closed day is refused on replay too (fail closed)
    forged = [*traded.events, DepositEvent(d("11-02"), 1)]
    with pytest.raises(ClosedDayError, match="closed through 2026-11-02"):
        Ledger.replay(forged, calendar, traded.fees, instruments)


def test_a_fill_of_a_closed_day_is_a_correction_on_the_open_day(traded):
    nav = traded.close_day(d("11-02"))
    cash_before = traded.projected_cash()
    res = traded.execute("X2", d("11-02"), "A", Side.BUY, 3, 10_050)  # reported after the close
    ev, entry = traded.events[-1], traded.journal[-1]
    assert isinstance(ev, LateExecutionEvent) and (ev.trade_date, ev.booked_on) == (d("11-02"), d("11-03"))
    assert book_date(ev) == d("11-03")
    assert (entry.date, entry.kind, entry.source_id) == (d("11-03"), "LATE_EXECUTION", "X2")
    assert res.settle_date == d("11-04")                              # T+2 from the day it really traded
    assert position_row(traded, ev)[:4] == (d("11-02"), "A", "X2", 3)  # the position row keeps the trade date
    assert traded.position("A").qty == 13 and traded.projected_cash() == cash_before - res.net
    abor = traded.abor()                                              # the closed day did not change
    assert abor.position("A").qty == 10 and abor.nav_history() == [nav] and abor.projected_cash() == cash_before
    assert traded.unclosed_events() == (ev,)
    with pytest.raises(ValueError, match="duplicate execution"):
        traded.execute("X2", d("11-02"), "A", Side.BUY, 3, 10_050)


def test_a_correction_never_goes_back_before_what_is_already_booked(traded):
    traded.close_day(d("11-02"))
    assert traded.open_day() == d("11-03")
    traded.execute("X2", d("11-05"), "A", Side.BUY, 1, 10_000)        # the books have moved on to 11-05
    assert traded.open_day() == d("11-05")
    traded.execute("X3", d("11-02"), "A", Side.BUY, 1, 10_000)
    assert traded.events[-1].booked_on == d("11-05") and traded.journal[-1].date == d("11-05")


def test_a_late_fill_whose_settlement_day_is_closed_too_settles_on_the_open_day(traded):
    traded.close_day(d("11-02"))
    for day in ("11-03", "11-04"):
        day_end(traded, d(day), {"A": 10_100})
        traded.close_day(d(day))
    cash = traded.cash_balance()
    res = traded.execute("X2", d("11-02"), "A", Side.SELL, 4, 10_300)  # found three days late
    assert traded.events[-1].booked_on == d("11-05") and res.settle_date == d("11-04")
    assert traded.next_settlement_date(d("11-05")) == d("11-04")
    assert traded.settle_due(d("11-04")) == ["X2"]
    entry = traded.journal[-1]
    assert (entry.date, entry.kind) == (d("11-05"), "SETTLEMENT")     # cash cannot move inside closed days
    assert traded.cash_balance() == cash + res.net and traded.account_balance(Account.SELL_RECEIVABLE) == 0
    day_end(traded, d("11-05"), {"A": 10_100})
    traded.close_day(d("11-05"))
    assert traded.abor(d("11-04")).position("A").qty == 10 and traded.abor().position("A").qty == 6
    assert traded.abor(d("11-06")) is None                            # never closed


def test_close_and_late_fill_events_survive_storage(traded):
    traded.close_day(d("11-02"))
    traded.execute("X2", d("11-02"), "A", Side.BUY, 3, 10_050)
    for ev in traded.events:
        assert from_payload(type(ev).__name__, to_payload(ev)) == ev
    assert to_payload(traded.events[-1])["booked_on"] == "2026-11-03"
    assert to_payload(traded.events[-2]) == {"date": "2026-11-02"}
    assert sum(isinstance(e, NavEvent) for e in traded.events) == 1
