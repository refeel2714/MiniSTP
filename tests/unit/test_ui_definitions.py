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
    assert any("다른 화면 것" in e for e in validate(screen([], tx="STP3300R01"), CATALOG))


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
    assert {"STP-1100", "STP-1200", "STP-2100", "STP-2200", "STP-3100", "STP-3200", "STP-3300"} <= set(screens)
    assert usages(screens)["journal_lines.debit"] == ["STP-3200 journal"]          # the journal has a screen of its own
    assert "STP-3100 positions" in usages(screens)["position_ledger.qty_delta"]


def test_unknown_transaction_and_parameters_are_refused():
    with pytest.raises(TxError, match="없는 트랜잭션"):
        run_tx("STP0000R01", None, {})
    with pytest.raises(TxError, match="없는 조회조건"):
        run_tx("STP3300R01", None, {"drop": "table"})
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
    for sid, on_left, on_right, under in [("STP-3100", ["events"], ["positions", "cash"], []),
                                          ("STP-3200", ["events"], ["journal"], []),   # the journal has the right side to itself
                                          ("STP-4100", ["instruments"], ["events"], [])]:
        assert shipped[sid].layout == "split"
        left, right, below = split_sides(shipped[sid])
        assert ([g.id for g in left], [g.id for g in right], [g.id for g in below]) == (on_left, on_right, under)
    assert shipped["STP-1100"].layout == "stack" and shipped["STP-3300"].layout == "stack"
    # 원장 조회와 분개 조회는 같은 이벤트로 서로를 열어요 (두 번 누르기)
    events = {sid: next(g for g in shipped[sid].grids if g.id == "events") for sid in ("STP-3100", "STP-3200")}
    assert events["STP-3100"].drill == {"screen": "STP-3200", "params": {"seq": "seq"}}
    assert events["STP-3200"].drill == {"screen": "STP-3100", "params": {"seq": "seq"}}


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


# ---------- 셀 표시 유형 ----------

def badge_columns(**badge):
    return [Column("symbol", "instruments.symbol", format="code"),
            Column("name", "instruments.name", **{"display": "badge", "code": "symbol", **badge})]


def test_badge_draws_its_code_inside_and_the_code_keeps_no_column_of_its_own():
    s = screen(badge_columns())
    assert validate(s, CATALOG) == []
    cols = {c["name"]: c for c in resolve(s["STP-1100"], CATALOG)["grids"][0]["columns"]}
    assert cols["symbol"]["inside"] == "name" and cols["name"]["inside"] is None
    assert cols["name"]["display"] == "badge" and cols["name"]["code"] == "symbol"
    # the code is still a column of the definition: 영향도 분석 keeps seeing it
    assert usages(s)["instruments.symbol"] == ["STP-1100 g"]


@pytest.mark.parametrize("cols, problem", [
    ([Column("qty", "positions_v.qty", format="number", display="gauge")], "없는 표시 유형"),
    ([Column("qty", "positions_v.qty", display="bar")], "표시 유형 bar의 형식"),
    ([Column("qty", "positions_v.qty", format="money", display="meter")], "표시 유형 meter의 형식"),
    ([Column("qty", "positions_v.qty", format="number", display="meter")], "of로 전체가 되는 열을 적어요"),
    ([Column("w", label="비중", format="rate", display="meter", of="qty"),
      Column("qty", "positions_v.qty", format="number")], "of로 전체가 되는 열을 적어요"),
    ([Column("qty", "positions_v.qty", format="number", display="meter", of="nope")], "다른 수량(number) 열이 아니에요"),
    ([Column("qty", "positions_v.qty", format="number", of="qty")], "비율 막대(display: meter)에만"),
    ([Column("p", label="진행", format="steps")], "진행 단계(display: steps)로만"),
    ([Column("p", label="진행", format="steps", display="steps", steps=["하나"])], "단계 이름을 2~6개"),
    ([Column("qty", "positions_v.qty", format="number", steps=["가", "나"])], "진행 단계(display: steps)에만"),
    ([Column("qty", "positions_v.qty", format="number", display="bar", hidden=True)], "숨긴 열에는 표시 유형"),
    ([Column("qty", "positions_v.qty", format="number", hidden=True)], "보이는 열이 하나도 없어요"),
    ([Column("trend", label="추세", format="series")], "추세선(display: spark)으로만"),
    ([Column("trend", label="추세", format="number", display="spark")], "표시 유형 spark의 형식"),
    ([Column("st", label="상태", format="code", display="pill")], "색 규칙이 있어야"),
    ([Column("qty", "positions_v.qty", format="number", code="symbol")], "배지(display: badge)에만"),
    (badge_columns(code="nope"), "이 그리드의 다른 열이 아니에요"),
    (badge_columns(code="name"), "이 그리드의 다른 열이 아니에요"),
    (badge_columns(shape="asset_type"), "이 그리드의 다른 열이 아니에요"),
])
def test_display_problems(cols, problem):
    errs = validate(screen(cols), CATALOG)
    assert any(problem in e for e in errs), errs


