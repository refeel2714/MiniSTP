"""주문 결재 (스펙 D9, R-99~101): 모든 주문은 요청으로 먼저 남고, 승인된 요청만 `run`이 보내요.

- 권한(R-99): 주문 작성, 주문 승인(1건 한도, 셀프 승인 허용), 권한 관리. 계좌마다 또는 모든 계좌에 줘요.
  권한은 부여·회수마다 한 행이 쌓이고, 오늘 적용되는 최근 행이 지금 권한이에요.
  셀프 승인은 권한에 "셀프 승인 허용"이 있는 사람만, 1건 한도 안에서만 해요. 없는 사람은 요청만 올려요.
- 흐름(R-100): 요청 → 승인·반려·취소 → 전송·전송 실패, 그리고 만료. 순서는 DB 트리거가 지켜요.
  묶음(바스켓, 다건 요청)은 전부 아니면 전무예요: 승인·반려·취소는 묶음 전체로만 해요.
- 묶음 검증(R-98)을 네 번 해요: 요청할 때(결재 대기와 승인 후 미전송 요청 포함), 승인할 때(고른 요청만,
  승인 후 미전송 요청 포함), `run`이 보내기 직전, 그리고 보낼 때 주문 경로가 한 줄씩 한 번 더(R-36~38).
  요청과 승인은 계좌마다 DB 잠금을 잡고 해서, 두 사람이 동시에 올려도 묶음 검증이 서로를 보고 판단해요.
- 전송(R-101): `run`이 승인된 오늘 요청을 줄 순서대로 보내요. 주문 의도(oms_orders)와 전송 기록은 증권사에
  보내기 전에 함께 남겨서, 중간에 프로그램이 죽어도 같은 요청이 두 번 나가지 않아요. 자동 재주문은 없어요.
- 만료: 주문일 장 마감까지 나가지 않은 요청은 만료돼요. 다음 날 내려면 새로 요청해요.

누가 하는지는 감사 로그와 같은 값(`audit.origin()`: 명령은 PC 로그인 이름)이에요. 사용자 ID 체계는 보류(11/24).
"""
import datetime as dt
from dataclasses import dataclass, replace
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from . import audit
from .ledger import Side
from .ops import Books, OpsError
from .precheck import DEFAULT_WEIGHT_LIMIT, AccountState, Proposed, SetCheck, check_set, issue_text
from .risk import RiskLimits

RIGHTS = {"ORDER_WRITE": "주문 작성", "ORDER_APPROVE": "주문 승인", "RIGHTS_ADMIN": "권한 관리"}
ACTIONS = {"REQUEST": "결재 요청", "APPROVE": "승인", "REJECT": "반려", "CANCEL": "요청 취소",
           "SEND": "전송", "SEND_FAILED": "전송 실패", "EXPIRE": "만료"}
STATUSES = {"PENDING": "결재 대기", "APPROVED": "승인", "REJECTED": "반려", "CANCELED": "취소",
            "SENT": "전송", "SEND_FAILED": "전송 실패", "EXPIRED": "만료"}
LIVE = ("PENDING", "APPROVED")      # 아직 주문가능금액·매도가능수량을 잡고 있는 요청


class ApprovalError(OpsError):
    pass


def actor() -> str:
    return audit.origin()[1]


# ---------- 사용자와 권한 (R-99) ----------

@dataclass(frozen=True)
class Right:
    user_id: str
    right_code: str
    account_id: str | None          # None = 모든 계좌
    max_order_value: int | None     # 1건 승인 한도 (원), None = 한도 없음
    self_approve: bool
    effective_from: dt.date


def has_users(conn: psycopg.Connection) -> bool:
    return conn.execute("select exists (select 1 from app_users)").fetchone()[0]


def add_user(conn: psycopg.Connection, user_id: str, name: str, today: dt.date) -> None:
    """첫 사용자는 누구나 등록해요(그다음 자기에게 권한 관리를 줘요). 그 뒤로는 권한 관리자만."""
    by = actor()
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext('ministp.rights'))")
        if has_users(conn):
            require(conn, by, "RIGHTS_ADMIN", None, today)
        if conn.execute("select 1 from app_users where user_id = %s", (user_id,)).fetchone():
            raise ApprovalError(f"이미 등록된 사용자예요: {user_id}")
        conn.execute("insert into app_users (user_id, name, created_by) values (%s, %s, %s)", (user_id, name, by))
        audit.record(conn, "USER_ADD", target=user_id, detail={"name": name})


