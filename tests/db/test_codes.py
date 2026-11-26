"""공통코드 (ERD v2.1): the code table, its seed on new and migrated databases, and the drift check."""
import json
import os

import pytest

from ministp import __main__ as cli
from ministp import codes
from ministp.ops import OpsError
from ministp.store import PostgresLedgerStore
from ministp.store import migrate as mg

psycopg = pytest.importorskip("psycopg")

from db.test_multi_account import _restore_v1_1  # noqa: E402
from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
ENV = {"MINISTP_DATABASE_URL": URL}


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        PostgresLedgerStore(c).init_schema()
        yield c


def _rows(c) -> tuple[list, list]:
    return (c.execute("select group_id, name, description, used_by, fixed from code_groups order by group_id").fetchall(),
            c.execute("select group_id, code, name, description, sort_order, in_use from common_codes"
                      " order by group_id, sort_order").fetchall())


def run(*argv, env=ENV) -> int:
    return cli.run(list(argv), environ=dict(env))


# ---------- the seed ----------

def test_a_new_database_holds_the_seed_and_it_agrees_with_the_schema_and_the_program(conn):
    groups, rows = _rows(conn)
    want_groups, want_codes = codes.seed_rows()
    assert groups == sorted(want_groups) and len(groups) == 21
    assert rows == sorted([(*c, True) for c in want_codes], key=lambda r: (r[0], r[4]))
    assert codes.check(conn) == []
    assert all(fixed for *_, fixed in groups)                      # every seeded group belongs to the program
    assert codes.names(conn, "ACCOUNT_STATUS") == {"OPEN": "정상", "SUSPENDED": "거래정지", "CLOSED": "해지"}


def test_every_checked_column_and_program_enum_has_its_group(conn):
    """A CHECK list in the schema without a code group would be a code nobody named."""
    listed = conn.execute(
        "select t.relname, a.attname from pg_constraint c join pg_class t on t.oid = c.conrelid"
        " join pg_namespace n on n.oid = t.relnamespace"
        " join pg_attribute a on a.attrelid = t.oid and a.attnum = any (c.conkey)"
        " where c.contype = 'c' and n.nspname = 'public' and cardinality(c.conkey) = 1").fetchall()
    with_list = {f"{t}.{col}" for t, col in listed if codes._check_list(conn, t, col)}
    used = {g.used_by for g in codes.SEED if g.used_by}
    assert with_list <= used, sorted(with_list - used)
    assert {"account_status_history.status", "recon_results.status", "accounts_chart.normal_side"} <= with_list
    assert set(codes.program_codes()) <= {g.group_id for g in codes.SEED}



def test_every_snapshot_kind_the_program_writes_has_a_name():
    """A kind added to broker_snapshots later (현재가 R-68, 취소 R-45) must get its code name too."""
    import pathlib
    import re
    from ministp import ops, quotes
    src = (pathlib.Path(ops.__file__)).read_text(encoding="utf-8")
    literal = set(re.findall(r'_snapshot\([^,]+,[^,]+,\s*"([A-Z_]+)"', src))
    written = literal | {ops.CANCEL_KIND, quotes.KIND, "INTRADAY_RECON"}
    named = {c for c, *_ in next(g for g in codes.SEED if g.group_id == "SNAPSHOT_KIND").codes}
    assert literal and written <= named, sorted(written - named)

def test_the_migration_leaves_the_same_codes_as_a_new_database(conn):
    fresh = _rows(conn)
    _restore_v1_1()
    with psycopg.connect(URL, autocommit=True) as old:
        reports = mg.migrate(old)
        assert [r.migration.version for r in reports] == [1, 2, 3, 4] and all(r.applied for r in reports)
        assert _rows(old) == fresh
        assert codes.check(old) == []                              # the codes a real ledger stored all have names


# ---------- the check ----------

