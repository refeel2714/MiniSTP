"""STP-2400 마감 점검: a day's steps read back from what the day left behind."""
from ministp import closeboard as cb
from ministp.ledger import Side
from ministp.ops import ReconDay
from ministp.ui.definitions import Column, Condition, Grid, Screen, load_screens, validate

from conftest import d

CLEAN = ReconDay(run_id=1, items=5, breaks=0, open_breaks=0)


def outside(orders=None, recon=None, breaks=0):
    return cb.Outside(orders or {}, recon or {}, 7, breaks)


def steps(ledger, calendar, today, day, out):
    return {s.name: s for s in cb.board(ledger, calendar, d(today), out)[d(day)]}


def traded(ledger):
    """11-02: money in, one order filled, the day valued and struck."""
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.execute("20261102-0001-1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.revalue(d("11-02"), {"A": 10_100})
    ledger.nav(d("11-02"), {"A": 10_100})
    return {d("11-02"): [(10, "ACCEPTED", "0001")]}


def test_a_finished_day_has_every_step_done_and_says_what_each_found(ledger, calendar):
    orders = traded(ledger)
    board = cb.board(ledger, calendar, d("11-02"), outside(orders, {d("11-02"): CLEAN}))
    assert list(board) == [d("11-02")]
    day = board[d("11-02")]
    assert [(s.no, s.name, s.screen) for s in day] == list(zip(range(1, 7), cb.STEPS, cb.SCREENS))
    assert [s.state for s in day] == [cb.NONE, cb.DONE, cb.DONE, cb.DONE, cb.DONE, cb.DONE]
    assert (cb.day_state(day), cb.finished(day), cb.needing(day)) == (cb.DAY_DONE, 6, 0)
    said = {s.name: s.detail for s in day}
    assert said["주문·체결"] == "주문 1건: 결제 대기 1건" and "종가 1개가 모두 정상" in said["시세"]
    assert "모두 분개로 설명" in said["기준가"] and "5개 항목이 모두 일치" in said["증권사 대사"]
    assert "점검 7개가 모두 일치" in said["원장 내부 대사"]


def test_today_waits_for_what_daily_has_not_done_yet(ledger, calendar):
    orders = traded(ledger)
    got = steps(ledger, calendar, "11-03", "11-03", outside(orders, {d("11-02"): CLEAN}))
    assert [got[n].state for n in cb.STEPS] == [cb.NONE, cb.NONE, cb.WAIT, cb.WAIT, cb.WAIT, cb.DONE]
    assert cb.day_state(list(got.values())) == cb.DAY_OPEN and cb.finished(list(got.values())) == 3
    # the fill of 11-02 is due on 11-04: waiting on that day, late the day after
    assert steps(ledger, calendar, "11-04", "11-04", outside(orders))["결제"].state == cb.WAIT
    late = steps(ledger, calendar, "11-05", "11-04", outside(orders))["결제"]
    assert late.state == cb.BAD and "1건 중 1건이 아직 결제되지 않았어요" in late.detail
    ledger.settle_due(d("11-04"))
    done = steps(ledger, calendar, "11-05", "11-04", outside(orders))["결제"]
    assert (done.state, done.detail) == (cb.DONE, "체결 1건을 결제했어요")


def test_a_day_that_passed_without_its_steps_is_missing_not_waiting(ledger, calendar):
    orders = traded(ledger)
    got = steps(ledger, calendar, "11-04", "11-03", outside(orders, {d("11-02"): CLEAN}))
    assert [got[n].state for n in ("시세", "기준가", "증권사 대사")] == [cb.MISSING] * 3
    day = list(got.values())
    assert (cb.day_state(day), cb.needing(day)) == (cb.DAY_BAD, 3)
    assert "이날 낸 기준가가 없어요" in got["기준가"].detail


def test_each_step_can_stop_the_day(ledger, calendar):
    orders = traded(ledger)
    ledger.execute("20261103-0002-1", d("11-03"), "A", Side.BUY, 10, 13_000)
    ledger.revalue(d("11-03"), {"A": 12_000})
    ledger.nav(d("11-03"), {"A": 12_500})                       # struck at a close the books did not take
    orders[d("11-03")] = [(10, "ACCEPTED", "0002"), (5, None, None)]
    recon = {d("11-02"): ReconDay(1, 5, 2, 0), d("11-03"): ReconDay(2, 6, 2, 1)}
    got = steps(ledger, calendar, "11-03", "11-03", outside(orders, recon, breaks=2))
    assert [got[n].state for n in cb.STEPS] == [cb.NONE, cb.BAD, cb.BAD, cb.BAD, cb.BAD, cb.BAD]
    assert "접수 기록 없음 1건" in got["주문·체결"].detail and "증권사 주문내역" in got["주문·체결"].detail
    assert "1개는 확인이 필요해요" in got["시세"].detail          # valuation and NAV took different closes
    assert "분개로 설명되지 않아요" in got["기준가"].detail
    assert "불일치 1건을 아직 판단하지 않았어요" in got["증권사 대사"].detail
    assert "7개 중 2개가 맞지 않아요" in got["원장 내부 대사"].detail
    # judged breaks do not hold the day, and the books as they are now are said on every day
    first = steps(ledger, calendar, "11-03", "11-02", outside(orders, recon, breaks=2))
    assert first["증권사 대사"].state == cb.DONE and "불일치 2건은 모두 판단을 마쳤어요" in first["증권사 대사"].detail
    assert first["원장 내부 대사"].state == cb.BAD


def test_a_big_move_is_a_caution_that_does_not_hold_the_day(ledger, calendar):
    orders = traded(ledger)
    ledger.revalue(d("11-03"), {"A": 12_000})
    ledger.nav(d("11-03"), {"A": 12_000})
    got = steps(ledger, calendar, "11-03", "11-03", outside(orders, {d("11-03"): CLEAN}))
    assert got["시세"].state == cb.WARN and "크게 움직였어요" in got["시세"].detail
    assert cb.day_state(list(got.values())) == cb.DAY_DONE


def test_only_business_days_since_the_books_started_are_listed_newest_first(ledger, calendar):
    assert cb.board(ledger, calendar, d("11-10"), outside()) == {}          # nothing recorded yet
    ledger.deposit(d("11-05"), 1_000_000)
    days = list(cb.board(ledger, calendar, d("11-10"), outside()))
    assert days == [d("11-10"), d("11-09"), d("11-06"), d("11-05")]           # no weekend, nothing before the start
    a_day = cb.board(ledger, calendar, d("11-10"), outside())[d("11-06")]
    assert a_day[1].detail == "낸 주문이 없어요" and a_day[1].state == cb.NONE


# ---------- 화면 이동: the row names the screen ----------

def board_screens(**drill):
    cols = [Column("step", label="단계"), Column("screen", label="상세 화면", format="code"),
            Column("date", label="영업일", format="date")]
    day = Condition("day", "date", label="기준일")
    return {"STP-1100": Screen("STP-1100", "잔고", "", [day], [Grid("g", "보유", "STP1100R01", cols)]),
            "STP-2400": Screen("STP-2400", "마감 점검", "", [], [Grid("s", "단계", "STP2400R02", cols, drill=drill)])}


def problems(**drill):
    return [e for e in validate(board_screens(**drill), {}) if "STP-2400" in e]


def test_a_row_may_name_the_screen_it_opens_from_a_list_the_definition_gives():
    assert problems(by="screen", screens=["STP-1100"], params={"day": "date"}) == []
    assert any("없는 화면으로 이동 STP-9999" in e for e in problems(by="screen", screens=["STP-1100", "STP-9999"]))
    assert any("이동할 화면을 정하는 열 nope" in e for e in problems(by="nope", screens=["STP-1100"]))
    assert any("갈 수 있는 화면(screens)" in e for e in problems(by="screen"))
    assert any("하나만 적어요" in e for e in problems(by="screen", screen="STP-1100", screens=["STP-1100"]))
    assert any("없는 조회조건 symbol로 이동" in e
               for e in problems(by="screen", screens=["STP-1100"], params={"symbol": "date"}))
    assert any("이동 값 nope이 열에 없어요" in e
               for e in problems(by="screen", screens=["STP-1100"], params={"day": "nope"}))


def test_the_shipped_board_sends_every_step_to_a_screen_that_exists():
    screens = load_screens()
    drill = next(g for g in screens["STP-2400"].grids if g.id == "steps").drill
    assert drill["by"] == "screen" and tuple(drill["screens"]) == cb.SCREENS
    assert set(cb.SCREENS) <= set(screens)
