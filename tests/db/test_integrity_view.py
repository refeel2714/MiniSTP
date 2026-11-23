"""STP-2110 원장 내부 대사: the integrity checks as numbers side by side, on a sample database."""
import os

import pytest

from ministp.ops import OpsError, load_books, load_instruments
from ministp.sample import build_sample
from ministp.store.integrity import integrity_report
from ministp.store.integrity_view import PLAIN, REPLAYED, compared
from ministp.ui.transactions import TxContext, TxError, run_tx

psycopg = pytest.importorskip("psycopg")

from db.test_integrity import damaged  # noqa: E402
from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=6, seed=3)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


def rows(conn, key):
    return {r.item: r for r in compared(conn, key, CAL, FEES, load_instruments(conn))}


def tx(conn, sample, tx_id, **params):
    return run_tx(tx_id, TxContext(conn, CAL, FEES, sample.last_day), params)


def test_every_check_of_a_clean_database_has_numbers_and_they_all_agree(conn):
    for check in integrity_report(conn, CAL, FEES, load_instruments(conn)):
        got = rows(conn, check.key)
        assert got, check.key
        assert all(r.ok and r.diff == 0 for r in got.values()), check.key
    assert set(PLAIN) | set(REPLAYED) == {"events", "guards", "balanced", "journal", "accounts", "positions", "cash"}


def test_the_numbers_are_the_ones_the_books_hold(conn):
    books = load_books(conn, CAL, FEES)
    assert rows(conn, "cash")["예수금"].stored == books.ledger.cash_balance()
    assert rows(conn, "accounts")["1010 예수금"].expected == books.ledger.cash_balance()
    symbol, position = next(iter(books.ledger.positions().items()))
    got = rows(conn, "positions")
    assert (got[f"{symbol} 보유수량"].stored, got[f"{symbol} 장부금액"].stored) == (position.qty, position.book_cost)
    n = conn.execute("select count(*) from ledger_events").fetchone()[0]
    assert rows(conn, "events")["이벤트 건수"].stored == n


def test_a_changed_cash_row_shows_as_a_difference_of_exactly_that_much(conn):
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only",
                 "update cash_ledger set amount = amount + 7 where id = (select min(id) from cash_ledger)"):
        got = rows(conn, "cash")
        assert got["예수금"].diff == 7 and not got["예수금"].ok
        assert got["한쪽에만 있는 현금 원장 행 수"].stored == 2       # the changed row, and the row it should be
        assert got["현금 원장 행 수"].ok
        guard = rows(conn, "guards")["cash_ledger"]
        assert (guard.stored, guard.expected) == (1, 2)
    assert rows(conn, "cash")["예수금"].ok


def test_a_check_without_numbers_and_books_that_cannot_be_replayed_give_no_rows(conn):
    assert compared(conn, "no-such-check", CAL, FEES, load_instruments(conn)) == []
    with damaged(conn, "insert into ledger_events (seq, event_type, payload) select max(seq) + 1, 'ExecutionEvent',"
                       " '{\"exec_id\": \"X-1\", \"trade_date\": \"2026-11-02\", \"symbol\": \"NOPE\","
                       " \"side\": \"BUY\", \"qty\": 1, \"price\": 100}' from ledger_events"):
        assert compared(conn, "accounts", CAL, FEES, load_instruments(conn)) == []
        assert rows(conn, "events")           # what needs no replay is still shown


def test_reading_the_numbers_leaves_no_transaction_open(conn):
    rows(conn, "accounts")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_the_screen_lists_the_verify_report_and_opens_on_nothing_when_all_agree(conn, sample):
    res = tx(conn, sample, "STP2110R01")
    listed = res.datasets["main"].records()
    assert [r["key"] for r in listed] == [c.key for c in integrity_report(conn, CAL, FEES, load_instruments(conn))]
    assert {r["result"] for r in listed} == {"일치"}
    assert res.meta == {"all_ok": True, "breaks": 0}


def test_the_screen_opens_on_the_break_while_every_other_screen_refuses(conn, sample):
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only",
                 "update cash_ledger set amount = amount + 7 where id = (select min(id) from cash_ledger)"):
        with pytest.raises(OpsError):                      # what the other screens read through
            tx(conn, sample, "STP1100R02")
        res = tx(conn, sample, "STP2110R01")
        broken = [r["key"] for r in res.datasets["main"].records() if r["result"] == "불일치"]
        assert broken == ["guards", "cash"] and res.meta == {"all_ok": False, "breaks": 2, "selected": "guards"}
        one = tx(conn, sample, "STP2110R02", title="현금").datasets
        assert one["check"].records()[0]["result"] == "불일치" and "저장에만" in one["check"].records()[0]["detail"]
        cash = next(r for r in one["rows"].records() if r["item"] == "예수금")
        assert (cash["diff"], cash["result"]) == (7, "불일치")


def test_the_detail_of_one_check_is_its_verdict_and_its_numbers(conn, sample):
    one = tx(conn, sample, "STP2110R02", title="계정 잔액").datasets
    assert [r["title"] for r in one["check"].records()] == ["계정 잔액"]
    assert len(one["rows"]) == 15 and {r["result"] for r in one["rows"].records()} == {"일치"}
    with pytest.raises(TxError, match="없는 점검 항목"):
        tx(conn, sample, "STP2110R02", title="없는 항목")
    with pytest.raises(TxError, match="점검 항목이 필요"):
        tx(conn, sample, "STP2110R02")
