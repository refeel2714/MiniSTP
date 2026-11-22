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
