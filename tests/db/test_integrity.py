"""Integrity report on a sample database: clean as built, and every kind of damage is named."""
import os

import pytest

from ministp import __main__ as cli
from ministp.ops import OpsError, load_books, load_instruments
from ministp.sample import build_sample
from ministp.store import migrate as mg
from ministp.store.integrity import integrity_report

psycopg = pytest.importorskip("psycopg")

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        build_sample(c, CAL, FEES, days=6, seed=3)
        yield c


def report(conn) -> dict:
    return {c.key: c for c in integrity_report(conn, CAL, FEES, load_instruments(conn))}


class damaged:
    """Run statements that damage the ledger inside a transaction that is always rolled back."""
    def __init__(self, conn, *statements):
        self.conn, self.statements = conn, statements

    def __enter__(self):
        self.tx = self.conn.transaction(force_rollback=True)
        self.tx.__enter__()
        for s in self.statements:
            self.conn.execute(s)
        return self.conn

    def __exit__(self, *exc):
        return self.tx.__exit__(*exc)


def test_a_database_built_by_the_real_operations_is_clean(conn):
    r = report(conn)
    assert list(r) == ["events", "guards", "balanced", "journal", "accounts", "positions", "cash"]
    assert all(c.ok for c in r.values()), [c for c in r.values() if not c.ok]
    assert "빈 번호 없음" in r["events"].detail and "10개 테이블" in r["guards"].detail


def test_the_report_only_reads(conn):
    before = mg.fingerprint(conn)
    report(conn)
    assert mg.fingerprint(conn) == before
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_a_switched_off_guard_is_named(conn):
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only"):
        r = report(conn)
        assert not r["guards"].ok and "cash_ledger_append_only" in r["guards"].detail
        assert r["cash"].ok          # nothing was changed yet: only the guard is off
    assert report(conn)["guards"].ok


def test_a_changed_cash_row_is_found_and_stops_every_command(conn):
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only",
                 "update cash_ledger set amount = amount + 1 where id = (select min(id) from cash_ledger)"):
        r = report(conn)
        assert not r["cash"].ok and "저장에만" in r["cash"].detail and "재생에만" in r["cash"].detail
        assert r["journal"].ok       # the journal itself was not touched
        with pytest.raises(OpsError, match="현금"):
            load_books(conn, CAL, FEES)
    assert report(conn)["cash"].ok


def test_a_deleted_position_row_is_found(conn):
    with damaged(conn, "alter table position_ledger disable trigger position_ledger_append_only",
                 "delete from position_ledger where id = (select max(id) from position_ledger)"):
        r = report(conn)
        assert not r["positions"].ok and "재생에만" in r["positions"].detail


def test_a_gap_in_event_numbers_is_found_and_nothing_is_replayed(conn):
    with damaged(conn, "insert into ledger_events (seq, event_type, payload) select max(seq) + 2, 'DepositEvent',"
                       " '{\"date\": \"2026-11-02\", \"amount\": 1}' from ledger_events"):
        r = report(conn)
        assert not r["events"].ok and "빈 번호" in r["events"].detail
        assert not r["replay"].ok and "journal" not in r


def test_an_event_without_its_derived_rows_is_found(conn):
    with damaged(conn, "insert into ledger_events (seq, event_type, payload) select max(seq) + 1, 'DepositEvent',"
                       " '{\"date\": \"2026-11-02\", \"amount\": 1000}' from ledger_events"):
        r = report(conn)
        assert r["events"].ok
        assert not r["journal"].ok and "저장된 분개가 모자라요" in r["journal"].detail
        assert not r["cash"].ok


def test_an_unbalanced_entry_is_found_before_commit_would_refuse_it(conn):
    with damaged(conn, "set constraints all deferred",
                 "with e as (insert into journal_entries (event_seq, date, kind, source_id)"
                 " values (1, '2026-11-02', 'X', 'x') returning entry_id)"
                 " insert into journal_lines (entry_id, account_code, debit)"
                 " select entry_id, (select min(code) from accounts_chart), 7 from e"):
        r = report(conn)
        assert not r["balanced"].ok and "1건" in r["balanced"].detail
        assert not r["journal"].ok and "이벤트에 없는 분개" in r["journal"].detail


def test_an_event_the_rules_reject_is_reported_not_raised(conn):
    with damaged(conn, "insert into ledger_events (seq, event_type, payload) select max(seq) + 1, 'ExecutionEvent',"
                       " '{\"exec_id\": \"X-1\", \"trade_date\": \"2026-11-02\", \"symbol\": \"NOPE\","
                       " \"side\": \"BUY\", \"qty\": 1, \"price\": 100}' from ledger_events"):
        r = report(conn)
        assert not r["replay"].ok and r["replay"].detail


def test_verify_command_prints_the_report_and_exits_zero(conn, capsys):
    rc = cli.run(["verify"], environ={"MINISTP_DATABASE_URL": URL})
    out = capsys.readouterr().out
    assert rc == 0 and "이상 없음" in out and out.count("OK") == 7 and "문제" not in out


def test_verify_command_exits_one_when_a_guard_is_off(conn, capsys):
    conn.execute("alter table recon_results disable trigger recon_results_no_truncate")
    try:
        rc = cli.run(["verify"], environ={"MINISTP_DATABASE_URL": URL})
    finally:
        conn.execute("alter table recon_results enable trigger recon_results_no_truncate")
    out = capsys.readouterr().out
    assert rc == 1 and "recon_results_no_truncate" in out and "문제 1건" in out
