"""법인과 위탁계좌 (스펙 D7, ERD v2.0; 법인은 v2.14): registering them and changing an account's status.

A company (법인) keeps its own books; its accounts add up to them, and its ERP company code says which
books of the ERP its journal goes to. The code can change: each change is a new row in `company_erp_codes`.

The database holds an alias, the broker, the environment and a *name* for the
credentials (`credential_ref`). The account number and the keys themselves stay
on the owner's PC, in environment variables that start with `MINISTP_<credential_ref>_`.
Rows are never changed: a status change is a new row in `account_status_history`.
"""
import datetime as dt
import os
import re
from dataclasses import dataclass

import psycopg

from . import audit
from .ops import OpsError

STATUSES = ("OPEN", "SUSPENDED", "CLOSED")
COMPANY_ID_RE = re.compile(r"^C[0-9]{4}$")
ERP_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,20}$")
ACCOUNT_ID_RE = re.compile(r"^A[0-9]{4}$")
CREDENTIAL_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,30}$")


@dataclass(frozen=True)
class AccountInfo:
    account_id: str
    company_id: str
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


def masked_account_no(credential_ref: str, environ=os.environ) -> str | None:
    """The account number as the screens may show it: the first two and last two digits of the 8-digit number and
    the product code, the rest hidden (`12****78-01`). Read from this PC's `MINISTP_<ref>_ACCOUNT` each time it is
    asked, never stored or logged. None when this PC has no number for the account (or it is not a number)."""
    raw = (environ.get(f"{env_prefix(credential_ref)}_ACCOUNT") or "").replace("-", "").strip()
    if len(raw) not in (8, 10) or not raw.isdigit():
        return None
    return f"{raw[:2]}****{raw[6:8]}-{raw[8:] or '01'}"


_SELECT = ("select t.account_id, t.company_id, t.alias, t.broker, t.env, t.account_type, t.credential_ref,"
           " t.opened_on, s.status, s.effective_from from trading_accounts t"
           " join account_status_v s using (account_id)")


def list_accounts(conn: psycopg.Connection) -> list[AccountInfo]:
    return [AccountInfo(*r) for r in conn.execute(_SELECT + " order by t.company_id, t.account_id")]


def get_account(conn: psycopg.Connection, account_id: str) -> AccountInfo:
    row = conn.execute(_SELECT + " where t.account_id = %s", (account_id,)).fetchone()
    if row is None:
        raise OpsError(f"등록되지 않은 계좌예요: {account_id}. account list로 확인해 주세요.")
    return AccountInfo(*row)


def add_company(conn: psycopg.Connection, company_id: str, alias: str, *, erp_code: str | None = None,
                effective_from: dt.date | None = None) -> None:
    """`alias` is a short name for the screens. With `erp_code`, its ERP company code from `effective_from`
    (today when not given) is registered in the same transaction."""
    if not COMPANY_ID_RE.match(company_id):
        raise OpsError(f"법인 ID는 C와 숫자 4자리예요 (예: C0002). 받은 값: {company_id}")
    if not alias.strip():
        raise OpsError("법인 별칭을 넣어 주세요.")
    if conn.execute("select 1 from companies where company_id = %s", (company_id,)).fetchone():
        raise OpsError(f"이미 있는 법인이에요: {company_id}")
    with conn.transaction():
        conn.execute("insert into companies (company_id, alias) values (%s, %s)", (company_id, alias.strip()))
        audit.record(conn, "COMPANY_ADD", target=company_id, detail={"alias": alias.strip()})
        if erp_code:
            set_erp_code(conn, company_id, erp_code, effective_from or dt.date.today(), "법인 등록")


def erp_code_on(conn: psycopg.Connection, company_id: str, day: dt.date) -> str | None:
    """The company's ERP company code on `day`: the last row that started on or before it."""
    row = conn.execute("select erp_company_code from company_erp_codes where company_id = %s and effective_from <= %s"
                       " order by effective_from desc, id desc limit 1", (company_id, day)).fetchone()
    return row[0] if row else None


