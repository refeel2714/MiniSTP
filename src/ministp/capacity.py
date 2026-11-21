"""주문가능 (order capacity): one calculation for the console, the screens and the order check.

The screen shows these numbers and recomputes them as the user types; the
backend decides with `check` on the same numbers, so what the screen shows and
what the order check says can never disagree (화면 기획서, 결정 사항).
"""
from dataclasses import dataclass

from .ledger import Ledger, Side
from .risk import RiskDecision, RiskLimits, check_order


@dataclass(frozen=True)
class OpenOrder:
    """The unfilled part of an order that is still live at the broker."""
    client_id: str
    symbol: str
    side: Side
    remaining_qty: int
    limit_price: int | None  # None = market order


@dataclass(frozen=True)
class Capacity:
    symbol: str
    ref_price: int             # 기준 단가: 지정가, 없으면 현재가
    cash_d0: int               # 예수금 D+0
    projected_cash: int        # 결제 예정분 반영 예수금
    pending_buy_value: int     # 미체결 매수 예약 금액
    unpriced_buy_orders: int   # 가격을 몰라 예약 금액에 넣지 못한 시장가 매수 건수
    available_cash: int        # 주문가능금액 = 결제 후 예수금 - 미체결 매수 예약
    max_buy_qty: int           # 이 단가로 살 수 있는 최대 수량
    held_qty: int              # 보유수량
    pending_sell_qty: int      # 미체결 매도 수량
    sellable_qty: int          # 매도가능수량


def pending_buy_value(open_orders: list[OpenOrder], ref_prices: dict[str, int]) -> tuple[int, int]:
    """(reserved won, market buys that could not be priced)."""
    total = unpriced = 0
    for o in open_orders:
        if o.side is not Side.BUY:
            continue
        price = o.limit_price or ref_prices.get(o.symbol)
        if price is None:
            unpriced += 1
        else:
            total += o.remaining_qty * price
    return total, unpriced


def _max_buy_qty(limits: RiskLimits, room: int, ref_price: int) -> int:
    if ref_price <= 0 or room <= 0:
        return 0
    qty = int(room / (ref_price * (1 + limits.cash_buffer_rate)))
    while qty > 0 and int(qty * ref_price * (1 + limits.cash_buffer_rate)) > room:
        qty -= 1
    while int((qty + 1) * ref_price * (1 + limits.cash_buffer_rate)) <= room:
        qty += 1
    if limits.max_order_value is not None:
        qty = min(qty, limits.max_order_value // ref_price)
    return qty


def capacity(ledger: Ledger, open_orders: list[OpenOrder], symbol: str, ref_price: int,
             limits: RiskLimits = RiskLimits(), ref_prices: dict[str, int] | None = None) -> Capacity:
    prices = {symbol: ref_price, **(ref_prices or {})}
    reserved, unpriced = pending_buy_value(open_orders, prices)
    projected = ledger.projected_cash()
    held = ledger.position(symbol).qty
    selling = sum(o.remaining_qty for o in open_orders if o.side is Side.SELL and o.symbol == symbol)
    return Capacity(symbol=symbol, ref_price=ref_price, cash_d0=ledger.cash_balance(),
                    projected_cash=projected, pending_buy_value=reserved, unpriced_buy_orders=unpriced,
                    available_cash=projected - reserved,
                    max_buy_qty=_max_buy_qty(limits, projected - reserved, ref_price),
                    held_qty=held, pending_sell_qty=selling, sellable_qty=max(held - selling, 0))


def check(cap: Capacity, side: Side, qty: int, limits: RiskLimits = RiskLimits(),
          halted: bool = False) -> RiskDecision:
    """The backend order check (R-36), fed by the same numbers the screen shows."""
    return check_order(limits, side=side, qty=qty, ref_price=cap.ref_price, held_qty=cap.held_qty,
                       pending_sell_qty=cap.pending_sell_qty, buying_power=cap.projected_cash,
                       pending_buy_value=cap.pending_buy_value, halted=halted)
