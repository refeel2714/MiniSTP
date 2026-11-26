"""Append-only event ledger: positions, cash, settlements, journal and NAV.

Every public command appends exactly one event and then applies it. State is
never edited directly, so `replay(events)` rebuilds the same ledger (spec R-04,
spec 03 불변 조건 5).
"""
import datetime as dt
from dataclasses import dataclass, field, replace
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


class ClosedDayError(ValueError):
    """R-78: the date is inside a closed day. Nothing more is booked there; what turns up
    late is booked on the first open day as a correction."""


class CannotCloseError(ValueError):
    """R-77: the day is not ready to be closed. `reason` is one of CLOSE_BLOCKERS."""

    def __init__(self, date: dt.date, reason: str):
        super().__init__(f"{date} cannot be closed: {reason}")
        self.date, self.reason = date, reason


# Why a day cannot be closed yet (R-77), in the order they are checked.
CLOSE_BLOCKERS = ("ALREADY_CLOSED",   # this date or a later one is closed already
                  "LATER_ENTRIES",    # a later day already has entries: the next close covers this one
                  "NAV_NOT_LAST",     # the day has no NAV, or something was booked after it
                  "UNSETTLED")        # a settlement due by this date has not been booked


class LedgerHaltedError(RuntimeError):
    """A listener (e.g. the DB store) failed; this in-memory ledger may no longer
    match the durable record and must be rebuilt from it."""


# ---------- events (immutable, append-only) ----------

@dataclass(frozen=True)
class DepositEvent:
    date: dt.date
    amount: int


@dataclass(frozen=True)
class OpeningPositionEvent:
    """A holding that existed before the ledger started (spec R-05).

    Booked at the broker's purchase amount, funded by contributed capital, so
    NAV starts at 1,000 on cash + book cost."""
    date: dt.date
    symbol: str
    qty: int
    book_cost: int


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


@dataclass(frozen=True)
class LateExecutionEvent(ExecutionEvent):
    """A fill of a day whose books were already closed: the correction event of spec D6 (R-78).

    `trade_date` stays the true trade date, so the position row, the settlement date and the
    day's filled quantity are what the broker says. The journal entry is dated `booked_on`,
    the first day still open, because a closed day takes no more entries."""
    booked_on: dt.date


@dataclass(frozen=True)
class DayClosedEvent:
    """R-77: the books are closed through `date`. What the ledger held at this event is the
    ABOR of that day, and no later event may be booked on or before it."""
    date: dt.date


Event = (DepositEvent | OpeningPositionEvent | ExecutionEvent | SettlementEvent | RevaluationEvent | NavEvent
         | LateExecutionEvent | DayClosedEvent)


def book_date(ev) -> dt.date:
    """The accounting date an event is booked on: the date a day close locks."""
    if isinstance(ev, LateExecutionEvent):
        return ev.booked_on
    if isinstance(ev, ExecutionEvent):
        return ev.trade_date
    return ev.date


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
    last_nav_row: NavRow | None = None
    closed_through: dt.date | None = None                           # R-77: the last closed date
    closed_days: dict[dt.date, NavRow] = field(default_factory=dict)  # closed date -> the NAV it was closed at
    closed_at: int = 0                                              # events up to and including the last close