def grant(conn: psycopg.Connection, user_id: str, right_code: str, today: dt.date, *, account_id: str | None = None,
          max_order_value: int | None = None, self_approve: bool = False, effective_from: dt.date | None = None,
          reason: str | None = None, revoke: bool = False) -> None:
    """권한을 주거나(한도·셀프 승인을 바꿀 때도 새 행) 거둬요. 권한 관리자가 없을 때는 권한 관리만 줄 수 있어요."""
    if right_code not in RIGHTS:
        raise ApprovalError(f"권한은 {', '.join(RIGHTS)} 중 하나예요: {right_code}")
    if right_code != "ORDER_APPROVE" and (max_order_value is not None or self_approve):
        raise ApprovalError("1건 한도와 셀프 승인 허용은 주문 승인 권한에만 붙어요")
    if revoke and (max_order_value is not None or self_approve):
        raise ApprovalError("회수할 때는 한도와 셀프 승인을 넣지 않아요")
    by = actor()
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext('ministp.rights'))")
        admins = conn.execute("select count(*) from user_rights_v where right_code = 'RIGHTS_ADMIN'").fetchone()[0]
        if admins:
            require(conn, by, "RIGHTS_ADMIN", None, today)
        elif right_code != "RIGHTS_ADMIN" or revoke:
            raise ApprovalError("권한 관리자가 아직 없어요. 먼저 right grant 사용자 RIGHTS_ADMIN 으로 정해 주세요")
        if not conn.execute("select 1 from app_users where user_id = %s", (user_id,)).fetchone():
            raise ApprovalError(f"등록되지 않은 사용자예요: {user_id}. user add로 먼저 등록해 주세요")
        if account_id and not conn.execute("select 1 from trading_accounts where account_id = %s",
                                           (account_id,)).fetchone():
            raise ApprovalError(f"등록되지 않은 계좌예요: {account_id}")
        conn.execute(
            "insert into user_rights (user_id, account_id, right_code, granted, max_order_value, self_approve,"
            " effective_from, granted_by, reason) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, account_id, right_code, not revoke, max_order_value, self_approve,
             effective_from or today, by, reason))
        audit.record(conn, "RIGHT_REVOKE" if revoke else "RIGHT_GRANT", account_id=account_id, target=user_id,
                     detail={"right": right_code, "max_order_value": max_order_value,
                             "self_approve": self_approve or None, "effective_from": effective_from or today,
                             "reason": reason})


def rights_of(conn: psycopg.Connection, user_id: str, account_id: str | None, day: dt.date) -> dict[str, Right]:
    """`day`에 적용되는 권한. 계좌를 정해 준 행이 모든 계좌 행보다 앞서요."""
    rows = conn.execute(
        "select distinct on (right_code, account_id is null) right_code, account_id, granted, max_order_value,"
        " self_approve, effective_from from user_rights"
        " where user_id = %s and effective_from <= %s and (account_id is null or account_id = %s)"
        " order by right_code, account_id is null, effective_from desc, right_row_id desc",
        (user_id, day, account_id)).fetchall()
    out: dict[str, Right] = {}
    seen: set[str] = set()
    for code, acct, granted, limit, self_ok, since in rows:   # 계좌 행이 먼저 와요 (false < true)
        if code in seen:
            continue
        seen.add(code)                  # 계좌 행의 회수는 모든 계좌 행의 부여보다 앞서요
        if granted:
            out[code] = Right(user_id, code, acct, limit, self_ok, since)
    return out


def require(conn, user_id: str, right_code: str, account_id: str | None, day: dt.date) -> Right:
    r = rights_of(conn, user_id, account_id, day).get(right_code)
    if r is None:
        where = f" (계좌 {account_id})" if account_id else ""
        raise ApprovalError(f"{user_id}에게 {RIGHTS[right_code]} 권한이 없어요{where}")
    return r


# ---------- 요청 ----------

@dataclass(frozen=True)
class Line:
    """화면의 입력 한 줄이나 명령의 주문 한 건."""
    symbol: str
    side: Side
    qty: int
    limit_price: int | None = None   # None = 시장가