def set_erp_code(conn: psycopg.Connection, company_id: str, erp_code: str, effective_from: dt.date,
                 reason: str) -> None:
    """Add a row to the company's ERP code history. Two companies never share a code on the same day,
    or one company's journal would land in the other's books."""
    erp_code = erp_code.strip()
    if not ERP_CODE_RE.match(erp_code):
        raise OpsError(f"ERP 회사코드는 영문·숫자·밑줄·하이픈 20자 이내예요. 받은 값: {erp_code}")
    if not reason.strip():
        raise OpsError("ERP 회사코드를 정하는 사유를 적어 주세요.")
    if conn.execute("select 1 from companies where company_id = %s", (company_id,)).fetchone() is None:
        raise OpsError(f"등록되지 않은 법인이에요: {company_id}. company list로 확인해 주세요.")
    last = conn.execute("select erp_company_code, effective_from from company_erp_codes where company_id = %s"
                        " order by effective_from desc, id desc limit 1", (company_id,)).fetchone()
    if last and last[0] == erp_code:
        raise OpsError(f"법인 {company_id}의 ERP 회사코드는 이미 {erp_code}예요.")
    if last and effective_from < last[1]:
        raise OpsError(f"적용 시작일이 지금 코드의 시작일({last[1]})보다 빨라요.")
    others = [c for (c,) in conn.execute("select company_id from companies where company_id <> %s order by 1",
                                         (company_id,))
              if erp_code in (erp_code_on(conn, c, effective_from), erp_code_on(conn, c, dt.date.max))]
    if others:
        raise OpsError(f"ERP 회사코드 {erp_code}는 법인 {others[0]}가 쓰고 있어요. 법인마다 다른 코드여야 "
                       "한 법인의 전표가 다른 법인 장부로 가지 않아요.")
    with conn.transaction():
        conn.execute("insert into company_erp_codes (company_id, erp_company_code, effective_from, reason)"
                     " values (%s, %s, %s, %s)", (company_id, erp_code, effective_from, reason.strip()))
        audit.record(conn, "COMPANY_ERP_CODE", target=company_id,
                     detail={"erp_code": erp_code, "from": last[0] if last else None,
                             "effective_from": effective_from, "reason": reason.strip()})


def add_account(conn: psycopg.Connection, account_id: str, company_id: str, alias: str, *,
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
    if conn.execute("select 1 from companies where company_id = %s", (company_id,)).fetchone() is None:
        raise OpsError(f"등록되지 않은 법인이에요: {company_id}. 먼저 company add로 등록해 주세요.")
    if conn.execute("select 1 from trading_accounts where account_id = %s", (account_id,)).fetchone():
        raise OpsError(f"이미 있는 계좌예요: {account_id}")
    used = conn.execute("select account_id from trading_accounts where broker = 'KIS' and env = %s"
                        " and credential_ref = %s", (env, credential_ref)).fetchone()
    if used:
        raise OpsError(f"인증정보 이름 {credential_ref}는 계좌 {used[0]}가 쓰고 있어요. "
                       "계좌마다 다른 이름을 써야 다른 계좌의 키로 주문이 나가지 않아요.")
    with conn.transaction():
        conn.execute(
            "insert into trading_accounts (account_id, company_id, alias, broker, env, account_type,"
            " credential_ref, opened_on) values (%s, %s, %s, 'KIS', %s, 'GENERAL', %s, %s)",
            (account_id, company_id, alias.strip(), env, credential_ref, opened_on))
        conn.execute("insert into account_status_history (account_id, status, effective_from, reason)"
                     " values (%s, 'OPEN', %s, '계좌 등록')", (account_id, opened_on))
        audit.record(conn, "ACCOUNT_ADD", account_id=account_id, target=account_id,
                     detail={"company_id": company_id, "alias": alias.strip(), "env": env,
                             "credential_ref": credential_ref, "opened_on": opened_on})
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
    with conn.transaction():
        conn.execute("insert into account_status_history (account_id, status, effective_from, reason)"
                     " values (%s, %s, %s, %s)", (account_id, status, effective_from, reason.strip()))
        audit.record(conn, "ACCOUNT_STATUS", account_id=account_id, target=account_id,
                     detail={"from": acct.status, "to": status, "effective_from": effective_from,
                             "reason": reason.strip()})
    return get_account(conn, account_id)
