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

from ..calendar import BusinessCalendar
from ..fees import FeeSchedule
from ..ops import resolve_break
from ..recon import Cause
from .transactions import COERCE


class ActionError(ValueError):
    pass


@dataclass
class ActionContext:
    conn: psycopg.Connection      # read-write, holding the writer lock
    calendar: BusinessCalendar
    fees: FeeSchedule
    today: dt.date


@dataclass(frozen=True)
class Action:
    tx_id: str
    title: str
    params: dict[str, str]        # name -> date | text | int | bool
    run: object                   # (ActionContext, params) -> the sentence shown when it is done
    optional: tuple[str, ...] = ()  # parameters that may be left empty; the rest are required

    def required(self, name: str) -> bool:
        return name not in self.optional


def _resolve(c: ActionContext, p) -> str:
    try:
        cause = Cause(p["cause"])
    except ValueError:
        raise ActionError(f"없는 원인 코드예요: {p['cause']}") from None
    rid = resolve_break(c.conn, p["result_id"], cause, p["note"])
    return f"불일치 {p['result_id']}번에 판단을 기록했어요 (새 행 {rid}). 원래 행은 그대로예요."


ACTIONS: dict[str, Action] = {a.tx_id: a for a in [
    Action("STP2100U01", "불일치 원인 판단", {"result_id": "int", "cause": "text", "note": "text"}, _resolve),
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
