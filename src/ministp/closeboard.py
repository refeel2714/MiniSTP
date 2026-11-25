"""마감 점검: what one business day's work left behind, step by step.

`daily` does its steps in order (결제 → 체결 반영 → 평가 → 기준가 → 대사 → 일 마감 → 원장 검증) and prints the
result once. This module reads the same steps back from what was recorded, for any day, so the question
"is this day finished, and if not, what is it waiting for" has one answer on one screen. Each step
names the screen that shows its detail. Nothing is fetched.

The steps are kept in the database (`close_steps`, ERD v2.12, R-105): name, order, in use, required, screen and
the asset type a step is for. What each step looks at is code (one check per step code, in `board`), so a
step can be renamed, reordered, switched off or made optional without a release, but a new kind of check
needs one. An optional step shows its state and does not hold the day. `record` keeps what a check found
(`close_checks`, `close_check_steps`), so a day's answer at the time it was checked stays after the books move on.

The states: 완료 · 대상 없음 (nothing to do that day) · 대기 (today, not run yet) · 주의 (done, worth a look) ·
확인 필요 (someone has to decide) · 누락 (the day has passed and the step never ran).
"""
import datetime as dt
from dataclasses import dataclass, field, replace

import psycopg

from . import audit
from .ledger import ExecutionEvent, Ledger, book_date

DONE, NONE, WAIT, WARN, BAD, MISSING = "완료", "대상 없음", "대기", "주의", "확인 필요", "누락"
DAY_DONE, DAY_OPEN, DAY_BAD = "점검 완료", "진행 중", "확인 필요"
STATE_CODES = {DONE: "DONE", NONE: "NONE", WAIT: "WAIT", WARN: "WARN", BAD: "BAD", MISSING: "MISSING"}
DAY_STATE_CODES = {DAY_DONE: "DONE", DAY_OPEN: "OPEN", DAY_BAD: "ATTENTION"}
_FINISHED = (DONE, NONE, WARN)


@dataclass(frozen=True)
class StepDef:
    """One row of `close_steps`: how a step is shown. Its check is `CHECKS[code]`."""
    code: str
    name: str
    sort_order: int
    in_use: bool = True
    required: bool = True
    screen: str | None = None
    asset_type: str | None = None      # only for accounts that traded or held this asset type
    description: str = ""


# The steps a new database gets (schema.sql and migration 0012 insert the same rows).
STEP_DEFAULTS: tuple[StepDef, ...] = (
    StepDef("SETTLEMENT", "결제", 1, screen="STP-3400", description="그날 결제일인 체결이 모두 결제됐는지"),
    StepDef("ORDERS", "주문·체결", 2, screen="STP-1200", description="낸 주문마다 접수·체결·결제 진행"),
    StepDef("DIVIDENDS", "배당", 3, screen="STP-3200", description="그날 인식·입금할 배당이 기록됐는지 (R-57~59)"),
    StepDef("PRICES", "시세", 4, screen="STP-2300", description="보유 종목 종가를 받았고 크게 튄 값이 없는지"),
    StepDef("NAV", "기준가", 5, screen="STP-2210", description="기준가를 냈고 순자산 변동이 분개로 설명되는지"),
    StepDef("RECON", "증권사 대사", 6, screen="STP-2100", description="일 마감 대사의 불일치를 모두 판단했는지"),
    StepDef("DAY_CLOSE", "일 마감", 7, screen="STP-2200", description="장부를 그날까지 마감했는지 (R-77)"),
    StepDef("ERP_EXPORT", "ERP 전송", 8, required=False,
            description="그날 전표를 ERP로 내보냈는지 (R-104). 기본은 선택 단계"),
    StepDef("INTEGRITY", "원장 내부 대사", 9, screen="STP-2110", description="지금 원장이 이벤트를 다시 계산한 결과와 같은지"),
)
STEP_CODES = tuple(s.code for s in STEP_DEFAULTS)
STEPS = tuple(s.name for s in STEP_DEFAULTS)
SCREENS = tuple(dict.fromkeys(s.screen for s in STEP_DEFAULTS if s.screen))   # where a step may lead (STP-2400 drill)


@dataclass(frozen=True)
class Step:
    no: int
    name: str
    state: str
    detail: str
    screen: str     # where the detail of this step is ("" when none)
    code: str = ""
    required: bool = True


@dataclass(frozen=True)
class Outside:
    """What the ledger alone does not know: the orders sent, the recon runs, the integrity checks,
    the dividends the register says are due, the days already sent to the ERP, the asset types held or traded."""
    orders: dict        # date -> [(qty, outcome, order_no)]
    recon: dict         # date -> ops.ReconDay (the day's latest 일 마감 대사)
    checks: int         # integrity checks run on the books as they are now
    breaks: int         # of those, failing
    dividends: list = field(default_factory=list)     # (accrued_on, pay_date, paid_on, status)
    erp_days: frozenset = frozenset()                  # dates of this account in an export not voided
    asset_types: frozenset | None = None               # None: unknown, every step applies


