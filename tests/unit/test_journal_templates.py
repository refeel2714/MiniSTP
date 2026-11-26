"""분개 모형 표는 원장 코드가 만드는 전표 유형을 모두 덮고, 모형마다 차변과 대변이 있어요."""
import datetime as dt
import re
from importlib.resources import files

from ministp.accounting import Account, Cr, Dr, entry
from ministp.journal_templates import TEMPLATES, template_of

DAY = dt.date(2026, 11, 20)


def test_every_journal_kind_the_ledger_makes_has_a_template():
    source = files("ministp").joinpath("ledger.py").read_text(encoding="utf-8")
    kind_args = re.findall(r'entry\([^,]+, ([^,]+),', source)     # "EXECUTION" or "LATE_EXECUTION" if late else …
    made = {k for arg in kind_args for k in re.findall(r'"([A-Z_]+)"', arg)}
    assert made and made == {t.kind for t in TEMPLATES}


def test_templates_are_numbered_once_and_have_both_sides():
    codes = [t.code for t in TEMPLATES]
    assert len(codes) == len(set(codes))
    for t in TEMPLATES:
        assert {ln.side for ln in t.lines} == {"D", "C"}, t.code
        assert all(ln.account is None or isinstance(ln.account, Account) for ln in t.lines)


def test_a_sell_finds_the_template_of_its_gain_or_loss():
    gain = entry(DAY, "EXECUTION", "E1", Dr(Account.SELL_RECEIVABLE, 990), Dr(Account.COMMISSION, 10),
                 Cr(Account.FVPL_STOCK, 900), Cr(Account.TRADING_GAIN, 100))
    loss = entry(DAY, "EXECUTION", "E2", Dr(Account.SELL_RECEIVABLE, 990), Dr(Account.COMMISSION, 10),
                 Dr(Account.TRADING_LOSS, 100), Cr(Account.FVPL_STOCK, 1100))
    buy = entry(DAY, "EXECUTION", "E3", Dr(Account.FVPL_STOCK, 1000), Dr(Account.COMMISSION, 10),
                Cr(Account.BUY_PAYABLE, 1010))
    assert [template_of(e).code for e in (gain, loss, buy)] == ["JT-04", "JT-05", "JT-03"]
    odd = entry(DAY, "EXECUTION", "E4", Dr(Account.CASH, 10), Cr(Account.FVPL_STOCK, 10))
    assert template_of(odd) is None


def test_a_reversal_fits_only_the_entry_it_turns_around():
    val = entry(DAY, "VALUATION", "d", Dr(Account.FVPL_VALUATION, 50), Cr(Account.VALUATION_GAIN, 50))
    back = entry(DAY, "VALUATION_REVERSAL", "d", Dr(Account.VALUATION_GAIN, 50), Cr(Account.FVPL_VALUATION, 50),
                 reverses=val)
    wrong = entry(DAY, "VALUATION_REVERSAL", "d", Dr(Account.FVPL_VALUATION, 50), Cr(Account.VALUATION_GAIN, 50),
                  reverses=val)
    assert template_of(val).code == "JT-09" and template_of(back).code == "JT-08" and template_of(wrong) is None


def test_dividend_entries_find_their_templates():
    accrual = entry(DAY, "DIVIDEND_ACCRUAL", "CA-1", Dr(Account.DIVIDEND_RECEIVABLE, 1000),
                    Cr(Account.DIVIDEND_INCOME, 1000))
    paid_less = entry(DAY, "DIVIDEND", "CA-1", Dr(Account.CASH, 770), Dr(Account.WITHHOLDING_TAX, 140),
                      Dr(Account.DIVIDEND_INCOME, 90), Cr(Account.DIVIDEND_RECEIVABLE, 1000))
    cancel = entry(DAY, "DIVIDEND_CANCEL", "CA-2", Dr(Account.DIVIDEND_INCOME, 1000),
                   Cr(Account.DIVIDEND_RECEIVABLE, 1000))
    assert [template_of(e).code for e in (accrual, paid_less, cancel)] == ["JT-14", "JT-15", "JT-16"]
