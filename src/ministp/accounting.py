"""Chart of accounts v2 and balanced journal entries (spec D4, R-50, docs/spec/05-chart-of-accounts-v2.md).

v2 (ERD v2.13): the books belong to a company (D9), so the names and two accounts follow a company ledger.
1010 is the brokerage deposit (증권예치금), not the liability a company calls 예수금; 3010 is the transfer from
head office (본사 자금 대체), not equity; tax withheld is prepaid tax (1040 선납세금, an asset), not an expense,
so 5050 is retired: kept for entries already booked, never used again; an ETF's distribution is its own income
(4050 분배금수익). Codes never change meaning (R-04): a renamed account keeps its code."""
import datetime as dt
from dataclasses import dataclass
from enum import Enum


class Account(Enum):
    # code, name, normal side ("D" debit / "C" credit)
    CASH = ("1010", "증권예치금", "D")
    SELL_RECEIVABLE = ("1020", "매도미수금", "D")
    DIVIDEND_RECEIVABLE = ("1030", "미수배당금", "D")
    PREPAID_TAX = ("1040", "선납세금", "D")
    FVPL_STOCK = ("1100", "FVPL 금융자산", "D")
    FVPL_VALUATION = ("1110", "FVPL 금융자산 평가조정", "D")
    BUY_PAYABLE = ("2010", "매수미지급금", "C")
    CONTRIBUTED_CAPITAL = ("3010", "본사 자금 대체", "C")
    TRADING_GAIN = ("4010", "매매이익", "C")
    VALUATION_GAIN = ("4020", "평가이익", "C")
    DIVIDEND_INCOME = ("4030", "배당금수익", "C")
    INTEREST_INCOME = ("4040", "이자수익", "C")
    DISTRIBUTION_INCOME = ("4050", "분배금수익", "C")
    MISC_INCOME = ("4090", "잡이익", "C")
    TRADING_LOSS = ("5010", "매매손실", "D")
    VALUATION_LOSS = ("5020", "평가손실", "D")
    COMMISSION = ("5030", "지급수수료", "D")
    TAXES = ("5040", "세금과공과", "D")
    WITHHOLDING_TAX = ("5050", "법인세등(원천징수)", "D")     # retired in v2: no new entry uses it
    MISC_LOSS = ("5090", "잡손실", "D")

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def normal_side(self) -> str:
        return self.value[2]

    @property
    def in_use(self) -> bool:
        return self not in RETIRED


RETIRED = frozenset({Account.WITHHOLDING_TAX})


class UnbalancedEntryError(ValueError):
    pass


@dataclass(frozen=True)
class JournalLine:
    account: Account
    debit: int = 0
    credit: int = 0


@dataclass(frozen=True)
class JournalEntry:
    date: dt.date
    kind: str
    source_id: str
    lines: tuple[JournalLine, ...]
    reverses: "JournalEntry | None" = None

    def __post_init__(self):
        dr = sum(ln.debit for ln in self.lines)
        cr = sum(ln.credit for ln in self.lines)
        if dr != cr:
            raise UnbalancedEntryError(f"{self.kind} {self.source_id}: debit {dr} != credit {cr}")
        if any(ln.debit < 0 or ln.credit < 0 for ln in self.lines):
            raise UnbalancedEntryError("negative amounts are not allowed; swap sides instead")


def entry(date: dt.date, kind: str, source_id: str, *lines: JournalLine, reverses=None) -> JournalEntry:
    """Build an entry, dropping zero lines."""
    kept = tuple(ln for ln in lines if ln.debit or ln.credit)
    return JournalEntry(date, kind, source_id, kept, reverses)


def Dr(account: Account, amount: int) -> JournalLine:
    return JournalLine(account, debit=amount)


def Cr(account: Account, amount: int) -> JournalLine:
    return JournalLine(account, credit=amount)
