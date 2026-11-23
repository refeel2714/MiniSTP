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
from .fees import FeeSchedule, dividend_withholding
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


class DividendError(ValueError):
    """R-57: a dividend step that does not follow from the ones before it (accrued twice, paid twice, ...)."""


class CashError(ValueError):
    """R-56, R-64: a cash movement the books cannot take: more than can leave the account,
    or a date behind entries that are already booked."""


class AdjustmentError(ValueError):
    """R-66: an adjustment the rules do not allow (an unknown reason, no memo, cancelled twice, ...)."""


class LedgerHaltedError(RuntimeError):
    """A listener (e.g. the DB store) failed; this in-memory ledger may no longer
    match the durable record and must be rebuilt from it."""


# ---------- events (immutable, append-only) ----------

@dataclass(frozen=True)
class DepositEvent:
    date: dt.date
    amount: int


@dataclass(frozen=True)
class WithdrawalEvent:
    """R-56: cash taken out of the account. Units are redeemed at the NAV of the day before (R-61)."""
    date: dt.date
    amount: int


@dataclass(frozen=True)
class TransferOutEvent:
    """R-65: cash sent to another account of this database. The other half is the TransferInEvent
    with the same `transfer_id` in the books of `to_account`; each side books it as its own
    withdrawal or deposit."""
    date: dt.date
    transfer_id: int
    to_account: str
    amount: int


@dataclass(frozen=True)
class TransferInEvent:
    """R-65: cash received from another account of this database (see TransferOutEvent)."""
    date: dt.date
    transfer_id: int
    from_account: str
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


@dataclass(frozen=True)
class DividendAccrualEvent:
    """R-57: a cash dividend the account is entitled to, recognised before the cash arrives.

    `action_id` is the row of `corporate_actions` it comes from. The event carries its own
    numbers, so a replay needs nothing but the events: `qty` is the holding that had settled
    by the record date, `gross` is that quantity times the amount per share."""
    date: dt.date
    action_id: int
    symbol: str
    qty: int
    gross: int


@dataclass(frozen=True)
class DividendPaymentEvent:
    """R-57, R-23: the dividend arrives. `gross` is what was paid before tax and `tax` what was
    withheld, so the cash is gross - tax. A gross that differs from the accrual (or a payment
    that was never accrued) moves 배당수익 by the difference."""
    date: dt.date
    action_id: int
    symbol: str
    gross: int
    tax: int


@dataclass(frozen=True)
class DividendCancelEvent:
    """An accrual taken back because its corporate action was cancelled before payment."""
    date: dt.date
    action_id: int
    symbol: str


@dataclass(frozen=True)
class AdjustmentEvent:
    """R-66: cash that moved at the broker for a reason the ledger cannot work out by itself
    (예탁금 이용료), or a difference left between what the broker booked and what the ledger
    computed (a fee, a tax, a dividend). The reason fixes the account on the other side.

    `amount` is the change of 예수금 before tax: + in, - out. `tax` is what was withheld from
    an amount coming in. `recon_result_id` names the reconciliation break it settles, if any."""
    date: dt.date
    adjustment_id: int
    reason: str
    amount: int
    tax: int
    memo: str
    recon_result_id: int | None = None


@dataclass(frozen=True)
class AdjustmentCancelEvent:
    """R-66: an adjustment taken back by the opposite entry (R-04). The adjustment itself stays."""
    date: dt.date
    adjustment_id: int
    memo: str


Event = (DepositEvent | OpeningPositionEvent | ExecutionEvent | SettlementEvent | RevaluationEvent | NavEvent
         | LateExecutionEvent | DayClosedEvent | DividendAccrualEvent | DividendPaymentEvent | DividendCancelEvent
         | WithdrawalEvent | TransferOutEvent | TransferInEvent | AdjustmentEvent | AdjustmentCancelEvent)


