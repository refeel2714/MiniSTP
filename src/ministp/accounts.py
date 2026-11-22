"""고객과 위탁계좌 (스펙 D7, ERD v2.0): registering them and changing an account's status.

The database holds an alias, the broker, the environment and a *name* for the
credentials (`credential_ref`). The account number and the keys themselves stay
on the owner's PC, in environment variables that start with `MINISTP_<credential_ref>_`.
Rows are never changed: a status change is a new row in `account_status_history`.
"""
import datetime as dt
import re
from dataclasses import dataclass

import psycopg

from .ops import OpsError

STATUSES = ("OPEN", "SUSPENDED", "CLOSED")
CUSTOMER_ID_RE = re.compile(r"^C[0-9]{4}$")
ACCOUNT_ID_RE = re.compile(r"^A[0-9]{4}$")
CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,30}$")


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    customer_id: str
    alias: str
    broker: str
    env: str               # PAPER / LIVE
    account_type: str
    credential_ref: str
    opened_on: dt.date
    status: str            # OPEN / SUSPENDED / CLOSED
    status_from: dt.date

    @property
    def env_prefix(self) -> str:
        """Environment variables of this account start with this, e.g. `MINISTP_KIS` + `_APP_KEY`."""
        return env_prefix(self.credential_ref)


def env_prefix(credential_ref: str) -> str:
    return f"MINISTP_{credential_ref}"


_SELECT = ("select t.account_id, t.customer_id, t.alias, t.broker, t.env, t.account_type, t.credential_ref,"
           " t.opened_on, s.status, s.effective_from from trading_accounts t"
           " join account_status_v s using (account_id)")


def list_accounts(conn: psycopg.Connection) -> list[AccountInfo]:
    return [AccountInfo(*r) for r in conn.execute(_SELECT + " order by t.customer_id, t.account_id")]


def get_account(conn: psycopg.Connection, account_id: str) -> AccountInfo:
    row = conn.execute(_SELECT + " where t.account_id = %s", (account_id,)).fetchone()
    if row is None:
        raise OpsError(f"등록되지 않은 계좌예요: {account_id}. account list로 확인해 주세요.")
    return AccountInfo(*row)


def add_customer(conn: psycopg.Connection, customer_id: str, alias: str) -> None:
    """`alias` is a nickname for the screens. Real names and other personal data do not belong here."""
    if not CUSTOMER_ID_RE.match(customer_id):
        raise OpsError(f"고객 ID는 C와 숫자 4자리예요 (예: C0002). 받은 값: {customer_id}")
    if not alias.strip():
        raise OpsError("고객 별칭을 넣어 주세요.")
    if conn.execute("select 1 from customers where customer_id = %s", (customer_id,)).fetchone():
        raise OpsError(f"이미 있는 고객이에요: {customer_id}")
    conn.execute("insert into customers (customer_id, alias) values (%s, %s)", (customer_id, alias.strip()))


def add_account(conn: psycopg.Connection, account_id: str, customer_id: str, alias: str, *,
                credential_ref: str, opened_on: dt.date, env: str = "PAPER") -> AccountInfo:
    """Register an account and its first status row (OPEN) together."""
    if not ACCOUNT_ID_RE.match(account_id):
        raise OpsError(f"계좌 ID는 A와 숫자 4자리예요 (예: A0002). 받은 값: {account_id}")
    if not alias.strip():
        raise OpsError("계좌 별칭을 넣어 주세요.")
    if not CREDENTIAL_REF_RE.match(credential_ref):
        raise OpsError("인증정보 이름은 영문 대문자로 시작하는 대문자·숫자·밑줄이에요 (예: KIS_2). "
                       "계좌번호나 키를 넣는 자리가 아니에요.")
    if env != "PAPER":
        raise OpsError("Phase 4 전까지는 모의투자(PAPER) 계좌만 등록해요.")
    if conn.execute("select 1 from customers where customer_id = %s", (customer_id,)).fetchone() is None:
        raise OpsError(f"등록되지 않은 고객이에요: {customer_id}. 먼저 customer add로 등록해 주세요.")
    if conn.execute("select 1 from trading_accounts where account_id = %s", (account_id,)).fetchone():
        raise OpsError(f"이미 있는 계좌예요: {account_id}")
    used = conn.execute("select account_id from trading_accounts where broker = 'KIS' and env = %s"
                        " and credential_ref = %s", (env, credential_ref)).fetchone()
    if used:
        raise OpsError(f"인증정보 이름 {credential_ref}는 계좌 {used[0]}가 쓰고 있어요. "
                       "계좌마다 다른 이름을 써야 다른 계좌의 키로 주문이 나가지 않아요.")
    with conn.transaction():
        conn.execute(
            "insert into trading_accounts (account_id, customer_id, alias, broker, env, account_type,"
            " credential_ref, opened_on) values (%s, %s, %s, 'KIS', %s, 'GENERAL', %s, %s)",
            (account_id, customer_id, alias.strip(), env, credential_ref, opened_on))
        conn.execute("insert into account_status_history (account_id, status, effective_from, reason)"
                     " values (%s, 'OPEN', %s, '계좌 등록')", (account_id, opened_on))
    return get_account(conn, account_id)


def set_status(conn: psycopg.Connection, account_id: str, status: str, effective_from: dt.date,
               reason: str) -> AccountInfo:
    """Add a status row. A closed account stays closed; closing needs empty books."""
    acct = get_account(conn, account_id)
    if status not in STATUSES:
        raise OpsError(f"상태는 {', '.join(STATUSES)} 중 하나예요.")
    if not reason.strip():
        raise OpsError("상태를 바꾸는 사유를 적어 주세요.")
    if acct.status == status:
        raise OpsError(f"계좌 {account_id}는 이미 {status} 상태예요.")
    if acct.status == "CLOSED":
        raise OpsError(f"해지된 계좌는 다시 열지 않아요: {account_id}. 새 계좌를 등록해 주세요.")
    if effective_from < acct.status_from:
        raise OpsError(f"적용 시작일이 지금 상태의 시작일({acct.status_from})보다 빨라요.")
    if status == "CLOSED":
        cash = conn.execute("select balance from cash_balance_v where account_id = %s", (account_id,)).fetchone()[0]
        held = conn.execute("select count(*) from positions_v where account_id = %s", (account_id,)).fetchone()[0]
        owed = conn.execute(   # 매도미수금 1020, 매수미지급금 2010: trades still waiting to settle
            "select coalesce(sum(abs(balance)), 0) from account_balances_v"
            " where account_id = %s and code in ('1020', '2010')", (account_id,)).fetchone()[0]
        if cash or held or owed:
            raise OpsError(f"계좌 {account_id}에 예수금 {int(cash):,}원, 보유 {held}종목, 미결제 {int(owed):,}원이 남아 있어 "
                           "해지할 수 없어요. 잔고가 0이 된 뒤에 해지해요.")
    conn.execute("insert into account_status_history (account_id, status, effective_from, reason)"
                 " values (%s, %s, %s, %s)", (account_id, status, effective_from, reason.strip()))
    return get_account(conn, account_id)
