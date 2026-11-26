"""감사 로그 (ERD v2.3): who did what, when, through which door, and how it ended.

The ledger records what happened to the books. This records what the operator
did to the system: every order attempt, every close, judgement, account or code
change, whether it was done, refused by a rule, or failed. A refused action
changes nothing else, so this row is the only trace it leaves.

Rules it keeps:

* The function that changes something writes the row itself (`record`), in the
  same transaction when the caller holds one. If the row cannot be written, the
  change does not happen.
* A refusal is recorded where the request came in (the console or a screen),
  because the function that refused has already rolled back (`refusals`).
* Rows are append-only and chained: each carries the hash of the one before it
  and its own, both set by the database, so a row edited or removed in the
  middle is found by `verify` (`ministp_audit_chain`, `ministp_audit_hash`).
* Never a key, a password or an account number: there are none in the
  arguments this sees, and callers pass only what they would print.
"""
import contextlib
import contextvars
import datetime as dt
import getpass
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

CHANNELS = {"CLI": "명령", "UI": "화면", "SYSTEM": "시스템"}
OUTCOMES = {"DONE": "처리", "REFUSED": "거부", "FAILED": "실패"}
# What can be done to the system from outside, by code. The common codes (AUDIT_ACTION) name them.
ACTIONS = {
    "OPEN": "원장 시작",
    "ORDER": "주문",
    "ORDER_CANCEL": "주문 취소",
    "DAILY": "일일 마감·대사",
    "CLOSE": "장부 마감",
    "RESOLVE": "불일치 원인 판단",
    "INSTRUMENT_ADD": "종목 등록",
    "CUSTOMER_ADD": "법인 등록 (v2.14 전 코드)",
    "ACCOUNT_ADD": "계좌 등록",
    "ACCOUNT_STATUS": "계좌 상태 변경",
    "CODE_GROUP_ADD": "코드 그룹 추가",
    "CODE_ADD": "코드 추가",
    "CODE_RENAME": "코드명 변경",
    "CODE_USE": "코드 사용 여부 변경",
    "DB_MIGRATE": "스키마 마이그레이션",
    "CORPORATE_ACTION_ADD": "권리 등록",
    "CORPORATE_ACTION_CANCEL": "권리 취소",
    "DIVIDEND_PAY": "배당 입금 기록",
    "CASH_DEPOSIT": "입금 기록",
    "CASH_WITHDRAW": "출금 기록",
    "CASH_TRANSFER": "계좌 간 대체",
    "ADJUSTMENT_ADD": "수동 조정",
    "ADJUSTMENT_CANCEL": "수동 조정 취소",
    "MASTER_LOAD": "종목 마스터 적재",
    "USER_ADD": "사용자 등록",
    "RIGHT_GRANT": "권한 부여",
    "RIGHT_REVOKE": "권한 회수",
    "ORDER_REQUEST": "주문 결재 요청",
    "ORDER_APPROVE": "주문 승인",
    "ORDER_REJECT": "주문 반려",
    "ORDER_REQUEST_CANCEL": "주문 요청 취소",
    "ORDER_CONFIRM": "주문 결과 확인",
    "ERP_EXPORT": "ERP 전표 내보내기",
    "ERP_EXPORT_VOID": "ERP 전송 무효",
    "CLOSE_STEP_SET": "마감 점검 단계 변경",
    "COMPANY_ADD": "법인 등록",
    "COMPANY_ERP_CODE": "ERP 회사코드 지정",
}
MESSAGE_MAX = 500

# Who is acting, for every row this process writes: (channel, actor, screen transaction or None).
# A command sets it once at the start; a screen request sets it with the 처리 transaction it runs.
_origin: contextvars.ContextVar[tuple[str, str, str | None]] = contextvars.ContextVar(
    "ministp_audit_origin", default=("SYSTEM", "system", None))


def os_user() -> str:
    """The login of whoever runs this process. A PC account name, never a brokerage id."""
    try:
        return getpass.getuser() or "unknown"
    except Exception:   # no login name in this environment (some services, containers)
        return "unknown"


def set_origin(channel: str, actor: str | None = None, tx: str | None = None):
    if channel not in CHANNELS:
        raise ValueError(f"channel은 {sorted(CHANNELS)} 중 하나")
    return _origin.set((channel, actor or os_user(), tx))


def origin() -> tuple[str, str, str | None]:
    return _origin.get()


@contextlib.contextmanager
def acting_as(channel: str, actor: str | None = None, tx: str | None = None):
    """Rows written inside carry this origin; the one before comes back afterwards."""
    token = set_origin(channel, actor, tx)
    try:
        yield
    finally:
        _origin.reset(token)