def adjustment_ref(adjustment_id: int) -> str:
    """The `source_id` of an adjustment's journal entries."""
    return f"ADJ-{adjustment_id}"


def transfer_ref(transfer_id: int) -> str:
    """The `source_id` of both entries of a transfer between accounts."""
    return f"TR-{transfer_id}"


def dividend_ref(action_id: int) -> str:
    """The `source_id` of a dividend's journal entries: the corporate action they come from."""
    return f"CA-{action_id}"


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


@dataclass(frozen=True)
class CashMovement:
    """Cash the owner put in or took out, and the units it issued or redeemed (R-56, R-61)."""
    date: dt.date
    kind: str                          # DEPOSIT, WITHDRAWAL, TRANSFER_IN or TRANSFER_OUT (the journal kind)
    amount: int                        # + in, - out
    units: int                         # + issued, - redeemed
    nav_per_1000: Decimal              # the NAV the units were priced at
    transfer_id: int | None = None
    counter_account: str | None = None


# R-66: why cash is adjusted, and the account on the other side: (when cash comes in, when it goes out).
# None: that direction does not exist for the reason. Only INTEREST may carry a withheld tax.
ADJUSTMENT_ACCOUNTS: dict[str, tuple[Account | None, Account | None]] = {
    "INTEREST": (Account.INTEREST_INCOME, None),                    # 예탁금 이용료
    "FEE": (Account.COMMISSION, Account.COMMISSION),                # the broker charged another commission
    "TAX": (Account.TAXES, Account.TAXES),                          # another transaction tax
    "DIVIDEND": (Account.DIVIDEND_INCOME, Account.DIVIDEND_INCOME),  # a dividend paid differs from what was booked
    "WITHHOLDING": (Account.WITHHOLDING_TAX, Account.WITHHOLDING_TAX),  # another tax withheld from it
    "OTHER": (Account.MISC_INCOME, Account.MISC_LOSS),
}


@dataclass(frozen=True)
class Adjustment:
    """One adjustment as this account's books know it."""
    adjustment_id: int
    date: dt.date
    reason: str
    amount: int
    tax: int
    memo: str
    recon_result_id: int | None = None
    canceled_on: dt.date | None = None
    cancel_memo: str | None = None

    @property
    def cash(self) -> int:
        """What it did to 예수금."""
        return self.amount - self.tax

    @property
    def ref(self) -> str:
        return adjustment_ref(self.adjustment_id)


DIVIDEND_STATUSES = ("ACCRUED", "PAID", "CANCELED")


@dataclass(frozen=True)
class Dividend:
    """One corporate action as this account's books know it."""
    action_id: int
    symbol: str
    qty: int = 0                       # shares entitled (0: paid without an accrual)
    accrued: int = 0                   # gross recognised as 미수배당금
    accrued_on: dt.date | None = None
    paid_gross: int | None = None
    tax: int | None = None
    paid_on: dt.date | None = None
    canceled_on: dt.date | None = None

    @property
    def status(self) -> str:
        return "CANCELED" if self.canceled_on else "PAID" if self.paid_on else "ACCRUED"

    @property
    def receivable(self) -> int:
        """What is still in 미수배당금 for this action."""
        return 0 if self.paid_on or self.canceled_on else self.accrued