def test_pictures_are_for_grid_rows_only():
    errs = validate(screen([Column("qty", "positions_v.qty", format="number", display="bar")], view="cards"), CATALOG)
    assert any("카드 보기에는 표시 유형" in e for e in errs)


def test_a_meter_reads_as_text_next_to_its_bar():
    assert Column("w", label="비중", format="rate", display="meter").align == "left"
    assert Column("w", label="비중", format="rate").align == "right"


# ---------- 달력 보기 ----------

T_COLUMNS = [Column("entry", label="전표", format="number"), Column("account", label="계정", format="code"),
             Column("name", label="계정명"), Column("debit", label="차변", format="money"),
             Column("credit", label="대변", format="money")]
T_ROLES = {"entry": "entry", "account": "account", "name": "name", "debit": "debit", "credit": "credit"}


def t_grid(columns=None, **grid):
    return screen(columns or T_COLUMNS, **{"view": "taccount", "taccount": T_ROLES, **grid})


def test_t_account_view_names_the_account_and_both_sides():
    """T계정 보기: 어느 열이 계정이고 어느 열이 차변·대변인지를 정의가 말해요."""
    s = t_grid()
    assert validate(s, CATALOG) == []
    assert resolve(s["STP-1100"], CATALOG)["grids"][0]["taccount"] == T_ROLES
    assert validate(t_grid(taccount={"account": "account", "debit": "debit", "credit": "credit"}), CATALOG) == []   # name, entry: optional
    shipped = next(g for g in load_screens()["STP-3200"].grids if g.id == "journal")
    assert shipped.view == "taccount" and shipped.taccount == {
        "entry": "entry_id", "account": "account_code", "name": "account_name", "debit": "debit", "credit": "credit"}


@pytest.mark.parametrize("s, problem", [
    (t_grid(taccount=None), "account(계정 열), debit(차변 열), credit(대변 열)을 적어요"),
    (t_grid(taccount={"account": "account", "debit": "debit"}), "account(계정 열), debit(차변 열), credit(대변 열)을 적어요"),
    (t_grid(taccount={**T_ROLES, "side": "name"}), "account(계정 열), debit(차변 열), credit(대변 열)을 적어요"),
    (t_grid(taccount={**T_ROLES, "credit": "nope"}), "credit 열 nope가 이 그리드에 없어요"),
    (t_grid(taccount={**T_ROLES, "debit": "entry"}), "debit 열 entry는 금액(money) 형식이어야 해요"),
    (t_grid(taccount={**T_ROLES, "credit": "debit"}), "T계정의 열은 서로 달라야 해요"),
    (t_grid(drill={"screen": "STP-1100", "params": {}}), "차트·화면 이동을 쓰지 않아요"),
    (t_grid([*T_COLUMNS[:3], Column("debit", label="차변", format="money", display="bar"), T_COLUMNS[4]]),
     "T계정 보기에는 표시 유형을 쓰지 않아요"),
    (screen(T_COLUMNS, taccount=T_ROLES), "taccount는 T계정 보기(view: taccount)에만 써요"),
])
def test_t_account_view_problems(s, problem):
    errs = validate(s, CATALOG)
    assert any(problem in e for e in errs), errs


def test_nothing_hangs_on_a_row_of_a_t_account_view():
    s = t_grid()
    s["STP-1100"].grids.append(Grid("d", "상세", "STP1100R01", [Column("qty", "positions_v.qty", format="number")],
                                    load="master", master="g", link={}))
    assert any("T계정 보기는 행을 고르지 않아요" in e for e in validate(s, CATALOG))


