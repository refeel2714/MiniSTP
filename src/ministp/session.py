"""Live/paper trading session: risk -> OMS -> broker -> fill sync -> ledger.

Fills are picked up by polling the broker's cumulative per-order state
(`daily_orders`) and booking only the increase since the last sync, so a sync
can run any number of times without double booking. A real-time fill feed can
later call the same `sync` path.
"""
import datetime as dt
from dataclasses import dataclass, field

from .broker.base import BrokerBalance, BrokerGateway, OrderAck, OrderRejected, recon_items
from .ledger import ExecutionEvent, LateExecutionEvent, Ledger, Side
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
    kind: str      # UNKNOWN_ORDER | ODD_FILL_PRICE | BROKER_REJECTED | SUBMIT_FAILED | OVERFILL | LATE_FILL
                   # | UNACKED_ORDER (R-103: sent, no answer) | UNACKED_FOUND (found in the broker's list)
    ref: str
    detail: str


@dataclass
class _Tracked:
    client_id: str
    day: dt.date
    ack: OrderAck | None      # None: sent but no answer yet (R-103); found later in the broker's order list
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
    _broker_orders: list = field(default_factory=list)
    last_balance: BrokerBalance | None = None
    on_submit: object = None  # callable(cid, day, symbol, side, qty, limit) before the broker call
    on_ack: object = None     # callable(cid, ack | None, detail) after it
    on_refused: object = None  # callable(day, symbol, side, qty, limit, detail): refused before anything was stored
    account_open: bool = True  # R-39: a suspended or closed account takes no new orders
    id_prefix: str = "S"       # client order ids are unique in the database; other accounts add their id
    on_cancel: object = None  # callable(cid, ack) after the broker took a cancel request
    on_unknown: object = None  # callable(cid, detail): sent, but no usable answer (R-103)
    confirming: bool = False   # a person is settling an unanswered order (audit names it ORDER_CONFIRM)

    @property
    def unacked(self) -> list[str]:
        """R-103: orders sent today with no answer yet. They may be live at the broker, so they keep their
        cash reserved, nothing more is sent until each is found or confirmed not placed, and none is resent."""
        return [cid for cid, t in self._by_client.items() if t.ack is None
                and self.oms.orders[cid].status is OrderStatus.NEW]

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
        cid = f"{self.id_prefix}{day:%Y%m%d}-{self._seq:04d}"
        def refuse(detail: str) -> None:
            # Nothing of this attempt is stored (its id is used again by the next order), so the
            # anomaly lives only as long as this process. `on_refused` is where it is kept.
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, detail))
            if self.on_refused:
                self.on_refused(day, symbol, side, qty, limit_price, detail)

        if self.oms.halted:
            return refuse("kill switch engaged")
        try:
            ref = limit_price or self.broker.get_price(symbol)
        except Exception as exc:
            return refuse(f"price: {exc}")
        inst = self.ledger.instruments.get(symbol)
        decision = check(self.capacity(symbol, ref), side, qty, self.limits, self.oms.halted,
                         limit_price=limit_price, asset_type=inst.asset_type if inst else None,
                         business_day=self.ledger.calendar.is_business_day(day),
                         account_open=self.account_open)
        self.oms.create(cid, symbol=symbol, side=side, qty=qty, limit_price=limit_price or 0)
        if not decision.ok:
            self.oms.on_rejected(cid)
            return refuse(f"risk: {decision.reason}")
        if self.on_submit:
            self.on_submit(cid, day, symbol, side, qty, limit_price)  # durable intent first
        try:
            ack = self.broker.place_order(symbol, side.value, qty, limit_price)
        except OrderRejected as exc:  # the broker answered no: nothing was placed
            self.oms.on_rejected(cid)
            self.anomalies.append(Anomaly(day, "SUBMIT_FAILED", cid, f"broker: {exc}"))
            if self.on_ack:
                self.on_ack(cid, None, f"broker: {exc}")
            return None
        except Exception as exc:      # R-103: no usable answer. It may be at the broker: never resend it
            self._by_client[cid] = _Tracked(cid, day, None)
            detail = f"{side.value} {symbol} x{qty}: 접수 결과 미확인 ({exc}). 증권사 주문내역에서 찾을 때까지 다음 주문을 멈춰요"
            self.anomalies.append(Anomaly(day, "UNACKED_ORDER", cid, detail))
            if self.on_unknown:
                self.on_unknown(cid, str(exc))
            return cid
        self._accept(cid, day, ack, "")
        return cid

    def _accept(self, cid: str, day: dt.date, ack: OrderAck, detail: str) -> None:
        if self.on_ack:
            self.on_ack(cid, ack, detail)
        self.oms.on_accepted(cid)
        t = self._by_client.get(cid)
        if t is None:
            t = self._by_client[cid] = _Tracked(cid, day, ack)
        t.ack = ack
        self._by_broker_no[ack.order_no] = t

    def restore_unacked(self, day: dt.date, client_id: str, symbol: str, side: Side, qty: int,
                        limit_price: int | None) -> None:
        """Re-track an order another process sent with no answer recorded (it died around the broker call,
        or the answer was lost). Same as a fresh unknown answer: cash stays reserved until it is found."""
        self.oms.create(client_id, symbol=symbol, side=side, qty=qty, limit_price=limit_price or 0)
        self._by_client[client_id] = _Tracked(client_id, day, None)
        self.anomalies.append(Anomaly(day, "UNACKED_ORDER", client_id,
                                      f"{side.value} {symbol} x{qty}: 접수 결과 미기록, 증권사 주문내역 확인 필요"))

    def _claim(self, day: dt.date, b) -> str | None:
        """R-103: a broker order MiniSTP does not track may be one of its own unanswered orders. It is taken
        only when exactly one unanswered order and exactly one untracked broker order match on symbol,
        side and quantity, so two look-alike orders are never guessed between; a person confirms those."""
        mine = [cid for cid in self.unacked if (o := self.oms.orders[cid]).symbol == b.symbol
                and o.side == b.side and o.qty == b.qty]
        if len(mine) != 1:
            return None
        twins = [x for x in self._broker_orders if x.order_no not in self._by_broker_no
                 and (x.symbol, x.side, x.qty) == (b.symbol, b.side, b.qty)]
        if len(twins) != 1:
            return None
        cid = mine[0]
        self._accept(cid, day, OrderAck(b.order_no, b.org_no), "증권사 주문내역에서 찾음")
        self.anomalies.append(Anomaly(day, "UNACKED_FOUND", cid, f"주문번호 {b.order_no}로 확인"))
        return cid

    def confirm_unacked(self, day: dt.date, client_id: str, order_no: str | None) -> None:
        """A person settles an unanswered order (R-103): `order_no` is the broker's number for it, or None
        when the broker has no such order (then it is closed as refused, and may be sent again as a new one)."""
        self.confirming = True
        try:
            self._confirm(day, client_id, order_no)
        finally:
            self.confirming = False

    def _confirm(self, day: dt.date, client_id: str, order_no: str | None) -> None:
        t = self._by_client.get(client_id)
        if t is None or t.ack is not None or self.oms.orders[client_id].status is not OrderStatus.NEW:
            raise InvalidTransition(f"{client_id}: 결과를 기다리는 주문이 아니에요")
        known = {b.order_no: b for b in self.broker.daily_orders(day)}
        o = self.oms.orders[client_id]
        if order_no is None:
            alike = [b for n, b in known.items() if n not in self._by_broker_no
                     and (b.symbol, b.side, b.qty) == (o.symbol, o.side, o.qty)]
            if alike:
                raise InvalidTransition(f"{client_id}: 증권사 주문내역에 같은 모양의 주문이 있어요 "
                                        f"({', '.join(b.order_no for b in alike)}). 그 번호로 확인해 주세요")
            self.oms.on_rejected(client_id)
            if self.on_ack:
                self.on_ack(client_id, None, "broker: 증권사 주문내역에 없음 (사람이 확인)")
            return
        b = known.get(order_no)
        if b is None:
            raise InvalidTransition(f"{client_id}: 오늘 증권사 주문내역에 {order_no}이(가) 없어요")
        if order_no in self._by_broker_no:
            raise InvalidTransition(f"{client_id}: {order_no}은(는) 이미 다른 주문 {self._by_broker_no[order_no].client_id}이에요")
        if (b.symbol, b.side, b.qty) != (o.symbol, o.side, o.qty):
            raise InvalidTransition(f"{client_id}: {order_no}은(는) {b.side} {b.symbol} x{b.qty}라 "
                                    f"{o.side} {o.symbol} x{o.qty}와 달라요")
        self._accept(client_id, day, OrderAck(order_no, b.org_no), "사람이 증권사 주문내역에서 확인")

    def executions_of(self, client_id: str) -> list[str]:
        t = self._by_client[client_id]
        if t.ack is None:
            return []
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
        if self._by_client[client_id].ack is None:
            raise InvalidTransition(f"{client_id}: 접수 결과를 아직 몰라서 취소할 수 없어요. 증권사 주문내역에서 먼저 확인해요")
        if o.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            raise InvalidTransition(f"{client_id}: {o.status} 상태라 취소할 수 없어요")
        self.broker.cancel_order(self._by_client[client_id].ack)
        self.oms.request_cancel(client_id)
        if self.on_cancel:
            self.on_cancel(client_id, self._by_client[client_id].ack)

    def sync(self, day: dt.date) -> list[str]:
        """Book fill increases reported by the broker. Returns new execution ids."""
        booked = []
        self._broker_orders = self.broker.daily_orders(day)
        for b in self._broker_orders:
            t = self._by_broker_no.get(b.order_no)
            if t is None and self.unacked and self._claim(day, b):
                t = self._by_broker_no[b.order_no]
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
                late = self.ledger.events[-1]
                if isinstance(late, LateExecutionEvent):   # R-78: kept in the recon snapshot with the run
                    self.anomalies.append(Anomaly(day, "LATE_FILL", b.order_no,
                                                  f"{exec_id} booked on {late.booked_on}: {day} was already closed"))
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
