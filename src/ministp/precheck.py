"""묶음 검증 (spec R-98): 여러 주문 줄을 한 번에, 보낼 순서대로 쌓아 가며 검사해요.

한 줄씩 보면 통과인데 같이 보내면 걸리는 경우(앞 줄 매수가 주문가능금액을 다 쓰거나, 같은 종목을 두 줄에서
사서 비중 한도를 넘거나, 두 줄의 매도 수량을 합치면 매도가능수량을 넘는 경우)를 잡는 게 목적이에요.

- 범위는 계좌 하나예요: 증권사 미체결 주문 + 먼저 잡힌 요청(`committed`: 결재 대기·승인 후 미전송) + 선택한 줄.
- 줄마다 주문 검증 함수(`risk.check_order`, R-36~38)를 두 번 불러요.
  `alone`은 이 줄만 보낼 때, `together`는 앞 줄들을 모두 보냈다고 칠 때예요. 화면은 둘 다 보여 줘서
  "혼자는 통과, 같이 보내면 실패"인 줄을 짚어요.
- 선택한 줄은 막힌 줄도 보낸다고 치고 쌓아요(사용자가 고른 그대로 보내면 어떻게 되는지를 보여 줘요).
- 매도 대금은 매수 자금으로 쓰지 않아요(체결·결제 전이니까요).
- 비중 한도는 매수에만 걸어요: (보유 평가금액 + 미체결 매수 + 앞서 잡힌 매수 + 이 줄) ÷ 총자산.
  한도를 넘은 종목을 줄이는 매도는 막지 않아요.
- 묶음(`group`, 예: 바스켓 번호)은 전부 아니면 전무예요: 한 줄이 막히면 나머지 줄도 `GROUP_BLOCKED`.
- 같은 계산을 네 번 써요: 체크박스를 바꿀 때, 결재 요청 때, 승인 때(고른 줄만 다시), `run`이 보내기 직전.

계산만 하고 DB에 쓰지 않아요.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from .instruments import Instrument
from .ledger import Side
from .risk import REASON_TEXT, RiskLimits, check_order
from .ticks import tick_size

HUNDRED = Decimal(100)
CENT = Decimal("0.01")
DEFAULT_WEIGHT_LIMIT = Decimal(30)   # 종목당 비중 한도(%), 투자 지침(D9 더할 것 2) 전까지의 예시값

ISSUE_TEXT = {
    "ACCOUNT_NOT_OPEN": "정상 상태의 계좌가 아니에요 (정지·해지 계좌는 주문을 받지 않아요)",
    **{k: v for k, v in REASON_TEXT.items() if k},     # 주문 검증의 거부 사유 그대로 (R-36~38)
    "NOT_REGISTERED": "등록되지 않은 종목이에요",
    "NO_PRICE": "현재가가 없어요: 가격을 넣어 주세요",
    "WEIGHT_LIMIT": "주문 후 비중이 종목당 한도를 넘어요",
    "INSUFFICIENT_CASH": "필요 금액이 주문가능금액보다 커요 (R-37)",
    "GROUP_BLOCKED": "같은 묶음의 다른 줄이 막혀서 함께 보낼 수 없어요",
}


@dataclass(frozen=True)
class Proposed:
    """검사할 주문 한 줄 (화면의 입력 줄, 결재 요청, 바스켓 줄)."""
    symbol: str
    side: Side
    qty: int
    ref_price: int | None              # 계산 단가: 지정가, 시장가는 현재가
    limit_price: int | None = None     # 지정가 (시장가는 None)
    key: str = ""                      # 화면 줄 번호나 결재 요청 번호
    group: str = ""                    # 묶음 번호 (바스켓 BK 번호): 전부 아니면 전무

    @property
    def amount(self) -> int:
        return self.qty * (self.ref_price or 0)


@dataclass(frozen=True)
class AccountState:
    """검사 기준이 되는 계좌의 지금 모습."""
    instruments: dict[str, Instrument]
    prices: dict[str, int]              # 종목별 현재가 (비중 계산용)
    held_qty: dict[str, int]            # 보유수량
    projected_cash: int                 # 결제 후 예수금
    reserved_buy: int = 0               # 증권사 미체결 매수 예약 금액 (전체)
    open_buy_value: dict[str, int] = field(default_factory=dict)   # 그중 종목별 (비중 한도용)
    open_sell_qty: dict[str, int] = field(default_factory=dict)    # 증권사 미체결 매도 수량
    business_day: bool = True
    halted: bool = False                # 킬 스위치
    active: bool = True                 # 계좌 상태 (해지·정지 계좌는 False)

    @property
    def available_cash(self) -> int:
        """주문가능금액 (R-37) = 결제 후 예수금 − 미체결 매수 예약."""
        return self.projected_cash - self.reserved_buy

    def held_value(self, symbol: str) -> int:
        price = self.prices.get(symbol)
        return self.held_qty.get(symbol, 0) * price if price else 0

    @property
    def total_assets(self) -> int:
        """비중의 분모: 보유 평가금액 + 결제 후 예수금 (매수는 현금이 주식으로 바뀔 뿐이라 분모가 그대로예요)."""
        return sum(self.held_value(s) for s in self.held_qty) + self.projected_cash


@dataclass(frozen=True)
class LineCheck:
    item: Proposed
    alone: tuple[str, ...]              # 이 줄만 보낼 때 (먼저 잡힌 요청은 포함)
    together: tuple[str, ...]           # 앞 줄들을 모두 보냈다고 칠 때
    post_weight: Decimal | None         # 앞 줄까지 반영한 주문 후 비중(%)

    @property
    def blocked(self) -> bool:
        return bool(self.together)

    @property
    def breaks_set(self) -> bool:
        """혼자는 통과인데 같이 보내면 걸리는 줄."""
        return not self.alone and bool(self.together)


@dataclass(frozen=True)
class SetCheck:
    lines: tuple[LineCheck, ...]
    issues: tuple[str, ...]             # 계좌 전체: 킬 스위치, 계좌 상태, 합계 주문가능금액
    available_cash: int                 # 먼저 잡힌 요청을 뺀 주문가능금액
    required_cash: int                  # 선택한 매수 Σ 주문금액 × (1 + 여유분율)

    @property
    def ok(self) -> bool:
        return not self.issues and not any(ln.blocked for ln in self.lines)

    @property
    def first_blocked(self) -> int | None:
        """보내는 순서로 처음 막히는 줄의 위치 (0부터)."""
        return next((i for i, ln in enumerate(self.lines) if ln.blocked), None)


def _pct(part: int, whole: int) -> Decimal | None:
    return (Decimal(max(part, 0)) * HUNDRED / whole).quantize(CENT) if whole > 0 else None


@dataclass
class _Running:
    """앞서 잡힌 주문들의 누적: 매수 예약(원), 종목별 매수 금액, 종목별 매도 수량·금액."""
    reserved: int = 0
    buys: dict[str, int] = field(default_factory=dict)
    sell_qty: dict[str, int] = field(default_factory=dict)
    sell_value: dict[str, int] = field(default_factory=dict)

    def add(self, p: Proposed) -> None:
        if p.qty <= 0 or p.ref_price is None:
            return
        if p.side is Side.BUY:
            self.reserved += p.amount
            self.buys[p.symbol] = self.buys.get(p.symbol, 0) + p.amount
        else:
            self.sell_qty[p.symbol] = self.sell_qty.get(p.symbol, 0) + p.qty
            self.sell_value[p.symbol] = self.sell_value.get(p.symbol, 0) + p.amount

    def copy(self) -> "_Running":
        return _Running(self.reserved, dict(self.buys), dict(self.sell_qty), dict(self.sell_value))


def _check(state: AccountState, p: Proposed, run: _Running, limits: RiskLimits,
           weight_limit: Decimal) -> tuple[tuple[str, ...], Decimal | None]:
    inst = state.instruments.get(p.symbol)
    if inst is None:
        return ("NOT_REGISTERED",), None
    if p.ref_price is None:
        return ("NO_PRICE",), None
    s = p.symbol
    held = state.held_qty.get(s, 0)
    tick = tick_size(p.limit_price, inst.asset_type) if p.limit_price else None
    d = check_order(limits, side=p.side, qty=p.qty, ref_price=p.ref_price, held_qty=held,
                    pending_sell_qty=state.open_sell_qty.get(s, 0) + run.sell_qty.get(s, 0),
                    buying_power=state.projected_cash,
                    pending_buy_value=state.reserved_buy + run.reserved, halted=state.halted,
                    limit_price=p.limit_price, tick=tick, business_day=state.business_day,
                    account_open=state.active)
    issues = [] if d.ok else [d.reason]
    exposure = state.held_value(s) + state.open_buy_value.get(s, 0) + run.buys.get(s, 0)
    if p.side is Side.BUY:
        post = _pct(exposure + p.amount, state.total_assets)
        if post is not None and post > weight_limit:
            issues.append("WEIGHT_LIMIT")
    else:   # 보여 주기만 해요: 줄이는 매도는 한도로 막지 않아요
        post = _pct(exposure - run.sell_value.get(s, 0) - p.amount, state.total_assets)
    return tuple(issues), post


def check_set(state: AccountState, selected: list[Proposed], committed: list[Proposed] = (),
              limits: RiskLimits = RiskLimits(), weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT) -> SetCheck:
    """`selected`를 순서대로 쌓아 가며 검사해요. `committed`는 이미 결재 대기·승인 후 미전송인 요청이에요."""
    base = _Running()
    for p in committed:
        base.add(p)
    run = base.copy()
    lines = []
    for p in selected:
        alone, _ = _check(state, p, base, limits, weight_limit)
        together, post = _check(state, p, run, limits, weight_limit)
        lines.append(LineCheck(p, alone, together, post))
        run.add(p)
    blocked_groups = {ln.item.group for ln in lines if ln.item.group and ln.blocked}
    lines = [LineCheck(ln.item, ln.alone, ("GROUP_BLOCKED",), ln.post_weight)
             if ln.item.group in blocked_groups and not ln.blocked else ln for ln in lines]
    available = state.available_cash - base.reserved
    required = sum(int(p.amount * (1 + limits.cash_buffer_rate))
                   for p in selected if p.side is Side.BUY and p.qty > 0 and p.ref_price)
    issues = []
    if state.halted:
        issues.append("KILL_SWITCH")
    if not state.active:
        issues.append("ACCOUNT_NOT_OPEN")
    if required > available:
        issues.append("INSUFFICIENT_CASH")
    return SetCheck(tuple(lines), tuple(issues), available, required)


def issue_text(issues) -> str:
    return ", ".join(ISSUE_TEXT.get(i, i) for i in issues) or "통과"
