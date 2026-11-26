"""Pre-trade checks (spec R-36~38): one decision for the console, the screens and the order path."""
from dataclasses import dataclass
from decimal import Decimal

from .ledger import Side


@dataclass(frozen=True)
class RiskLimits:
    max_order_value: int | None = None  # 1회 주문 금액 한도 (원)
    cash_buffer_rate: Decimal = Decimal("0.001")  # 가격 변동·수수료 여유분


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str = ""


# What each refusal means, for the console and the screens (one wording for both).
REASON_TEXT = {
    "": "주문 가능",
    "KILL_SWITCH": "킬 스위치가 켜져 있어요",
    "ACCOUNT_NOT_OPEN": "정상 상태의 계좌가 아니에요 (정지·해지 계좌는 주문을 받지 않아요, R-39)",
    "NOT_BUSINESS_DAY": "영업일이 아니에요 (휴장일에는 주문을 내지 않아요, R-38)",
    "BAD_QTY": "수량을 1주 이상 넣어 주세요",
    "BAD_TICK": "호가단위에 맞지 않는 가격이에요 (R-38)",
    "MAX_ORDER_VALUE": "1회 주문 금액 한도를 넘어요",
    "SHORT_SELL": "매도가능수량보다 많아요 (공매도 금지, R-36)",
    "INSUFFICIENT_CASH": "주문가능금액이 모자라요",
    "NO_PRICE": "기준 단가가 없어요: 지정가를 넣어 주세요",
}


def explain(reason: str) -> str:
    """`BAD_TICK` -> `호가단위에 맞지 않는 가격이에요 (R-38) [BAD_TICK]`; unknown codes stay as they are."""
    text = REASON_TEXT.get(reason)
    return f"{text} [{reason}]" if text and reason else reason


def check_order(limits: RiskLimits, *, side: Side, qty: int, ref_price: int,
                held_qty: int, pending_sell_qty: int, buying_power: int,
                pending_buy_value: int, halted: bool, limit_price: int | None = None,
                tick: int | None = None, business_day: bool = True,
                account_open: bool = True) -> RiskDecision:
    """`limit_price` with `tick` checks the price unit; `business_day=False` refuses the order (R-38);
    `account_open=False` refuses it because the account is suspended or closed (R-39).

    Order form first (day, quantity, price unit), then limits and capacity, so a
    mistyped order is reported as mistyped and not as a shortage of cash."""
    if halted:
        return RiskDecision(False, "KILL_SWITCH")
    if not account_open:
        return RiskDecision(False, "ACCOUNT_NOT_OPEN")
    if not business_day:
        return RiskDecision(False, "NOT_BUSINESS_DAY")
    if qty <= 0:
        return RiskDecision(False, "BAD_QTY")
    if limit_price is not None and tick and limit_price % tick:
        return RiskDecision(False, "BAD_TICK")
    value = qty * ref_price
    if limits.max_order_value is not None and value > limits.max_order_value:
        return RiskDecision(False, "MAX_ORDER_VALUE")
    if side is Side.SELL:
        if qty > held_qty - pending_sell_qty:
            return RiskDecision(False, "SHORT_SELL")  # R-36
    else:
        need = int(value * (1 + limits.cash_buffer_rate)) + pending_buy_value
        if need > buying_power:
            return RiskDecision(False, "INSUFFICIENT_CASH")
    return RiskDecision(True)
