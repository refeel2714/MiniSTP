"""입출금·대체 (스펙 R-56, R-61, R-64, R-65, ERD v2.5): cash the owner puts in, takes out, or moves
between two of their accounts.

MiniSTP does not move money. The owner moves it at the broker and records here what happened, so
that the books and the broker agree again at the next reconciliation. What is recorded still
follows the rules: no more leaves than can leave (출금가능금액), nothing is booked into a closed
day or behind entries that already exist, and the two halves of a transfer are written in one
transaction or not at all.
"""
import datetime as dt
from dataclasses import dataclass

import psycopg

from . import audit
from .accounts import get_account
from .ledger import CashMovement, transfer_ref
from .ops import Books, OpsError, is_opened


@dataclass(frozen=True)
class Transfer:
    """One transfer as `transfers_v` shows it: both halves in one row."""
    transfer_id: int
    date: dt.date
    from_account: str
    to_account: str
    amount: int

    @property
    def ref(self) -> str:
        return transfer_ref(self.transfer_id)


def list_transfers(conn: psycopg.Connection) -> list[Transfer]:
    return [Transfer(*r) for r in conn.execute(
        "select transfer_id, date, from_account, to_account, amount from transfers_v order by transfer_id")]


def check_bookable(books: Books, day: dt.date, today: dt.date | None) -> None:
    """Refuse, in the owner's words, a cash entry dated `day` that these books cannot take at all:
    not opened, a closed account, a day still to come, a closed day, a day behind the last entry."""
    led, acct = books.ledger, books.account_id
    if not is_opened(books):
        raise OpsError(f"계좌 {acct}의 원장이 아직 시작되지 않았어요. 먼저 open --account {acct}를 실행해 주세요.")
    if books.status == "CLOSED":
        raise OpsError(f"계좌 {acct}는 해지된 계좌예요. 더 기록하지 않아요.")
    if today is not None and day > today:
        raise OpsError(f"{day}은 아직 오지 않은 날짜예요. 실제로 돈이 움직인 날짜로 기록해 주세요.")
    if led.is_closed(day):
        raise OpsError(f"{day}은 이미 마감한 날이에요. 열려 있는 날짜({led.open_day()})로 기록해 주세요 (--date).")
    last = led.last_entry_date()
    if last is not None and day < last:
        raise OpsError(f"계좌 {acct}에는 {last} 날짜의 기록이 이미 있어요. 그보다 앞선 {day}로는 넣지 않아요. "
                       f"{last} 이후 날짜로 기록해 주세요 (--date).")


def check_can_leave(books: Books, amount: int) -> None:
    """R-64: refuse cash going out beyond 출금가능금액."""
    led = books.ledger
    if amount > led.withdrawable():
        raise OpsError(f"계좌 {books.account_id}의 출금가능금액은 {led.withdrawable():,}원이에요 (예수금 "
                       f"{led.cash_balance():,}원, 결제 후 {led.projected_cash():,}원). {amount:,}원은 나갈 수 없어요.")


def _ready(books: Books, day: dt.date, amount: int, today: dt.date | None, out: bool) -> None:
    if amount <= 0:
        raise OpsError("금액은 0보다 커야 해요.")
    check_bookable(books, day, today)
    if out and books.status != "OPEN":
        raise OpsError(f"계좌 {books.account_id}는 정지 상태({books.status})라 출금과 대체 출금을 받지 않아요. "
                       "입금은 기록할 수 있어요.")
    if out:
        check_can_leave(books, amount)


def _verified(books: Books) -> None:
    problems = books.store.verify(books.ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")


def _detail(day: dt.date, m: CashMovement, memo: str, **more) -> dict:
    return {"date": day, "amount": abs(m.amount), **more, "units": abs(m.units), "nav_per_1000": m.nav_per_1000,
            "memo": memo.strip() or None}


def deposit(books: Books, day: dt.date, amount: int, memo: str = "", today: dt.date | None = None) -> CashMovement:
    """`cash deposit`: money that came into the account at the broker (R-56). Units are issued for it (R-61)."""
    _ready(books, day, amount, today, out=False)
    with books.conn.transaction():   # the event and its audit row together
        books.ledger.deposit(day, amount)
        _verified(books)
        m = books.ledger.cash_movements()[-1]
        audit.record(books.conn, "CASH_DEPOSIT", account_id=books.account_id, detail=_detail(day, m, memo))
    return m


def withdraw(books: Books, day: dt.date, amount: int, memo: str = "", today: dt.date | None = None) -> CashMovement:
    """`cash withdraw`: money taken out at the broker (R-56), within 출금가능금액 (R-64)."""
    _ready(books, day, amount, today, out=True)
    with books.conn.transaction():
        books.ledger.withdraw(day, amount)
        _verified(books)
        m = books.ledger.cash_movements()[-1]
        audit.record(books.conn, "CASH_WITHDRAW", account_id=books.account_id, detail=_detail(day, m, memo))
    return m


def transfer(src: Books, dst: Books, day: dt.date, amount: int, memo: str = "",
             today: dt.date | None = None) -> tuple[Transfer, CashMovement, CashMovement]:
    """`cash transfer`: money moved between two accounts of one customer (R-65). The sender books a
    withdrawal and the receiver a deposit, both under one transfer number, in one transaction."""
    conn = src.conn
    if src.account_id == dst.account_id:
        raise OpsError("보내는 계좌와 받는 계좌가 같아요.")
    a, b = get_account(conn, src.account_id), get_account(conn, dst.account_id)
    if a.customer_id != b.customer_id:
        raise OpsError(f"대체는 같은 고객의 계좌 사이에서만 기록해요 ({a.account_id} 고객 {a.customer_id}, "
                       f"{b.account_id} 고객 {b.customer_id}). 다른 고객에게 보낸 돈은 출금과 입금으로 따로 기록해 주세요.")
    _ready(src, day, amount, today, out=True)
    _ready(dst, day, amount, today, out=False)
    with conn.transaction():   # both halves and the audit row, or nothing
        tid = conn.execute("select coalesce(max((payload ->> 'transfer_id')::bigint), 0) + 1 from ledger_events"
                           " where event_type in ('TransferOutEvent', 'TransferInEvent')").fetchone()[0]
        src.ledger.transfer_out(day, tid, dst.account_id, amount)
        dst.ledger.transfer_in(day, tid, src.account_id, amount)
        _verified(src)
        _verified(dst)
        sent, got = src.ledger.cash_movements()[-1], dst.ledger.cash_movements()[-1]
        audit.record(conn, "CASH_TRANSFER", account_id=src.account_id, target=transfer_ref(tid),
                     detail=_detail(day, sent, memo, to=dst.account_id, units_issued=got.units))
    return Transfer(tid, day, src.account_id, dst.account_id, amount), sent, got