@dataclass(frozen=True)
class Request:
    request_id: str
    account_id: str
    trade_date: dt.date
    batch_id: str | None
    line_no: int
    symbol: str
    side: Side
    qty: int
    limit_price: int | None
    created_by: str
    status: str
    approved_by: str | None = None
    self_approved: bool = False
    client_id: str | None = None
    last_reason: str | None = None


_COLS = ("request_id, account_id, trade_date, batch_id, line_no, symbol, side, qty, limit_price, created_by,"
         " status, approved_by, coalesce(self_approved, false), client_id, last_reason")


def requests(conn: psycopg.Connection, *, account_id: str | None = None, trade_date: dt.date | None = None,
             from_date: dt.date | None = None, statuses=None, ids=None) -> list[Request]:
    where, args = [], []
    for cond, val in (("account_id = %s", account_id), ("trade_date = %s", trade_date),
                      ("trade_date >= %s", from_date), ("status = any(%s)", list(statuses) if statuses else None),
                      ("request_id = any(%s)", list(ids) if ids else None)):
        if val is not None:
            where.append(cond)
            args.append(val)
    q = f"select {_COLS} from order_requests_v" + (" where " + " and ".join(where) if where else "")
    rows = conn.execute(q + " order by trade_date, request_id", args).fetchall()
    return [Request(*r[:6], Side(r[6]), *r[7:]) for r in rows]


def _lock(conn, account_id: str) -> None:
    """요청·승인·전송은 계좌마다 한 번에 하나 (트랜잭션이 끝나면 풀려요)."""
    conn.execute("select pg_advisory_xact_lock(hashtext('ministp.orders:' || %s))", (account_id,))


def account_state(books: Books, day: dt.date, now: dt.datetime | None = None,
                  prices: dict[str, int] | None = None) -> AccountState:
    """`prices`: 방금 증권사에서 받은 현재가(시장가 주문의 단가). 저장된 현재가보다 앞서요."""
    from .basket import load_portfolio
    state = load_portfolio(books, books.conn, day, now).state()
    return replace(state, active=books.status == "OPEN", prices={**state.prices, **(prices or {})})


def _proposed(state: AccountState, symbol, side, qty, limit, key, group="") -> Proposed:
    return Proposed(symbol, side, qty, limit or state.prices.get(symbol), limit, key=key, group=group or "")


def _from_request(state: AccountState, r: Request) -> Proposed:
    return _proposed(state, r.symbol, r.side, r.qty, r.limit_price, r.request_id, r.batch_id)


def snapshot(result: SetCheck) -> dict:
    """결재 이력에 남기는 그때의 묶음 검증 결과."""
    return {"ok": result.ok, "issues": list(result.issues), "available_cash": result.available_cash,
            "required_cash": result.required_cash,
            "lines": [{"key": ln.item.key, "alone": list(ln.alone), "together": list(ln.together),
                       "post_weight": None if ln.post_weight is None else str(ln.post_weight)} for ln in result.lines]}


def describe(result: SetCheck) -> str:
    """사람이 읽는 거부 이유: 계좌 전체 사유와 처음 막히는 줄."""
    parts = [issue_text(result.issues)] if result.issues else []
    i = result.first_blocked
    if i is not None:
        ln = result.lines[i]
        p = ln.item
        how = " (혼자는 통과, 같이 보내면 실패)" if ln.breaks_set else ""
        parts.append(f"{i + 1}번째 줄 {p.symbol} {'매수' if p.side is Side.BUY else '매도'} {p.qty}주: "
                     f"{issue_text(ln.together)}{how}")
    return "; ".join(parts) or "통과"


def _next_ids(conn, day: dt.date, n: int) -> list[str]:
    last = conn.execute("select max(request_id) from order_requests where request_id like %s",
                        (f"Q{day:%Y%m%d}-%",)).fetchone()[0]
    start = int(last.rsplit("-", 1)[1]) if last else 0
    return [f"Q{day:%Y%m%d}-{start + i:04d}" for i in range(1, n + 1)]


def _next_batch(conn, prefix: str, day: dt.date) -> str:
    last = conn.execute("select max(batch_id) from order_requests where batch_id like %s",
                        (f"{prefix}{day:%Y%m%d}-%",)).fetchone()[0]
    return f"{prefix}{day:%Y%m%d}-{(int(last.rsplit('-', 1)[1]) if last else 0) + 1:02d}"


