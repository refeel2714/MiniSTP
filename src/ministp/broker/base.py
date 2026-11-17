"""Broker-neutral view of an account, and its mapping to reconciliation items (spec R-70~71)."""
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


class BrokerGateway(Protocol):
    def get_balance(self) -> BrokerBalance: ...


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
