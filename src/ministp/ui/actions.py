"""처리 트랜잭션: the few things a button on a screen may change, by id.

A button never writes by itself. It names one of these, and each one calls the
same command function the CLI calls, so a screen and the console can never
disagree about a rule. The server runs it on a short read-write connection that
holds the single-writer lock for the length of the call (so it waits its turn
behind `run`), inside one DB transaction. Every other screen request stays on
the read-only session.

IDs follow the read transactions: `STP<screen>U<nn>` (R = 조회, U = 처리).
"""
import datetime as dt
from dataclasses import dataclass

import psycopg

from .. import accounts, approval, codes
from ..calendar import BusinessCalendar
from ..fees import FeeSchedule
from ..ops import OpsError, load_books, resolve_break
from ..queries import CHART_GROUP
from ..recon import Cause
from ..store import DEFAULT_ACCOUNT
from .transactions import COERCE, parse_lines, picked_keys


class ActionError(ValueError):
    pass


@dataclass
class ActionContext:
    conn: psycopg.Connection      # read-write, holding the writer lock
    calendar: BusinessCalendar
    fees: FeeSchedule
    today: dt.date
    account_id: str = DEFAULT_ACCOUNT   # the account the screen is looking at (계좌 선택)


@dataclass(frozen=True)
class Action:
    tx_id: str
    title: str
    params: dict[str, str]        # name -> date | text | int | bool
    run: object                   # (ActionContext, params) -> the sentence shown when it is done
    optional: tuple[str, ...] = ()  # parameters that may be left empty; the rest are required
    audit: str = ""               # the action code of its audit row (audit.ACTIONS)
    target: tuple[str, ...] = ()  # the parameters that name what it acts on, joined with a dot
    account: str | None = None    # the parameter that is an account id, when the action is about one account
    # 원장에 쓰는 처리는 단일 작성자 잠금을 잡고 `run` 뒤에서 차례를 기다려요. 결재는 원장에 쓰지 않고 계좌별 DB 잠금으로
    # 차례를 지켜서, 장중 `run`이 떠 있어도 바로 돼요 (CLI의 request·user·right 명령과 같아요).
    writer: bool = True

    def named(self, raw: dict):
        """This request as the audit log names it, from what the screen sent: (action, account, target, detail).
        For the row of a request that was refused; one that goes through is recorded by the command function."""
        said = {k: str(v)[:200] for k, v in raw.items() if k in self.params and v not in (None, "")}
        target = ".".join(said[k] for k in self.target if k in said) or None
        return self.audit, said.get(self.account) if self.account else None, target, said

    def required(self, name: str) -> bool:
        return name not in self.optional


def _resolve(c: ActionContext, p) -> str:
    try:
        cause = Cause(p["cause"])
    except ValueError:
        raise ActionError(f"없는 원인 코드예요: {p['cause']}") from None
    rid = resolve_break(c.conn, p["result_id"], cause, p["note"])
    return f"불일치 {p['result_id']}번에 판단을 기록했어요 (새 행 {rid}). 원래 행은 그대로예요."


# ---------- 계좌 관리 (STP-4400), 고객 (STP-1400): the `customer` and `account` commands ----------

def _account_add(c: ActionContext, p) -> str:
    a = accounts.add_account(c.conn, p["account_id"], p["customer_id"], p["alias"],
                             credential_ref=p["credential_ref"], opened_on=c.today)
    return (f"계좌를 등록했어요: {a.account_id} {a.alias} (고객 {a.customer_id}, 모의투자, 개설일 {a.opened_on}). "
            f"키는 이 PC의 환경변수 {a.env_prefix}_*에서 읽어요.")


def _account_status(c: ActionContext, p) -> str:
    a = accounts.set_status(c.conn, p["account_id"], p["status"], p.get("effective_from") or c.today, p["reason"])
    return f"계좌 상태를 바꿨어요: {a.account_id} → {a.status} ({a.status_from}부터). 이전 상태는 이력에 남아 있어요."


def _customer_add(c: ActionContext, p) -> str:
    accounts.add_customer(c.conn, p["customer_id"], p["alias"])
    return f"고객을 등록했어요: {p['customer_id']} {p['alias']}"


# ---------- 코드 관리 (STP-4600): the `code` command ----------

def _common(group_id: str) -> str:
    if group_id == CHART_GROUP:
        raise OpsError("계정과목은 원장 분개가 직접 참조하는 전용 테이블이라 화면에서 바꾸지 않아요.")
    return group_id


def _group_add(c: ActionContext, p) -> str:
    codes.add_group(c.conn, p["group_id"], p["name"], p.get("description", ""))
    return f"코드 그룹을 만들었어요: {p['group_id']} {p['name']}. 이제 이 그룹에 코드를 추가할 수 있어요."


def _code_add(c: ActionContext, p) -> str:
    codes.add_code(c.conn, _common(p["group_id"]), p["code"], p["name"], p.get("description", ""))
    return f"코드를 추가했어요: {p['group_id']} {p['code']} {p['name']}"


