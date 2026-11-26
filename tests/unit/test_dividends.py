"""현금배당 (spec R-23, R-57~59): who is entitled, the accrual, the payment and its tax, taking one back."""
import pytest

from conftest import d
from ministp.accounting import Account
from ministp.fees import dividend_withholding
from ministp.ledger import (ClosedDayError, DividendAccrualEvent, DividendCancelEvent, DividendError,
                            DividendPaymentEvent, Ledger, Side)
from ministp.store import from_payload, to_payload

RECV, INCOME, TAX = Account.DIVIDEND_RECEIVABLE, Account.DIVIDEND_INCOME, Account.WITHHOLDING_TAX


@pytest.fixture
def held(ledger) -> Ledger:
    """10 shares of A bought on 11-02 (Mon), settled on 11-04."""
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.settle_due(d("11-04"))
    return ledger


def lines(entry):
    return sorted((ln.account.code, ln.debit, ln.credit) for ln in entry.lines)


def test_a_record_date_counts_what_had_settled_by_then(ledger):
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)        # settles 11-04 (T+2)
    assert [ledger.settled_qty("A", d(day)) for day in ("11-02", "11-03", "11-04")] == [0, 0, 10]
    ledger.execute("X2", d("11-04"), "A", Side.SELL, 4, 10_000)       # leaves on 11-06
    assert [ledger.settled_qty("A", d(day)) for day in ("11-04", "11-05", "11-06")] == [10, 10, 6]
    assert ledger.position("A").qty == 6 and ledger.settled_qty("B", d("11-06")) == 0
    # the count does not wait for the settlement to be booked: the date decides
    assert ledger.next_settlement_date(d("11-06")) is not None


def test_a_holding_from_before_the_ledger_counts_from_the_day_the_books_opened(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.open_position(d("11-02"), "E", 7, 70_000)
    assert ledger.settled_qty("E", d("10-30")) == 0 and ledger.settled_qty("E", d("11-02")) == 7


def test_an_accrual_is_income_and_a_receivable_before_any_cash(held):
    cash, nav_before = held.cash_balance(), held.nav(d("11-04"), {"A": 10_000})
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)
    assert lines(held.journal[-1]) == [("1030", 3_610, 0), ("4030", 0, 3_610)]
    assert (held.journal[-1].date, held.journal[-1].kind, held.journal[-1].source_id) == (
        d("11-05"), "DIVIDEND_ACCRUAL", "CA-3")
    assert held.cash_balance() == cash and held.account_balance(RECV) == 3_610
    nav = held.nav(d("11-05"), {"A": 10_000})                          # R-60: 미수배당금 is part of net assets
    assert nav.net_assets == nav_before.net_assets + 3_610 and nav.receivables == 3_610
    div = held.dividends()[3]
    assert (div.symbol, div.qty, div.accrued, div.accrued_on, div.status, div.receivable) == (
        "A", 10, 3_610, d("11-05"), "ACCRUED", 3_610)
    for again in (lambda: held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610),
                  lambda: held.accrue_dividend(d("11-05"), 4, "A", 0, 3_610),
                  lambda: held.accrue_dividend(d("11-05"), 4, "Z", 10, 3_610)):
        with pytest.raises(ValueError):
            again()
    assert len(held.dividends()) == 1


def test_the_payment_withholds_the_tax_and_clears_the_receivable(held):
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)
    cash = held.cash_balance()
    assert dividend_withholding(3_610) == 505 + 50                    # R-23: 14% and 1.4%, each cut to the won
    paid = held.pay_dividend(d("11-20"), 3)
    assert lines(held.journal[-1]) == [("1010", 3_055, 0), ("1030", 0, 3_610), ("5050", 555, 0)]
    assert (held.journal[-1].kind, held.journal[-1].source_id) == ("DIVIDEND", "CA-3")
    assert held.cash_balance() == cash + 3_055 and held.account_balance(RECV) == 0
    assert held.account_balance(INCOME) == 3_610 and held.account_balance(TAX) == 555
    assert (paid.status, paid.paid_gross, paid.tax, paid.paid_on, paid.receivable) == ("PAID", 3_610, 555, d("11-20"), 0)
    with pytest.raises(DividendError, match="already PAID"):
        held.pay_dividend(d("11-20"), 3)
    with pytest.raises(DividendError, match="no open accrual"):
        held.cancel_dividend(d("11-20"), 3)