def submit(books: Books, today: dt.date, trade_date: dt.date, lines: list[Line], *, batch: str | None = None,
           self_approve: bool = False, limits: RiskLimits = RiskLimits(),
           weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None,
           prices: dict[str, int] | None = None) -> tuple[list[str], SetCheck]:
    """결재 요청. 묶음 검증(R-98)을 통과한 줄들만 요청으로 남아요. `batch`(예: "B", 바스켓은 "BK")를 주면
    한 묶음이 돼요. `self_approve`면 같은 자리에서 셀프 승인까지 해요(권한이 있을 때만)."""
    if not lines:
        raise ApprovalError("요청할 주문 줄이 없어요")
    conn, user = books.conn, actor()
    with conn.transaction():
        _lock(conn, books.account_id)
        require(conn, user, "ORDER_WRITE", books.account_id, today)
        expire(conn, books.account_id, today)
        for ln in lines:
            if ln.symbol not in books.instruments:
                raise ApprovalError(f"종목 등록이 필요해요: {ln.symbol}")
        live = requests(conn, account_id=books.account_id, from_date=today, statuses=LIVE)
        state = account_state(books, today, now, prices)
        selected = [_proposed(state, ln.symbol, ln.side, ln.qty, ln.limit_price, f"#{i}", batch or "")
                    for i, ln in enumerate(lines, 1)]
        result = check_set(state, selected, [_from_request(state, r) for r in live], limits, weight_limit)
        if not result.ok:
            raise ApprovalError(f"묶음 검증에 걸려 요청하지 않았어요: {describe(result)}")
        ids = _next_ids(conn, today, len(lines))
        batch_id = _next_batch(conn, batch, today) if batch else None
        for i, (rid, ln, lc) in enumerate(zip(ids, lines, result.lines), 1):
            conn.execute(
                "insert into order_requests (request_id, account_id, trade_date, batch_id, line_no, symbol, side,"
                " qty, limit_price, created_by) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rid, books.account_id, trade_date, batch_id, i, ln.symbol, ln.side.value, ln.qty,
                 ln.limit_price, user))
            conn.execute("insert into order_approvals (request_id, action, actor, set_check) values (%s, 'REQUEST', %s, %s)",
                         (rid, user, Jsonb(snapshot(result) | {"line": i})))
        audit.record(conn, "ORDER_REQUEST", account_id=books.account_id, target=",".join(ids),
                     detail={"trade_date": trade_date, "batch_id": batch_id,
                             "lines": [{"symbol": ln.symbol, "side": ln.side.value, "qty": ln.qty,
                                        "limit_price": ln.limit_price} for ln in lines]})
        if self_approve:
            approve(books, today, ids, limits=limits, weight_limit=weight_limit, now=now, prices=prices)
    return ids, result


def _whole_batches(conn, reqs: list[Request], what: str) -> None:
    """묶음은 전부 아니면 전무: 고른 요청이 속한 묶음의 살아 있는 요청을 모두 골라야 해요."""
    chosen = {r.request_id for r in reqs}
    for b in sorted({r.batch_id for r in reqs if r.batch_id}):
        rest = [r.request_id for r in requests(conn, statuses=LIVE) if r.batch_id == b and r.request_id not in chosen]
        if rest:
            raise ApprovalError(f"묶음 {b}은(는) 함께 {what}해야 해요. 빠진 요청: {', '.join(rest)}")


def _load(conn, account_id: str, ids, statuses, what: str) -> list[Request]:
    ids = list(dict.fromkeys(ids))
    reqs = requests(conn, ids=ids)
    missing = set(ids) - {r.request_id for r in reqs}
    if missing:
        raise ApprovalError(f"없는 요청 번호예요: {', '.join(sorted(missing))}")
    other = [r.request_id for r in reqs if r.account_id != account_id]
    if other:
        raise ApprovalError(f"계좌 {account_id}의 요청이 아니에요: {', '.join(other)}")
    wrong = [f"{r.request_id}({STATUSES[r.status]})" for r in reqs if r.status not in statuses]
    if wrong:
        raise ApprovalError(f"{what}할 수 없는 상태예요: {', '.join(wrong)}")
    order = {rid: i for i, rid in enumerate(ids)}
    reqs.sort(key=lambda r: (r.trade_date, r.batch_id or "", r.line_no, order[r.request_id]))
    return reqs


