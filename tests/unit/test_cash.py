"""입출금·대체 (spec R-56, R-61, R-64, R-65): units for money in and out, what can leave, where cash may be dated."""
from decimal import Decimal

import pytest

from conftest import d
from ministp.accounting import Account
from ministp.ledger import (CashError, CashMovement, ClosedDayError, Ledger, Side, TransferInEvent, TransferOutEvent,
                            WithdrawalEvent)
from ministp.store import from_payload, to_payload

CAPITAL = Account.CONTRIBUTED_CAPITAL


def lines(entry):
    return sorted((ln.account.code, ln.debit, ln.credit) for ln in entry.lines)


def day_end(ledger: Ledger, day, closes):
    while (sd := ledger.next_settlement_date(day)) is not None:
        ledger.settle_due(sd)
    ledger.revalue(day, closes)
    return ledger.nav(day, closes)


@pytest.fixture
def running(ledger) -> Ledger:
    """11-02 (Mon): 10,000,000 in, 100 shares of A bought at 10,000 and worth 12,000 at the close.
    The day's NAV is 1,019.99 (the fee took the rest)."""
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 100, 10_000)
    assert day_end(ledger, d("11-02"), {"A": 12_000}).nav_per_1000 == Decimal("1019.99")
    return ledger


def test_money_that_comes_later_buys_units_at_the_nav_of_the_day_before(running):
    running.deposit(d("11-03"), 1_000_000)
    assert lines(running.journal[-1]) == [("1010", 1_000_000, 0), ("3010", 0, 1_000_000)]
    assert (running.journal[-1].kind, running.journal[-1].source_id) == ("DEPOSIT", "")
    assert running.units == 10_000_000 + 980_401                       # 1,000,000 / 1,019.99 * 1,000, cut to a unit
    nav = day_end(running, d("11-03"), {"A": 12_000})
    assert nav.nav_per_1000 == Decimal("1019.99")                      # R-61: money in does not move the NAV
    # R-61: a deposit dated the same day as a NAV already struck is still priced the day before, and
    # the day's NAV is struck again afterwards (R-75)
    running.deposit(d("11-03"), 500_000)
    assert running.cash_movements()[-1] == CashMovement(d("11-03"), "DEPOSIT", 500_000, 490_200, Decimal("1019.99"))
    assert day_end(running, d("11-03"), {"A": 12_000}).nav_per_1000 == Decimal("1019.99")


def test_a_withdrawal_gives_capital_back_and_redeems_units(running):
    running.settle_due(d("11-04"))
    cash = running.cash_balance()
    running.withdraw(d("11-04"), 2_000_000)
    assert lines(running.journal[-1]) == [("1010", 0, 2_000_000), ("3010", 2_000_000, 0)]
    assert (running.journal[-1].kind, running.journal[-1].source_id) == ("WITHDRAWAL", "")
    assert running.cash_balance() == cash - 2_000_000 and running.account_balance(CAPITAL) == 8_000_000
    assert running.cash_movements()[-1] == CashMovement(d("11-04"), "WITHDRAWAL", -2_000_000, -1_960_803,
                                                        Decimal("1019.99"))
    assert running.units == 10_000_000 - 1_960_803
    # money out does not move the NAV, beyond the hundredth it is rounded to (1,019.985 before, 1,019.984 after)
    assert day_end(running, d("11-04"), {"A": 12_000}).nav_per_1000 == Decimal("1019.98")
    for bad in (0, -1):
        with pytest.raises(ValueError):
            running.withdraw(d("11-04"), bad)
        with pytest.raises(ValueError):
            running.deposit(d("11-04"), bad)


