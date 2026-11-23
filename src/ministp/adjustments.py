"""수동 조정 (스펙 R-66, ERD v2.6): cash the ledger cannot work out by itself, booked by the owner.

Two kinds of thing end up here. Cash the broker pays or takes for a reason no event of the ledger
produces (예탁금 이용료). And a difference that stays between the broker and the ledger after a
break has been judged: the broker charged another fee, withheld another tax, paid another dividend.
Until it is booked, the same difference breaks every later reconciliation.

It is not a free journal entry. The reason is one of a fixed list and decides the account on the
other side, a memo is required, the audit log keeps the request, and an adjustment is never edited:
it is cancelled by the opposite entry.
"""
import datetime as dt

import psycopg

from . import audit
from .cash import check_bookable, check_can_leave
from .ledger import ADJUSTMENT_ACCOUNTS, Adjustment, adjustment_ref
from .ops import Books, OpsError

REASONS = tuple(ADJUSTMENT_ACCOUNTS)


def list_adjustments(conn: psycopg.Connection, account_id: str | None = None) -> list[tuple]:
    """Rows of `adjustments_v`: (account, id, date, reason, amount, tax, cash, memo, break row, canceled on, why)."""
    return conn.execute(
        "select account_id, adjustment_id, date, reason, amount, tax, cash_amount, memo, recon_result_id,"
        " canceled_on, cancel_reason from adjustments_v where %(a)s::text is null or account_id = %(a)s"
        " order by adjustment_id", {"a": account_id}).fetchall()


def _verified(books: Books) -> None:
    problems = books.store.verify(books.ledger)
    if problems:
        raise OpsError(f"DB 원장 검증 실패: {problems}")


def _detail(a: Adjustment, day: dt.date, **more) -> dict:
    return {"date": day, "reason": a.reason, "amount": a.amount, "tax": a.tax or None, "cash": a.cash,
            "recon_result_id": a.recon_result_id, **more}


def add(books: Books, day: dt.date, reason: str, amount: int, tax: int = 0, memo: str = "",
        recon_result_id: int | None = None, today: dt.date | None = None) -> Adjustment:
    """`adjust add`: book the cash and the account its reason names, in one transaction with the audit row."""
    conn = books.conn
    if reason not in ADJUSTMENT_ACCOUNTS:
        raise OpsError(f"조정 사유는 {', '.join(REASONS)} 중 하나예요. 받은 값: {reason}")
    if not memo.strip():
        raise OpsError("조정에는 왜 하는지 메모가 있어야 해요 (--memo).")
    if amount == 0:
        raise OpsError("금액은 0이 아니어야 해요. 예수금이 늘면 +, 줄면 -로 적어요.")
    if ADJUSTMENT_ACCOUNTS[reason][amount < 0] is None:
        raise OpsError(f"{reason}는 예수금이 늘어나는 조정만 해요.")
    if tax and (reason != "INTEREST" or not 0 < tax < amount):
        raise OpsError("세금(--tax)은 INTEREST에만 적고, 0보다 크고 세전 금액보다 작아야 해요.")
    check_bookable(books, day, today)
    if amount < 0:
        check_can_leave(books, -amount)
    if recon_result_id is not None:
        row = conn.execute("select account_id, status from recon_results where id = %s", (recon_result_id,)).fetchone()
        if row is None or row[1] == "MATCH":
            raise OpsError(f"불일치 행이 아니에요: {recon_result_id}")
        if row[0] != books.account_id:
            raise OpsError(f"불일치 행 {recon_result_id}은 계좌 {row[0]}의 것이에요. 이 조정은 계좌 {books.account_id}에 기록돼요.")
    with conn.transaction():   # the event and its audit row together
        new_id = conn.execute("select coalesce(max((payload ->> 'adjustment_id')::bigint), 0) + 1 from ledger_events"
                              " where event_type = 'AdjustmentEvent'").fetchone()[0]
        a = books.ledger.adjust(day, new_id, reason, amount, tax, memo, recon_result_id)
        _verified(books)
        audit.record(conn, "ADJUSTMENT_ADD", account_id=books.account_id, target=a.ref,
                     detail=_detail(a, day, memo=a.memo))
    return a


def cancel(books: Books, adjustment_id: int, day: dt.date, memo: str, today: dt.date | None = None) -> Adjustment:
    """`adjust cancel`: the opposite entry on an open day. The adjustment and its entry stay as they were."""
    a = books.ledger.adjustments().get(adjustment_id)
    if a is None:
        raise OpsError(f"계좌 {books.account_id}에 {adjustment_ref(adjustment_id)} 조정이 없어요. adjust list로 확인해 주세요.")
    if a.canceled_on:
        raise OpsError(f"{a.ref}는 이미 취소됐어요 ({a.canceled_on}).")
    if not memo.strip():
        raise OpsError("취소 사유를 적어 주세요.")
    check_bookable(books, day, today)
    if a.cash > 0:
        check_can_leave(books, a.cash)
    with books.conn.transaction():
        a = books.ledger.cancel_adjustment(day, adjustment_id, memo)
        _verified(books)
        audit.record(books.conn, "ADJUSTMENT_CANCEL", account_id=books.account_id, target=a.ref,
                     detail=_detail(a, day, reason=a.reason, why=a.cancel_memo))
    return a
