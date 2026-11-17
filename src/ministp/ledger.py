"""Append-only event ledger: positions, cash, settlements, journal and NAV.

Every public command appends exactly one event and then applies it. State is
never edited directly, so `replay(events)` rebuilds the same ledger (spec R-04,
spec 03 불변 조건 5).
"""
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .accounting import Account, Cr, Dr, JournalEntry, entry
from .calendar import BusinessCalendar, SettlementCycle
from .fees import FeeSchedule
from .instruments import Instrument
from .money import round2, truncate_won

INITIAL_NAV = Decimal("1000.00")


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class ShortSellError(ValueError):
    """R-36: selling more than the trade-date position."""


# ---------- events (immutable, append-only) ----------

@dataclass(frozen=True)
class DepositEvent:
    date: dt.date
    amount: int


@dataclass(frozen=True)
class ExecutionEvent:
    exec_id: str
    trade_date: dt.date
    symbol: str
    side: Side
    qty: int
    price: int


@dataclass(frozen=True)
class SettlementEvent:
    date: dt.date
    exec_id: str


@dataclass(frozen=True)
class RevaluationEvent:
    date: dt.date
    closes: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class NavEvent:
    date: dt.date
    closes: tuple[tuple[str, int], ...]


Event = DepositEvent | ExecutionEvent | SettlementEvent | RevaluationEvent | NavEvent


# ---------- read models ----------

@dataclass(frozen=True)
class Position:
    qty: int = 0
    book_cost: int = 0

    @property
    def avg_price(self) -> Decimal | None:
        """Display only (R-30)."""
        return round2(Decimal(self.book_cost) / self.qty) if self.qty else None


@dataclass(frozen=True)
class ExecutionResult:
    exec_id: str
    gross: int
    commission: int
    tx_tax: int
    rural_tax: int
    net: int
    settle_date: dt.date
    cost_of_sale: int | None
    realized_pnl: int | None


@dataclass
class Settlement:
    exec_id: str
    trade_date: dt.date
    settle_date: dt.date
    side: Side
    net: int
    settled: bool = False

    @property
    def signed(self) -> int:
        """+ = cash to receive, - = cash to pay."""
        return self.net if self.side is Side.SELL else -self.net


@dataclass(frozen=True)
class NavRow:
    date: dt.date
    cash: int
    receivables: int
    payables: int
    market_value: int
    net_assets: int
    units: int
    nav_per_1000: Decimal


@dataclass
class _State:
    positions: dict[str, Position] = field(default_factory=dict)
    settlements: dict[str, Settlement] = field(default_factory=dict)
    journal: list[JournalEntry] = field(default_factory=list)
    results: dict[str, ExecutionResult] = field(default_factory=dict)
    units: int = 0
    last_nav: Decimal | None = None
    last_valuation: JournalEntry | None = None