MAP_COLUMNS = [Column("name", label="이름"), Column("cost", label="장부금액", format="money"),
               Column("pnl", label="평가손익", format="money", hidden=True)]
MAP_ROLES = {"label": "name", "size": "cost", "tone": "pnl"}


def test_a_table_can_offer_a_map_of_its_rows():
    """구성 지도: 표 보기의 그리드에 map을 적으면 바꿔 보는 보기가 붙고, view: map이면 지도가 먼저 나와요."""
    for view in ("grid", "map"):
        s = screen(MAP_COLUMNS, view=view, map=MAP_ROLES) if view == "grid" else screen(MAP_COLUMNS[:2], view=view, map={"label": "name", "size": "cost"})
        assert validate(s, CATALOG) == []
        assert resolve(s["STP-1100"], CATALOG)["grids"][0]["map"]["size"] == "cost"
    shipped = next(g for g in load_screens()["STP-1100"].grids if g.id == "holdings")
    assert shipped.view == "grid" and shipped.map == {"label": "instrument_name", "size": "book_cost", "tone": "unrealized_pnl"}


@pytest.mark.parametrize("s, problem", [
    (screen(MAP_COLUMNS[:2], view="map"), "label(이름 열)과 size(면적이 되는 열)를 적어요"),
    (screen(MAP_COLUMNS, map={"label": "name"}), "label(이름 열)과 size(면적이 되는 열)를 적어요"),
    (screen(MAP_COLUMNS, map={**MAP_ROLES, "depth": "pnl"}), "label(이름 열)과 size(면적이 되는 열)를 적어요"),
    (screen(MAP_COLUMNS, map={**MAP_ROLES, "tone": "nope"}), "tone 열 nope가 이 그리드에 없어요"),
    (screen(MAP_COLUMNS, map={**MAP_ROLES, "size": "name"}), "수량(number)이나 금액(money) 형식이어야 해요"),
    (screen(MAP_COLUMNS, map={**MAP_ROLES, "tone": "cost"}), "구성 지도의 열은 서로 달라야 해요"),
    (screen(MAP_COLUMNS[:2], view="cards", map={"label": "name", "size": "cost"}), "표 보기나 구성 지도 보기의 그리드에만 써요"),
    (screen(MAP_COLUMNS, map=MAP_ROLES, chart={"x": "name", "y": "cost"}), "구성 지도와 차트를 한 그리드에 같이 두지 않아요"),
])
def test_map_problems(s, problem):
    errs = validate(s, CATALOG)
    assert any(problem in e for e in errs), errs


def calendar_grid(columns=None, **grid):
    cols = columns or [Column("date", label="날짜", format="date"), Column("state", label="결과", color="status"),
                       Column("qty", "positions_v.qty", format="number")]
    return screen(cols, **{"view": "calendar", "calendar": {"date": "date", "state": "state"}, **grid})


def test_calendar_view_names_its_date_and_state_columns():
    s = calendar_grid()
    assert validate(s, CATALOG) == []
    assert resolve(s["STP-1100"], CATALOG)["grids"][0]["calendar"] == {"date": "date", "state": "state"}


@pytest.mark.parametrize("s, problem", [
    (calendar_grid(calendar=None), "date(날짜 열)와 state(상태 열)를 적어요"),
    (calendar_grid(calendar={"date": "date"}), "date(날짜 열)와 state(상태 열)를 적어요"),
    (calendar_grid(calendar={"date": "qty", "state": "state"}), "날짜(date) 형식 열이어야 해요"),
    (calendar_grid(calendar={"date": "date", "state": "qty"}), "색 규칙이 status인"),
    (calendar_grid(calendar={"date": "date", "state": "nope"}), "색 규칙이 status인"),
    (calendar_grid(key="state"), "행 식별 열(key)이 날짜 열이어야 해요"),
    (calendar_grid(totals=["qty"]), "합계·차트·화면 이동을 쓰지 않아요"),
    (calendar_grid([Column("date", label="날짜", format="date"),
                    Column("state", label="결과", color="status", display="pill")]), "달력 보기에는 표시 유형"),
    (calendar_grid(view="grid"), "달력 보기(view: calendar)에만 써요"),
    (calendar_grid(view="timeline"), "view는"),
])
def test_calendar_problems(s, problem):
    errs = validate(s, CATALOG)
    assert any(problem in e for e in errs), errs


