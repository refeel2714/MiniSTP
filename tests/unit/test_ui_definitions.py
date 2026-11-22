"""Screen definitions are checked against the schema before any screen is drawn."""
import pytest

from ministp.ui.definitions import Column, Condition, Grid, Screen, load_screens, resolve, usages, validate
from ministp.ui.transactions import TxError, run_tx

CATALOG = {
    "instruments": {"columns": [{"name": "symbol", "comment": "종목코드"}, {"name": "name", "comment": "종목명"}]},
    "positions_v": {"columns": [{"name": "qty", "comment": "보유수량 (주)"}]},
}


def screen(columns, conditions=(), tx="STP1100R01", sid="STP-1100", **grid):
    return {sid: Screen(sid, "잔고", "", list(conditions), [Grid("g", "보유", tx, list(columns), **grid)])}


def test_labels_come_from_db_comments():
    s = screen([Column("qty", "positions_v.qty", format="number")])["STP-1100"]
    col = resolve(s, CATALOG)["grids"][0]["columns"][0]
    assert col["label"] == "보유수량" and col["align"] == "right"


def test_good_definition_has_no_problems():
    cols = [Column("symbol", "instruments.symbol", format="code"), Column("qty", "positions_v.qty", format="number")]
    assert validate(screen(cols, [Condition("day", "date", label="기준일")]), CATALOG) == []


@pytest.mark.parametrize("col, problem", [
    (Column("x", "positions_v.nope"), "DB에 없는 컬럼"),
    (Column("qty", "positions_v.qty", label="수량"), "DB 주석을 써요"),
    (Column("x"), "source(DB 컬럼)나 label"),
    (Column("qty", "positions_v.qty", format="percent"), "없는 형식"),
    (Column("qty", "positions_v.qty", color="green"), "없는 색 규칙"),
])
def test_column_problems(col, problem):
    errs = validate(screen([col]), CATALOG)
    assert any(problem in e for e in errs), errs


def test_transaction_must_exist_and_belong_to_the_screen():
    assert any("없는 트랜잭션" in e for e in validate(screen([], tx="STP1100R99"), CATALOG))
    assert any("다른 화면 것" in e for e in validate(screen([], tx="STP3100R03"), CATALOG))


def test_condition_must_be_taken_by_a_transaction():
    errs = validate(screen([], [Condition("symbol", "text", label="종목")]), CATALOG)
    assert any("어디에도 없는 조건" in e for e in errs)


def test_totals_drill_and_chart_must_name_real_things():
    cols = [Column("qty", "positions_v.qty", format="number")]
    errs = validate(screen(cols, totals=["book_cost"], drill={"screen": "STP-9999"},
                           chart={"x": "date", "y": "qty"}), CATALOG)
    assert any("합계 대상" in e for e in errs)
    assert any("없는 화면으로 이동" in e for e in errs)
    assert any("차트 열" in e for e in errs)


def test_drill_needs_a_condition_on_the_target():
    screens = screen([Column("qty", "positions_v.qty")], drill={"screen": "STP-1100", "params": {"symbol": "qty"}})
    assert any("없는 조회조건 symbol로 이동" in e for e in validate(screens, CATALOG))


def test_shipped_screens_load_and_report_usages():
    screens = load_screens()
    assert {"STP-1100", "STP-1200", "STP-2100", "STP-2200", "STP-3100"} <= set(screens)
    assert "STP-3100 journal" in usages(screens)["journal_lines.debit"]


def test_unknown_transaction_and_parameters_are_refused():
    with pytest.raises(TxError, match="없는 트랜잭션"):
        run_tx("STP0000R01", None, {})
    with pytest.raises(TxError, match="없는 조회조건"):
        run_tx("STP3100R03", None, {"drop": "table"})
    with pytest.raises(TxError, match="올바르지 않아요"):
        run_tx("STP3100R02", None, {"seq": "abc"})


def test_input_screens_and_required_conditions_are_accepted():
    s = load_screens()["STP-1300"]
    assert {c.name for c in s.conditions if c.required} == {"symbol", "side", "qty"}
    assert s.grids[0].load == "input"


