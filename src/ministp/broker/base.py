"""Broker-neutral view of an account, and its mapping to reconciliation items (spec R-70~71)."""
import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from ..ledger import Ledger
from ..recon import ReconItem


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int
    purchase_amount: int  # 매입금액 (수수료 제외)


@dataclass(frozen=True)
class BrokerBalance:
    positions: dict[str, BrokerPosition]
    cash_d0: int         # 예수금 (결제 완료분)
    cash_projected: int  # 결제 후 예상 예수금 (현재 D+2)
    raw: tuple[dict, ...] = ()  # 원본 응답 (broker_snapshots 저장용)


@dataclass(frozen=True)
class OrderAck:
    order_no: str   # 주문번호 (ODNO)
    org_no: str     # 주문조직번호 (KRX_FWDG_ORD_ORGNO), needed to cancel


@dataclass(frozen=True)
class BrokerOrder:
    """One order as the broker reports it today (cumulative)."""
    order_no: str
    symbol: str
    side: str            # "BUY" | "SELL"
    qty: int
    filled_qty: int
    filled_amount: int   # 총체결금액
    canceled_qty: int
    rejected_qty: int


class BrokerGateway(Protocol):
    def get_balance(self) -> BrokerBalance: ...
    def get_price(self, symbol: str) -> int: ...
    def place_order(self, symbol: str, side: str, qty: int, limit_price: int | None) -> OrderAck: ...
    def cancel_order(self, ack: OrderAck) -> None: ...
    def daily_orders(self, day: dt.date) -> list[BrokerOrder]: ...


def recon_items(ledger: Ledger, broker: BrokerBalance) -> list[ReconItem]:
    """Position, book cost and cash items for every symbol on either side."""
    items = []
    internal = ledger.positions()
    for sym in sorted(set(internal) | set(broker.positions)):
        mine = internal.get(sym)
        theirs = broker.positions.get(sym)
        items.append(ReconItem("POSITION_QTY", sym, mine.qty if mine else 0,
                               theirs.qty if theirs else 0))
        items.append(ReconItem("BOOK_COST", sym, mine.book_cost if mine else 0,
                               theirs.purchase_amount if theirs else 0))
    items.append(ReconItem("CASH_D0", "", ledger.cash_balance(), broker.cash_d0))
    items.append(ReconItem("CASH_PROJECTED", "", ledger.projected_cash(), broker.cash_projected))
    return items