def day_state(steps: list[Step]) -> str:
    states = {s.state for s in steps if s.required}
    return DAY_BAD if states & {BAD, MISSING} else DAY_OPEN if WAIT in states else DAY_DONE


def finished(steps: list[Step]) -> int:
    return sum(s.state in _FINISHED for s in steps if s.required)


def required(steps: list[Step]) -> int:
    return sum(s.required for s in steps)


def needing(steps: list[Step]) -> int:
    return sum(s.state in (BAD, MISSING) for s in steps if s.required)


def _integrity(outside: Outside) -> tuple[str, str]:
    """The books as they are now, not as they were that day: a damaged row has no date."""
    if outside.breaks:
        return BAD, f"지금 원장 기준으로 점검 {outside.checks}개 중 {outside.breaks}개가 맞지 않아요"
    return DONE, f"지금 원장 기준으로 점검 {outside.checks}개가 모두 일치해요"


def integrity_step(outside: Outside, steps: tuple[StepDef, ...] = STEP_DEFAULTS) -> Step:
    """The one row a board can show when the events cannot be replayed."""
    shown = [s for s in steps if s.in_use]
    d = next((s for s in shown if s.code == "INTEGRITY"), None) or next(s for s in STEP_DEFAULTS if s.code == "INTEGRITY")
    no = shown.index(d) + 1 if d in shown else 1
    return Step(no, d.name, *_integrity(outside), d.screen or "", d.code, d.required)


# ---------- the steps in the database (R-105) ----------

def load_steps(conn: psycopg.Connection) -> tuple[StepDef, ...]:
    """The steps as `close_steps` holds them, in order. The defaults on a database before ERD v2.12."""
    if not conn.execute("select to_regclass('close_steps') is not null").fetchone()[0]:
        return STEP_DEFAULTS
    rows = conn.execute("select step_code, name, sort_order, in_use, required, screen, asset_type, description"
                        " from close_steps order by sort_order, step_code").fetchall()
    known = [StepDef(*r) for r in rows if r[0] in STEP_CODES]
    return tuple(known) or STEP_DEFAULTS


def master_problems(conn: psycopg.Connection) -> list[str]:
    """Where `close_steps` and the program disagree: a step without its check, a check without its row."""
    if not conn.execute("select to_regclass('close_steps') is not null").fetchone()[0]:
        return []
    have = {c for (c,) in conn.execute("select step_code from close_steps")}
    out = [f"CLOSE_STEP: 점검 로직이 없는 단계 {c}" for c in sorted(have - set(STEP_CODES))]
    out += [f"CLOSE_STEP: 단계 기준에 없는 점검 {c}" for c in STEP_CODES if c not in have]
    return out


class CloseStepError(ValueError):
    pass


def set_step(conn: psycopg.Connection, code: str, *, name: str | None = None, sort_order: int | None = None,
             in_use: bool | None = None, required: bool | None = None, screen: str | None = None,
             asset_type: str | None = None, description: str | None = None) -> StepDef:
    """`close-step set`: change how a step is shown (R-105). `screen=""` / `asset_type=""` clear the value.
    The audit row keeps the values before and after."""
    from .instruments import AssetType
    with conn.transaction():
        row = conn.execute("select step_code, name, sort_order, in_use, required, screen, asset_type, description"
                           " from close_steps where step_code = %s for update", (code,)).fetchone()
        if row is None:
            raise CloseStepError(f"마감 점검 단계 {code}가 없어요. close-step list로 확인하세요: {', '.join(STEP_CODES)}")
        before = StepDef(*row)
        after = before
        if name is not None:
            if not name.strip():
                raise CloseStepError("단계 이름을 비울 수 없어요.")
            after = replace(after, name=name.strip())
        if sort_order is not None:
            if sort_order <= 0:
                raise CloseStepError("순서는 1 이상이에요.")
            after = replace(after, sort_order=sort_order)
        if in_use is not None:
            after = replace(after, in_use=in_use)
        if required is not None:
            after = replace(after, required=required)
        if screen is not None:
            if screen and screen not in SCREENS:
                raise CloseStepError(f"마감 점검에서 열 수 있는 화면이 아니에요: {screen} ({', '.join(SCREENS)})")
            after = replace(after, screen=screen or None)
        if asset_type is not None:
            if asset_type and asset_type not in {a.value for a in AssetType}:
                raise CloseStepError(f"자산 유형 {asset_type}이 없어요: {', '.join(a.value for a in AssetType)}")
            after = replace(after, asset_type=asset_type or None)
        if description is not None:
            after = replace(after, description=description.strip())
        if after == before:
            raise CloseStepError("바뀐 값이 없어요.")
        conn.execute("update close_steps set name = %s, sort_order = %s, in_use = %s, required = %s, screen = %s,"
                     " asset_type = %s, description = %s where step_code = %s",
                     (after.name, after.sort_order, after.in_use, after.required, after.screen, after.asset_type,
                      after.description, code))
        changed = {k: [getattr(before, k), getattr(after, k)] for k in StepDef.__dataclass_fields__
                   if getattr(before, k) != getattr(after, k)}
        audit.record(conn, "CLOSE_STEP_SET", target=code, detail=changed)
    return after