def test_the_income_follows_what_was_actually_paid(held):
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)                # recognised on an expected amount
    held.pay_dividend(d("11-20"), 3, gross=3_700, tax=569)            # the broker paid a little more
    assert lines(held.journal[-1]) == [("1010", 3_131, 0), ("1030", 0, 3_610), ("4030", 0, 90), ("5050", 569, 0)]
    held.accrue_dividend(d("11-05"), 4, "A", 10, 5_000)
    held.pay_dividend(d("11-20"), 4, gross=4_000)                     # and here less: income comes back down
    assert lines(held.journal[-1]) == [("1010", 3_384, 0), ("1030", 0, 5_000), ("4030", 1_000, 0), ("5050", 616, 0)]
    assert held.account_balance(INCOME) == 3_700 + 4_000 and held.account_balance(RECV) == 0
    # a dividend of a holding from before the ledger: never accrued, so the payment names itself
    with pytest.raises(DividendError, match="was not accrued"):
        held.pay_dividend(d("11-20"), 5)
    held.pay_dividend(d("11-20"), 5, symbol="E", gross=1_000)
    assert lines(held.journal[-1]) == [("1010", 846, 0), ("4030", 0, 1_000), ("5050", 154, 0)]
    assert (held.dividends()[5].qty, held.dividends()[5].accrued_on, held.dividends()[5].status) == (0, None, "PAID")
    for wrong in (lambda: held.pay_dividend(d("11-20"), 6, symbol="E", gross=0),
                  lambda: held.pay_dividend(d("11-20"), 6, symbol="E", gross=100, tax=100),
                  lambda: held.pay_dividend(d("11-20"), 6, symbol="Z", gross=100)):
        with pytest.raises(ValueError):
            wrong()
    held.accrue_dividend(d("11-05"), 7, "A", 10, 100)
    with pytest.raises(DividendError, match="is of A, not E"):
        held.pay_dividend(d("11-20"), 7, symbol="E")


def test_a_cancelled_action_takes_its_accrual_back(held):
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)
    held.cancel_dividend(d("11-06"), 3)
    assert lines(held.journal[-1]) == [("1030", 0, 3_610), ("4030", 3_610, 0)]
    assert (held.journal[-1].kind, held.journal[-1].date) == ("DIVIDEND_CANCEL", d("11-06"))
    assert held.account_balance(RECV) == held.account_balance(INCOME) == 0
    assert held.dividends()[3].status == "CANCELED" and held.dividends()[3].receivable == 0
    for after in (lambda: held.pay_dividend(d("11-20"), 3), lambda: held.cancel_dividend(d("11-06"), 3),
                  lambda: held.accrue_dividend(d("11-06"), 3, "A", 10, 3_610), lambda: held.cancel_dividend(d("11-06"), 9)):
        with pytest.raises(DividendError):
            after()


def test_dividend_events_survive_storage_and_replay(held, calendar, instruments):
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)
    held.accrue_dividend(d("11-05"), 4, "A", 10, 500)
    held.pay_dividend(d("11-20"), 3)
    held.cancel_dividend(d("11-20"), 4)
    kinds = [type(e) for e in held.events[-4:]]
    assert kinds == [DividendAccrualEvent, DividendAccrualEvent, DividendPaymentEvent, DividendCancelEvent]
    assert to_payload(held.events[-2]) == {"date": "2026-11-20", "action_id": 3, "symbol": "A", "gross": 3_610, "tax": 555}
    stored = [from_payload(type(e).__name__, to_payload(e)) for e in held.events]
    assert stored == list(held.events)
    again = Ledger.replay(stored, calendar, held.fees, instruments)
    assert again.journal == held.journal and again.dividends() == held.dividends()


def test_a_closed_day_takes_no_dividend_entry(held):
    held.revalue(d("11-04"), {"A": 10_000})
    held.nav(d("11-04"), {"A": 10_000})
    held.close_day(d("11-04"))
    with pytest.raises(ClosedDayError):
        held.accrue_dividend(d("11-04"), 3, "A", 10, 3_610)
    held.accrue_dividend(d("11-05"), 3, "A", 10, 3_610)                # the open day takes it
    assert held.abor().dividends() == {} and held.abor().account_balance(RECV) == 0