def _code_rename(c: ActionContext, p) -> str:
    old = codes.rename(c.conn, _common(p["group_id"]), p["code"], p["name"], p.get("description"))
    return f"코드명을 바꿨어요: {p['group_id']} {p['code']} '{old}' → '{p['name']}'. 코드와 저장된 값은 그대로예요."


IN_USE = {"사용": True, "사용 안 함": False}


def _code_use(c: ActionContext, p) -> str:
    if p["in_use"] not in IN_USE:
        raise ActionError(f"사용 여부는 {', '.join(IN_USE)} 중 하나예요.")
    codes.set_in_use(c.conn, _common(p["group_id"]), p["code"], IN_USE[p["in_use"]])
    return f"사용 여부를 바꿨어요: {p['group_id']} {p['code']} → {p['in_use']}. 코드는 지우지 않아요."


# ---------- 주문 결재 (STP-1210, STP-5100, STP-5200): the `request`, `user` and `right` commands ----------
# 누가 하는지는 감사 로그와 같은 값(화면 서버를 띄운 PC 로그인 이름)이에요. 권한은 버튼이 잠겨 있어도 여기서 다시 봐요.

def _books(c: ActionContext):
    return load_books(c.conn, c.calendar, c.fees, c.account_id)


def _picked_lines(p) -> tuple[list, bool]:
    drafts = {d.key: d for d in parse_lines(p["lines"])}
    keys = picked_keys(p["picked"])
    missing = [k for k in keys if k not in drafts]
    if missing:
        raise ActionError(f"화면에 없는 줄이에요: {', '.join(missing)}. 화면을 새로 고친 뒤 다시 해 주세요")
    chosen = [drafts[k] for k in keys]
    if not chosen:
        raise ActionError("결재 요청할 줄을 체크하세요")
    groups = {d.group for d in chosen}
    if len(groups) > 1:
        raise ActionError("바스켓에서 온 줄(BK)은 바스켓 줄끼리 요청해요")
    return [d.line for d in chosen], groups == {"BK"}


def _numbers(keys) -> str:
    """사람에게는 요청 번호만 보여요. 주문일이 둘 이상 섞이면 날짜도 붙여요."""
    days = {k.trade_date for k in keys}
    return ", ".join(str(k.request_no) if len(days) == 1 else str(k) for k in keys)


def _request(c: ActionContext, p, self_approve: bool) -> str:
    lines, basket = _picked_lines(p)
    now = dt.datetime.combine(c.today, dt.datetime.now().time())
    day = approval.order_day(c.calendar, now)
    keys, _ = approval.submit(_books(c), c.today, day, lines, basket=basket, self_approve=self_approve, now=now)
    if basket:
        head = f"바스켓 1건({len(lines)}종목)을 결재 요청했어요 ({day} 주문): 요청 {_numbers(keys)}"
    else:
        head = f"단건 {len(keys)}건을 결재 요청했어요 ({day} 주문): 요청 {_numbers(keys)}"
    if self_approve:
        return head + ". 셀프 승인까지 했어요. 장중이면 run이 다시 검증하고 바로 보내요."
    return head + ". 승인자가 STP-5100에서 승인하면 run이 보내요."


def _request_send(c: ActionContext, p) -> str:
    return _request(c, p, True)


def _request_only(c: ActionContext, p) -> str:
    return _request(c, p, False)


def _picked_requests(c: ActionContext, p) -> list:
    keys = approval.parse_keys(picked_keys(p["picked"]), c.today)
    if not keys:
        raise ActionError("요청을 체크하세요")
    return keys


def _approve(c: ActionContext, p) -> str:
    keys = _picked_requests(c, p)
    approval.approve(_books(c), c.today, keys)
    return f"{len(keys)}건을 승인했어요: 요청 {_numbers(keys)}. 장중이면 run이 보내기 직전에 한 번 더 검증하고 보내요."


def _reject(c: ActionContext, p) -> str:
    keys = _picked_requests(c, p)
    approval.reject(c.conn, c.account_id, c.today, keys, p["reason"])
    return f"{len(keys)}건을 반려했어요: 요청 {_numbers(keys)}"


def _cancel_requests(c: ActionContext, p) -> str:
    keys = _picked_requests(c, p)
    approval.cancel(c.conn, c.account_id, keys, p.get("reason"), today=c.today)
    return f"{len(keys)}건의 요청을 거뒀어요: 요청 {_numbers(keys)}"


def _user_add(c: ActionContext, p) -> str:
    approval.add_user(c.conn, p["user_id"], p["name"], c.today)
    return f"사용자를 등록했어요: {p['user_id']} {p['name']}. 권한은 오른쪽 권한 이력에서 줘요."


def _grant(c: ActionContext, p) -> str:
    right = p["right_code"]
    approval.grant(c.conn, p["user_id"], right, c.today, account_id=p.get("account_id"),
                   max_order_value=p.get("max_order_value"), self_approve=p.get("self_approve") or False,
                   effective_from=p.get("effective_from"), reason=p.get("reason"))
    return (f"권한을 줬어요: {p['user_id']} {approval.RIGHTS.get(right, right)} ({p.get('account_id') or '모든 계좌'},"
            f" {p.get('effective_from') or c.today}부터). 이전 행은 이력에 그대로 있어요.")


