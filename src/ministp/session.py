"""Live/paper trading session: risk -> OMS -> broker -> fill sync -> ledger.

Fills are picked up by polling the broker's cumulative per-order state
(`daily_orders`) and booking only the increase since the last sync, so a sync
can run any number of times without double booking. A real-time fill feed can
later call the same `sync` path.
"""
import datetime as dt
from dataclasses import dataclass, field

from .broker.base import BrokerBalance, BrokerGateway, OrderAck, recon_items
from .ledger import ExecutionEvent, Ledger, Side
from .oms import InvalidTransition, OrderManager, OrderStatus, OverfillError
from .recon import ReconItem, ReconResult, reconcile
from .capacity import OpenOrder, capacity, check
from .risk import RiskLimits, explain


def exec_prefix(day: dt.date, order_no: str) -> str:
    """Execution ids are `YYYYMMDD-주문번호-순번`: brokers reuse order numbers on other days."""
    return f"{day:%Y%m%d}-{order_no}-"


def failure_text(detail: str) -> str:
    """An anomaly detail for a person: `risk: BAD_TICK` -> `주문 검증: 호가단위에 맞지 않는 … [BAD_TICK]`."""
    kind, _, rest = detail.partition(": ")
    return f"주문 검증: {explain(rest)}" if kind == "risk" else detail


@dataclass(frozen=True)
class Anomaly:
    day: dt.date
    kind: str      # UNKNOWN_ORDER | ODD_FILL_PRICE | BROKER_REJECTED | SUBMIT_FAILED | OVERFILL
    ref: str
    detail: str


@dataclass
class _Tracked:
    client_id: str
    day: dt.date
    ack: OrderAck
    booked_qty: int = 0
    booked_amount: int = 0
    fills: int = 0