def approve(books: Books, today: dt.date, ids, *, limits: RiskLimits = RiskLimits(),
            weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None,
            prices: dict[str, int] | None = None) -> SetCheck:
    """고른 요청을 승인해요. 권한(한도, 셀프 승인)과 묶음 검증(고른 요청 + 승인 후 미전송 요청)을 통과해야 해요."""
    conn, user = books.conn, actor()
    with conn.transaction():
        _lock(conn, books.account_id)
        expire(conn, books.account_id, today)
        reqs = _load(conn, books.account_id, ids, ("PENDING",), "승인")
        _whole_batches(conn, reqs, "승인")
        right = require(conn, user, "ORDER_APPROVE", books.account_id, today)
        approved = requests(conn, account_id=books.account_id, from_date=today, statuses=("APPROVED",))
        state = account_state(books, today, now, prices)
        selected = [_from_request(state, r) for r in reqs]
        for r, p in zip(reqs, selected):
            if r.created_by == user and not right.self_approve:
                raise ApprovalError(f"{r.request_id}: 본인이 쓴 요청이에요. 셀프 승인 허용이 없어 다른 승인자가 승인해야 해요")
            if right.max_order_value is not None and p.amount > right.max_order_value:
                raise ApprovalError(f"{r.request_id}: 주문금액 {p.amount:,}원이 1건 승인 한도 "
                                    f"{right.max_order_value:,}원을 넘어요")
        result = check_set(state, selected, [_from_request(state, r) for r in approved], limits, weight_limit)
        if not result.ok:
            raise ApprovalError(f"묶음 검증에 걸려 승인하지 않았어요: {describe(result)}")
        for r in reqs:
            conn.execute("insert into order_approvals (request_id, action, actor, set_check) values (%s, 'APPROVE', %s, %s)",
                         (r.request_id, user, Jsonb(snapshot(result))))
        audit.record(conn, "ORDER_APPROVE", account_id=books.account_id, target=",".join(r.request_id for r in reqs),
                     detail={"self": [r.request_id for r in reqs if r.created_by == user] or None})
    return result


def reject(conn: psycopg.Connection, account_id: str, today: dt.date, ids, reason: str) -> None:
    if not (reason or "").strip():
        raise ApprovalError("반려 사유를 넣어 주세요")
    user = actor()
    with conn.transaction():
        _lock(conn, account_id)
        reqs = _load(conn, account_id, ids, ("PENDING",), "반려")
        _whole_batches(conn, reqs, "반려")
        require(conn, user, "ORDER_APPROVE", account_id, today)
        for r in reqs:
            conn.execute("insert into order_approvals (request_id, action, actor, reason) values (%s, 'REJECT', %s, %s)",
                         (r.request_id, user, reason))
        audit.record(conn, "ORDER_REJECT", account_id=account_id, target=",".join(r.request_id for r in reqs),
                     detail={"reason": reason})


def cancel(conn: psycopg.Connection, account_id: str, ids, reason: str | None = None) -> None:
    """작성자가 전송 전에 거둬요. 전송한 뒤에는 주문 취소(cancel)로 해요."""
    user = actor()
    with conn.transaction():
        _lock(conn, account_id)
        reqs = _load(conn, account_id, ids, LIVE, "취소")
        _whole_batches(conn, reqs, "취소")
        others = [r.request_id for r in reqs if r.created_by != user]
        if others:
            raise ApprovalError(f"작성자만 요청을 취소할 수 있어요: {', '.join(others)}")
        for r in reqs:
            conn.execute("insert into order_approvals (request_id, action, actor, reason) values (%s, 'CANCEL', %s, %s)",
                         (r.request_id, user, reason))
        audit.record(conn, "ORDER_REQUEST_CANCEL", account_id=account_id,
                     target=",".join(r.request_id for r in reqs), detail={"reason": reason})


def expire(conn: psycopg.Connection, account_id: str, today: dt.date, *, through_today: bool = False) -> list[str]:
    """지난 주문일(장 마감 뒤에는 오늘까지)의 결재 대기·승인 요청을 만료해요."""
    stale = [r for r in requests(conn, account_id=account_id, statuses=LIVE)
             if r.trade_date < today or (through_today and r.trade_date == today)]
    with conn.transaction():
        for r in stale:
            conn.execute("insert into order_approvals (request_id, action, reason) values (%s, 'EXPIRE', %s)",
                         (r.request_id, f"주문일 {r.trade_date} 장 마감까지 전송되지 않음"))
    return [r.request_id for r in stale]