# ---------- 진행 단계, 수량 막대, 숨긴 열 ----------

def test_a_quantity_meter_draws_its_whole_inside_and_a_hidden_column_stays_in_the_rows():
    s = screen([Column("symbol", "instruments.symbol", format="code", hidden=True),
                Column("filled", label="체결 / 주문", format="number", display="meter", of="qty"),
                Column("qty", "positions_v.qty", format="number"),
                Column("progress", label="진행 단계", format="steps", display="steps", steps=["의도", "접수", "체결", "결제"])])
    assert validate(s, CATALOG) == []
    cols = {c["name"]: c for c in resolve(s["STP-1100"], CATALOG)["grids"][0]["columns"]}
    assert cols["qty"]["inside"] == "filled" and cols["filled"]["align"] == "left"
    assert cols["symbol"]["hidden"] and cols["progress"]["steps"] == ["의도", "접수", "체결", "결제"]
    assert usages(s)["instruments.symbol"] == ["STP-1100 g"]     # 숨겨도 영향도 분석에는 남아요


def test_pictures_are_not_for_calendar_or_cards_and_hidden_columns_are_for_tables():
    errs = validate(screen([Column("qty", "positions_v.qty", format="number", hidden=True),
                            Column("symbol", "instruments.symbol")], view="cards"), CATALOG)
    assert any("숨긴 열은 표 보기에만" in e for e in errs)


def test_an_instrument_looks_the_same_on_every_screen():
    """종목명을 보여 주는 표는 어디서나 같은 배지: 색은 종목코드, 모양은 자산 유형."""
    shown = [(s.id, g.id, c) for s in load_screens().values() for g in s.grids if g.view == "grid"
             for c in g.columns if c.source == "instruments.name"]
    assert len(shown) >= 5
    for sid, gid, c in shown:
        assert (c.display, c.code, c.shape) == ("badge", "symbol", "asset_type"), f"{sid} {gid}"


def test_the_jump_box_offers_a_code_for_every_screen_that_takes_it():
    """바로 가기 창: 조회조건이 찾기 팝업으로 받는 코드는 그 화면을 여는 열쇠가 돼요. 따로 적어 두는 목록이 없어요."""
    from ministp.ui.definitions import Lookup, jump_targets, load_lookups
    lk = Lookup("symbol", "종목 찾기", "STP4100R01", "symbol", [Column("symbol", "instruments.symbol", format="code"),
                                                              Column("name", "instruments.name")], shows="name")
    screens = {
        **screen([], [Condition("symbol", source="instruments.symbol", lookup="symbol")], sid="STP-1300"),
        **screen([], [Condition("day", "date", label="기준일")], sid="STP-2100"),                       # takes no code
        **screen([], [Condition("code", label="코드", lookup="symbol", lookup_params={"group_id": "X"})],
                 sid="STP-4600"),                                                                     # a list that needs a fixed parameter
        **screen([], [Condition("item", source="instruments.symbol", lookup="symbol")], sid="STP-3100"),
    }
    (jump,) = jump_targets(screens, CATALOG, {"symbol": lk})
    assert (jump["name"], jump["tx"], jump["value"], jump["shows"]) == ("symbol", "STP4100R01", "symbol", "name")
    assert [c["label"] for c in jump["columns"]] == ["종목코드", "종목명"]
    assert jump["into"] == [{"id": "STP-1300", "title": "잔고", "param": "symbol"},
                            {"id": "STP-3100", "title": "잔고", "param": "item"}]
    assert jump_targets(screen([], [Condition("day", "date", label="기준일")]), CATALOG, {"symbol": lk}) == []

    real = {j["name"]: [t["id"] for t in j["into"]] for j in jump_targets(load_screens(), CATALOG, load_lookups())}
    assert real == {"symbol": ["STP-1300", "STP-3100", "STP-3200"]}    # a 종목 opens 주문가능 조회, 원장 조회 and 분개 조회
