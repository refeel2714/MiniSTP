"""분개 모형: 이벤트 유형마다 원장이 어떤 전표를 만드는지, 사람이 읽는 표로.

전표는 `ledger.Ledger`의 `_apply_*`가 만들어요. 이 표는 그 규칙을 옮겨 적은 것이고, 원장이 이 표를 읽어 분개하지는
않아요(분개 모형 테이블과 그 테이블을 읽는 원장은 게이트 뒤). 그래서 표와 원장이 어긋나지 않게
`tests/unit/test_ledger_properties.py`가 무작위로 만든 장부의 전표마다 이 표의 모형 하나에 들어맞는지 확인해요.

줄의 금액이 0이면 그 줄은 전표에 들어가지 않아요(`accounting.entry`). 예를 들어 ETF 매도에는 거래세 줄이 없어요.
"""
from dataclasses import dataclass

from .accounting import Account, JournalEntry


@dataclass(frozen=True)
class TemplateLine:
    side: str               # "D" 차변 / "C" 대변
    account: Account | None  # None: 직전 전표에서 정해져요 (평가 역분개)
    amount: str             # 금액 산식
    rule: str = ""          # 업무 규칙 번호 (스펙)


@dataclass(frozen=True)
class JournalTemplate:
    code: str
    event_type: str         # ledger_events.event_type
    kind: str               # journal_entries.kind
    case: str               # 같은 이벤트에서 모형이 갈리는 조건
    lines: tuple[TemplateLine, ...]
    note: str = ""

    def fits(self, e: JournalEntry) -> bool:
        """Whether a journal entry is one this template makes: same kind, and every line on a side and account
        the template has (a template line may be missing from the entry when its amount was 0)."""
        if e.kind != self.kind:
            return False
        if any(t.account is None for t in self.lines):   # 역분개: the lines are those of the entry it reverses
            return e.reverses is not None and {(ln.account, bool(ln.debit)) for ln in e.lines} == \
                {(ln.account, bool(ln.credit)) for ln in e.reverses.lines}
        allowed = {(t.account, t.side == "D") for t in self.lines}
        return all((ln.account, bool(ln.debit)) in allowed for ln in e.lines)


# 전표 유형 코드 -> 화면에 적는 이름 (분개 모형 STP-4500과 분개 조회 전표 보기가 함께 써요).
# 아직 원장이 만들지 않는 유형(지연 체결, 배당, 수동 조정)도 미리 적어 둬요. 없는 코드는 코드 그대로 보여요.
KIND_NAMES = {
    "DEPOSIT": "입금", "OPENING": "기초 잔고", "EXECUTION": "체결", "LATE_EXECUTION": "지연 체결", "SETTLEMENT": "결제",
    "VALUATION": "평가", "VALUATION_REVERSAL": "평가 역분개", "DIVIDEND_ACCRUAL": "배당 미수", "DIVIDEND": "배당 입금",
    "DIVIDEND_CANCEL": "배당 취소", "ADJUSTMENT": "수동 조정", "ADJUSTMENT_CANCEL": "조정 취소",
}
SIDE_NAMES = {"D": "차변", "C": "대변"}


def D(account, amount, rule=""):
    return TemplateLine("D", account, amount, rule)


def C(account, amount, rule=""):
    return TemplateLine("C", account, amount, rule)


