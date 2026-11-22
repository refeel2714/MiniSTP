"""Migrations on a sample database: rehearsal, the unchanged-rows proof, and the record of what ran."""
import os

import pytest

from ministp import __main__ as cli
from ministp.ops import load_books
from ministp.sample import build_sample
from ministp.store import migrate as mg

psycopg = pytest.importorskip("psycopg")

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

LEDGER_TABLES = ["ledger_events", "journal_entries", "position_ledger", "cash_ledger", "oms_orders",
                 "broker_snapshots", "recon_results"]
# The step the 다계좌 design (ERD v2.0) relies on: a NOT NULL column with a default on append-only tables.
ADD_ACCOUNT = "-- title: 계좌ID 추가\n-- erd: 2.0\n" + "".join(
    f"alter table {t} add column account_id text not null default 'A0001';\n"
    f"alter table {t} alter column account_id drop default;\n" for t in LEDGER_TABLES)
CLEANUP = (f"drop table if exists {mg.TABLE}; drop table if exists mig_note; drop table if exists mig_scratch;"
           + "".join(f"alter table {t} drop column if exists account_id;" for t in LEDGER_TABLES))


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        build_sample(c, CAL, FEES, days=6, seed=3)
        c.execute(CLEANUP)
        c.execute("create table mig_scratch (id int primary key, v text); insert into mig_scratch values (1, 'a'), (2, 'b')")
        yield c
        c.execute(CLEANUP)


def _state(conn):
    books = load_books(conn, CAL, FEES)
    led = books.ledger
    return (len(led.events), {s: (p.qty, p.book_cost) for s, p in led.positions().items()}, led.cash_balance(),
            [(r.date, r.nav_per_1000) for r in led.nav_history()])


def _mig(version, name, text):
    return mg.parse(f"{version:04d}_{name}.sql", text)


def test_status_on_a_fresh_database_changes_nothing(conn):
    assert mg.applied(conn) == {} and mg.pending(conn, []) == []
    assert mg.migrate(conn, []) == []
    assert not mg._has_table(conn, mg.TABLE)        # looking never creates the record table


def test_rehearsal_adds_the_account_column_and_leaves_no_trace(conn):
    before, prints = _state(conn), mg.fingerprint(conn)
    (report,) = mg.migrate(conn, [_mig(1, "accounts", ADD_ACCOUNT)], dry_run=True)
    assert not report.applied
    kept = {t.table: t.rows_before for t in report.kept}
    assert set(LEDGER_TABLES) <= set(kept) and kept["ledger_events"] == before[0]
    assert mg.fingerprint(conn) == prints and _state(conn) == before
    assert not mg._has_table(conn, mg.TABLE)
    assert "account_id" not in mg._tables(conn)["ledger_events"]


def test_applying_keeps_every_existing_row_and_records_the_migration(conn):
    prints = mg.fingerprint(conn)
    mig = _mig(1, "accounts", ADD_ACCOUNT)
    (report,) = mg.migrate(conn, [mig])
    assert report.applied and all(t.unchanged for t in report.tables)
    assert mg.fingerprint(conn, like=prints) == prints          # same rows over the old columns
    assert conn.execute("select distinct account_id from ledger_events").fetchall() == [("A0001",)]
    assert mg.applied(conn) == {1: ("accounts", mig.checksum)}
    assert conn.execute(f"select erd_version from {mg.TABLE}").fetchone() == ("2.0",)
    assert mg.migrate(conn, [mig]) == []                         # nothing left to do
    with pytest.raises(psycopg.errors.NotNullViolation):         # the default is gone: new rows must name an account
        conn.execute("insert into broker_snapshots (date, kind, payload) values (current_date, 'X', '{}')")


def test_a_migration_that_changes_existing_rows_is_rolled_back(conn):
    bad = _mig(1, "rewrite", "create table mig_note (id int);\nupdate mig_scratch set v = 'z' where id = 1;")
    with pytest.raises(mg.MigrationError, match="mig_scratch: 기존 행의 내용이 바뀌었어요"):
        mg.migrate(conn, [bad])
    assert conn.execute("select v from mig_scratch where id = 1").fetchone() == ("a",)
    assert not mg._has_table(conn, "mig_note") and mg.applied(conn) == {}
    with pytest.raises(mg.MigrationError, match="행 수가 2 → 1"):
        mg.migrate(conn, [_mig(1, "drop_row", "delete from mig_scratch where id = 2;")])


def test_a_declared_data_change_is_allowed_and_reported(conn):
    ok = _mig(1, "rewrite", "-- changes-data: mig_scratch\nupdate mig_scratch set v = 'z' where id = 1;")
    (report,) = mg.migrate(conn, [ok])
    row = next(t for t in report.tables if t.table == "mig_scratch")
    assert row.declared and not row.unchanged
    with pytest.raises(mg.MigrationError, match="changes-data에 적힌 테이블이 없어요"):
        mg.migrate(conn, [ok, _mig(2, "typo", "-- changes-data: no_such_table\nselect 1;")])


def test_append_only_tables_cannot_be_rewritten_even_when_declared(conn):
    bad = _mig(1, "rewrite", "-- changes-data: ledger_events\nupdate ledger_events set event_type = 'X';")
    with pytest.raises(mg.MigrationError, match="SQL 오류로 되돌렸어요"):
        mg.migrate(conn, [bad])
    assert mg.applied(conn) == {}


def test_a_failure_in_the_second_migration_keeps_the_first(conn):
    first, second = _mig(1, "note", "create table mig_note (id int);"), _mig(2, "broken", "select no_such_function();")
    with pytest.raises(mg.MigrationError, match="0002"):
        mg.migrate(conn, [first, second])
    assert list(mg.applied(conn)) == [1] and mg._has_table(conn, "mig_note")
    assert [m.version for m in mg.pending(conn, [first, second])] == [2]


def test_an_applied_file_must_not_be_edited(conn):
    first = _mig(1, "note", "create table mig_note (id int);")
    mg.migrate(conn, [first])
    with pytest.raises(mg.MigrationError, match="파일 내용이 달라졌어요"):
        mg.pending(conn, [_mig(1, "note", "create table mig_note (id int, extra text);")])
    with pytest.raises(mg.MigrationError, match="코드에는 없어요"):
        mg.pending(conn, [])


def test_rehearsal_checks_deferred_constraints_too(conn):
    """The journal balance check is deferred to commit; a rehearsal never commits, so it is forced."""
    seq = conn.execute("select max(seq) from ledger_events").fetchone()[0]
    bad = _mig(1, "unbalanced", "-- changes-data: journal_entries\n"
               f"insert into journal_entries (event_seq, date, kind, source_id) values ({seq}, current_date, 'X', 'x');")
    with pytest.raises(mg.MigrationError, match="unbalanced"):
        mg.migrate(conn, [bad], dry_run=True)


def test_db_migrate_command_reports_nothing_to_do(conn, capsys):
    rc = cli.run(["db-migrate"], environ={"MINISTP_DATABASE_URL": URL})
    out = capsys.readouterr().out
    assert rc == 0 and "적용할 마이그레이션이 없어요" in out and "남은 것 0개" in out
    assert not mg._has_table(conn, mg.TABLE)