# ---------- check results (R-105) ----------

def record(conn: psycopg.Connection, account_id: str, day: dt.date, steps: list[Step], command: str) -> int:
    """Keep what one check of one day found. Rows are only added: the next check adds its own."""
    _, actor, _ = audit.origin()
    with conn.transaction():
        check_id = conn.execute(
            "insert into close_checks (account_id, check_date, day_state, done, steps, command, checked_by)"
            " values (%s, %s, %s, %s, %s, %s, %s) returning check_id",
            (account_id, day, DAY_STATE_CODES[day_state(steps)], finished(steps), required(steps), command,
             actor)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "insert into close_check_steps (check_id, step_no, step_code, name, required, state, detail)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                [(check_id, s.no, s.code, s.name, s.required, STATE_CODES[s.state], s.detail) for s in steps])
    return check_id


def first_day(ledger: Ledger) -> dt.date | None:
    """The day the books started: nothing is owed before it."""
    for ev in ledger.events:
        return book_date(ev)
    return None


def board(ledger: Ledger, calendar, today: dt.date, outside: Outside,
          steps: tuple[StepDef, ...] = STEP_DEFAULTS) -> dict[dt.date, list[Step]]:
    """Every business day from the day the books started to today, newest first, with its steps: those in use,
    in their order, and of those with an asset type only the ones for an asset type the account has had."""
    from .pricecheck import BAD as PRICE_BAD, WARN as PRICE_WARN, closes_by_day, price_checks
    from .queries import _order_key, _settlement_facts, nav_changes, order_progress

    start = first_day(ledger)
    if start is None:
        return {}
    fills, settled_on, pending = _settlement_facts(ledger)
    due: dict[dt.date, list[bool]] = {}                    # settlement date -> [settled?] per execution
    for exec_id, day in settled_on.items():
        due.setdefault(day, []).append(True)
    for exec_id, day in pending.items():
        due.setdefault(day, []).append(False)
    booked: dict[dt.date, int] = {}
    for ev in ledger.events:
        if isinstance(ev, ExecutionEvent):
            booked[ev.trade_date] = booked.get(ev.trade_date, 0) + 1
    valued = closes_by_day(ledger)
    prices: dict[dt.date, list[str]] = {}
    for c in price_checks(ledger):
        prices.setdefault(c.date, []).append(c.level)
    navs = {c.date: c for c in nav_changes(ledger)}

    def owed(day):      # a step that did not run: still to come today, missed on a day that has passed
        return WAIT if day >= today else MISSING

    def settlement(day):
        mine = due.get(day, [])
        if not mine:
            return NONE, "결제할 체결이 없어요"
        late = mine.count(False)
        if late:
            return (WAIT if day >= today else BAD), f"결제할 체결 {len(mine)}건 중 {late}건이 아직 결제되지 않았어요"
        return DONE, f"체결 {len(mine)}건을 결제했어요"

    def orders(day):
        mine = outside.orders.get(day, [])
        if not mine:
            n = booked.get(day, 0)
            return (DONE, f"주문 기록 없이 반영된 체결 {n}건이 있어요") if n else (NONE, "낸 주문이 없어요")
        stages: dict[str, int] = {}
        for qty, outcome, order_no in mine:
            hit = fills.get(_order_key(day, order_no), []) if order_no else []
            settled = sum(e.qty for e in hit if e.exec_id in settled_on)
            overdue = any(pending.get(e.exec_id, today) < today for e in hit)
            stage = order_progress(outcome, qty, sum(e.qty for e in hit), settled, day < today, overdue)[1]
            stages[stage] = stages.get(stage, 0) + 1
        said = f"주문 {len(mine)}건: " + ", ".join(f"{k} {v}건" for k, v in stages.items())
        if stages.get("접수 기록 없음"):
            return BAD, said + ". 접수 결과가 남지 않은 주문은 증권사 주문내역과 맞춰 봐야 해요"
        if stages.get("체결 대기") or stages.get("부분 체결"):
            return WAIT, said
        return DONE, said

    def closes(day):
        if day not in valued:
            return owed(day), "종가를 아직 받지 않았어요" if day >= today else "이날 받은 종가가 없어요"
        levels = prices.get(day, [])
        if not levels:
            return NONE, "보유 종목이 없어 받을 종가가 없어요"
        bad, warn = levels.count(PRICE_BAD), levels.count(PRICE_WARN)
        if bad:
            return BAD, f"종가 {len(levels)}개 중 {bad}개는 확인이 필요해요" + (f" (주의 {warn}개)" if warn else "")
        if warn:
            return WARN, f"종가 {len(levels)}개 중 {warn}개가 크게 움직였어요"
        return DONE, f"종가 {len(levels)}개가 모두 정상이에요"

    def nav(day):
        c = navs.get(day)
        if c is None:
            return owed(day), "기준가를 아직 내지 않았어요" if day >= today else "이날 낸 기준가가 없어요"
        if c.unexplained:
            return BAD, f"순자산 변동 {c.change:+,}원 중 {c.unexplained:+,}원이 분개로 설명되지 않아요"
        return DONE, f"순자산 {c.net_assets:,}원 (전일 대비 {c.change:+,}원, 모두 분개로 설명돼요)"

    def recon(day):
        r = outside.recon.get(day)
        if r is None:
            return owed(day), "일 마감 대사를 아직 하지 않았어요" if day >= today else "이날 한 일 마감 대사가 없어요"
        if r.open_breaks:
            return BAD, f"대사 {r.items}개 항목 중 불일치 {r.open_breaks}건을 아직 판단하지 않았어요"
        if r.breaks:
            return DONE, f"대사 {r.items}개 항목, 불일치 {r.breaks}건은 모두 판단을 마쳤어요"
        return DONE, f"대사 {r.items}개 항목이 모두 일치해요"

    closed = ledger.closed_days()

    def close(day):     # R-77: a close locks every day up to its date
        if day in closed:
            return DONE, f"순자산 {closed[day].net_assets:,}원으로 장부를 마감했어요"
        if ledger.is_closed(day):
            return DONE, f"{min(d for d in closed if d > day)} 마감이 이 날까지 함께 닫았어요"
        if day >= today:
            return WAIT, "아직 마감하지 않았어요"
        r = outside.recon.get(day)
        if r is not None and r.open_breaks:
            return BAD, "대사 불일치의 판단이 남아 마감하지 않았어요. 판단한 뒤 close 명령으로 마감해요"
        return BAD, "날이 지났는데 장부를 마감하지 않았어요. 다음 마감이 이 날까지 함께 닫아요"

    def dividends(day):
        rows = outside.dividends
        accrued = sum(1 for a, _, _, _ in rows if a == day)
        paid = sum(1 for _, _, p, _ in rows if p == day)
        owed_pay = sum(1 for _, pay, p, st in rows if pay == day and p is None and st == "ACCRUED")
        if owed_pay:
            return ((WAIT if day >= today else BAD),
                    f"지급일인 배당 {owed_pay}건의 입금이 아직 기록되지 않았어요" + ("" if day >= today else ". dividend 명령으로 실제 금액을 기록해요"))
        if not accrued and not paid:
            return NONE, "인식하거나 입금할 배당이 없어요"
        said = [f"인식 {accrued}건"] if accrued else []
        said += [f"입금 {paid}건"] if paid else []
        return DONE, "배당 " + ", ".join(said) + "을 기록했어요"

    def erp_export(day):
        if day in outside.erp_days:
            return DONE, "이날 전표를 ERP로 내보냈어요"
        if not ledger.is_closed(day):
            return WAIT, "장부를 마감한 뒤 내보내요"
        return WAIT, "아직 ERP로 내보내지 않았어요 (erp export)"

    checks = {"SETTLEMENT": settlement, "ORDERS": orders, "DIVIDENDS": dividends, "PRICES": closes, "NAV": nav,
              "RECON": recon, "DAY_CLOSE": close, "ERP_EXPORT": erp_export, "INTEGRITY": lambda day: _integrity(outside)}
    shown = [d for d in steps if d.in_use and d.code in checks
             and (d.asset_type is None or outside.asset_types is None or d.asset_type in outside.asset_types)]

    out, day = {}, today
    while day >= start:
        try:
            open_day = calendar.is_business_day(day)
        except LookupError:      # the holiday calendar does not cover that year
            open_day = day.weekday() < 5
        if open_day:
            out[day] = [Step(i + 1, d.name, *checks[d.code](day), d.screen or "", d.code, d.required)
                        for i, d in enumerate(shown)]
        day -= dt.timedelta(days=1)
    return out