A = Account
TEMPLATES: tuple[JournalTemplate, ...] = (
    JournalTemplate("JT-01", "DepositEvent", "DEPOSIT", "투자원금 입금", (
        D(A.CASH, "입금액"),
        C(A.CONTRIBUTED_CAPITAL, "입금액")),
        "좌수는 직전 기준가로 늘어나요 (R-61)"),
    JournalTemplate("JT-02", "OpeningPositionEvent", "OPENING", "기초 보유 등록", (
        D(A.FVPL_STOCK, "장부금액"),
        C(A.CONTRIBUTED_CAPITAL, "장부금액")),
        "이미 들고 있던 종목을 장부에 올릴 때 한 번"),
    JournalTemplate("JT-03", "ExecutionEvent", "EXECUTION", "매수 체결", (
        D(A.FVPL_STOCK, "체결금액 (수량 × 체결단가)", "R-31"),
        D(A.COMMISSION, "수수료"),
        C(A.BUY_PAYABLE, "체결금액 + 수수료", "R-51")),
        "결제일(T+2)에 예수금에서 나가요 (JT-06)"),
    JournalTemplate("JT-04", "ExecutionEvent", "EXECUTION", "매도 체결, 이익", (
        D(A.SELL_RECEIVABLE, "체결금액 − 수수료 − 세금", "R-14"),
        D(A.COMMISSION, "수수료"),
        D(A.TAXES, "거래세 + 농특세"),
        C(A.FVPL_STOCK, "매도원가 (이동평균)", "R-32·33"),
        C(A.TRADING_GAIN, "체결금액 − 매도원가", "R-34·53")),
        "결제일에 예수금으로 들어와요 (JT-07)"),
    JournalTemplate("JT-05", "ExecutionEvent", "EXECUTION", "매도 체결, 손실", (
        D(A.SELL_RECEIVABLE, "체결금액 − 수수료 − 세금", "R-14"),
        D(A.COMMISSION, "수수료"),
        D(A.TAXES, "거래세 + 농특세"),
        D(A.TRADING_LOSS, "매도원가 − 체결금액", "R-34·53"),
        C(A.FVPL_STOCK, "매도원가 (이동평균)", "R-32·33")),
        "결제일에 예수금으로 들어와요 (JT-07)"),
    JournalTemplate("JT-06", "SettlementEvent", "SETTLEMENT", "매수 결제", (
        D(A.BUY_PAYABLE, "체결 때 잡은 미지급금", "R-52"),
        C(A.CASH, "체결 때 잡은 미지급금", "R-52"))),
    JournalTemplate("JT-07", "SettlementEvent", "SETTLEMENT", "매도 결제", (
        D(A.CASH, "체결 때 잡은 미수금", "R-54"),
        C(A.SELL_RECEIVABLE, "체결 때 잡은 미수금", "R-54"))),
    JournalTemplate("JT-08", "RevaluationEvent", "VALUATION_REVERSAL", "직전 평가가 있을 때 먼저", (
        D(None, "직전 평가 전표의 대변 줄 그대로"),
        C(None, "직전 평가 전표의 차변 줄 그대로")),
        "평가는 매일 새로 해요: 어제 평가를 되돌린 뒤 오늘 평가를 잡아요"),
    JournalTemplate("JT-09", "RevaluationEvent", "VALUATION", "평가이익 (합계가 0보다 클 때)", (
        D(A.FVPL_VALUATION, "Σ(보유수량 × 종가 − 장부금액)"),
        C(A.VALUATION_GAIN, "Σ(보유수량 × 종가 − 장부금액)")),
        "종목별이 아니라 계좌 합계로 한 전표"),
    JournalTemplate("JT-10", "RevaluationEvent", "VALUATION", "평가손실 (합계가 0보다 작을 때)", (
        D(A.VALUATION_LOSS, "Σ(장부금액 − 보유수량 × 종가)"),
        C(A.FVPL_VALUATION, "Σ(장부금액 − 보유수량 × 종가)")),
        "종목별이 아니라 계좌 합계로 한 전표"),
)


# 지연 체결(R-78): 마감한 날의 체결은 그날 장부에 넣을 수 없어 다음 영업일을 회계일로 잡아요. 줄은 체결 모형과 같아요.
TEMPLATES += tuple(
    JournalTemplate(code, "LateExecutionEvent", "LATE_EXECUTION", f"{t.case}, 마감한 날의 체결", t.lines,
                    f"줄은 {t.code}와 같고 회계일만 다음 영업일이에요 (R-78)")
    for code, t in zip(("JT-11", "JT-12", "JT-13"), (t for t in TEMPLATES if t.kind == "EXECUTION")))


def template_of(e: JournalEntry) -> JournalTemplate | None:
    """The template a journal entry fits, or None (the ledger made an entry this table does not describe)."""
    return next((t for t in TEMPLATES if t.fits(e)), None)