class Ledger:
    def __init__(self, calendar: BusinessCalendar, fees: FeeSchedule, instruments: dict[str, Instrument],
                 settlement_cycle: SettlementCycle = SettlementCycle.t_plus(2)):
        self.calendar = calendar
        self.settlement_cycle = settlement_cycle
        self.fees = fees
        self.instruments = instruments
        self._events: list[Event] = []
        self._s = _State()
        self._listeners = []
        self._halted: str | None = None

    def subscribe(self, listener) -> None:
        """listener(ledger, seq, event, new_journal_entries) runs after each event is applied.
        If it raises, the ledger halts (fail closed)."""
        self._listeners.append(listener)

    # ----- commands -----

    def deposit(self, date: dt.date, amount: int) -> None:
        if amount <= 0:
            raise ValueError("deposit must be positive")
        self._record(DepositEvent(date, amount))

    def open_position(self, date: dt.date, symbol: str, qty: int, book_cost: int) -> None:
        """Only before any trade: the ledger's starting holdings (R-05)."""
        if any(isinstance(e, (ExecutionEvent, NavEvent)) for e in self._events):
            raise ValueError("opening positions must come before any execution or NAV")
        if symbol not in self.instruments:
            raise ValueError(f"unknown instrument {symbol}")
        if self.position(symbol).qty:
            raise ValueError(f"{symbol} already has an opening position")
        if qty <= 0 or book_cost <= 0:
            raise ValueError("qty and book_cost must be positive")
        self._record(OpeningPositionEvent(date, symbol, qty, book_cost))

    def execute(self, exec_id: str, trade_date: dt.date, symbol: str, side: Side,
                qty: int, price: int) -> ExecutionResult:
        if exec_id in self._s.results:
            raise ValueError(f"duplicate execution {exec_id}")
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        if side is Side.SELL and qty > self.position(symbol).qty:
            raise ShortSellError(f"{symbol}: sell {qty} > held {self.position(symbol).qty}")
        if self.is_closed(trade_date):   # R-78: the day is closed, so this is a correction on the open day
            self._record(LateExecutionEvent(exec_id, trade_date, symbol, side, qty, price, self.open_day()))
        else:
            self._record(ExecutionEvent(exec_id, trade_date, symbol, side, qty, price))
        return self._s.results[exec_id]

    def next_settlement_date(self, up_to: dt.date) -> dt.date | None:
        """Earliest unsettled settlement date on or before `up_to`."""
        due = [s.settle_date for s in self._s.settlements.values()
               if not s.settled and s.settle_date <= up_to]
        return min(due) if due else None

    def settle_due(self, date: dt.date) -> list[str]:
        due = sorted(
            (s for s in self._s.settlements.values() if not s.settled and s.settle_date <= date),
            key=lambda s: (s.settle_date, s.exec_id),
        )
        when = self.open_day() if self.is_closed(date) else date   # R-78: a late fill can be due inside closed days
        for s in due:
            self._record(SettlementEvent(when, s.exec_id))
        return [s.exec_id for s in due]

    def revalue(self, date: dt.date, closes: dict[str, int]) -> None:
        """R-55: reverse yesterday's valuation entry, book today's."""
        self._record(RevaluationEvent(date, tuple(sorted(closes.items()))))

    def nav(self, date: dt.date, closes: dict[str, int]) -> NavRow:
        """R-60~63: strike the day's NAV (recorded, used for later unit issuance)."""
        self._record(NavEvent(date, tuple(sorted(closes.items()))))
        return self._nav_row(date, closes)

    def close_blocker(self, date: dt.date) -> str | None:
        """Why `date` cannot be closed now (one of CLOSE_BLOCKERS), or None when it can."""
        if self.is_closed(date):
            return "ALREADY_CLOSED"
        if any(book_date(e) > date for e in self._events[self._s.closed_at:]):
            return "LATER_ENTRIES"
        last = self._events[-1] if self._events else None
        if not (isinstance(last, NavEvent) and last.date == date):
            return "NAV_NOT_LAST"
        if self.next_settlement_date(date) is not None:
            return "UNSETTLED"
        return None

    def close_day(self, date: dt.date) -> NavRow:
        """R-77: close the books through `date`, right after that day's NAV. Returns the NAV it closed at.

        A day is closed only as the last word on it: its NAV is the latest event, nothing of a
        later day has been booked and nothing due is unsettled. So the events up to the close
        are exactly the books of that day (ABOR), and `abor()` can rebuild them at any time."""
        reason = self.close_blocker(date)
        if reason:
            raise CannotCloseError(date, reason)
        self._record(DayClosedEvent(date))
        return self._s.closed_days[date]

    # ----- queries -----

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def closed_through(self) -> dt.date | None:
        """The last closed date (R-77), or None while nothing is closed."""
        return self._s.closed_through

    def is_closed(self, date: dt.date) -> bool:
        return self._s.closed_through is not None and date <= self._s.closed_through

    def closed_days(self) -> dict[dt.date, NavRow]:
        """Every closed date with the NAV it was closed at, oldest first."""
        return dict(self._s.closed_days)

    def unclosed_events(self) -> tuple[Event, ...]:
        """The events after the last close: all that IBOR holds and ABOR does not (D6)."""
        return tuple(self._events[self._s.closed_at:])

    def open_day(self) -> dt.date | None:
        """The date a correction is booked on (R-78): the business day after the last close,
        or the latest day that already has entries when the books have moved on since."""
        if self._s.closed_through is None:
            return None
        first = self.calendar.add_business_days(self._s.closed_through, 1)
        return max([first, *(book_date(e) for e in self._events[self._s.closed_at:])])

    def abor(self, date: dt.date | None = None) -> "Ledger | None":
        """The books as the close of `date` left them (default: the last close), or None when
        that day was never closed. A replay of the events up to that close, nothing else."""
        date = date or self._s.closed_through
        for n, ev in enumerate(self._events, 1):
            if isinstance(ev, DayClosedEvent) and ev.date == date:
                return Ledger.replay(self._events[:n], self.calendar, self.fees, self.instruments,
                                     self.settlement_cycle)
        return None

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

    def pending_settlements(self) -> list[Settlement]:
        """Booked trades not settled yet, by settlement date (copies: changing them changes nothing)."""
        return sorted((replace(s) for s in self._s.settlements.values() if not s.settled),
                      key=lambda s: (s.settle_date, s.exec_id))

    def projected_cash(self) -> int:
        """Cash after every booked trade settles (증권사 D+2 예수금, D+1 after a T+1 cutover)."""
        return self.cash_balance() + self.pending_settlement_net()

    def nav_steps(self) -> list[tuple[NavRow, tuple[JournalEntry, ...]]]:
        """Every recorded NAV with the journal entries booked after the NAV before it, in recording
        order: what the books say moved net assets from one NAV to the next (STP-2210)."""
        led = Ledger(self.calendar, self.fees, self.instruments, self.settlement_cycle)
        steps, since = [], 0
        for ev in self._events:
            led._record(ev)
            if isinstance(ev, NavEvent):
                journal = led.journal
                steps.append((led._nav_row(ev.date, dict(ev.closes)), journal[since:]))
                since = len(journal)
        return steps

    @property
    def units(self) -> int:
        return self._s.units

    def nav_history(self) -> list[NavRow]:
        """Every recorded NAV as it stood on its own day (replays the events in order)."""
        led = Ledger(self.calendar, self.fees, self.instruments, self.settlement_cycle)
        rows: dict[dt.date, NavRow] = {}
        for ev in self._events:
            led._record(ev)
            if isinstance(ev, NavEvent):   # struck again before the close (R-75): the last one is the day's NAV
                rows[ev.date] = led._nav_row(ev.date, dict(ev.closes))
        return list(rows.values())

    @classmethod
    def replay(cls, events, calendar, fees, instruments,
               settlement_cycle: SettlementCycle = SettlementCycle.t_plus(2)) -> "Ledger":
        led = cls(calendar, fees, instruments, settlement_cycle)
        for ev in events:
            led._record(ev)
        return led

    # ----- event application -----

    def _record(self, ev: Event) -> None:
        if self._halted:
            raise LedgerHaltedError(self._halted)
        if self.is_closed(book_date(ev)):   # R-78, also on replay: a stream that books into a closed day is refused
            raise ClosedDayError(f"{type(ev).__name__} dated {book_date(ev)}: the books are closed "
                                 f"through {self._s.closed_through}")
        n = len(self._s.journal)
        self._apply(ev)
        self._events.append(ev)
        new_entries = tuple(self._s.journal[n:])
        for listener in self._listeners:
            try:
                listener(self, len(self._events), ev, new_entries)
            except Exception as exc:
                self._halted = f"listener failed at seq {len(self._events)}: {exc!r}"
                raise LedgerHaltedError(self._halted) from exc

    def execution_result(self, exec_id: str) -> ExecutionResult:
        return self._s.results[exec_id]

    def _apply(self, ev: Event) -> None:
        match ev:
            case DepositEvent():
                self._apply_deposit(ev)
            case OpeningPositionEvent():
                self._apply_opening(ev)
            case ExecutionEvent():
                self._apply_execution(ev)
            case SettlementEvent():
                self._apply_settlement(ev)
            case RevaluationEvent():
                self._apply_revaluation(ev)
            case NavEvent():
                self._s.last_nav_row = self._nav_row(ev.date, dict(ev.closes))
                self._s.last_nav = self._s.last_nav_row.nav_per_1000
            case DayClosedEvent():
                self._s.closed_through = ev.date
                self._s.closed_days[ev.date] = self._s.last_nav_row
                self._s.closed_at = len(self._events) + 1   # this event is appended right after it is applied

    def _apply_deposit(self, ev: DepositEvent) -> None:
        nav = self._s.last_nav or INITIAL_NAV
        self._s.units += truncate_won(Decimal(ev.amount) / nav * 1000)  # R-61
        self._s.journal.append(entry(ev.date, "DEPOSIT", "",
                                     Dr(Account.CASH, ev.amount),
                                     Cr(Account.CONTRIBUTED_CAPITAL, ev.amount)))

    def _apply_opening(self, ev: OpeningPositionEvent) -> None:
        self._s.positions[ev.symbol] = Position(ev.qty, ev.book_cost)
        nav = self._s.last_nav or INITIAL_NAV
        self._s.units += truncate_won(Decimal(ev.book_cost) / nav * 1000)
        self._s.journal.append(entry(ev.date, "OPENING", ev.symbol,
                                     Dr(Account.FVPL_STOCK, ev.book_cost),
                                     Cr(Account.CONTRIBUTED_CAPITAL, ev.book_cost)))

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

        late = isinstance(ev, LateExecutionEvent)   # R-78: the entry goes on the open day, marked as a correction
        self._s.journal.append(entry(book_date(ev), "LATE_EXECUTION" if late else "EXECUTION", ev.exec_id, *lines))
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
