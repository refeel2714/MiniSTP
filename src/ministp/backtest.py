"""Event-driven daily backtest: Strategy -> risk -> OMS -> Ledger.

Timeline for each business day D (no look-ahead):
  1. settle trades due on D
  2. fill orders submitted on the previous day at D's open (+ slippage, tick-rounded)
  3. strategy sees D's bars (close known) and may submit orders for D+1's open
  4. end of day: revalue at close, strike NAV
"""
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .ledger import Ledger, NavRow, Side
from .oms import OrderManager, OrderStatus
from .risk import RiskLimits, check_order
from .strategy import Bar, Strategy
from .ticks import round_to_tick


@dataclass(frozen=True)
class SlippageModel:
    bps: Decimal = Decimal("10")  # 0.10%

    def fill_price(self, open_price: int, side: Side, asset_type) -> int:
        k = self.bps / Decimal(10_000)
        raw = Decimal(open_price) * (1 + k if side is Side.BUY else 1 - k)
        return round_to_tick(raw, asset_type, up=side is Side.BUY)


@dataclass(frozen=True)
class Rejection:
    date: dt.date
    order_id: str
    symbol: str
    side: Side
    qty: int
    reason: str


@dataclass
class BacktestResult:
    navs: list[NavRow] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    fills: list[tuple[dt.date, str, str, Side, int, int]] = field(default_factory=list)

    @property
    def total_return(self) -> Decimal:
        if not self.navs:
            return Decimal(0)
        return self.navs[-1].nav_per_1000 / Decimal(1000) - 1

    @property
    def max_drawdown(self) -> Decimal:
        peak, mdd = None, Decimal(0)
        for n in self.navs:
            peak = n.nav_per_1000 if peak is None else max(peak, n.nav_per_1000)
            mdd = min(mdd, n.nav_per_1000 / peak - 1)
        return mdd


class _Context:
    def __init__(self, engine: "BacktestEngine", today: dt.date):
        self._e = engine
        self.today = today

    def position_qty(self, symbol: str) -> int:
        return self._e.ledger.position(symbol).qty

    def buying_power(self) -> int:
        return self._e.ledger.projected_cash()

    def closes(self, symbol: str, n: int) -> list[int]:
        return self._e._history[symbol][-n:]

    def submit(self, symbol: str, side: Side, qty: int) -> str | None:
        return self._e._submit(self.today, symbol, side, qty)


class BacktestEngine:
    def __init__(self, ledger: Ledger, strategy: Strategy, *, limits: RiskLimits = RiskLimits(),
                 slippage: SlippageModel = SlippageModel(), oms: OrderManager | None = None):
        self.ledger = ledger
        self.strategy = strategy
        self.limits = limits
        self.slippage = slippage
        self.oms = oms or OrderManager()
        self._pending: list[str] = []  # order ids waiting for next open
        self._history: dict[str, list[int]] = defaultdict(list)
        self._last_close: dict[str, int] = {}
        self._seq = 0
        self.result = BacktestResult()

    # ----- order intake -----

    def _next_id(self) -> str:
        self._seq += 1
        return f"BT{self._seq:06d}"

    def _pending_totals(self, symbol: str, exclude: str | None) -> tuple[int, int]:
        sell_qty = buy_value = 0
        for oid in self._pending:
            if oid == exclude:
                continue
            o = self.oms.orders[oid]
            if o.side == Side.SELL and o.symbol == symbol:
                sell_qty += o.qty
            elif o.side == Side.BUY:
                buy_value += o.qty * self._last_close[o.symbol]
        return sell_qty, buy_value

    def _risk(self, symbol: str, side: Side, qty: int, ref_price: int, exclude: str | None = None):
        sell_qty, buy_value = self._pending_totals(symbol, exclude)
        return check_order(self.limits, side=side, qty=qty, ref_price=ref_price,
                           held_qty=self.ledger.position(symbol).qty, pending_sell_qty=sell_qty,
                           buying_power=self.ledger.projected_cash(), pending_buy_value=buy_value,
                           halted=self.oms.halted)

    def _submit(self, today: dt.date, symbol: str, side: Side, qty: int) -> str | None:
        oid = self._next_id()
        if self.oms.halted:
            self.result.rejections.append(Rejection(today, oid, symbol, side, qty, "KILL_SWITCH"))
            return None
        decision = self._risk(symbol, side, qty, self._last_close[symbol])
        self.oms.create(oid, symbol=symbol, side=side, qty=qty, limit_price=0)
        if not decision.ok:
            self.oms.on_rejected(oid)
            self.result.rejections.append(Rejection(today, oid, symbol, side, qty, decision.reason))
            return None
        self.oms.on_accepted(oid)
        self._pending.append(oid)
        return oid

    # ----- day loop -----

    def _fill_pending(self, day: dt.date, bars: dict[str, Bar]) -> None:
        still = []
        for oid in self._pending:
            o = self.oms.orders[oid]
            bar = bars.get(o.symbol)
            if bar is None:  # no trading in this symbol today: keep waiting
                still.append(oid)
                continue
            inst = self.ledger.instruments[o.symbol]
            price = self.slippage.fill_price(bar.open, o.side, inst.asset_type)
            recheck = self._risk(o.symbol, o.side, o.qty, price, exclude=oid)  # gap re-check
            if not recheck.ok:
                self.oms.on_rejected(oid)
                self.result.rejections.append(Rejection(day, oid, o.symbol, o.side, o.qty,
                                                        recheck.reason))
                continue
            self.oms.on_fill(oid, o.qty)
            self.ledger.execute(f"{oid}-1", day, o.symbol, o.side, o.qty, price)
            self.result.fills.append((day, oid, o.symbol, o.side, o.qty, price))
        self._pending = still

    def run(self, bars: list[Bar]) -> BacktestResult:
        by_day: dict[dt.date, dict[str, Bar]] = defaultdict(dict)
        for b in bars:
            by_day[b.date][b.symbol] = b
        for day in sorted(by_day):
            if not self.ledger.calendar.is_business_day(day):
                raise ValueError(f"bar on non-business day {day}")
            today = by_day[day]
            self.ledger.settle_due(day)
            self._fill_pending(day, today)
            for sym, b in today.items():
                self._history[sym].append(b.close)
                self._last_close[sym] = b.close
            self.strategy.on_bar(_Context(self, day), today)
            closes = {s: self._last_close[s] for s in self.ledger.positions()}
            self.ledger.revalue(day, closes)
            self.result.navs.append(self.ledger.nav(day, closes))
        return self.result