def test_no_more_leaves_than_the_coming_settlements_leave_room_for(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    assert ledger.withdrawable() == 10_000_000
    buy = ledger.execute("X1", d("11-02"), "A", Side.BUY, 100, 10_000)   # 1,000,000 and the fee, due on 11-04
    assert ledger.cash_balance() == 10_000_000                          # the cash is still there (R-13)...
    assert ledger.withdrawable() == 10_000_000 - buy.net               # ...but it is spoken for (R-64)
    with pytest.raises(CashError, match="more than can leave"):
        ledger.withdraw(d("11-02"), 10_000_000 - buy.net + 1)
    ledger.withdraw(d("11-02"), 10_000_000 - buy.net)                  # to the last won
    assert ledger.withdrawable() == 0 and ledger.projected_cash() == 0
    ledger.settle_due(d("11-04"))
    assert ledger.cash_balance() == 0
    # money a sale will bring is not here yet: it does not count before its settlement date
    sell = ledger.execute("X2", d("11-04"), "A", Side.SELL, 50, 10_000)
    assert ledger.projected_cash() == sell.net and ledger.withdrawable() == 0
    ledger.settle_due(d("11-06"))
    assert ledger.withdrawable() == sell.net
    # a buy that settles before a sale: the low point between the two is what can leave
    ledger.execute("X3", d("11-06"), "B", Side.BUY, 10, 10_000)         # pays on 11-10
    ledger.execute("X4", d("11-09"), "A", Side.SELL, 50, 10_000)        # receives on 11-11
    assert ledger.withdrawable() == sell.net - ledger.execution_result("X3").net


def test_cash_is_not_booked_into_a_closed_day_nor_behind_the_books(running):
    running.close_day(d("11-02"))
    with pytest.raises(ClosedDayError):
        running.deposit(d("11-02"), 1_000)
    with pytest.raises(ClosedDayError):
        running.withdraw(d("10-30"), 1_000)
    running.deposit(d("11-03"), 1_000)                                  # the open day takes it
    running.execute("X2", d("11-05"), "A", Side.BUY, 1, 10_000)
    for back in (lambda: running.deposit(d("11-04"), 1_000), lambda: running.withdraw(d("11-04"), 1_000),
                 lambda: running.transfer_in(d("11-04"), 1, "A0002", 1_000),
                 lambda: running.transfer_out(d("11-04"), 1, "A0002", 1_000)):
        with pytest.raises(CashError, match="behind the books"):       # the NAVs since then did not know about it
            back()
    running.deposit(d("11-05"), 1_000)
    running.deposit(d("11-07"), 1_000)                                  # a Saturday: money can arrive on any day
    assert running.last_entry_date() == d("11-07")


def test_when_everything_leaves_no_unit_stays_behind(ledger):
    ledger.deposit(d("11-02"), 3_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 100, 10_000)
    day_end(ledger, d("11-02"), {"A": 10_700})
    ledger.execute("X2", d("11-03"), "A", Side.SELL, 100, 10_700)
    day_end(ledger, d("11-05"), {})
    everything = ledger.cash_balance()
    assert ledger.withdrawable() == everything and not ledger.positions()
    ledger.withdraw(d("11-06"), everything - 1)
    assert ledger.units > 0                                             # one won is still in
    ledger.withdraw(d("11-06"), 1)
    assert ledger.units == 0 and ledger.cash_balance() == 0
    assert day_end(ledger, d("11-06"), {}).nav_per_1000 == Decimal("1000.00")
    ledger.deposit(d("11-09"), 1_000_000)                               # and the next money starts at 1,000 again
    assert ledger.units == 1_000_000 and ledger.cash_movements()[-1].nav_per_1000 == Decimal("1000.00")


def test_a_transfer_is_a_withdrawal_on_one_side_and_a_deposit_on_the_other(running, calendar, instruments):
    other = Ledger(calendar, running.fees, instruments)
    running.settle_due(d("11-04"))
    running.transfer_out(d("11-04"), 7, "A0002", 3_000_000)
    other.transfer_in(d("11-04"), 7, "A0001", 3_000_000)                # an account can start with a transfer
    assert lines(running.journal[-1]) == [("1010", 0, 3_000_000), ("3010", 3_000_000, 0)]
    assert lines(other.journal[-1]) == [("1010", 3_000_000, 0), ("3010", 0, 3_000_000)]
    assert [(e.kind, e.source_id) for e in (running.journal[-1], other.journal[-1])] == [
        ("TRANSFER_OUT", "TR-7"), ("TRANSFER_IN", "TR-7")]
    assert running.cash_movements()[-1] == CashMovement(d("11-04"), "TRANSFER_OUT", -3_000_000, -2_941_205,
                                                        Decimal("1019.99"), 7, "A0002")
    assert other.cash_movements() == [CashMovement(d("11-04"), "TRANSFER_IN", 3_000_000, 3_000_000,
                                                   Decimal("1000.00"), 7, "A0001")]   # each side at its own NAV
    with pytest.raises(CashError, match="more than can leave"):
        other.transfer_out(d("11-04"), 8, "A0001", 3_000_001)


def test_cash_events_survive_storage_and_replay(running, calendar, instruments):
    running.settle_due(d("11-04"))
    running.withdraw(d("11-04"), 100_000)
    running.transfer_out(d("11-04"), 1, "A0002", 200_000)
    running.transfer_in(d("11-05"), 2, "A0002", 50_000)
    assert [type(e) for e in running.events[-3:]] == [WithdrawalEvent, TransferOutEvent, TransferInEvent]
    assert [to_payload(e) for e in running.events[-3:]] == [
        {"date": "2026-11-04", "amount": 100_000},
        {"date": "2026-11-04", "transfer_id": 1, "to_account": "A0002", "amount": 200_000},
        {"date": "2026-11-05", "transfer_id": 2, "from_account": "A0002", "amount": 50_000}]
    stored = [from_payload(type(e).__name__, to_payload(e)) for e in running.events]
    assert stored == list(running.events)
    again = Ledger.replay(stored, calendar, running.fees, instruments)
    assert again.journal == running.journal and again.units == running.units
    assert again.cash_movements() == running.cash_movements()
    put_in = sum(m.amount for m in running.cash_movements())
    assert running.account_balance(CAPITAL) == put_in == 10_000_000 - 100_000 - 200_000 + 50_000
