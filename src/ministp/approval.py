"""주문 결재 (스펙 D9, R-99~101): 모든 주문은 요청으로 먼저 남고, 승인된 요청만 `run`이 보내요.

- 권한(R-99): 주문 작성, 주문 승인(1건 한도, 셀프 승인 허용), 권한 관리. 계좌마다 또는 모든 계좌에 줘요.
  권한은 부여·회수마다 한 행이 쌓이고, 오늘 적용되는 최근 행이 지금 권한이에요.
  셀프 승인은 권한에 "셀프 승인 허용"이 있는 사람만, 1건 한도 안에서만 해요. 없는 사람은 요청만 올려요.
- 요청 번호: 키는 주문일 + 요청 번호(주문일마다 1부터, 계좌와 무관). 주문 유형은 단건(한 줄)과 바스켓(여러 줄).
  바스켓은 요청 한 건이라 승인·반려·취소가 요청 전체로 되고, 전송 검증도 전부 아니면 전무예요.
- 흐름(R-100): 요청 → 승인·반려·취소, 그리고 만료(요청 단위). 승인된 요청은 줄마다 전송·전송 실패가 한 번
  남아요(줄 단위). 순서는 DB 트리거가 지켜요.
- 묶음 검증(R-98)을 네 번 해요: 요청할 때(결재 대기와 승인 후 미전송 요청 포함), 승인할 때(고른 요청만,
  승인 후 미전송 요청 포함), `run`이 보내기 직전, 그리고 보낼 때 주문 경로가 한 줄씩 한 번 더(R-36~38).
  요청과 승인은 계좌마다 DB 잠금을 잡고 해서, 두 사람이 동시에 올려도 묶음 검증이 서로를 보고 판단해요.
- 전송(R-101): `run`이 승인된 오늘 요청을 요청 번호·줄 순서대로 보내요. 주문 의도(oms_orders)와 전송 기록은 증권사에
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
ACTIONS = {"REQUEST": "결재 요청", "APPROVE": "승인", "REJECT": "반려", "CANCEL": "요청 취소", "EXPIRE": "만료"}
STATUSES = {"PENDING": "결재 대기", "APPROVED": "승인", "REJECTED": "반려", "CANCELED": "취소",
            "SENT": "전송", "SEND_FAILED": "전송 실패", "EXPIRED": "만료", "SEND_UNKNOWN": "결과 미확인"}
LIVE = ("PENDING", "APPROVED")      # 아직 주문가능금액·매도가능수량을 잡고 있는 요청


class ApprovalError(OpsError):
    pass


def actor() -> str:
    return audit.origin()[1]


ORDER_CUTOFF = dt.time(15, 20)      # 장 마감: 이 뒤에 쓴 요청은 다음 영업일 주문이에요


def order_day(calendar, at: dt.datetime) -> dt.date:
    """요청이 나갈 주문일: 영업일 장 마감 전이면 오늘, 아니면 다음 영업일."""
    today = at.date()
    if calendar.is_business_day(today) and at.time() <= ORDER_CUTOFF:
        return today
    return calendar.add_business_days(today, 1)


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
# 요청 한 건 = 결재 한 건. 키는 주문일 + 요청 번호(주문일마다 1부터, 계좌와 무관, 11/24 사용자).
# 단건은 종목 한 줄, 바스켓은 종목 여러 줄. 승인·반려·취소는 요청 단위, 전송은 줄 단위로 남아요.

@dataclass(frozen=True)
class Line:
    """화면의 입력 한 줄이나 명령의 주문 한 건."""
    symbol: str
    side: Side
    qty: int
    limit_price: int | None = None   # None = 시장가


@dataclass(frozen=True, order=True)
class Key:
    """요청의 키: 주문일과 그날의 요청 번호. 글자로는 `20261120-3`, 사람에게는 요청 번호만 보여요."""
    trade_date: dt.date
    request_no: int

    def __str__(self) -> str:
        return f"{self.trade_date:%Y%m%d}-{self.request_no}"


def parse_key(text: str, default_date: dt.date) -> Key:
    """`3`(주문일은 `default_date`) 또는 `20261120-3`."""
    t = str(text).strip()
    try:
        if "-" in t:
            day, no = t.split("-", 1)
            return Key(dt.datetime.strptime(day, "%Y%m%d").date(), int(no))
        return Key(default_date, int(t))
    except ValueError:
        raise ApprovalError(f"요청 번호는 3 또는 20261120-3 모양이에요: {text}") from None


def parse_keys(texts, default_date: dt.date) -> list[Key]:
    return [k if isinstance(k, Key) else parse_key(k, default_date) for k in texts]


@dataclass(frozen=True)
class RequestLine:
    line_no: int
    symbol: str
    side: Side
    qty: int
    limit_price: int | None
    send_outcome: str | None = None   # SENT / FAILED, 아직 안 보냈으면 None
    client_id: str | None = None
    send_reason: str | None = None
    ack_outcome: str | None = None    # 증권사 답: ACCEPTED / REJECTED, 보냈는데 None이면 결과 미확인 (R-103)
    order_no: str | None = None
    ack_detail: str | None = None

    @property
    def awaiting_ack(self) -> bool:
        return self.send_outcome == "SENT" and self.ack_outcome is None


@dataclass(frozen=True)
class Request:
    trade_date: dt.date
    request_no: int
    account_id: str
    order_type: str                   # SINGLE / BASKET
    created_by: str
    status: str
    approved_by: str | None = None
    self_approved: bool = False
    last_reason: str | None = None
    lines: tuple[RequestLine, ...] = ()

    @property
    def key(self) -> Key:
        return Key(self.trade_date, self.request_no)

    @property
    def unsent(self) -> tuple[RequestLine, ...]:
        return tuple(ln for ln in self.lines if ln.send_outcome is None)


ORDER_TYPES = {"SINGLE": "단건", "BASKET": "바스켓"}
SEND_OUTCOMES = {"SENT": "전송", "FAILED": "전송 실패"}


def requests(conn: psycopg.Connection, *, account_id: str | None = None, trade_date: dt.date | None = None,
             from_date: dt.date | None = None, statuses=None, keys=None) -> list[Request]:
    where, args = [], []
    keys = list(keys) if keys is not None else None
    for cond, val in (("q.account_id = %s", account_id), ("q.trade_date = %s", trade_date),
                      ("q.trade_date >= %s", from_date), ("q.status = any(%s)", list(statuses) if statuses else None)):
        if val is not None:
            where.append(cond)
            args.append(val)
    if keys is not None:
        where.append("(q.trade_date, q.request_no) in (select * from unnest(%s::date[], %s::int[]))")
        args += [[k.trade_date for k in keys], [k.request_no for k in keys]]
    cond = (" where " + " and ".join(where)) if where else ""
    heads = conn.execute(
        "select q.trade_date, q.request_no, q.account_id, q.order_type, q.created_by, q.status, q.approved_by,"
        " coalesce(q.self_approved, false), q.last_reason from order_requests_v q" + cond
        + " order by q.trade_date, q.request_no", args).fetchall()
    lines: dict[tuple, list[RequestLine]] = {}
    for row in conn.execute(
            "select l.trade_date, l.request_no, l.line_no, l.symbol, l.side, l.qty, l.limit_price, l.send_outcome,"
            " l.client_id, l.send_reason, l.ack_outcome, l.order_no, l.ack_detail from order_request_lines_v l join order_requests_v q"
            " using (trade_date, request_no)" + cond + " order by l.trade_date, l.request_no, l.line_no", args):
        lines.setdefault(row[:2], []).append(RequestLine(row[2], row[3], Side(row[4]), *row[5:]))
    return [Request(*h, lines=tuple(lines.get(h[:2], ()))) for h in heads]


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


def _from_line(state: AccountState, r: Request, ln: RequestLine) -> Proposed:
    """바스켓의 줄은 요청 번호로 묶여요: 한 줄이 막히면 요청 전체가 막혀요(전부 아니면 전무)."""
    return _proposed(state, ln.symbol, ln.side, ln.qty, ln.limit_price, f"{r.key}:{ln.line_no}",
                     str(r.key) if r.order_type == "BASKET" else "")


def _held(state: AccountState, reqs: list[Request]) -> list[Proposed]:
    """이미 잡힌 요청: 아직 보내지 않은 줄만 (보낸 줄은 미체결 주문으로 잡혀 있어요)."""
    return [_from_line(state, r, ln) for r in reqs for ln in r.unsent]


def snapshot(result: SetCheck) -> dict:
    """결재·전송 이력에 남기는 그때의 묶음 검증 결과."""
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


def _next_no(conn, trade_date: dt.date) -> int:
    """주문일의 다음 요청 번호. 계좌와 무관하게 한 줄로 세서 같은 날 번호가 겹치지 않아요."""
    conn.execute("select pg_advisory_xact_lock(hashtext('ministp.request_no:' || %s))", (str(trade_date),))
    return conn.execute("select coalesce(max(request_no), 0) + 1 from order_requests where trade_date = %s",
                        (trade_date,)).fetchone()[0]


def submit(books: Books, today: dt.date, trade_date: dt.date, lines: list[Line], *, basket: bool = False,
           self_approve: bool = False, limits: RiskLimits = RiskLimits(),
           weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None,
           prices: dict[str, int] | None = None) -> tuple[list[Key], SetCheck]:
    """결재 요청. 묶음 검증(R-98)을 통과해야 요청으로 남아요. `basket`이면 모든 줄이 요청 한 건(바스켓),
    아니면 줄마다 단건 요청 한 건이에요. `self_approve`면 같은 자리에서 셀프 승인까지 해요(권한이 있을 때만)."""
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
        selected = [_proposed(state, ln.symbol, ln.side, ln.qty, ln.limit_price, f"#{i}", "new" if basket else "")
                    for i, ln in enumerate(lines, 1)]
        result = check_set(state, selected, _held(state, live), limits, weight_limit)
        if not result.ok:
            raise ApprovalError(f"묶음 검증에 걸려 요청하지 않았어요: {describe(result)}")
        groups = [list(enumerate(lines, 1))] if basket else [[(1, ln)] for ln in lines]
        keys: list[Key] = []
        no = _next_no(conn, trade_date)
        for group in groups:
            key = Key(trade_date, no)
            no += 1
            conn.execute("insert into order_requests (trade_date, request_no, account_id, order_type, created_by)"
                         " values (%s, %s, %s, %s, %s)",
                         (key.trade_date, key.request_no, books.account_id, "BASKET" if basket else "SINGLE", user))
            for line_no, ln in group:
                conn.execute(
                    "insert into order_request_lines (trade_date, request_no, line_no, symbol, side, qty, limit_price)"
                    " values (%s, %s, %s, %s, %s, %s, %s)",
                    (key.trade_date, key.request_no, line_no, ln.symbol, ln.side.value, ln.qty, ln.limit_price))
            conn.execute("insert into order_approvals (trade_date, request_no, action, actor, set_check)"
                         " values (%s, %s, 'REQUEST', %s, %s)", (key.trade_date, key.request_no, user,
                                                                 Jsonb(snapshot(result))))
            keys.append(key)
        audit.record(conn, "ORDER_REQUEST", account_id=books.account_id, target=",".join(map(str, keys)),
                     detail={"trade_date": trade_date, "order_type": "BASKET" if basket else "SINGLE",
                             "lines": [{"symbol": ln.symbol, "side": ln.side.value, "qty": ln.qty,
                                        "limit_price": ln.limit_price} for ln in lines]})
        if self_approve:
            approve(books, today, keys, limits=limits, weight_limit=weight_limit, now=now, prices=prices)
    return keys, result


def _load(conn, account_id: str, keys, statuses, what: str) -> list[Request]:
    keys = list(dict.fromkeys(keys))
    reqs = requests(conn, keys=keys)
    missing = set(keys) - {r.key for r in reqs}
    if missing:
        raise ApprovalError(f"없는 요청 번호예요: {', '.join(map(str, sorted(missing)))}")
    other = [str(r.key) for r in reqs if r.account_id != account_id]
    if other:
        raise ApprovalError(f"계좌 {account_id}의 요청이 아니에요: {', '.join(other)}")
    wrong = [f"{r.key}({STATUSES[r.status]})" for r in reqs if r.status not in statuses]
    if wrong:
        raise ApprovalError(f"{what}할 수 없는 상태예요: {', '.join(wrong)}")
    order = {k: i for i, k in enumerate(keys)}
    reqs.sort(key=lambda r: (r.trade_date, order[r.key]))
    return reqs


def _approval_row(conn, r: Request, action: str, user: str | None, reason=None, check=None) -> None:
    conn.execute("insert into order_approvals (trade_date, request_no, action, actor, reason, set_check)"
                 " values (%s, %s, %s, %s, %s, %s)",
                 (r.trade_date, r.request_no, action, user, reason, Jsonb(check or {})))


def approve(books: Books, today: dt.date, keys, *, limits: RiskLimits = RiskLimits(),
            weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None,
            prices: dict[str, int] | None = None) -> SetCheck:
    """고른 요청을 승인해요. 권한(한도, 셀프 승인)과 묶음 검증(고른 요청 + 승인 후 미전송 줄)을 통과해야 해요."""
    conn, user = books.conn, actor()
    keys = parse_keys(keys, today)
    with conn.transaction():
        _lock(conn, books.account_id)
        expire(conn, books.account_id, today)
        reqs = _load(conn, books.account_id, keys, ("PENDING",), "승인")
        right = require(conn, user, "ORDER_APPROVE", books.account_id, today)
        approved = requests(conn, account_id=books.account_id, from_date=today, statuses=("APPROVED",))
        state = account_state(books, today, now, prices)
        selected = _held(state, reqs)
        for r in reqs:
            if r.created_by == user and not right.self_approve:
                raise ApprovalError(f"요청 {r.request_no}: 본인이 쓴 요청이에요. 셀프 승인 허용이 없어 다른 승인자가 승인해야 해요")
        for p in selected:
            if right.max_order_value is not None and p.amount > right.max_order_value:
                raise ApprovalError(f"요청 {p.key.split(':')[0]} {p.symbol}: 주문금액 {p.amount:,}원이 1건 승인 한도 "
                                    f"{right.max_order_value:,}원을 넘어요")
        result = check_set(state, selected, _held(state, approved), limits, weight_limit)
        if not result.ok:
            raise ApprovalError(f"묶음 검증에 걸려 승인하지 않았어요: {describe(result)}")
        for r in reqs:
            _approval_row(conn, r, "APPROVE", user, check=snapshot(result))
        audit.record(conn, "ORDER_APPROVE", account_id=books.account_id, target=",".join(str(r.key) for r in reqs),
                     detail={"self": [str(r.key) for r in reqs if r.created_by == user] or None})
    return result


def reject(conn: psycopg.Connection, account_id: str, today: dt.date, keys, reason: str) -> None:
    if not (reason or "").strip():
        raise ApprovalError("반려 사유를 넣어 주세요")
    user = actor()
    keys = parse_keys(keys, today)
    with conn.transaction():
        _lock(conn, account_id)
        reqs = _load(conn, account_id, keys, ("PENDING",), "반려")
        require(conn, user, "ORDER_APPROVE", account_id, today)
        for r in reqs:
            _approval_row(conn, r, "REJECT", user, reason)
        audit.record(conn, "ORDER_REJECT", account_id=account_id, target=",".join(str(r.key) for r in reqs),
                     detail={"reason": reason})


def cancel(conn: psycopg.Connection, account_id: str, keys, reason: str | None = None,
           today: dt.date | None = None) -> None:
    """작성자가 전송 전에 거둬요. 한 줄이라도 나간 요청은 주문 취소(cancel)로 해요."""
    user = actor()
    keys = parse_keys(keys, today or dt.date.today())
    with conn.transaction():
        _lock(conn, account_id)
        reqs = _load(conn, account_id, keys, LIVE, "취소")
        others = [str(r.request_no) for r in reqs if r.created_by != user]
        if others:
            raise ApprovalError(f"작성자만 요청을 취소할 수 있어요: {', '.join(others)}")
        started = [str(r.request_no) for r in reqs if len(r.unsent) < len(r.lines)]
        if started:
            raise ApprovalError(f"이미 나간 줄이 있어 요청 취소가 안 돼요(주문 취소로 해요): {', '.join(started)}")
        for r in reqs:
            _approval_row(conn, r, "CANCEL", user, reason)
        audit.record(conn, "ORDER_REQUEST_CANCEL", account_id=account_id,
                     target=",".join(str(r.key) for r in reqs), detail={"reason": reason})


def expire(conn: psycopg.Connection, account_id: str, today: dt.date, *, through_today: bool = False) -> list[str]:
    """지난 주문일(장 마감 뒤에는 오늘까지)의 결재 대기·승인 요청을 만료해요. 한 줄이라도 나간 요청은 그대로 둬요."""
    stale = [r for r in requests(conn, account_id=account_id, statuses=LIVE)
             if (r.trade_date < today or (through_today and r.trade_date == today)) and len(r.unsent) == len(r.lines)]
    with conn.transaction():
        for r in stale:
            _approval_row(conn, r, "EXPIRE", None, f"주문일 {r.trade_date} 장 마감까지 전송되지 않음")
    return [str(r.key) for r in stale]


# ---------- 전송 (R-101) ----------

@dataclass(frozen=True)
class Sent:
    key: Key
    line_no: int
    symbol: str
    client_id: str | None
    detail: str = ""        # 전송 실패 이유, 또는 결과 미확인 내용
    unknown: bool = False   # R-103: 보냈지만 답이 없음. 찾을 때까지 다음 줄을 보내지 않아요

    def __str__(self) -> str:
        return f"요청 {self.key.request_no}-{self.line_no} {self.symbol}"


def send_approved(books: Books, session, day: dt.date, *, keys=None, limits: RiskLimits = RiskLimits(),
                  weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, now: dt.datetime | None = None) -> list[Sent]:
    """오늘 승인된 요청의 보내지 않은 줄을 보내요. 보내기 직전에 묶음 검증을 다시 하고(막힌 바스켓은 통째로
    전송 실패), 통과한 줄은 주문 경로(`session.submit`, R-36~38)로 한 줄씩 보내요.

    R-103: 증권사가 거부한 줄은 그 줄만 실패이고 다음 줄은 계속 보내요(줄마다 앞에서 다시 검증했어요). 답이 없는 줄이
    생기면 거기서 멈추고, 그 주문을 증권사 주문내역에서 찾거나 사람이 확인할 때까지 이 계좌는 아무것도 보내지 않아요.
    어느 경우에도 같은 줄을 저절로 다시 보내지 않아요."""
    from .session import failure_text
    conn = books.conn
    keys = None if keys is None else set(parse_keys(keys, day))
    if session.unacked:      # R-103: 답이 없는 주문이 있으면 찾을 때까지 아무것도 보내지 않아요
        return []
    conn.execute("select pg_advisory_lock(hashtext('ministp.orders:' || %s))", (books.account_id,))
    try:
        reqs = [r for r in requests(conn, account_id=books.account_id, trade_date=day, statuses=("APPROVED",))
                if keys is None or r.key in keys]
        todo = [(r, ln) for r in reqs for ln in r.unsent]
        if not todo:
            return []
        quoted = {}
        for sym in {ln.symbol for _, ln in todo if ln.limit_price is None}:   # 시장가는 지금 가격으로 검증해요
            try:
                quoted[sym] = session.broker.get_price(sym)
            except Exception:     # 가격을 못 받으면 저장된 현재가로, 그것도 없으면 NO_PRICE로 걸러요
                pass
        state = account_state(books, day, now, quoted)
        result = check_set(state, [_from_line(state, r, ln) for r, ln in todo], [], limits, weight_limit)
        out: list[Sent] = []
        current: list = []
        orig = session.on_submit

        def send_row(r: Request, ln: RequestLine, outcome: str, cid=None, why=None) -> None:
            conn.execute("insert into order_sends (trade_date, request_no, line_no, outcome, client_id, reason, set_check)"
                         " values (%s, %s, %s, %s, %s, %s, %s)",
                         (r.trade_date, r.request_no, ln.line_no, outcome, cid, why, Jsonb(snapshot(result))))

        def on_submit(cid, d, symbol, side, qty, limit_price):
            with conn.transaction():   # 주문 의도와 전송 기록을 함께, 증권사에 보내기 전에
                if orig:
                    orig(cid, d, symbol, side, qty, limit_price)
                send_row(*current, "SENT", cid)

        def failed(r: Request, ln: RequestLine, why: str, refused: bool = False) -> None:
            with conn.transaction():
                send_row(r, ln, "FAILED", why=why)
                if refused:
                    audit.record(conn, "ORDER", "REFUSED", account_id=books.account_id, target=f"{r.key}:{ln.line_no}",
                                 message=why, detail={"symbol": ln.symbol, "side": ln.side.value, "qty": ln.qty,
                                                      "limit_price": ln.limit_price, "request": str(r.key),
                                                      "line_no": ln.line_no})
            out.append(Sent(r.key, ln.line_no, ln.symbol, None, why))

        # 계좌 전체 사유 가운데 현금 합계는 줄마다 쌓아 본 결과(앞 줄을 보냈다고 친 검사)로 갈음해요:
        # 앞 줄은 나가고 모자라는 줄부터 멈춰요. 킬 스위치와 계좌 상태는 모든 줄을 막아요.
        hard = tuple(i for i in result.issues if i != "INSUFFICIENT_CASH")
        session.on_submit = on_submit
        try:
            for (r, ln), lc in zip(todo, result.lines):
                if lc.blocked or hard:
                    failed(r, ln, f"보내기 직전 묶음 검증: {issue_text(hard + lc.together)}", refused=True)
                    continue
                current[:] = [r, ln]
                before = len(session.anomalies)
                cid = session.submit(day, ln.symbol, ln.side, ln.qty, ln.limit_price)
                if cid is not None and cid in session.unacked:
                    # R-103: 답이 없어요. 증권사에 있을 수도 있으니 다시 보내지 않고, 남은 줄도 멈춰요.
                    out.append(Sent(r.key, ln.line_no, ln.symbol, cid, session.anomalies[-1].detail, unknown=True))
                    break
                if cid is not None:
                    out.append(Sent(r.key, ln.line_no, ln.symbol, cid))
                    continue
                again = conn.execute("select outcome from order_sends where trade_date = %s and request_no = %s"
                                     " and line_no = %s", (r.trade_date, r.request_no, ln.line_no)).fetchone()
                if again:             # 의도는 남았고 증권사가 거부
                    out.append(Sent(r.key, ln.line_no, ln.symbol, None, failure_text(session.anomalies[-1].detail)))
                else:
                    why = session.anomalies[-1].detail if len(session.anomalies) > before else "주문 경로 거부"
                    failed(r, ln, failure_text(why))
        finally:
            session.on_submit = orig
        return out
    finally:
        conn.execute("select pg_advisory_unlock(hashtext('ministp.orders:' || %s))", (books.account_id,))
