"""Live/paper trading session: risk -> OMS -> broker -> fill sync -> ledger.

Fills are picked up by polling the broker's cumulative per-order state
(`daily_orders`) and booking only the increase since the last sync, so a sync
can run any number of times without double booking. A real-time fill feed can
later call the same `sync` path.
"""
import datetime as dt
from dataclasses import dataclass, field

from .broker.base import BrokerGateway, OrderAck, recon_items
from .ledger import ExecutionEvent, Ledger, Side
from .oms import OrderManager, OrderStatus, OverfillError
from .recon import ReconItem, ReconResult, reconcile
from .risk import RiskLimits, check_order


@dataclass(frozen=True)
class Anomaly:
    day: dt.date
    kind: str      # UNKNOWN_ORDER | ODD_FILL_PRICE | BROKER_REJECTED | SUBMIT_FAILED | OVERFILL
    ref: str
    detail: str


@dataclass
class _Tracked:
    client_id: str
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

    def _open_orders(self):
        closed = (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)
        return [o for o in (self.oms.orders[t.client_id] for t in self._by_client.values())
                if o.status not in closed]

    def _pending_buy_value(self, ref: dict[str, int]) -> int:
        return sum((o.qty - o.filled_qty) * (o.limit_price or ref.get(o.symbol, 0))
                   for o in self._open_orders() if o.side == Side.BUY)

    def _pending_sell_qty(self, symbol: str) -> int:
        return sum(o.qty - o.filled_qty for o in self._open_orders()
                   if o.side == Side.SELL and o.symbol == symbol)

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
        decision = check_order(self.limits, side=side, qty=qty, ref_price=ref,
                               held_qty=self.ledger.position(symbol).qty,
                               pending_sell_qty=self._pending_sell_qty(symbol),
                               buying_power=self.ledger.projected_cash(),
                               pending_buy_value=self._pending_buy_value({symbol: ref}),
                               halted=self.oms.halted)
        self.oms.create(cid, symbol=symbol, side=side, qty=qty, limit_price=limit_price or 0)
        if not decision.ok:
            self.oms.on_rejected(cid)
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"risk: {decision.reason}"))
            return None
        try:
            ack = self.broker.place_order(symbol, side.value, qty, limit_price)
        except Exception as exc:  # broker said no, or network: nothing was placed we know of
            self.oms.on_rejected(cid)
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"broker: {exc}"))
            return None
        self.oms.on_accepted(cid)
        t = _Tracked(cid, ack)
        self._by_client[cid] = t
        self._by_broker_no[ack.order_no] = t
        return cid

    def executions_of(self, client_id: str) -> list[str]:
        t = self._by_client[client_id]
        return [f"{t.ack.order_no}-{i}" for i in range(1, t.fills + 1)]

    def cancel(self, client_id: str) -> None:
        self.oms.request_cancel(client_id)
        self.broker.cancel_order(self._by_client[client_id].ack)

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
                exec_id = f"{b.order_no}-{t.fills}"
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
        bal = self.broker.get_balance()
        items = recon_items(self.ledger, bal)
        broker_qty = sum(b.filled_qty for b in self.broker.daily_orders(day))
        mine = sum(e.qty for e in self.ledger.events
                   if isinstance(e, ExecutionEvent) and e.trade_date == day)
        items.append(ReconItem("EXEC_QTY", day.isoformat(), mine, broker_qty))
        return reconcile(day, items)