class Ledger:
    def __init__(self, calendar: BusinessCalendar, fees: FeeSchedule, instruments: dict[str, Instrument],
                 settlement_cycle: SettlementCycle = SettlementCycle.t_plus(2)):
        self.calendar = calendar
        self.settlement_cycle = settlement_cycle
        self.fees = fees
        self.instruments = instruments
        self._events: list[Event] = []
        self._s = _State()

    # ----- commands -----

    def deposit(self, date: dt.date, amount: int) -> None:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self._record(DepositEvent(date, amount))

    def execute(self, exec_id: str, trade_date: dt.date, symbol: str, side: Side,
                qty: int, price: int) -> ExecutionResult:
        if exec_id in self._s.results:
            raise ValueError(f"duplicate execution {exec_id}")
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        if side is Side.SELL and qty > self.position(symbol).qty:
            raise ShortSellError(f"{symbol}: sell {qty} > held {self.position(symbol).qty}")
        self._record(ExecutionEvent(exec_id, trade_date, symbol, side, qty, price))
        return self._s.results[exec_id]

    def settle_due(self, date: dt.date) -> list[str]:
        due = sorted(
            (s for s in self._s.settlements.values() if not s.settled and s.settle_date <= date),
            key=lambda s: (s.settle_date, s.exec_id),
        )
        for s in due:
            self._record(SettlementEvent(date, s.exec_id))
        return [s.exec_id for s in due]

    def revalue(self, date: dt.date, closes: dict[str, int]) -> None:
        """R-55: reverse yesterday's valuation entry, book today's."""
        self._record(RevaluationEvent(date, tuple(sorted(closes.items()))))

    def nav(self, date: dt.date, closes: dict[str, int]) -> NavRow:
        """R-60~63: strike the day's NAV (recorded, used for later unit issuance)."""
        self._record(NavEvent(date, tuple(sorted(closes.items()))))
        return self._nav_row(date, closes)

    # ----- queries -----

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return tuple(self._s.journal)

    def position(self, symbol: str) -> Position:
        return self._s.positions.get(symbol, Position())

    def positions(self) -> dict[str, Position]:
        return {k: v for k, v in self._s.positions.items() if v.qty}

    def account_balance(self, account: Account) -> int:
        dr = cr = 0
        for e in self._s.journal:
            for ln in e.lines:
                if ln.account is account:
                    dr += ln.debit
                    cr += ln.credit
        return dr - cr if account.normal_side == "D" else cr - dr

    def cash_balance(self) -> int:
        return self.account_balance(Account.CASH)

    def pending_settlement_net(self) -> int:
        return sum(s.signed for s in self._s.settlements.values() if not s.settled)

    def projected_cash(self) -> int:
        """Cash after every booked trade settles (증권사 D+2 예수금, D+1 after a T+1 cutover)."""
        return self.cash_balance() + self.pending_settlement_net()

    @property
    def units(self) -> int:
        return self._s.units

    @classmethod
    def replay(cls, events, calendar, fees, instruments,
               settlement_cycle: SettlementCycle = SettlementCycle.t_plus(2)) -> "Ledger":
        led = cls(calendar, fees, instruments, settlement_cycle)
        for ev in events:
            led._record(ev)
        return led

    # ----- event application -----

    def _record(self, ev: Event) -> None:
        self._apply(ev)
        self._events.append(ev)

    def _apply(self, ev: Event) -> None:
        match ev:
            case DepositEvent():
                self._apply_deposit(ev)
            case ExecutionEvent():
                self._apply_execution(ev)
            case SettlementEvent():
                self._apply_settlement(ev)
            case RevaluationEvent():
                self._apply_revaluation(ev)
            case NavEvent():
                self._s.last_nav = self._nav_row(ev.date, dict(ev.closes)).nav_per_1000

    def _apply_deposit(self, ev: DepositEvent) -> None:
        nav = self._s.last_nav or INITIAL_NAV
        self._s.units += truncate_won(Decimal(ev.amount) / nav * 1000)  # R-61
        self._s.journal.append(entry(ev.date, "DEPOSIT", "",
                                     Dr(Account.CASH, ev.amount),
                                     Cr(Account.CONTRIBUTED_CAPITAL, ev.amount)))

    def _apply_execution(self, ev: ExecutionEvent) -> None:
        inst = self.instruments[ev.symbol]
        gross = ev.qty * ev.price
        is_sell = ev.side is Side.SELL
        c = self.fees.costs(inst, is_sell, gross)
        settle_date = self.calendar.settlement_date(ev.trade_date, self.settlement_cycle)
        pos = self.position(ev.symbol)

        if not is_sell:
            net = gross + c.commission  # R-14
            self._s.positions[ev.symbol] = Position(pos.qty + ev.qty, pos.book_cost + gross)  # R-31
            cost_of_sale = pnl = None
            lines = [Dr(Account.FVPL_STOCK, gross), Dr(Account.COMMISSION, c.commission),
                     Cr(Account.BUY_PAYABLE, net)]  # R-51
        else:
            net = gross - c.commission - c.taxes  # R-14
            if ev.qty == pos.qty:
                cost_of_sale = pos.book_cost  # R-33
            else:
                cost_of_sale = truncate_won(Decimal(pos.book_cost) * ev.qty / pos.qty)  # R-32
            pnl = gross - cost_of_sale  # R-34
            self._s.positions[ev.symbol] = Position(pos.qty - ev.qty, pos.book_cost - cost_of_sale)
            lines = [Dr(Account.SELL_RECEIVABLE, net), Dr(Account.COMMISSION, c.commission),
                     Dr(Account.TAXES, c.taxes), Cr(Account.FVPL_STOCK, cost_of_sale),
                     Cr(Account.TRADING_GAIN, max(pnl, 0)), Dr(Account.TRADING_LOSS, max(-pnl, 0))]  # R-53

        self._s.journal.append(entry(ev.trade_date, "EXECUTION", ev.exec_id, *lines))
        self._s.settlements[ev.exec_id] = Settlement(ev.exec_id, ev.trade_date, settle_date, ev.side, net)
        self._s.results[ev.exec_id] = ExecutionResult(
            ev.exec_id, gross, c.commission, c.tx_tax, c.rural_tax, net, settle_date, cost_of_sale, pnl)

    def _apply_settlement(self, ev: SettlementEvent) -> None:
        s = self._s.settlements[ev.exec_id]
        if s.settled:
            raise ValueError(f"{ev.exec_id} already settled")
        s.settled = True
        if s.side is Side.BUY:
            lines = (Dr(Account.BUY_PAYABLE, s.net), Cr(Account.CASH, s.net))  # R-52
        else:
            lines = (Dr(Account.CASH, s.net), Cr(Account.SELL_RECEIVABLE, s.net))  # R-54
        self._s.journal.append(entry(ev.date, "SETTLEMENT", ev.exec_id, *lines))

    def _apply_revaluation(self, ev: RevaluationEvent) -> None:
        closes = dict(ev.closes)
        prev = self._s.last_valuation
        if prev is not None:
            swapped = tuple(Dr(ln.account, ln.credit) if ln.credit else Cr(ln.account, ln.debit)
                            for ln in prev.lines)
            self._s.journal.append(entry(ev.date, "VALUATION_REVERSAL", prev.source_id, *swapped,
                                         reverses=prev))
            self._s.last_valuation = None
        held = self.positions()
        diff = sum(p.qty * closes[sym] - p.book_cost for sym, p in held.items())
        if diff:
            if diff > 0:
                lines = (Dr(Account.FVPL_VALUATION, diff), Cr(Account.VALUATION_GAIN, diff))
            else:
                lines = (Dr(Account.VALUATION_LOSS, -diff), Cr(Account.FVPL_VALUATION, -diff))
            e = entry(ev.date, "VALUATION", ev.date.isoformat(), *lines)
            self._s.journal.append(e)
            self._s.last_valuation = e

    def _nav_row(self, date: dt.date, closes: dict[str, int]) -> NavRow:
        cash = self.cash_balance()
        recv = self.account_balance(Account.SELL_RECEIVABLE) + self.account_balance(Account.DIVIDEND_RECEIVABLE)
        pay = self.account_balance(Account.BUY_PAYABLE)
        mv = sum(p.qty * closes[sym] for sym, p in self.positions().items())
        net = cash + recv - pay + mv  # R-60
        units = self._s.units
        per1000 = round2(Decimal(net) / units * 1000) if units else INITIAL_NAV  # R-62
        return NavRow(date, cash, recv, pay, mv, net, units, per1000)