def _plain(v):
    """Detail values as JSON: dates as text, everything else as it prints."""
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def record(conn: psycopg.Connection, action: str, outcome: str = "DONE", *, account_id: str | None = None,
           target: str | None = None, detail: dict | None = None, message: str | None = None) -> int:
    """Add one row. The database numbers it, stamps the time and links it to the row before."""
    if action not in ACTIONS:
        raise ValueError(f"감사 로그에 없는 행위 코드: {action}")
    if outcome not in OUTCOMES:
        raise ValueError(f"감사 로그에 없는 결과 코드: {outcome}")
    channel, actor, tx = origin()
    detail = {**(detail or {}), **({"tx": tx} if tx else {})}
    message = message if message is None or len(message) <= MESSAGE_MAX else message[:MESSAGE_MAX - 1] + "…"
    return conn.execute(
        "insert into audit_log (channel, actor, action, outcome, account_id, target, detail, message)"
        " values (%s, %s, %s, %s, %s, %s, %s, %s) returning id",
        (channel, actor, action, outcome, account_id, target, Jsonb(_plain(detail or {})), message)).fetchone()[0]


def has_log(conn: psycopg.Connection) -> bool:
    return conn.execute("select to_regclass('audit_log') is not null").fetchone()[0]


def _known_account(conn: psycopg.Connection, account_id: str | None) -> str | None:
    """A refusal may be about an account that does not exist; the row then carries it in the detail only."""
    if account_id is None:
        return None
    ok = conn.execute("select 1 from trading_accounts where account_id = %s", (account_id,)).fetchone()
    return account_id if ok else None


def record_refusal(conn: psycopg.Connection, action: str, exc: BaseException, *, failed: bool = False,
                   account_id: str | None = None, target: str | None = None, detail: dict | None = None) -> None:
    """Leave the row of an action that did not happen. Never hides the error it reports: if the
    log itself cannot be written here (schema behind, connection gone), the error still reaches the user."""
    try:
        if not has_log(conn):
            return
        known = _known_account(conn, account_id)
        if account_id and not known:
            detail = {**(detail or {}), "account": account_id}
        record(conn, action, "FAILED" if failed else "REFUSED", account_id=known, target=target, detail=detail,
               message=str(exc) or type(exc).__name__)
    except psycopg.Error:
        pass


@contextlib.contextmanager
def refusals(conn: psycopg.Connection, what, refused: tuple = (), failed: tuple = ()):
    """Around the handling of one request: an error of `refused` (a rule said no) or `failed`
    (something outside broke) leaves a row, then goes on to whoever reports it.

    `what()` names the request: (action, account_id, target, detail), or None when it is not
    something the log records (a read)."""
    try:
        yield
    except refused + failed as exc:
        named = what()
        if named is not None:
            action, account_id, target, detail = named
            record_refusal(conn, action, exc, failed=isinstance(exc, failed) and not isinstance(exc, refused),
                           account_id=account_id, target=target, detail=detail)
        raise


@dataclass(frozen=True)
class Row:
    id: int
    at: dt.datetime
    channel: str
    actor: str
    action: str
    outcome: str
    account_id: str | None
    target: str | None
    detail: dict
    message: str | None


def rows(conn: psycopg.Connection, *, limit: int = 50, account_id: str | None = None, action: str | None = None,
         outcome: str | None = None) -> list[Row]:
    """The latest rows, newest first."""
    found = conn.execute(
        "select id, at, channel, actor, action, outcome, account_id, target, detail, message from audit_log"
        " where (%(acct)s::text is null or account_id = %(acct)s) and (%(action)s::text is null or action = %(action)s)"
        " and (%(outcome)s::text is null or outcome = %(outcome)s) order by id desc limit %(n)s",
        {"acct": account_id, "action": action, "outcome": outcome, "n": limit}).fetchall()
    return [Row(*r) for r in found]


def chain_problems(conn: psycopg.Connection, shown: int = 4) -> tuple[int, str | None, list[int]]:
    """(rows, last hash, ids where the chain does not hold). The numbers must run 1..n, each row's
    hash must be the hash of its own content and of the row before it."""
    n, last = conn.execute("select count(*), (select hash from audit_log order by id desc limit 1)"
                           " from audit_log").fetchone()
    bad = [r[0] for r in conn.execute(
        "with x as (select a.id, a.hash, a.prev_hash, ministp_audit_hash(a.prev_hash, a) as want,"
        "                  lag(a.hash) over (order by a.id) as before, row_number() over (order by a.id) as nth"
        "             from audit_log a)"
        " select id from x where hash <> want or prev_hash <> coalesce(before, repeat('0', 64)) or id <> nth"
        " order by id limit %s", (shown,))]
    return n, last, bad