def test_the_check_reports_a_code_that_drifted(conn):
    conn.execute("insert into broker_snapshots (date, kind, payload, account_id)"
                 " values ('2026-11-20', 'WEEKLY_RECON', '{}', 'A0001')")
    assert codes.check(conn) == ["SNAPSHOT_KIND: broker_snapshots.kind에 저장돼 있는데 코드명이 없는 값 WEEKLY_RECON"]

    conn.execute("insert into common_codes (group_id, code, name) values"
                 " ('SNAPSHOT_KIND', 'WEEKLY_RECON', '주간 대사'), ('ACCOUNT_STATUS', 'FROZEN', '동결'),"
                 " ('RECON_CAUSE', 'FAT_FINGER', '주문 실수')")
    problems = codes.check(conn)
    assert "ACCOUNT_STATUS: 공통코드에는 있는데 프로그램에 없는 코드 FROZEN" in problems
    assert "ACCOUNT_STATUS: 공통코드에는 있는데 스키마 제약(account_status_history.status)에 없는 코드 FROZEN" in problems
    assert "RECON_CAUSE: 공통코드에는 있는데 프로그램에 없는 코드 FAT_FINGER" in problems
    assert not any(p.startswith("SNAPSHOT_KIND") for p in problems)

    conn.execute("insert into code_groups (group_id, name, used_by) values ('GHOST', '없는 컬럼', 'no_table.no_column')")
    assert "GHOST: 쓰는 곳 no_table.no_column이 스키마에 없어요" in codes.check(conn)


# ---------- changing ----------