# ---------- 전송 (R-101) ----------

@dataclass(frozen=True)
class Sent:
    request_id: str
    client_id: str | None
    detail: str = ""        # 전송 실패 이유


def send_approved(books: Books, session, day: dt.date, *, ids=None, limits: RiskLimits = RiskLimits(),
                  weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None) -> list[Sent]:
    """오늘 승인된 요청을 보내요. 보내기 직전에 묶음 검증을 다시 하고(막힌 묶음은 통째로 전송 실패),
    통과한 줄은 주문 경로(`session.submit`, R-36~38)로 한 줄씩 보내요."""
    from .session import failure_text
    conn = books.conn
    conn.execute("select pg_advisory_lock(hashtext('ministp.orders:' || %s))", (books.account_id,))
    try:
        reqs = [r for r in requests(conn, account_id=books.account_id, trade_date=day, statuses=("APPROVED",))
                if ids is None or r.request_id in ids]
        if not reqs:
            return []
        reqs.sort(key=lambda r: (r.batch_id or "", r.line_no, r.request_id))
        quoted = {}
        for sym in {r.symbol for r in reqs if r.limit_price is None}:   # 시장가는 지금 가격으로 검증해요
            try:
                quoted[sym] = session.broker.get_price(sym)
            except Exception:     # 가격을 못 받으면 저장된 현재가로, 그것도 없으면 NO_PRICE로 걸러요
                pass
        state = account_state(books, day, now, quoted)
        result = check_set(state, [_from_request(state, r) for r in reqs], [], limits, weight_limit)
        out: list[Sent] = []
        current: list[Request] = []
        orig = session.on_submit

        def on_submit(cid, d, symbol, side, qty, limit_price):
            with conn.transaction():   # 주문 의도와 전송 기록을 함께, 증권사에 보내기 전에
                if orig:
                    orig(cid, d, symbol, side, qty, limit_price)
                conn.execute("insert into order_approvals (request_id, action, client_id, set_check)"
                             " values (%s, 'SEND', %s, %s)", (current[0].request_id, cid, Jsonb(snapshot(result))))

        def failed(r: Request, why: str) -> None:
            with conn.transaction():
                conn.execute("insert into order_approvals (request_id, action, reason, set_check)"
                             " values (%s, 'SEND_FAILED', %s, %s)", (r.request_id, why, Jsonb(snapshot(result))))
                audit.record(conn, "ORDER", "REFUSED", account_id=books.account_id, target=r.request_id,
                             message=why, detail={"symbol": r.symbol, "side": r.side.value, "qty": r.qty,
                                                  "limit_price": r.limit_price, "request_id": r.request_id})
            out.append(Sent(r.request_id, None, why))

        # 계좌 전체 사유 가운데 현금 합계는 줄마다 쌓아 본 결과(앞 줄을 보냈다고 친 검사)로 갈음해요:
        # 앞 줄은 나가고 모자라는 줄부터 멈춰요. 킬 스위치와 계좌 상태는 모든 줄을 막아요.
        hard = tuple(i for i in result.issues if i != "INSUFFICIENT_CASH")
        session.on_submit = on_submit
        try:
            for r, ln in zip(reqs, result.lines):
                if ln.blocked or hard:
                    failed(r, f"보내기 직전 묶음 검증: {issue_text(hard + ln.together)}")
                    continue
                current[:] = [r]
                before = len(session.anomalies)
                cid = session.submit(day, r.symbol, r.side, r.qty, r.limit_price)
                if cid is not None:
                    out.append(Sent(r.request_id, cid))
                elif requests(conn, ids=[r.request_id])[0].status == "SENT":   # 의도는 남았고 증권사가 거부
                    out.append(Sent(r.request_id, None, failure_text(session.anomalies[-1].detail)))
                else:
                    why = session.anomalies[-1].detail if len(session.anomalies) > before else "주문 경로 거부"
                    with conn.transaction():
                        conn.execute("insert into order_approvals (request_id, action, reason, set_check)"
                                     " values (%s, 'SEND_FAILED', %s, %s)",
                                     (r.request_id, failure_text(why), Jsonb(snapshot(result))))
                    out.append(Sent(r.request_id, None, failure_text(why)))
        finally:
            session.on_submit = orig
        return out
    finally:
        conn.execute("select pg_advisory_unlock(hashtext('ministp.orders:' || %s))", (books.account_id,))
