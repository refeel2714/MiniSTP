"""권리처리 (스펙 R-57~59, ERD v2.4): a cash dividend from its announcement to the cash.

A dividend is registered once, for the instrument (`corporate_actions`). Each
account's `daily` then reads the register: on the business day after the record
date it recognises what the account is entitled to (the holding that had settled
by the record date), and on the pay date it books the cash less the withholding
tax. A row is never changed: a wrong or withdrawn action gets a cancel row, and
an account that had already recognised it takes the accrual back.

The ledger events carry their own numbers, so the books replay without this
table. The table says where those numbers came from, and `verify` holds the two
against each other.
"""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from . import audit
from .calendar import BusinessCalendar
from .ledger import Dividend, Ledger, dividend_ref
from .money import truncate_won
from .ops import Books, OpsError

ACTION_TYPES = ("CASH_DIVIDEND", "CANCEL")


@dataclass(frozen=True)
class CorporateAction:
    action_id: int
    symbol: str
    record_date: dt.date
    pay_date: dt.date
    amount_per_share: Decimal
    note: str | None
    registered_at: dt.datetime
    canceled_at: dt.datetime | None
    cancel_reason: str | None

    @property
    def canceled(self) -> bool:
        return self.canceled_at is not None

    @property
    def ref(self) -> str:
        return dividend_ref(self.action_id)

    def accrual_date(self, calendar: BusinessCalendar) -> dt.date:
        """R-57: the business day after the record date."""
        return calendar.add_business_days(self.record_date, 1)

    def entitlement(self, ledger: Ledger) -> tuple[int, int]:
        """R-58: (shares, gross won) this account is entitled to. Won below one is cut."""
        qty = ledger.settled_qty(self.symbol, self.record_date)
        return qty, truncate_won(qty * self.amount_per_share)


_SELECT = ("select action_id, symbol, record_date, pay_date, amount_per_share, note, registered_at, canceled_at,"
           " cancel_reason from corporate_actions_v")


def list_actions(conn: psycopg.Connection, symbol: str | None = None) -> list[CorporateAction]:
    """Every dividend ever registered, cancelled ones included, in the order they were registered."""
    return [CorporateAction(*r) for r in conn.execute(
        _SELECT + " where (%(s)s::text is null or symbol = %(s)s) order by action_id", {"s": symbol})]


def get_action(conn: psycopg.Connection, action_id: int) -> CorporateAction:
    row = conn.execute(_SELECT + " where action_id = %s", (action_id,)).fetchone()
    if row is None:
        raise OpsError(f"등록되지 않은 권리예요: {dividend_ref(action_id)}. dividend list로 확인해 주세요.")
    return CorporateAction(*row)


def add_dividend(conn: psycopg.Connection, symbol: str, record_date: dt.date, pay_date: dt.date,
                 amount_per_share, note: str = "") -> CorporateAction:
    """Register a cash dividend (주식 배당금, ETF 분배금): the record date, the pay date, won per share before tax."""
    per_share = Decimal(str(amount_per_share))
    if conn.execute("select 1 from instruments where symbol = %s", (symbol,)).fetchone() is None:
        raise OpsError(f"종목 등록이 필요해요: {symbol}")
    if per_share <= 0 or per_share != per_share.quantize(Decimal("0.01")):
        raise OpsError("주당 배당금은 0보다 크고 소수 둘째 자리까지예요.")
    if pay_date < record_date:
        raise OpsError(f"지급일({pay_date})이 배당기준일({record_date})보다 빨라요.")
    same = [a for a in list_actions(conn, symbol) if a.record_date == record_date and not a.canceled]
    if same:
        raise OpsError(f"{symbol}의 배당기준일 {record_date} 권리가 이미 있어요 ({same[0].ref}). "
                       "금액을 고치려면 먼저 dividend cancel로 취소한 뒤 다시 등록해 주세요.")
    with conn.transaction():
        action_id = conn.execute(
            "insert into corporate_actions (symbol, action_type, record_date, pay_date, amount_per_share, note)"
            " values (%s, 'CASH_DIVIDEND', %s, %s, %s, %s) returning action_id",
            (symbol, record_date, pay_date, per_share, note.strip() or None)).fetchone()[0]
        audit.record(conn, "CORPORATE_ACTION_ADD", target=dividend_ref(action_id),
                     detail={"symbol": symbol, "record_date": record_date, "pay_date": pay_date,
                             "amount_per_share": per_share, "note": note.strip() or None})
    return get_action(conn, action_id)


