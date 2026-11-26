"""수동 조정 (spec R-66): the reason decides the account, a memo is required, a cancel is the opposite entry."""
import pytest

from conftest import d
from ministp.accounting import Account
from ministp.ledger import (ADJUSTMENT_ACCOUNTS, Adjustment, AdjustmentCancelEvent, AdjustmentError, AdjustmentEvent,
                            CashError, ClosedDayError, Ledger, Side)
from ministp.store import from_payload, to_payload


def lines(entry):
    return sorted((ln.account.code, ln.debit, ln.credit) for ln in entry.lines)


@pytest.fixture
def funded(ledger) -> Ledger:
    """11-02 (Mon): 10,000,000 in, 10 shares of A bought at 10,000 and settled on 11-04."""
    ledger.deposit(d("11-02"), 10_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.settle_due(d("11-04"))
    return ledger


def test_interest_is_income_less_the_tax_withheld(funded):
    cash, units = funded.cash_balance(), funded.units
    before = funded.nav(d("11-04"), {"A": 10_000})
    a = funded.adjust(d("11-05"), 1, "INTEREST", 12_340, tax=1_890, memo="3분기 예탁금 이용료")
    assert lines(funded.journal[-1]) == [("1010", 10_450, 0), ("1040", 1_890, 0), ("4040", 0, 12_340)]
    assert (funded.journal[-1].kind, funded.journal[-1].source_id, funded.journal[-1].date) == (
        "ADJUSTMENT", "ADJ-1", d("11-05"))
    assert a == Adjustment(1, d("11-05"), "INTEREST", 12_340, 1_890, "3분기 예탁금 이용료") and a.cash == 10_450
    assert funded.cash_balance() == cash + 10_450 and funded.units == units          # income, not capital
    after = funded.nav(d("11-05"), {"A": 10_000})
    assert after.net_assets == before.net_assets + 10_450 and after.nav_per_1000 > before.nav_per_1000
    assert funded.account_balance(Account.INTEREST_INCOME) == 12_340
    assert funded.account_balance(Account.PREPAID_TAX) == 1_890


def test_the_reason_decides_the_other_account(funded):
    day, booked = d("11-05"), []
    for n, (reason, amount) in enumerate([("FEE", -30), ("FEE", 20), ("TAX", -7), ("DIVIDEND", 90), ("DIVIDEND", -40),
                                          ("WITHHOLDING", -14), ("WITHHOLDING", 5), ("OTHER", 3), ("OTHER", -2)], 1):
        funded.adjust(day, n, reason, amount, memo="증권사 거래내역과 맞춤")
        booked.append(lines(funded.journal[-1]))
    assert booked == [
        [("1010", 0, 30), ("5030", 30, 0)],      # the broker charged 30 more: 지급수수료
        [("1010", 20, 0), ("5030", 0, 20)],      # or 20 less
        [("1010", 0, 7), ("5040", 7, 0)],        # 세금과공과
        [("1010", 90, 0), ("4030", 0, 90)],      # a dividend paid 90 more than booked: 배당금수익
        [("1010", 0, 40), ("4030", 40, 0)],
        [("1010", 0, 14), ("1040", 14, 0)],      # 14 more withheld: 선납세금 (v2)
        [("1010", 5, 0), ("1040", 0, 5)],
        [("1010", 3, 0), ("4090", 0, 3)],        # 잡이익
        [("1010", 0, 2), ("5090", 2, 0)],        # 잡손실
    ]
    assert set(ADJUSTMENT_ACCOUNTS) == {"INTEREST", "FEE", "TAX", "DIVIDEND", "WITHHOLDING", "OTHER"}
    assert sum(a.cash for a in funded.adjustments().values()) == -30 + 20 - 7 + 90 - 40 - 14 + 5 + 3 - 2


def test_an_adjustment_that_does_not_say_what_it_is_is_refused(funded):
    day, events = d("11-05"), len(funded.events)
    for bad in (lambda: funded.adjust(day, 1, "BONUS", 100, memo="?"),                 # not one of the reasons
                lambda: funded.adjust(day, 1, "FEE", -30),                             # no memo
                lambda: funded.adjust(day, 1, "FEE", -30, memo="   "),
                lambda: funded.adjust(day, 1, "FEE", 0, memo="0원"),
                lambda: funded.adjust(day, 1, "INTEREST", -100, memo="이자가 나갈 수는 없음"),
                lambda: funded.adjust(day, 1, "FEE", 100, tax=10, memo="세금은 이자에만"),
                lambda: funded.adjust(day, 1, "INTEREST", 100, tax=100, memo="세금이 전부"),
                lambda: funded.adjust(day, 1, "INTEREST", 100, tax=-1, memo="음수 세금")):
        with pytest.raises(AdjustmentError):
            bad()
    with pytest.raises(CashError, match="more than can leave"):                       # R-64 holds here too
        funded.adjust(day, 1, "OTHER", -(funded.cash_balance() + 1), memo="예수금보다 큼")
    funded.adjust(day, 1, "FEE", -30, memo="수수료 차이")
    with pytest.raises(AdjustmentError, match="already booked"):
        funded.adjust(day, 1, "FEE", -30, memo="같은 번호")
    with pytest.raises(CashError, match="behind the books"):
        funded.adjust(d("11-04"), 2, "FEE", -30, memo="지난 날짜")
    funded.revalue(day, {"A": 10_000})
    funded.nav(day, {"A": 10_000})
    funded.close_day(day)
    with pytest.raises(ClosedDayError):
        funded.adjust(day, 2, "FEE", -30, memo="마감한 날")
    assert len(funded.events) == events + 4                                            # one adjustment, then the close


def test_a_cancel_is_the_opposite_entry_and_the_adjustment_stays(funded):
    funded.adjust(d("11-05"), 1, "INTEREST", 12_340, tax=1_890, memo="예탁금 이용료")
    booked, cash = funded.journal[-1], funded.cash_balance()
    a = funded.cancel_adjustment(d("11-06"), 1, "다른 계좌의 이자였음")
    undo = funded.journal[-1]
    assert lines(undo) == [("1010", 0, 10_450), ("1040", 0, 1_890), ("4040", 12_340, 0)]
    assert (undo.kind, undo.source_id, undo.date, undo.reverses) == ("ADJUSTMENT_CANCEL", "ADJ-1", d("11-06"), booked)
    assert (a.canceled_on, a.cancel_memo, a.date, a.amount) == (d("11-06"), "다른 계좌의 이자였음", d("11-05"), 12_340)
    assert funded.cash_balance() == cash - 10_450
    assert funded.account_balance(Account.INTEREST_INCOME) == funded.account_balance(Account.PREPAID_TAX) == 0
    for again in (lambda: funded.cancel_adjustment(d("11-06"), 1, "두 번"),
                  lambda: funded.cancel_adjustment(d("11-06"), 9, "없는 번호")):
        with pytest.raises(AdjustmentError):
            again()
    funded.adjust(d("11-06"), 2, "FEE", -30, memo="수수료 차이")
    with pytest.raises(AdjustmentError, match="memo"):
        funded.cancel_adjustment(d("11-06"), 2, " ")
    funded.cancel_adjustment(d("11-06"), 2, "증권사가 돌려줌")                          # cash comes back: no limit to check
    assert lines(funded.journal[-1]) == [("1010", 30, 0), ("5030", 0, 30)]
    # taking back money that came in is money going out: within what can leave
    funded.adjust(d("11-06"), 3, "OTHER", 500, memo="잘못 들어온 돈")
    funded.withdraw(d("11-06"), funded.withdrawable())
    with pytest.raises(CashError, match="more than can leave"):
        funded.cancel_adjustment(d("11-06"), 3, "돌려줘야 함")


def test_adjustment_events_survive_storage_and_replay(funded, calendar, instruments):
    funded.adjust(d("11-05"), 1, "INTEREST", 12_340, tax=1_890, memo="예탁금 이용료")
    funded.adjust(d("11-05"), 2, "FEE", -30, memo="수수료 차이", recon_result_id=41)
    funded.cancel_adjustment(d("11-06"), 2, "증권사가 돌려줌")
    assert [type(e) for e in funded.events[-3:]] == [AdjustmentEvent, AdjustmentEvent, AdjustmentCancelEvent]
    assert [to_payload(e) for e in funded.events[-3:]] == [
        {"date": "2026-11-05", "adjustment_id": 1, "reason": "INTEREST", "amount": 12_340, "tax": 1_890,
         "memo": "예탁금 이용료", "recon_result_id": None},
        {"date": "2026-11-05", "adjustment_id": 2, "reason": "FEE", "amount": -30, "tax": 0, "memo": "수수료 차이",
         "recon_result_id": 41},
        {"date": "2026-11-06", "adjustment_id": 2, "memo": "증권사가 돌려줌"}]
    stored = [from_payload(type(e).__name__, to_payload(e)) for e in funded.events]
    assert stored == list(funded.events)
    again = Ledger.replay(stored, calendar, funded.fees, instruments)
    assert again.journal == funded.journal and again.adjustments() == funded.adjustments()
    assert again.journal[-1].reverses is again.journal[-2]