@dataclass
class TradingSession:
    ledger: Ledger
    broker: BrokerGateway
    oms: OrderManager = field(default_factory=OrderManager)
    limits: RiskLimits = field(default_factory=RiskLimits)
    anomalies: list[Anomaly] = field(default_factory=list)
    _by_broker_no: dict[str, _Tracked] = field(default_factory=dict)
    _by_client: dict[str, _Tracked] = field(default_factory=dict)
    _seq: int = 0
    last_balance: BrokerBalance | None = None
    on_submit: object = None  # callable(cid, day, symbol, side, qty, limit) before the broker call
    on_ack: object = None     # callable(cid, ack | None, detail) after it
    on_cancel: object = None  # callable(cid, ack) after the broker took a cancel request

    def _open_orders(self):
        closed = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)
        return [o for o in (self.oms.orders[t.client_id] for t in self._by_client.values())
                if o.status not in closed]

    def open_orders(self) -> list[OpenOrder]:
        return [OpenOrder(o.order_id, o.symbol, Side(o.side), o.qty - o.filled_qty, o.limit_price or None)
                for o in self._open_orders()]

    def capacity(self, symbol: str, ref_price: int, ref_prices: dict[str, int] | None = None):
        return capacity(self.ledger, self.open_orders(), symbol, ref_price, self.limits, ref_prices)

    def restore(self, day: dt.date, client_id: str, symbol: str, side: Side, qty: int, limit_price: int | None,
                ack: OrderAck, cancel_requested: bool = False) -> None:
        """Re-track an order placed earlier today by another process, with fills already in the ledger.
        `cancel_requested`: that process asked the broker to cancel it (R-45)."""
        self.oms.create(client_id, symbol=symbol, side=side, qty=qty, limit_price=limit_price or 0)
        self.oms.on_accepted(client_id)
        t = _Tracked(client_id, day, ack)
        prefix = exec_prefix(day, ack.order_no)
        for e in self.ledger.events:
            if isinstance(e, ExecutionEvent) and e.trade_date == day and e.exec_id.startswith(prefix):
                t.fills += 1
                t.booked_qty += e.qty
                t.booked_amount += e.qty * e.price
                self.oms.on_fill(client_id, e.qty)
        if cancel_requested and self.oms.orders[client_id].status is not OrderStatus.FILLED:
            self.oms.request_cancel(client_id)
        self._by_client[client_id] = t
        self._by_broker_no[ack.order_no] = t

    def submit(self, day: dt.date, symbol: str, side: Side, qty: int,
               limit_price: int | None = None) -> str | None:
        """Returns the client order id, or None if risk or the broker refused it."""
        self._seq += 1
        cid = f"S{day:%Y%m%d}-{self._seq:04d}"
        if self.oms.halted:
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, "kill switch engaged"))
            return None
        try:
            ref = limit_price or self.broker.get_price(symbol)
        except Exception as exc:
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"price: {exc}"))
            return None
        inst = self.ledger.instruments.get(symbol)
        decision = check(self.capacity(symbol, ref), side, qty, self.limits, self.oms.halted,
                         limit_price=limit_price, asset_type=inst.asset_type if inst else None,
                         business_day=self.ledger.calendar.is_business_day(day))
        self.oms.create(cid, symbol=symbol, side=side, qty=qty, limit_price=limit_price or 0)
        if not decision.ok:
            self.oms.on_rejected(cid)
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"risk: {decision.reason}"))
            return None
        if self.on_submit:
            self.on_submit(cid, day, symbol, side, qty, limit_price)  # durable intent first
        try:
            ack = self.broker.place_order(symbol, side.value, qty, limit_price)
        except Exception as exc:  # broker said no, or network: nothing was placed we know of
            self.oms.on_rejected(cid)
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"broker: {exc}"))
            if self.on_ack:
                self.on_ack(cid, None, f"broker: {exc}")
            return None
        if self.on_ack:
            self.on_ack(cid, ack, "")
        self.oms.on_accepted(cid)
        t = _Tracked(cid, day, ack)
        self._by_client[cid] = t
        self._by_broker_no[ack.order_no] = t
        return cid

    def executions_of(self, client_id: str) -> list[str]:
        t = self._by_client[client_id]
        return [f"{exec_prefix(t.day, t.ack.order_no)}{i}" for i in range(1, t.fills + 1)]

    def find(self, ref: str) -> str:
        """An order of this session by client id (`S20261124-0003`), its number (`3`) or the broker's
        order number (`0000012345`)."""
        ref = ref.strip()
        if ref in self._by_client:
            return ref
        if ref in self._by_broker_no:
            return self._by_broker_no[ref].client_id
        if ref.isdigit():
            hits = [cid for cid in self._by_client if cid.rsplit("-", 1)[-1].lstrip("0") == ref.lstrip("0")]
            if len(hits) == 1:
                return hits[0]
        raise KeyError(ref)

    def cancel(self, client_id: str) -> None:
        """R-45: cancel the whole unfilled rest. The broker is asked first; the order becomes
        `CANCEL_PENDING` only once the broker took the request, and `CANCELED` when a sync sees
        the broker's confirmed quantity. A refused request leaves the order as it was."""
        o = self.oms.orders[client_id]
        if o.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise InvalidTransition(f"{client_id}: {o.status} 상태라 취소할 수 없어요")
        self.broker.cancel_order(self._by_client[client_id].ack)
        self.oms.request_cancel(client_id)
        if self.on_cancel:
            self.on_cancel(client_id, self._by_client[client_id].ack)

    def sync(self, day: dt.date) -> list[str]:
        """Book fill increases reported by the broker. Returns new execution ids."""
        booked = []
        for b in self.broker.daily_orders(day):
            t = self._by_broker_no.get(b.order_no)
            if t is None:
                if not any(a.kind == "UNKNOWN_ORDER" and a.ref == b.order_no for a in self.anomalies):
                    self.anomalies.append(Anomaly(day, "UNKNOWN_ORDER", b.order_no,
                                                  f"{b.side} {b.symbol} x{b.qty} placed outside MiniSTP"))
                continue
            o = self.oms.orders[t.client_id]
            dq, da = b.filled_qty - t.booked_qty, b.filled_amount - t.booked_amount
            if dq > 0:
                if da % dq:
                    # several prices inside one increase; wait for per-fill data
                    self.anomalies.append(Anomaly(day, "ODD_FILL_PRICE", b.order_no,
                                                  f"+{dq} shares for {da} won"))
                    continue
                try:
                    self.oms.on_fill(t.client_id, dq)
                except OverfillError as exc:
                    self.anomalies.append(Anomaly(day, "OVERFILL", b.order_no, str(exc)))
                    raise
                t.fills += 1
                exec_id = f"{exec_prefix(day, b.order_no)}{t.fills}"
                self.ledger.execute(exec_id, day, b.symbol, Side(b.side), dq, da // dq)
                t.booked_qty, t.booked_amount = b.filled_qty, b.filled_amount
                booked.append(exec_id)
            done = b.filled_qty + b.canceled_qty + b.rejected_qty
            if b.canceled_qty and done == o.qty and o.status is not OrderStatus.CANCELED \
                    and t.booked_qty == b.filled_qty:
                if o.status is not OrderStatus.CANCEL_PENDING:
                    self.oms.request_cancel(t.client_id)  # canceled from outside (e.g. HTS)
                self.oms.on_cancel_confirmed(t.client_id, b.canceled_qty + b.rejected_qty)
            elif b.rejected_qty and not b.filled_qty and o.status is OrderStatus.SUBMITTED:
                self.oms.on_rejected(t.client_id)
                self.anomalies.append(Anomaly(day, "BROKER_REJECTED", b.order_no, "rejected by exchange"))
        return booked

    def reconcile(self, day: dt.date) -> list[ReconResult]:
        """R-70~71: positions, book cost, cash and today's filled quantity."""
        bal = self.last_balance = self.broker.get_balance()
        items = recon_items(self.ledger, bal)
        broker_qty = sum(b.filled_qty for b in self.broker.daily_orders(day))
        mine = sum(e.qty for e in self.ledger.events
                   if isinstance(e, ExecutionEvent) and e.trade_date == day)
        items.append(ReconItem("EXEC_QTY", day.isoformat(), mine, broker_qty))
        return reconcile(day, items)