def test_live_needs_a_real_key_and_a_top_level_grid():
    cols = [Column("qty", "positions_v.qty")]
    assert any("행 식별 열" in e for e in validate(screen(cols, live=True, key="nope"), CATALOG))
    s = {"STP-1100": Screen("STP-1100", "잔고", "", [], [
        Grid("m", "보유", "STP1100R01", cols),
        Grid("d", "상세", "STP1100R01", cols, load="master", master="m", live=True)])}
    assert any("실시간 갱신 대상이 아니에요" in e for e in validate(s, CATALOG))


def test_live_stream_sends_a_change_only_when_the_head_moves():
    import asyncio

    from ministp.ui.app import live_events
    heads = iter([{"seq": 1}, {"seq": 1}, {"seq": 1}, {"seq": 2}, {"seq": 2}])

    async def no_sleep(_):
        pass

    async def collect():
        return [c async for c in live_events(lambda: next(heads), interval=1, keepalive=2, sleep=no_sleep, limit=2)]
    out = asyncio.run(collect())
    assert out == ['event: change\ndata: {"seq": 1}\n\n', ": keep-alive\n\n", 'event: change\ndata: {"seq": 2}\n\n']


def test_split_layout_is_one_list_on_the_left_and_a_few_details_on_the_right():
    """좌우 구성: 목록 왼쪽, 상세 오른쪽. A screen does not get to hold everything."""
    from ministp.ui.definitions import MAX_DETAILS, split_sides
    cols = [Column("qty", "positions_v.qty")]

    def split(details, **kw):
        grids = [Grid("m", "목록", "STP1100R01", cols, load="open")]
        grids += [Grid(f"d{i}", "상세", "STP1100R01", cols, load="master", master="m") for i in range(details)]
        return {"STP-1100": Screen("STP-1100", "잔고", "", [], grids, layout="split", **kw)}

    assert validate(split(2), CATALOG) == []
    assert any("상세가 딸린 목록 그리드가 하나" in e for e in validate(split(0), CATALOG))
    assert any(f"{MAX_DETAILS}개까지" in e for e in validate(split(MAX_DETAILS + 1), CATALOG))
    assert any("25~75" in e for e in validate(split(1, left=90), CATALOG))
    tall = split(2)
    tall["STP-1100"].grids[1].weight = 9
    assert any("weight" in e for e in validate(tall, CATALOG))
    odd = split(1)
    odd["STP-1100"].layout = "tabs"
    assert any("layout은" in e for e in validate(odd, CATALOG))

    shipped = load_screens()
    for sid, on_left, on_right, under in [("STP-3100", ["events"], ["journal", "positions", "cash"], ["trial"]),
                                          ("STP-4100", ["instruments"], ["events"], [])]:
        assert shipped[sid].layout == "split"
        left, right, below = split_sides(shipped[sid])
        assert ([g.id for g in left], [g.id for g in right], [g.id for g in below]) == (on_left, on_right, under)
    assert shipped["STP-1100"].layout == "stack"


