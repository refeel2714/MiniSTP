"""다계좌 (ERD v2.0): the shipped migration on a real v1.1 database, and two accounts in one ledger.

`fixtures/sample_v1_1.sql` is a dump of a database built by the v1.1 code (6 business days of sample
trading). It is the stand-in for the operating database: the migration is rehearsed and applied on it.
"""
import datetime as dt
import os
from pathlib import Path

import pytest

from ministp import __main__ as cli
from ministp.ledger import LedgerHaltedError, Side
from ministp.ops import OpsError, load_books, load_instruments
from ministp.store import DEFAULT_ACCOUNT, PostgresLedgerStore, SchemaError, migrate as mg
from ministp.store.integrity import integrity_report

psycopg = pytest.importorskip("psycopg")

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

FIXTURE = Path(__file__).parent / "fixtures" / "sample_v1_1.sql"
V1_TABLES = ["accounts_chart", "broker_snapshots", "cash_ledger", "instruments", "journal_entries", "journal_lines",
             "ledger_events", "oms_order_acks", "oms_orders", "position_ledger", "recon_results"]
WITH_ACCOUNT = ["ledger_events", "journal_entries", "position_ledger", "cash_ledger", "oms_orders",
                "broker_snapshots", "recon_results"]
NEW_TABLES = ["customers", "trading_accounts", "account_status_history"]
FIRST_DAY = dt.date(2026, 9, 1)


