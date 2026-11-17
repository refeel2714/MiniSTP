"""Order state machine with append-only status history (spec R-40~43)."""
import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum


class OrderStatus(StrEnum):
    NEW = "NEW"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    REPLACED = "REPLACED"


TERMINAL = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.REPLACED}


class OverfillError(RuntimeError):
    """R-42: fills exceed order quantity. Engages the kill switch."""


class InvalidTransition(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderEvent:
    event_type: str
    status_after: str
    qty: int = 0
    at: dt.datetime = field(default_factory=dt.datetime.now)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: int
    orig_order_id: str | None = None
    events: list[OrderEvent] = field(default_factory=list)

    @property
    def filled_qty(self) -> int:
        return sum(e.qty for e in self.events if e.event_type == "FILL")

    @property
    def status(self) -> OrderStatus:
        """Current status is derived from history (R-43)."""
        for e in reversed(self.events):
            if e.status_after in OrderStatus.__members__:
                return OrderStatus(e.status_after)
        raise AssertionError("order without events")


class OrderManager:
    def __init__(self):
        self.orders: dict[str, Order] = {}
        self.halted = False
        self.halt_reason: str | None = None

    def halt(self, reason: str) -> None:
        self.halted, self.halt_reason = True, reason

    def _log(self, o: Order, event_type: str, status: str, qty: int = 0) -> None:
        o.events.append(OrderEvent(event_type, status, qty))

    def create(self, order_id: str, *, symbol: str, side: str, qty: int, limit_price: int,
               orig_order_id: str | None = None) -> Order:
        if self.halted:
            raise RuntimeError(f"trading halted: {self.halt_reason}")
        if order_id in self.orders:
            raise ValueError(f"duplicate order {order_id}")
        o = Order(order_id, symbol, side, qty, limit_price, orig_order_id)
        self.orders[order_id] = o
        self._log(o, "CREATE", OrderStatus.NEW)
        return o

    def on_accepted(self, order_id: str) -> None:
        o = self.orders[order_id]
        if o.status is not OrderStatus.NEW:
            raise InvalidTransition(f"{order_id}: accept from {o.status}")
        self._log(o, "ACCEPT", OrderStatus.SUBMITTED)

    def on_rejected(self, order_id: str) -> None:
        o = self.orders[order_id]
        self._log(o, "REJECT", OrderStatus.REJECTED)

    def on_fill(self, order_id: str, qty: int) -> None:
        o = self.orders[order_id]
        if o.filled_qty + qty > o.qty:
            self._log(o, "OVERFILL", "ERROR_OVERFILL", 0)
            self.halt(f"overfill on {order_id}")
            raise OverfillError(f"{order_id}: {o.filled_qty}+{qty} > {o.qty}")
        status = o.status
        if status in (OrderStatus.NEW, OrderStatus.REJECTED, OrderStatus.REPLACED):
            raise InvalidTransition(f"{order_id}: fill in {status}")
        # R-42: fills are booked even after cancel request / confirmation
        if o.filled_qty + qty == o.qty:
            nxt = OrderStatus.FILLED
        elif status in (OrderStatus.CANCEL_PENDING, OrderStatus.CANCELED):
            nxt = status
        else:
            nxt = OrderStatus.PARTIALLY_FILLED
        self._log(o, "FILL", nxt, qty)

    def request_cancel(self, order_id: str) -> None:
        o = self.orders[order_id]
        if o.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise InvalidTransition(f"{order_id}: cancel from {o.status}")
        self._log(o, "CANCEL_REQUEST", OrderStatus.CANCEL_PENDING)

    def on_cancel_confirmed(self, order_id: str, canceled_qty: int) -> None:
        o = self.orders[order_id]
        if o.filled_qty + canceled_qty != o.qty:
            self.halt(f"cancel qty mismatch on {order_id}")
            raise OverfillError(f"{order_id}: filled {o.filled_qty} + canceled {canceled_qty} != {o.qty}")
        self._log(o, "CANCEL_CONFIRM", OrderStatus.CANCELED)
