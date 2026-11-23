"""검증 화면 (STP-2110, 2210, 2300, 2400) on books with several accounts: each looks at the picked account."""
import os

import pytest

from ministp.sample import build_sample
from ministp.ui.transactions import TxContext, run_tx

psycopg = pytest.importorskip("psycopg")

from db.test_integrity import damaged  # noqa: E402
from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=6, seed=3, accounts=3)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


def tx(conn, sample, account_id, tx_id, **params):
    return run_tx(tx_id, TxContext(conn, CAL, FEES, sample.last_day, account_id), params)


def accounts(conn):
    return [a for (a,) in conn.execute("select account_id from trading_accounts order by account_id")]


def test_every_account_has_its_own_clean_set_of_checks_and_days(conn, sample):
    ids = accounts(conn)
    assert len(ids) == 3
    seen = set()
    for account_id in ids:
        checks = tx(conn, sample, account_id, "STP2110R01")
        titles = [r["title"] for r in checks.datasets["main"].records()]
        assert len(titles) == len(set(titles)) == 7 and checks.meta == {"all_ok": True, "breaks": 0}
        cash = tx(conn, sample, account_id, "STP2110R02", title="현금").datasets["rows"].records()
        stored = int(conn.execute("select coalesce(sum(amount), 0) from cash_ledger where account_id = %s",
                                  (account_id,)).fetchone()[0])
        assert next(r for r in cash if r["item"] == "예수금")["stored"] == stored
        days = tx(conn, sample, account_id, "STP2400R01")
        assert days.meta == {"attention_days": 0} and len(days.datasets["main"].records()) >= 1
        nav = tx(conn, sample, account_id, "STP2210R01").datasets["main"].records()
        assert nav and all(r["unexplained"] == 0 for r in nav)
        assert tx(conn, sample, account_id, "STP2300R01").datasets["main"].records()
        seen.add((stored, nav[0]["net_assets"]))
    assert len(seen) == 3          # three accounts, three different sets of books


def test_a_damaged_row_of_one_account_shows_on_that_account_only(conn, sample):
    first, other = accounts(conn)[:2]
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only",
                 "update cash_ledger set amount = amount + 7 where id ="
                 f" (select min(id) from cash_ledger where account_id = '{other}')"):
        mine = {r["key"]: r["result"] for r in tx(conn, sample, other, "STP2110R01").datasets["main"].records()}
        theirs = {r["key"]: r["result"] for r in tx(conn, sample, first, "STP2110R01").datasets["main"].records()}
        assert (mine["cash"], theirs["cash"]) == ("불일치", "일치")
        assert mine["guards"] == theirs["guards"] == "불일치"       # the switched-off guard is the database's
        cash = tx(conn, sample, other, "STP2110R02", title="현금").datasets["rows"].records()
        assert next(r for r in cash if r["item"] == "예수금")["diff"] == 7
        last = tx(conn, sample, other, "STP2400R02", date=sample.last_day).datasets["main"].records()[-1]
        assert last["state"] == "확인 필요" and "7개 중 2개" in last["detail"]
        clean = tx(conn, sample, first, "STP2400R02", date=sample.last_day).datasets["main"].records()[-1]
        assert clean["state"] == "확인 필요" and "7개 중 1개" in clean["detail"]