def _revoke(c: ActionContext, p) -> str:
    right = p["right_code"]
    approval.grant(c.conn, p["user_id"], right, c.today, account_id=p.get("account_id"),
                   effective_from=p.get("effective_from"), reason=p.get("reason"), revoke=True)
    return (f"권한을 거뒀어요: {p['user_id']} {approval.RIGHTS.get(right, right)} ({p.get('account_id') or '모든 계좌'},"
            f" {p.get('effective_from') or c.today}부터)")


ACTIONS: dict[str, Action] = {a.tx_id: a for a in [
    Action("STP2100U01", "불일치 원인 판단", {"result_id": "int", "cause": "text", "note": "text"}, _resolve,
           audit="RESOLVE", target=("result_id",)),
    Action("STP4400U01", "계좌 등록", {"account_id": "text", "customer_id": "text", "alias": "text",
                                    "credential_ref": "text"}, _account_add,
           audit="ACCOUNT_ADD", target=("account_id",), account="account_id"),
    Action("STP4400U02", "계좌 상태 변경", {"account_id": "text", "status": "text", "effective_from": "date",
                                       "reason": "text"}, _account_status, optional=("effective_from",),
           audit="ACCOUNT_STATUS", target=("account_id",), account="account_id"),
    Action("STP1400U01", "고객 등록", {"customer_id": "text", "alias": "text"}, _customer_add,
           audit="CUSTOMER_ADD", target=("customer_id",)),
    Action("STP4600U01", "코드 그룹 추가", {"group_id": "text", "name": "text", "description": "text"}, _group_add,
           optional=("description",), audit="CODE_GROUP_ADD", target=("group_id",)),
    Action("STP4600U02", "코드 추가", {"group_id": "text", "code": "text", "name": "text", "description": "text"},
           _code_add, optional=("description",), audit="CODE_ADD", target=("group_id", "code")),
    Action("STP4600U03", "코드명 변경", {"group_id": "text", "code": "text", "name": "text", "description": "text"},
           _code_rename, optional=("description",), audit="CODE_RENAME", target=("group_id", "code")),
    Action("STP4600U04", "코드 사용 여부 변경", {"group_id": "text", "code": "text", "in_use": "text"}, _code_use,
           audit="CODE_USE", target=("group_id", "code")),
    Action("STP1210U01", "결재 요청", {"lines": "text", "picked": "text"}, _request_only,
           audit="ORDER_REQUEST", target=("picked",), writer=False),
    Action("STP1210U02", "결재 요청하고 셀프 승인", {"lines": "text", "picked": "text"}, _request_send,
           audit="ORDER_REQUEST", target=("picked",), writer=False),
    Action("STP5100U01", "승인", {"picked": "text"}, _approve, audit="ORDER_APPROVE", target=("picked",), writer=False),
    Action("STP5100U02", "반려", {"picked": "text", "reason": "text"}, _reject,
           audit="ORDER_REJECT", target=("picked",), writer=False),
    Action("STP5100U03", "요청 취소", {"picked": "text", "reason": "text"}, _cancel_requests, optional=("reason",),
           audit="ORDER_REQUEST_CANCEL", target=("picked",), writer=False),
    Action("STP5200U01", "사용자 등록", {"user_id": "text", "name": "text"}, _user_add,
           audit="USER_ADD", target=("user_id",), writer=False),
    Action("STP5200U02", "권한 부여", {"user_id": "text", "right_code": "text", "account_id": "text",
                                    "max_order_value": "int", "self_approve": "bool", "effective_from": "date",
                                    "reason": "text"}, _grant,
           optional=("account_id", "max_order_value", "self_approve", "effective_from"),
           audit="RIGHT_GRANT", target=("user_id",), account="account_id", writer=False),
    Action("STP5200U03", "권한 회수", {"user_id": "text", "right_code": "text", "account_id": "text",
                                    "effective_from": "date", "reason": "text"}, _revoke,
           optional=("account_id", "effective_from"),
           audit="RIGHT_REVOKE", target=("user_id",), account="account_id", writer=False),
]}


def run_action(tx_id: str, ctx: ActionContext, raw: dict) -> str:
    action = ACTIONS.get(tx_id)
    if action is None:
        raise ActionError(f"없는 처리 트랜잭션: {tx_id}")
    unknown = set(raw) - set(action.params)
    if unknown:
        raise ActionError(f"{tx_id}에 없는 입력 항목: {', '.join(sorted(unknown))}")
    params = {}
    for name, kind in action.params.items():
        v = raw.get(name)
        if isinstance(v, str):
            v = v.strip()
        if v in (None, ""):
            if action.required(name):
                raise ActionError(f"{name} 값을 넣어 주세요")
            continue
        try:
            params[name] = COERCE[kind](v)
        except (TypeError, ValueError) as e:
            raise ActionError(f"{name} 값이 올바르지 않아요: {v}") from e
    return action.run(ctx, params)
