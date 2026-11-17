"""PostgreSQL store: persistence, replay, DB-enforced controls (spec 03)."""
import os

import pytest

from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day
from ministp.fees import FeeSchedule
from ministp.ledger import Ledger, LedgerHaltedError, Side
from ministp.store import PostgresLedgerStore, SchemaError, from_payload, to_payload

psycopg = pytest.importorskip("psycopg")
URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db,
              pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        yield c


@pytest.fixture
def store(conn):
    s = PostgresLedgerStore(conn)
    s.init_schema()
    return s


def run_sample(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
        if day == "11-02":
            ledger.revalue(d(day), {"A": 50_500})
            ledger.nav(d(day), {"A": 50_500})
        if day == "11-03":
            ledger.revalue(d(day), {"A": 52_000})


def test_sample_set_persists_and_matches(store, ledger):
    store.attach(ledger)
    run_sample(ledger)
    assert store.verify(ledger) == []
    assert store.conn.execute("select balance from cash_balance_v").fetchone()[0] == 19_051_823
    assert store.conn.execute("select symbol, qty, book_cost from positions_v").fetchall() == [
        ("E", 100, 1_005_000)]


def test_reload_by_replay_and_keep_writing(store, ledger, calendar, instruments):
    store.attach(ledger)
    run_sample(ledger)
    again = store.load(calendar, FeeSchedule.spec_v1(), instruments)
    assert again.journal == ledger.journal
    assert again.projected_cash() == ledger.projected_cash()
    again.execute("T7", d("11-11"), "E", Side.SELL, 100, 10_300)
    assert store.verify(again) == []
    assert store.conn.execute("select max(seq) from ledger_events").fetchone()[0] == len(again.events)


@pytest.mark.parametrize("table", ["ledger_events", "journal_entries", "journal_lines",
                                   "position_ledger", "cash_ledger"])
@pytest.mark.parametrize("stmt", ["update {t} set {c} = {c}", "delete from {t}", "truncate {t} cascade"])
def test_ledger_tables_are_append_only(store, ledger, table, stmt):
    store.attach(ledger)
    run_sample(ledger)
    col = {"ledger_events": "seq", "journal_entries": "kind", "journal_lines": "debit",
           "position_ledger": "qty_delta", "cash_ledger": "amount"}[table]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        store.conn.execute(stmt.format(t=table, c=col))


def test_db_rejects_unbalanced_entry(store):
    c = store.conn
    with pytest.raises(psycopg.errors.CheckViolation):
        with c.transaction():
            c.execute("insert into ledger_events (seq, event_type, payload) values (1, 'X', '{}')")
            eid = c.execute("insert into journal_entries (event_seq, date, kind, source_id)"
                            " values (1, '2026-11-02', 'BAD', '') returning entry_id").fetchone()[0]
            c.execute("insert into journal_lines (entry_id, account_code, debit) values (%s, '1010', 10)", (eid,))
            c.execute("insert into journal_lines (entry_id, account_code, credit) values (%s, '3010', 9)", (eid,))
    assert c.execute("select count(*) from ledger_events").fetchone()[0] == 0


def test_store_failure_halts_ledger_without_partial_rows(store, ledger):
    store.attach(ledger)
    ledger.deposit(d("11-02"), 1_000_000)
    # Someone else wrote seq 2 behind our back: our next write must fail closed.
    store.conn.execute("insert into ledger_events (seq, event_type, payload) values (2, 'X', '{}')")
    with pytest.raises(LedgerHaltedError):
        ledger.execute("H1", d("11-02"), "A", Side.BUY, 1, 10_000)
    assert store.conn.execute("select count(*) from position_ledger").fetchone()[0] == 0
    with pytest.raises(LedgerHaltedError):
        ledger.deposit(d("11-02"), 1)


def test_attach_refuses_mismatched_stream(store, calendar, instruments):
    led = Ledger(calendar, FeeSchedule.spec_v1(), instruments)
    led.deposit(d("11-02"), 1_000)  # in memory only
    with pytest.raises(SchemaError):
        store.attach(led)