def test_a_button_names_a_real_action_and_feeds_every_required_input():
    """처리 버튼: a definition cannot point a button at something the action does not take."""
    from ministp.ui.definitions import Button, Field
    cols = [Column("id", label="행 번호", format="number"), Column("status", label="상태")]

    def problems(sid="STP-2100", tx="STP2100R01", fields=None, **kw):
        fields = fields or [Field("result_id", label="행", column="id", readonly=True),
                            Field("cause", "select", label="원인", options=["TIMING"]), Field("note", label="메모")]
        b = Button(**{"id": "resolve", "label": "원인 판단", "tx": "STP2100U01", "needs_row": True, "fields": fields, **kw})
        return validate(screen(cols, tx=tx, sid=sid, actions=[b]), CATALOG)

    assert problems() == []
    for kw, problem in [
        ({"when": {"gone": ["BREAK"]}}, "고른 행의 열 gone"),
        ({"shows": ["status"], "needs_row": False, "fields": [Field("result_id", "number", label="행"),
                                                              Field("cause", label="원인"), Field("note", label="메모")]},
         "행을 고르는 버튼"),
        ({"fields": [Field("result_id", label="행", column="id")]}, "필수 입력 cause"),
        ({"fields": [Field("result_id", label="행", column="id"), Field("cause", label="원인"), Field("note", label="메모"),
                     Field("extra", label="덤")]}, "없는 입력 항목"),
        ({"fields": [Field("result_id", "date", label="행"), Field("cause", label="원인"), Field("note", label="메모")]},
         "int 값을 받는데"),
        ({"fields": [Field("result_id", label="행", column="nope"), Field("cause", label="원인"),
                     Field("note", label="메모")]}, "채울 값 nope"),
        ({"needs_row": False}, "needs_row가 필요"),
        ({"fields": [Field("result_id", label="행", column="id"), Field("cause", "select", label="원인"),
                     Field("note", label="메모")]}, "options가 필요"),
        ({"fields": [Field("result_id", label="행", column="id"), Field("cause", label="원인", lookup="nope"),
                     Field("note", label="메모")]}, "없는 찾기 팝업 nope"),
        ({"fields": [Field("result_id", column="id"), Field("cause", label="원인"), Field("note", label="메모")]},
         "source나 label"),
    ]:
        errs = problems(**kw)
        assert any(problem in e for e in errs), (problem, errs)
    b = Button("resolve", "원인 판단", "STP2100U99")
    assert any("없는 처리 트랜잭션" in e for e in validate(screen(cols, tx="STP2100R01", sid="STP-2100", actions=[b]), CATALOG))
    b = Button("resolve", "원인 판단", "STP2100U01", [Field("result_id", "number", label="행"),
                                                     Field("cause", label="원인"), Field("note", label="메모")])
    assert any("다른 화면 것" in e for e in validate(screen(cols, actions=[b]), CATALOG))


def test_a_lookup_popup_is_checked_like_a_grid():
    from ministp.ui.definitions import Lookup, load_lookups, lookup_problems
    good = Lookup("symbol", "종목 찾기", "STP4100R01", "symbol", [Column("symbol", "instruments.symbol", format="code"),
                                                                Column("name", "instruments.name")], shows="name")
    assert lookup_problems({"symbol": good}, CATALOG) == []
    bad = Lookup("x", "찾기", "STP0000R01", "code", [Column("name", "instruments.gone"), Column("free")], shows="label")
    errs = lookup_problems({"x": bad}, CATALOG)
    for problem in ("없는 트랜잭션", "고르는 값 code", "함께 보여 줄 열 label", "DB에 없는 컬럼", "source(DB 컬럼)나 label"):
        assert any(problem in e for e in errs), (problem, errs)
    cond = Condition("day", "date", label="기준일", lookup="symbol", lookup_params={"group": "X"})
    errs = validate(screen([], [cond]), CATALOG, {"symbol": good})
    assert any("STP4100R01에 없는 조건 group" in e for e in errs)
    assert "symbol" in load_lookups() and load_screens()["STP-1300"].conditions[0].lookup == "symbol"


def test_actions_refuse_unknown_ids_and_incomplete_input():
    from ministp.ui.actions import ACTIONS, ActionError, run_action
    with pytest.raises(ActionError, match="없는 처리 트랜잭션"):
        run_action("STP2100U99", None, {})
    with pytest.raises(ActionError, match="없는 입력 항목"):
        run_action("STP2100U01", None, {"drop": "table"})
    with pytest.raises(ActionError, match="note 값을 넣어"):
        run_action("STP2100U01", None, {"result_id": 1, "cause": "TIMING", "note": "  "})
    with pytest.raises(ActionError, match="올바르지 않아요"):
        run_action("STP2100U01", None, {"result_id": "abc", "cause": "TIMING", "note": "x"})
    with pytest.raises(ActionError, match="없는 원인 코드"):
        run_action("STP2100U01", None, {"result_id": 1, "cause": "NOPE", "note": "x"})
    assert all(tx[7] == "U" for tx in ACTIONS)        # R = 조회, U = 처리: an id says which kind it is
