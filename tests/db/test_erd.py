"""The committed ERD must match the schema, with a Korean name on everything."""
import os
from pathlib import Path

import pytest

from ministp.store import erd

psycopg = pytest.importorskip("psycopg")
URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
DOCS = Path(__file__).resolve().parents[2] / "docs" / "erd"


@pytest.fixture
def conn():
    with psycopg.connect(URL) as c:
        yield c


def test_committed_erd_is_current(conn):
    committed = (DOCS / "ERD.md").read_text(encoding="utf-8")
    fresh = erd.generate(conn, (DOCS / "HISTORY.md").read_text(encoding="utf-8"), committed)
    assert fresh == committed, "스키마가 바뀌었어요: python scripts/gen_erd.py 로 ERD를 다시 만들어 주세요"


def test_every_table_and_column_has_a_korean_name(conn):
    catalog = erd.build_catalog(conn, erd.schema_sql())
    assert len([t for t in catalog.values() if t["kind"] == "table"]) == 17   # v2.0: +고객, 계좌, 상태 이력, 마이그레이션 기록 · v2.1: +공통코드 그룹, 공통코드
    assert erd.missing_names(catalog) == []


def test_missing_comment_is_reported(conn):
    sql = erd.schema_sql() + "\ncreate table extra_t (x int);\n"
    assert set(erd.missing_names(erd.build_catalog(conn, sql))) == {"extra_t", "extra_t.x"}


def test_schema_change_needs_a_version_bump(conn, monkeypatch):
    committed = (DOCS / "ERD.md").read_text(encoding="utf-8")
    changed = (f"-- ERD version: {erd.schema_version(erd.schema_sql())}\ncreate table t (x int);\n"
               "comment on table t is 't'; comment on column t.x is 'x';")
    monkeypatch.setattr(erd, "schema_sql", lambda: changed)
    with pytest.raises(erd.ErdError, match="버전"):
        erd.generate(conn, (DOCS / "HISTORY.md").read_text(encoding="utf-8"), committed)


def test_committed_html_is_current(conn):
    sql = erd.schema_sql()
    html = erd.render_html(erd.build_catalog(conn, sql), erd.schema_version(sql), erd.schema_hash(sql),
                           erd.history_rows((DOCS / "HISTORY.md").read_text(encoding="utf-8")))
    assert html == (DOCS / "erd.html").read_text(encoding="utf-8"), "python scripts/gen_erd.py 로 다시 만들어 주세요"