def _restore_v1_1() -> None:
    with psycopg.connect(URL, autocommit=True) as c:      # the dump empties search_path: use the session once
        c.execute("drop schema if exists public cascade")
        c.execute(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def old():
    """A connection to a v1.1 database that holds a ledger, as the operating database does today."""
    _restore_v1_1()
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


@pytest.fixture
def fresh():
    """A new database built by `db-init` at the current version."""
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        PostgresLedgerStore(c).init_schema()
        yield c


def _shape(conn) -> dict:
    """Everything that defines the schema, read from the catalog, without the rows."""
    q = lambda sql: conn.execute(sql).fetchall()   # noqa: E731
    return {
        "columns": q("""select c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                               pg_get_expr(d.adbin, d.adrelid),
                               row_number() over (partition by c.oid order by a.attnum)
                          from pg_class c join pg_namespace n on n.oid = c.relnamespace
                          join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
                          left join pg_attrdef d on d.adrelid = c.oid and d.adnum = a.attnum
                         where n.nspname = 'public' and c.relkind in ('r', 'v') order by 1, 6"""),
        "constraints": q("""select c.relname, k.conname, pg_get_constraintdef(k.oid), k.condeferrable, k.condeferred
                              from pg_constraint k join pg_class c on c.oid = k.conrelid
                              join pg_namespace n on n.oid = c.relnamespace
                             where n.nspname = 'public' order by 1, 2"""),
        "indexes": q("select tablename, indexname, indexdef from pg_indexes where schemaname = 'public' order by 1, 2"),
        "triggers": q("""select c.relname, t.tgname, pg_get_triggerdef(t.oid), t.tgenabled
                           from pg_trigger t join pg_class c on c.oid = t.tgrelid
                           join pg_namespace n on n.oid = c.relnamespace
                          where n.nspname = 'public' and not t.tgisinternal order by 1, 2"""),
        "views": q("select viewname, definition from pg_views where schemaname = 'public' order by 1"),
        "functions": q("""select p.proname, p.prosrc from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                           where n.nspname = 'public' order by 1"""),
        "sequences": q("select sequencename from pg_sequences where schemaname = 'public' order by 1"),
        "comments": q("""select c.relname, coalesce(a.attname, ''), d.description
                           from pg_description d join pg_class c on c.oid = d.objoid and d.classoid = 'pg_class'::regclass
                           join pg_namespace n on n.oid = c.relnamespace
                           left join pg_attribute a on a.attrelid = c.oid and a.attnum = d.objsubid and d.objsubid > 0
                          where n.nspname = 'public' order by 1, 2"""),
    }


def _report(conn) -> list:
    return integrity_report(conn, CAL, FEES, load_instruments(conn))


def _open_account(conn, account_id, customer_id="C0001") -> PostgresLedgerStore:
    conn.execute("insert into customers (customer_id, alias) values (%s, %s) on conflict do nothing",
                 (customer_id, f"고객 {customer_id}"))
    conn.execute("insert into trading_accounts (account_id, customer_id, alias, broker, env, account_type,"
                 " credential_ref, opened_on) values (%s, %s, %s, 'KIS', 'PAPER', 'GENERAL', %s, %s)",
                 (account_id, customer_id, f"계좌 {account_id}", f"KIS_{account_id}", FIRST_DAY))
    conn.execute("insert into account_status_history (account_id, status, effective_from) values (%s, 'OPEN', %s)",
                 (account_id, FIRST_DAY))
    return PostgresLedgerStore(conn, account_id)


# ---------- the migration on a v1.1 database ----------

def test_the_fixture_is_a_v1_1_database_with_a_ledger(old):
    assert sorted(mg._tables(old)) == V1_TABLES
    assert old.execute("select count(*), min((payload->>'date')::date) from ledger_events").fetchone() == (29, FIRST_DAY)
    assert "account_id" not in mg._tables(old)["ledger_events"]


def test_rehearsal_on_v1_1_passes_and_leaves_no_trace(old):
    before, shape = mg.fingerprint(old), _shape(old)
    report, *later = mg.migrate(old, dry_run=True)
    assert not report.applied and report.migration.erd_version == "2.0"
    assert sorted(t.table for t in report.kept) == V1_TABLES and all(t.unchanged for t in report.kept)
    assert sorted(t.table for t in report.tables if t.rows_before is None) == sorted(NEW_TABLES)
    assert [r.migration.erd_version for r in later] == ["2.1", "2.2", "2.3", "2.4", "2.5", "2.6"] and not any(r.applied for r in later)
    assert mg.fingerprint(old) == before and _shape(old) == shape
    assert not mg._has_table(old, mg.TABLE)


def test_applying_keeps_every_row_and_puts_the_ledger_in_the_default_account(old):
    before = mg.fingerprint(old)
    chart = old.execute("select code, name, normal_side from accounts_chart order by code").fetchall()
    report, *later = mg.migrate(old)
    assert report.applied and all(t.unchanged for t in report.kept)
    assert all(r.applied and all(t.unchanged for t in r.kept) for r in later)   # 0002 공통코드: new tables only
    after = mg.fingerprint(old, like=before)                     # same rows over the v1.1 columns...
    assert {t: p for t, p in after.items() if t != "accounts_chart"} == {
        t: p for t, p in before.items() if t != "accounts_chart"}
    # ...and the one v1.1 table that grew (0007: three accounts for adjustments) still holds its fifteen as they were
    assert old.execute("select code, name, normal_side from accounts_chart where code = any(%s) order by code",
                       ([c for c, _, _ in chart],)).fetchall() == chart
    assert old.execute("select code from accounts_chart where code <> all(%s) order by code",
                       ([c for c, _, _ in chart],)).fetchall() == [("4040",), ("4090",), ("5090",)]
    for t in WITH_ACCOUNT:
        assert old.execute(f"select distinct account_id from {t}").fetchall() == [(DEFAULT_ACCOUNT,)], t
    assert old.execute("select customer_id, broker, env, account_type, credential_ref, opened_on"
                       " from trading_accounts").fetchall() == [("C0001", "KIS", "PAPER", "GENERAL", "KIS", FIRST_DAY)]
    assert old.execute("select account_id, customer_id, status, effective_from from account_status_v").fetchall() == [
        (DEFAULT_ACCOUNT, "C0001", "OPEN", FIRST_DAY)]
    assert list(mg.applied(old)) == [1, 2, 3, 4, 5, 6, 7] and mg.migrate(old) == []
    checks = _report(old)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_a_migrated_database_has_the_same_schema_as_a_new_one(old, fresh):
    """schema.sql (new databases) and the migrations (existing ones) must arrive at the same place."""
    new = _shape(fresh)
    new_rows = {t: fresh.execute(f"select * from {t} order by 1").fetchall() for t in ("accounts_chart",)}
    new_record = fresh.execute(f"select version, name, erd_version, checksum from {mg.TABLE}").fetchall()
    _restore_v1_1()
    with psycopg.connect(URL, autocommit=True) as c:
        mg.migrate(c)
        migrated = _shape(c)
        for part in new:
            assert migrated[part] == new[part], part
        assert {t: c.execute(f"select * from {t} order by 1").fetchall() for t in new_rows} == new_rows
        assert c.execute(f"select version, name, erd_version, checksum from {mg.TABLE}").fetchall() == new_record


def test_the_ledger_keeps_working_after_the_migration(old):
    mg.migrate(old)
    books = load_books(old, CAL, FEES)
    n = len(books.ledger.events)
    assert n == 29 and books.account_id == DEFAULT_ACCOUNT
    day = CAL.add_business_days(max(e.date for e in books.ledger.journal), 1)
    books.ledger.deposit(day, 1_000_000)
    assert old.execute("select seq, account_id from ledger_events order by seq desc limit 1").fetchone() == (
        n + 1, DEFAULT_ACCOUNT)
    assert all(c.ok for c in _report(old))
    assert len(load_books(old, CAL, FEES).ledger.events) == n + 1


def test_code_ahead_of_the_database_refuses_instead_of_guessing(old, capsys):
    """Before `db-migrate`: nothing reads or writes the ledger, and `db-init` does not rebuild it."""
    before, shape = mg.fingerprint(old), _shape(old)
    with pytest.raises(OpsError, match="db-migrate"):
        load_books(old, CAL, FEES)
    with pytest.raises(SchemaError, match="db-migrate"):
        PostgresLedgerStore(old).init_schema()
    assert cli.run(["db-init"], environ={"MINISTP_DATABASE_URL": URL}) == 1
    assert "db-migrate" in capsys.readouterr().out
    assert mg.fingerprint(old) == before and _shape(old) == shape


def test_db_migrate_and_verify_commands_on_v1_1(old, capsys):
    env = {"MINISTP_DATABASE_URL": URL}
    assert cli.run(["verify"], environ=env) == 1                 # the code is ahead: it says so instead of guessing
    assert "db-migrate --dry-run" in capsys.readouterr().out
    assert cli.run(["db-migrate", "--dry-run"], environ=env) == 0
    out = capsys.readouterr().out
    assert "대기: 0001 다계좌" in out and "ERD v2.0" in out and "리허설 통과" in out and "새 테이블 customers" in out
    assert "적용한 상태에서 원장 무결성 점검 14개 통과 (계좌 1개)" in out
    assert not mg._has_table(old, mg.TABLE)
    assert cli.run(["db-migrate", "--apply"], environ=env) == 0
    out = capsys.readouterr().out
    assert out.index("리허설(되돌림): 0001") < out.index("적용: 0001") < out.index("마이그레이션 완료")
    assert "적용된 DB에서 원장 무결성 점검 14개 통과" in out and out.count("14개 통과") == 2   # rehearsed, then applied
    assert cli.run(["verify"], environ=env) == 0
    out = capsys.readouterr().out
    assert "문제" not in out and "16개 테이블" in out and "[A0001]" not in out      # one account: no prefix


def test_apply_refuses_a_database_whose_ledger_would_not_verify_afterwards(old, capsys):
    """`--apply` rehearses first, with the integrity checks on the migrated state; a failure applies nothing."""
    old.execute("alter table cash_ledger disable trigger user")
    old.execute("delete from cash_ledger where id = (select max(id) from cash_ledger)")
    old.execute("alter table cash_ledger enable trigger user")
    before, shape = mg.fingerprint(old), _shape(old)
    assert cli.run(["db-migrate", "--apply"], environ={"MINISTP_DATABASE_URL": URL}) == 1
    out = capsys.readouterr().out
    assert "문제  현금" in out and "무결성 점검 1건이 어긋나서 되돌렸어요" in out and "적용: 0001" not in out
    assert mg.fingerprint(old) == before and _shape(old) == shape and not mg._has_table(old, mg.TABLE)


# ---------- two accounts in one ledger ----------

@pytest.fixture
def two(fresh, calendar, instruments):
    """Two accounts of one customer plus one of another, writing in turn into the one event stream."""
    from ministp.fees import FeeSchedule
    fees = FeeSchedule.spec_v1()
    stores = {DEFAULT_ACCOUNT: PostgresLedgerStore(fresh), "A0002": _open_account(fresh, "A0002"),
              "A0003": _open_account(fresh, "A0003", "C0002")}
    ledgers = {a: s.load(calendar, fees, instruments) for a, s in stores.items()}
    day = dt.date(2026, 11, 2)
    for a, amount in ((DEFAULT_ACCOUNT, 10_000_000), ("A0002", 5_000_000), ("A0003", 3_000_000)):
        ledgers[a].deposit(day, amount)
    ledgers[DEFAULT_ACCOUNT].execute("X1", day, "E", Side.BUY, 100, 10_000)
    ledgers["A0002"].execute("X1", day, "E", Side.BUY, 30, 10_100)       # same broker exec id, another account
    ledgers[DEFAULT_ACCOUNT].execute("X2", day, "A", Side.BUY, 10, 50_000)
    ledgers["A0003"].execute("X9", day, "E", Side.BUY, 7, 10_050)
    return fresh, stores, ledgers, (calendar, fees, instruments)


def test_accounts_share_one_sequence_and_each_replays_only_its_own_events(two):
    conn, stores, ledgers, (calendar, fees, instruments) = two
    rows = conn.execute("select seq, account_id from ledger_events order by seq").fetchall()
    assert [s for s, _ in rows] == list(range(1, 8))
    assert [a for _, a in rows] == ["A0001", "A0002", "A0003", "A0001", "A0002", "A0001", "A0003"]
    for acct, store in stores.items():
        assert store.verify(ledgers[acct]) == []
        again = PostgresLedgerStore(conn, acct).load(calendar, fees, instruments)
        assert again.journal == ledgers[acct].journal and again.cash_balance() == ledgers[acct].cash_balance()
    assert dict(conn.execute("select account_id, balance from cash_balance_v")) == {
        a: led.cash_balance() for a, led in ledgers.items()}
    assert conn.execute("select account_id, symbol, qty from positions_v order by 1, 2").fetchall() == [
        ("A0001", "A", 10), ("A0001", "E", 100), ("A0002", "E", 30), ("A0003", "E", 7)]


def test_the_integrity_report_checks_every_account(two):
    conn, _, _, (calendar, fees, instruments) = two
    checks = integrity_report(conn, calendar, fees, instruments)
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert [c.account for c in checks] == [""] * 6 + ["A0001"] * 8 + ["A0002"] * 8 + ["A0003"] * 8
    # a row moved to the wrong account is found in both accounts
    with conn.transaction(force_rollback=True):
        conn.execute("alter table cash_ledger disable trigger user")
        conn.execute("alter table cash_ledger drop constraint cash_ledger_event_account_fkey")
        conn.execute("update cash_ledger set account_id = 'A0002' where id = (select min(id) from cash_ledger"
                     " where account_id = 'A0001')")
        bad = [(c.account, c.key) for c in integrity_report(conn, calendar, fees, instruments) if not c.ok]
    assert ("A0001", "cash") in bad and ("A0002", "cash") in bad and ("A0003", "cash") not in bad


def test_customer_view_sums_the_accounts_of_one_customer(two):
    conn = two[0]
    assert conn.execute("select customer_id, symbol, qty, book_cost from customer_positions_v order by 1, 2").fetchall() == [
        ("C0001", "A", 10, two[2]["A0001"].positions()["A"].book_cost),
        ("C0001", "E", 130, two[2]["A0001"].positions()["E"].book_cost + two[2]["A0002"].positions()["E"].book_cost),
        ("C0002", "E", 7, two[2]["A0003"].positions()["E"].book_cost)]


def test_a_derived_row_cannot_point_at_another_accounts_event(two):
    conn = two[0]
    seq = conn.execute("select min(seq) from ledger_events where account_id = 'A0001'").fetchone()[0]
    for sql in ("insert into cash_ledger (event_seq, date, reason, amount, ref_id, account_id)"
                " values (%s, current_date, 'DEPOSIT', 1, 'x', 'A0002')",
                "insert into position_ledger (event_seq, trade_date, symbol, exec_id, qty_delta, cost_delta,"
                " realized_pnl, account_id) values (%s, current_date, 'E', 'Z1', 1, 1, 0, 'A0002')",
                "insert into journal_entries (event_seq, date, kind, source_id, account_id)"
                " values (%s, current_date, 'X', 'x', 'A0002')"):
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute(sql, (seq,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):      # an account that does not exist
        conn.execute("insert into ledger_events (seq, event_type, payload, account_id)"
                     " values (999, 'X', '{}', 'A9999')")


def test_the_same_exec_id_is_refused_only_within_one_account(two):
    conn, _, ledgers, _ = two
    n = conn.execute("select count(*) from ledger_events").fetchone()[0]
    with pytest.raises(Exception):
        ledgers["A0002"].execute("X1", dt.date(2026, 11, 2), "E", Side.BUY, 1, 10_000)
    assert conn.execute("select count(*) from ledger_events").fetchone()[0] == n
    assert conn.execute("select account_id from position_ledger where exec_id = 'X1' order by 1").fetchall() == [
        ("A0001",), ("A0002",)]


def test_a_stale_ledger_of_one_account_halts_and_the_other_account_goes_on(two):
    """Two sessions on one account: the second to write is behind, so it stops instead of writing on top."""
    conn, stores, ledgers, (calendar, fees, instruments) = two
    day = dt.date(2026, 11, 3)
    stale = PostgresLedgerStore(conn, "A0002").load(calendar, fees, instruments)
    ledgers["A0002"].deposit(day, 1)
    n = conn.execute("select count(*) from ledger_events").fetchone()[0]
    with pytest.raises(LedgerHaltedError, match="account A0002 has 3 stored events"):
        stale.deposit(day, 2)
    with pytest.raises(LedgerHaltedError):
        stale.deposit(day, 3)
    assert conn.execute("select count(*) from ledger_events").fetchone()[0] == n
    ledgers[DEFAULT_ACCOUNT].deposit(day, 4)                     # another account is not affected
    assert conn.execute("select max(seq), count(*) from ledger_events").fetchone() == (n + 1, n + 1)
    assert all(c.ok for c in integrity_report(conn, calendar, fees, instruments))


def test_account_and_customer_rows_are_append_only_and_shaped(fresh):
    for t in NEW_TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            fresh.execute(f"delete from {t}")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        fresh.execute("update trading_accounts set alias = 'x'")
    with pytest.raises(psycopg.errors.CheckViolation):
        fresh.execute("insert into customers (customer_id, alias) values ('홍길동', 'x')")
    with pytest.raises(psycopg.errors.UniqueViolation):          # one broker key cannot serve two accounts
        fresh.execute("insert into trading_accounts (account_id, customer_id, alias, broker, env, account_type,"
                      " credential_ref, opened_on) values ('A0002', 'C0001', 'x', 'KIS', 'PAPER', 'GENERAL', 'KIS',"
                      " current_date)")
    cols = set(mg._tables(fresh)["trading_accounts"]) | set(mg._tables(fresh)["customers"])
    assert not cols & {"account_no", "app_key", "app_secret", "name", "rrn", "phone"}   # no secrets, no identity