def test_a_code_is_renamed_or_switched_off_never_removed(conn):
    before = conn.execute("select updated_at from common_codes where group_id = 'RECON_CAUSE'"
                          " and code = 'TIMING'").fetchone()[0]
    assert codes.rename(conn, "RECON_CAUSE", "TIMING", "조회 시점 차이") == "시점 차이"
    name, desc, after = conn.execute("select name, description, updated_at from common_codes"
                                     " where group_id = 'RECON_CAUSE' and code = 'TIMING'").fetchone()
    assert name == "조회 시점 차이" and desc and after > before          # the description stays, the time moves
    assert codes.check(conn) == []                                    # a name is free to change

    for statement in ("delete from common_codes where code = 'TIMING'",
                      "update common_codes set code = 'LATE' where code = 'TIMING'",
                      "update common_codes set group_id = 'RECON_ITEM' where code = 'TIMING'",
                      "truncate common_codes",
                      "delete from code_groups where group_id = 'RECON_CAUSE'",
                      "update code_groups set fixed = false where group_id = 'RECON_CAUSE'"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(statement)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("update common_codes set name = '' where code = 'TIMING'")


def test_program_codes_are_fixed_and_operator_groups_are_open(conn):
    with pytest.raises(OpsError, match="프로그램이 정한 코드"):
        codes.add_code(conn, "ACCOUNT_STATUS", "FROZEN", "동결")
    with pytest.raises(OpsError, match="끌 수 없어요"):
        codes.set_in_use(conn, "ACCOUNT_STATUS", "CLOSED", False)
    with pytest.raises(OpsError, match="없는 코드 그룹"):
        codes.list_codes(conn, "NOPE")
    with pytest.raises(OpsError, match="없는 코드예요"):
        codes.rename(conn, "ACCOUNT_STATUS", "FROZEN", "동결")
    with pytest.raises(OpsError, match="코드 그룹 ID는"):
        codes.add_group(conn, "memo kind", "메모 분류")

    codes.add_group(conn, "MEMO_KIND", "메모 분류", "운영 메모를 나누는 기준")
    codes.add_code(conn, "MEMO_KIND", "CHECK", "확인 필요")
    codes.add_code(conn, "MEMO_KIND", "DONE", "처리 완료", "더 볼 것 없음")
    with pytest.raises(OpsError, match="이미 있는 코드"):
        codes.add_code(conn, "MEMO_KIND", "DONE", "끝")
    codes.set_in_use(conn, "MEMO_KIND", "CHECK", False)
    assert codes.list_codes(conn, "MEMO_KIND") == [("CHECK", "확인 필요", None, False),
                                                   ("DONE", "처리 완료", "더 볼 것 없음", True)]
    assert codes.check(conn) == []                                    # an operator group answers to nobody


# ---------- the console ----------

def test_code_commands(conn, capsys):
    assert run("code", "list") == 0
    out = capsys.readouterr().out
    assert "ACCOUNT_STATUS  계좌 상태: 코드 3개, 쓰는 곳 account_status_history.status" in out
    assert "계정과목은 원장이 직접 참조하는 코드라서 전용 테이블(accounts_chart)" in out
    assert run("code", "list", "RECON_STATUS") == 0
    assert "BREAK  불일치" in capsys.readouterr().out

    assert run("code", "rename", "RECON_STATUS", "BREAK", "차이 있음") == 0
    assert "'불일치' → '차이 있음'" in capsys.readouterr().out
    assert run("code", "group-add", "MEMO_KIND", "메모 분류") == 0 and run("code", "add", "MEMO_KIND", "A", "첫째") == 0
    assert run("code", "use", "MEMO_KIND", "A", "off") == 0
    capsys.readouterr()
    assert run("code", "list", "MEMO_KIND") == 0 and "[사용 안 함]" in capsys.readouterr().out
    assert run("code", "check") == 0
    seeded = sum(len(g.codes) for g in codes.SEED)
    assert f"공통코드 22개 그룹, 코드 {seeded + 1}개가" in capsys.readouterr().out

    assert run("code", "add", "RECON_STATUS", "MAYBE", "글쎄") == 1
    assert "프로그램이 정한 코드" in capsys.readouterr().out
    conn.execute("insert into common_codes (group_id, code, name) values ('RECON_STATUS', 'MAYBE', '글쎄')")
    assert run("code", "check") == 1
    assert "문제  RECON_STATUS: 공통코드에는 있는데 프로그램에 없는 코드 MAYBE" in capsys.readouterr().out


# ---------- the screen ----------

def test_the_code_screen_lists_the_chart_of_accounts_beside_the_common_codes(conn):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from ministp.ui import app as ui_app
    from ministp.ui.definitions import load_screens

    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, allowed_hosts=["testserver"]))

    def rows(tx, params=None):
        r = client.post(f"/api/tx/{tx}", json={"params": params or {}})
        assert r.status_code == 200, r.text
        ds = r.json()["datasets"]["main"]
        return [dict(zip(ds["columns"], row)) for row in ds["rows"]], r.json()

    screen = load_screens()["STP-4600"]
    assert screen.layout == "split" and [(g.id, g.load) for g in screen.grids] == [("groups", "open"), ("codes", "master")]

    groups, body = rows("STP4600R01")
    assert body["meta"]["consistent"] is True
    assert (groups[0]["group_id"], groups[0]["name"], groups[0]["store"], groups[0]["codes"]) == (
        "accounts_chart", "계정과목", "전용 테이블", 15)
    assert [g["group_id"] for g in groups[1:]] == sorted(g.group_id for g in codes.SEED)
    assert {g["store"] for g in groups[1:]} == {"공통코드"}

    chart, _ = rows("STP4600R02", {"group_id": "accounts_chart"})
    assert chart[0] == {"code": "1010", "name": "예수금", "description": "정상잔액 차변", "in_use": "사용"}
    status, _ = rows("STP4600R02", {"group_id": "ACCOUNT_STATUS"})
    assert [(s["code"], s["name"]) for s in status] == [("OPEN", "정상"), ("SUSPENDED", "거래정지"), ("CLOSED", "해지")]
    assert client.post("/api/tx/STP4600R02", json={"params": {"group_id": "NOPE"}}).status_code == 400
    assert client.post("/api/tx/STP4600R02", json={"params": {}}).status_code == 400

    with psycopg.connect(URL, autocommit=True) as ro:                 # a rename moves the live head
        before = ui_app.read_head(ro)
        codes.rename(conn, "ACCOUNT_STATUS", "SUSPENDED", "정지")
        assert ui_app.read_head(ro) != before
    conn.execute("insert into common_codes (group_id, code, name) values ('ACCOUNT_STATUS', 'FROZEN', '동결')")
    _, body = rows("STP4600R01")
    assert body["meta"]["consistent"] is False and "FROZEN" not in json.dumps(body["meta"])
