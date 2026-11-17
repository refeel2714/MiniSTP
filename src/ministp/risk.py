"""Pre-trade risk checks (spec R-36, MVP 리스크 관리)."""
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


def check_order(limits: RiskLimits, *, side: Side, qty: int, ref_price: int,
                held_qty: int, pending_sell_qty: int, buying_power: int,
                pending_buy_value: int, halted: bool) -> RiskDecision:
    if halted:
        return RiskDecision(False, "KILL_SWITCH")
    if qty <= 0:
        return RiskDecision(False, "BAD_QTY")
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