@dataclass
class _State:
    positions: dict[str, Position] = field(default_factory=dict)
    settlements: dict[str, Settlement] = field(default_factory=dict)
    journal: list[JournalEntry] = field(default_factory=list)
    results: dict[str, ExecutionResult] = field(default_factory=dict)
    units: int = 0
    last_valuation: JournalEntry | None = None
    last_nav_row: NavRow | None = None
    closed_through: dt.date | None = None                           # R-77: the last closed date
    closed_days: dict[dt.date, NavRow] = field(default_factory=dict)  # closed date -> the NAV it was closed at
    closed_at: int = 0                                              # events up to and including the last close
    dividends: dict[int, Dividend] = field(default_factory=dict)    # corporate action id -> its state here (R-57)
    navs: list[tuple[dt.date, Decimal]] = field(default_factory=list)  # every NAV struck, in order (R-61)
    movements: list[CashMovement] = field(default_factory=list)     # deposits, withdrawals, transfers (R-56)
    adjustments: dict[int, Adjustment] = field(default_factory=dict)   # adjustment id -> its state here (R-66)
    adjustment_entries: dict[int, JournalEntry] = field(default_factory=dict)   # the entry a cancel reverses


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
        """R-56: cash put into the account (차 예수금 / 대 투자원금). Units are issued for it (R-61)."""
        self.check_cash(date, amount)
        self._record(DepositEvent(date, amount))

    def withdraw(self, date: dt.date, amount: int) -> None:
        """R-56: cash taken out (차 투자원금 / 대 예수금), no more than can leave the account (R-64)."""
        self.check_cash(date, amount, out=True)
        self._record(WithdrawalEvent(date, amount))

    def transfer_out(self, date: dt.date, transfer_id: int, to_account: str, amount: int) -> None:
        """R-65: this account's half of a transfer to `to_account`. A withdrawal by every rule."""
        self.check_cash(date, amount, out=True)
        self._record(TransferOutEvent(date, transfer_id, to_account, amount))

    def transfer_in(self, date: dt.date, transfer_id: int, from_account: str, amount: int) -> None:
        """R-65: this account's half of a transfer from `from_account`. A deposit by every rule."""
        self.check_cash(date, amount)
        self._record(TransferInEvent(date, transfer_id, from_account, amount))

    def check_cash(self, date: dt.date, amount: int, out: bool = False) -> None:
        """Raise unless cash of `amount` can be booked on `date` (R-56), and can leave when `out` (R-64)."""
        if amount <= 0:
            raise ValueError("the amount must be positive")
        if self.is_closed(date):
            raise ClosedDayError(f"cash dated {date}: the books are closed through {self._s.closed_through}")
        last = self.last_entry_date() or date
        if date < last:   # the NAVs struck since then did not know about it
            raise CashError(f"{date} is behind the books: the last entry is dated {last}")
        if out and amount > self.withdrawable():
            raise CashError(f"{amount:,} is more than can leave the account ({self.withdrawable():,})")

    def adjust(self, date: dt.date, adjustment_id: int, reason: str, amount: int, tax: int = 0, memo: str = "",
               recon_result_id: int | None = None) -> Adjustment:
        """R-66: book cash the ledger could not have computed. The reason decides the other account."""
        if adjustment_id in self._s.adjustments:
            raise AdjustmentError(f"adjustment {adjustment_id} is already booked")
        if reason not in ADJUSTMENT_ACCOUNTS:
            raise AdjustmentError(f"unknown adjustment reason {reason}")
        if not memo.strip():
            raise AdjustmentError("an adjustment needs a memo that says why")
        if amount == 0 or ADJUSTMENT_ACCOUNTS[reason][amount < 0] is None:
            raise AdjustmentError(f"{reason} cannot be adjusted by {amount:,}")
        if tax and (reason != "INTEREST" or not 0 < tax < amount):
            raise AdjustmentError("a tax is withheld only from interest, and is smaller than it")
        self.check_cash(date, abs(amount - tax), out=amount < 0)
        self._record(AdjustmentEvent(date, adjustment_id, reason, amount, tax, memo.strip(), recon_result_id))
        return self._s.adjustments[adjustment_id]

    def cancel_adjustment(self, date: dt.date, adjustment_id: int, memo: str) -> Adjustment:
        """R-66: take an adjustment back by the opposite entry. Once, and with a reason."""
        a = self._s.adjustments.get(adjustment_id)
        if a is None or a.canceled_on:
            raise AdjustmentError(f"adjustment {adjustment_id} is not there to cancel" if a is None
                                  else f"adjustment {adjustment_id} is already cancelled")
        if not memo.strip():
            raise AdjustmentError("a cancellation needs a memo that says why")
        self.check_cash(date, abs(a.cash), out=a.cash > 0)
        self._record(AdjustmentCancelEvent(date, adjustment_id, memo.strip()))
        return self._s.adjustments[adjustment_id]

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

    def accrue_dividend(self, date: dt.date, action_id: int, symbol: str, qty: int, gross: int) -> None:
        """R-57: recognise a dividend this account is entitled to (차 미수배당금 / 대 배당수익)."""
        if action_id in self._s.dividends:
            raise DividendError(f"corporate action {action_id} is already booked ({self._s.dividends[action_id].status})")
        if symbol not in self.instruments:
            raise ValueError(f"unknown instrument {symbol}")
        if qty <= 0 or gross <= 0:
            raise ValueError("qty and gross must be positive")
        self._record(DividendAccrualEvent(date, action_id, symbol, qty, gross))

    def pay_dividend(self, date: dt.date, action_id: int, symbol: str | None = None, gross: int | None = None,
                     tax: int | None = None) -> Dividend:
        """R-57: the dividend is paid (차 예수금, 법인세등 / 대 미수배당금). Without numbers it is the
        accrued gross and the R-23 withholding; the actual numbers can be given when they differ.
        A payment that was never accrued (a holding from before the ledger) needs its symbol and gross."""
        d = self._s.dividends.get(action_id)
        if d is not None and d.status != "ACCRUED":
            raise DividendError(f"corporate action {action_id} is already {d.status}")
        if d is None and (symbol is None or gross is None):
            raise DividendError(f"corporate action {action_id} was not accrued: give the symbol and the gross paid")
        if d is not None and symbol not in (None, d.symbol):
            raise DividendError(f"corporate action {action_id} is of {d.symbol}, not {symbol}")
        symbol = d.symbol if d else symbol
        if symbol not in self.instruments:
            raise ValueError(f"unknown instrument {symbol}")
        gross = d.accrued if gross is None else gross
        tax = dividend_withholding(gross) if tax is None else tax
        if gross <= 0 or not 0 <= tax < gross:
            raise ValueError("gross must be positive and the tax smaller than it")
        self._record(DividendPaymentEvent(date, action_id, symbol, gross, tax))
        return self._s.dividends[action_id]

    def cancel_dividend(self, date: dt.date, action_id: int) -> None:
        """Take an accrual back (차 배당수익 / 대 미수배당금): its corporate action was cancelled."""
        d = self._s.dividends.get(action_id)
        if d is None or d.status != "ACCRUED":
            raise DividendError(f"corporate action {action_id} has no open accrual to cancel")
        self._record(DividendCancelEvent(date, action_id, d.symbol))

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

    def last_entry_date(self) -> dt.date | None:
        """The latest date anything is booked on. Cash is not booked behind it (R-56)."""
        return max((book_date(e) for e in self._events), default=None)

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

    def settled_qty(self, symbol: str, as_of: dt.date) -> int:
        """Shares of `symbol` that had settled by `as_of`: the holding a record date counts (R-58).
        A buy counts from its settlement date, a sell stops counting on its own."""
        qty = 0
        for ev in self._events:
            if isinstance(ev, OpeningPositionEvent) and ev.symbol == symbol and ev.date <= as_of:
                qty += ev.qty
            elif isinstance(ev, ExecutionEvent) and ev.symbol == symbol \
                    and self._s.results[ev.exec_id].settle_date <= as_of:
                qty += ev.qty if ev.side is Side.BUY else -ev.qty
        return qty

    def dividends(self) -> dict[int, Dividend]:
        """Every corporate action booked in this account, by id, in booking order."""
        return dict(self._s.dividends)

    def adjustments(self) -> dict[int, Adjustment]:
        """Every adjustment booked in this account, by id, in booking order (R-66)."""
        return dict(self._s.adjustments)

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

    def withdrawable(self) -> int:
        """R-64 출금가능금액: the cash that can leave now without any booked trade falling short
        on its settlement date. The lowest point of 예수금 from today to the last settlement."""
        cash = low = self.cash_balance()
        by_date: dict[dt.date, int] = {}
        for s in self.pending_settlements():
            by_date[s.settle_date] = by_date.get(s.settle_date, 0) + s.signed
        for date in sorted(by_date):
            cash += by_date[date]
            low = min(low, cash)
        return max(low, 0)

    def cash_movements(self) -> list[CashMovement]:
        """Every deposit, withdrawal and transfer, with the units each issued or redeemed."""
        return list(self._s.movements)

    def projected_cash(self) -> int:
        """Cash after every booked trade settles (증권사 D+2 예수금, D+1 after a T+1 cutover)."""
        return self.cash_balance() + self.pending_settlement_net()

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
                self._apply_cash_in(ev, "DEPOSIT", "")
            case TransferInEvent():
                self._apply_cash_in(ev, "TRANSFER_IN", transfer_ref(ev.transfer_id), ev.transfer_id, ev.from_account)
            case WithdrawalEvent():
                self._apply_cash_out(ev, "WITHDRAWAL", "")
            case TransferOutEvent():
                self._apply_cash_out(ev, "TRANSFER_OUT", transfer_ref(ev.transfer_id), ev.transfer_id, ev.to_account)
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
                self._s.navs.append((ev.date, self._s.last_nav_row.nav_per_1000))
            case DividendAccrualEvent():
                self._s.dividends[ev.action_id] = Dividend(ev.action_id, ev.symbol, ev.qty, ev.gross, ev.date)
                self._s.journal.append(entry(ev.date, "DIVIDEND_ACCRUAL", dividend_ref(ev.action_id),
                                             Dr(Account.DIVIDEND_RECEIVABLE, ev.gross),
                                             Cr(Account.DIVIDEND_INCOME, ev.gross)))   # R-57
            case DividendPaymentEvent():
                self._apply_dividend_payment(ev)
            case DividendCancelEvent():
                d = self._s.dividends[ev.action_id]
                self._s.dividends[ev.action_id] = replace(d, canceled_on=ev.date)
                self._s.journal.append(entry(ev.date, "DIVIDEND_CANCEL", dividend_ref(ev.action_id),
                                             Dr(Account.DIVIDEND_INCOME, d.accrued),
                                             Cr(Account.DIVIDEND_RECEIVABLE, d.accrued)))
            case AdjustmentEvent():
                self._apply_adjustment(ev)
            case AdjustmentCancelEvent():
                a, booked = self._s.adjustments[ev.adjustment_id], self._s.adjustment_entries[ev.adjustment_id]
                self._s.adjustments[ev.adjustment_id] = replace(a, canceled_on=ev.date, cancel_memo=ev.memo)
                swapped = tuple(Dr(ln.account, ln.credit) if ln.credit else Cr(ln.account, ln.debit)
                                for ln in booked.lines)
                self._s.journal.append(entry(ev.date, "ADJUSTMENT_CANCEL", booked.source_id, *swapped,
                                             reverses=booked))
            case DayClosedEvent():
                self._s.closed_through = ev.date
                self._s.closed_days[ev.date] = self._s.last_nav_row
                self._s.closed_at = len(self._events) + 1   # this event is appended right after it is applied

    def _apply_dividend_payment(self, ev: DividendPaymentEvent) -> None:
        d = self._s.dividends.get(ev.action_id) or Dividend(ev.action_id, ev.symbol)
        if d.status != "ACCRUED" and ev.action_id in self._s.dividends:
            raise DividendError(f"corporate action {ev.action_id} is already {d.status}")
        more = ev.gross - d.accrued   # paid more (or less) than recognised: the income follows the payment
        self._s.dividends[ev.action_id] = replace(d, paid_gross=ev.gross, tax=ev.tax, paid_on=ev.date)
        self._s.journal.append(entry(ev.date, "DIVIDEND", dividend_ref(ev.action_id),
                                     Dr(Account.CASH, ev.gross - ev.tax), Dr(Account.WITHHOLDING_TAX, ev.tax),
                                     Cr(Account.DIVIDEND_RECEIVABLE, d.accrued),
                                     Cr(Account.DIVIDEND_INCOME, max(more, 0)),
                                     Dr(Account.DIVIDEND_INCOME, max(-more, 0))))   # R-57, R-23

    def _apply_adjustment(self, ev: AdjustmentEvent) -> None:
        income, expense = ADJUSTMENT_ACCOUNTS[ev.reason]
        if ev.amount > 0:   # cash in: what is withheld never arrives
            lines = (Dr(Account.CASH, ev.amount - ev.tax), Dr(Account.WITHHOLDING_TAX, ev.tax), Cr(income, ev.amount))
        else:
            lines = (Dr(expense, -ev.amount), Cr(Account.CASH, -ev.amount))
        e = entry(ev.date, "ADJUSTMENT", adjustment_ref(ev.adjustment_id), *lines)
        self._s.journal.append(e)
        self._s.adjustment_entries[ev.adjustment_id] = e
        self._s.adjustments[ev.adjustment_id] = Adjustment(ev.adjustment_id, ev.date, ev.reason, ev.amount, ev.tax,
                                                           ev.memo, ev.recon_result_id)

    def _unit_price(self, date: dt.date) -> Decimal:
        """R-61: money that comes or goes on `date` is priced at the last NAV struck for an earlier
        day. 1,000.00 before the first NAV, and again when no unit is left."""
        if self._s.units:
            for day, nav in reversed(self._s.navs):
                if day < date and nav > 0:
                    return nav
        return INITIAL_NAV

    def _apply_cash_in(self, ev, kind: str, ref: str, transfer_id: int | None = None,
                       counter: str | None = None) -> None:
        nav = self._unit_price(ev.date)
        units = truncate_won(Decimal(ev.amount) / nav * 1000)  # R-61
        self._s.units += units
        self._s.journal.append(entry(ev.date, kind, ref,
                                     Dr(Account.CASH, ev.amount),
                                     Cr(Account.CONTRIBUTED_CAPITAL, ev.amount)))   # R-56
        self._s.movements.append(CashMovement(ev.date, kind, ev.amount, units, nav, transfer_id, counter))

    def _apply_cash_out(self, ev, kind: str, ref: str, transfer_id: int | None = None,
                        counter: str | None = None) -> None:
        nav = self._unit_price(ev.date)
        units = min(self._s.units, truncate_won(Decimal(ev.amount) / nav * 1000))  # R-61
        self._s.journal.append(entry(ev.date, kind, ref,
                                     Dr(Account.CONTRIBUTED_CAPITAL, ev.amount),
                                     Cr(Account.CASH, ev.amount)))   # R-56
        if self._is_empty():   # everything has left: no unit stays behind on a rounding difference
            units = self._s.units
        self._s.units -= units
        self._s.movements.append(CashMovement(ev.date, kind, -ev.amount, -units, nav, transfer_id, counter))

    def _is_empty(self) -> bool:
        """No cash, no holding, nothing to receive or to pay."""
        return not self.positions() and not any(
            self.account_balance(a) for a in (Account.CASH, Account.SELL_RECEIVABLE, Account.BUY_PAYABLE,
                                              Account.DIVIDEND_RECEIVABLE))

    def _apply_opening(self, ev: OpeningPositionEvent) -> None:
        self._s.positions[ev.symbol] = Position(ev.qty, ev.book_cost)
        self._s.units += truncate_won(Decimal(ev.book_cost) / INITIAL_NAV * 1000)   # before any NAV (R-05)
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
