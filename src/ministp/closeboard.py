"""마감 점검: what one business day's work left behind, step by step.

`daily` does its steps in order (결제 → 체결 반영 → 평가 → 기준가 → 대사 → 일 마감 → 원장 검증) and prints the
result once. This module reads the same steps back from what was recorded, for any day, so the question
"is this day finished, and if not, what is it waiting for" has one answer on one screen. Each step
names the screen that shows its detail. Nothing is fetched and nothing is written.

The states: 완료 · 대상 없음 (nothing to do that day) · 대기 (today, not run yet) · 주의 (done, worth a look) ·
확인 필요 (someone has to decide) · 누락 (the day has passed and the step never ran).
"""
import datetime as dt
from dataclasses import dataclass

from .ledger import ExecutionEvent, Ledger, book_date

DONE, NONE, WAIT, WARN, BAD, MISSING = "완료", "대상 없음", "대기", "주의", "확인 필요", "누락"
DAY_DONE, DAY_OPEN, DAY_BAD = "점검 완료", "진행 중", "확인 필요"
STEPS = ("결제", "주문·체결", "시세", "기준가", "증권사 대사", "일 마감", "원장 내부 대사")
SCREENS = ("STP-4200", "STP-1200", "STP-2300", "STP-2210", "STP-2100", "STP-2200", "STP-2110")
_FINISHED = (DONE, NONE, WARN)


@dataclass(frozen=True)
class Step:
    no: int
    name: str
    state: str
    detail: str
    screen: str     # where the detail of this step is


@dataclass(frozen=True)
class Outside:
    """What the ledger alone does not know: the orders sent, the recon runs, the integrity checks."""
    orders: dict        # date -> [(qty, outcome, order_no)]
    recon: dict         # date -> ops.ReconDay (the day's latest 일 마감 대사)
    checks: int         # integrity checks run on the books as they are now
    breaks: int         # of those, failing


def day_state(steps: list[Step]) -> str:
    states = {s.state for s in steps}
    return DAY_BAD if states & {BAD, MISSING} else DAY_OPEN if WAIT in states else DAY_DONE


def finished(steps: list[Step]) -> int:
    return sum(s.state in _FINISHED for s in steps)


def needing(steps: list[Step]) -> int:
    return sum(s.state in (BAD, MISSING) for s in steps)


def integrity_step(outside: Outside) -> Step:
    """The books as they are now, not as they were that day: a damaged row has no date."""
    if outside.breaks:
        return Step(len(STEPS), STEPS[-1], BAD,
                    f"지금 원장 기준으로 점검 {outside.checks}개 중 {outside.breaks}개가 맞지 않아요", SCREENS[-1])
    return Step(len(STEPS), STEPS[-1], DONE, f"지금 원장 기준으로 점검 {outside.checks}개가 모두 일치해요", SCREENS[-1])


def first_day(ledger: Ledger) -> dt.date | None:
    """The day the books started: nothing is owed before it."""
    for ev in ledger.events:
        return book_date(ev)
    return None


def board(ledger: Ledger, calendar, today: dt.date, outside: Outside) -> dict[dt.date, list[Step]]:
    """Every business day from the day the books started to today, newest first, with its seven steps."""
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

    out, day = {}, today
    while day >= start:
        try:
            open_day = calendar.is_business_day(day)
        except LookupError:      # the holiday calendar does not cover that year
            open_day = day.weekday() < 5
        if open_day:
            steps = [Step(i + 1, STEPS[i], *f(day), SCREENS[i])
                     for i, f in enumerate((settlement, orders, closes, nav, recon, close))]
            out[day] = steps + [integrity_step(outside)]
        day -= dt.timedelta(days=1)
    return out