def cancel_action(conn: psycopg.Connection, action_id: int, reason: str) -> CorporateAction:
    """Make a registered dividend void with a cancel row. Accounts that recognised it take the accrual
    back at their next `daily`. Refused once any account has been paid: that cash is real."""
    action = get_action(conn, action_id)
    if not reason.strip():
        raise OpsError("취소하는 사유를 적어 주세요.")
    if action.canceled:
        raise OpsError(f"이미 취소된 권리예요: {action.ref}")
    paid = [r[0] for r in conn.execute("select account_id from dividends_v where action_id = %s and status = 'PAID'"
                                       " order by account_id", (action_id,))]
    if paid:
        raise OpsError(f"{action.ref}는 계좌 {', '.join(paid)}에 이미 입금으로 기록돼서 취소할 수 없어요.")
    with conn.transaction():
        conn.execute("insert into corporate_actions (symbol, action_type, cancels_id, note)"
                     " values (%s, 'CANCEL', %s, %s)", (action.symbol, action_id, reason.strip()))
        audit.record(conn, "CORPORATE_ACTION_CANCEL", target=action.ref,
                     detail={"symbol": action.symbol, "record_date": action.record_date, "reason": reason.strip()})
    return get_action(conn, action_id)


def account_dividends(conn: psycopg.Connection, action_id: int | None = None) -> list[tuple]:
    """(account_id, action_id, symbol, qty, accrued_gross, accrued_on, paid_gross, tax, paid_on, canceled_on, status)."""
    return conn.execute(
        "select account_id, action_id, symbol, qty, accrued_gross, accrued_on, paid_gross, tax, paid_on,"
        " canceled_on, status from dividends_v where (%(a)s::bigint is null or action_id = %(a)s)"
        " order by action_id, account_id", {"a": action_id}).fetchall()


def apply_due(books: Books, day: dt.date) -> list[str]:
    """What `daily` does about dividends, in the register's order (R-59). Returns a line per thing booked.

    * a cancelled action this account had recognised: the accrual is taken back;
    * an action whose recognition day has come: recognised, if the account held any on the record date;
    * a recognised action whose pay date has come: paid, less the R-23 withholding.

    Everything is dated `day`: an action registered late is recognised on the day it is first seen,
    never on a date that may already be closed."""
    ledger, said = books.ledger, []
    for a in list_actions(books.conn):
        have = ledger.dividends().get(a.action_id)
        if a.canceled:
            if have is not None and have.status == "ACCRUED":
                ledger.cancel_dividend(day, a.action_id)
                said.append(f"배당 인식 취소 {a.ref} {a.symbol}: 권리가 취소돼 {have.accrued:,}원을 되돌렸어요.")
            continue
        if have is None and a.accrual_date(ledger.calendar) <= day:
            qty, gross = a.entitlement(ledger)
            if qty > 0 and gross > 0:
                ledger.accrue_dividend(day, a.action_id, a.symbol, qty, gross)
                have = ledger.dividends()[a.action_id]
                said.append(f"배당 인식 {a.ref} {a.symbol}: 기준일 {a.record_date} 보유 {qty:,}주 × "
                            f"{_won(a.amount_per_share)}원 = {gross:,}원 (지급일 {a.pay_date})")
        if have is not None and have.status == "ACCRUED" and a.pay_date <= day:
            paid = ledger.pay_dividend(day, a.action_id)
            said.append(f"배당 입금 {a.ref} {a.symbol}: {paid.paid_gross:,}원에서 세금 {paid.tax:,}원을 떼고 "
                        f"{paid.paid_gross - paid.tax:,}원")
    return said


def record_payment(books: Books, action_id: int, day: dt.date, gross: int | None = None,
                   tax: int | None = None) -> Dividend:
    """`dividend pay`: book the payment with the numbers the broker actually paid, when they differ from
    the expected ones or when the account never recognised the dividend (a holding from before the ledger)."""
    action = get_action(books.conn, action_id)
    ledger = books.ledger
    if action.canceled:
        raise OpsError(f"취소된 권리예요: {action.ref}")
    if ledger.is_closed(day):
        raise OpsError(f"{day}은 이미 마감한 날이에요. 열려 있는 날짜({ledger.open_day()})로 기록해 주세요 (--date).")
    have = ledger.dividends().get(action_id)
    if have is not None and have.status != "ACCRUED":
        raise OpsError(f"{action.ref}는 이 계좌에 이미 {'입금으로' if have.status == 'PAID' else '취소로'} 기록돼 있어요.")
    if have is None and gross is None:
        raise OpsError(f"{action.ref}는 이 계좌에 인식된 적이 없어요. 실제로 들어온 세전 금액을 --gross로 알려 주세요.")
    paid_gross = have.accrued if gross is None else gross
    if paid_gross <= 0 or (tax is not None and not 0 <= tax < paid_gross):
        raise OpsError("세전 금액은 0보다 크고, 세금은 0 이상이면서 세전 금액보다 작아야 해요.")
    with books.conn.transaction():   # the payment and its audit row together
        paid = ledger.pay_dividend(day, action_id, action.symbol, gross, tax)
        problems = books.store.verify(ledger)
        if problems:
            raise OpsError(f"DB 원장 검증 실패: {problems}")
        audit.record(books.conn, "DIVIDEND_PAY", account_id=books.account_id, target=action.ref,
                     detail={"symbol": action.symbol, "date": day, "gross": paid.paid_gross, "tax": paid.tax,
                             "accrued": paid.accrued})
    return paid


def _won(x: Decimal) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")
