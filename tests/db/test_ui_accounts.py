"""STP-UI on the three-account sample: one account per view, picked in the top bar (다계좌)."""
import json
import os
import re

import pytest

from ministp.sample import build_sample
from ministp.store import erd
from ministp.ui import app as ui_app
from ministp.ui.definitions import load_screens, validate

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=12, seed=3, accounts=3)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


@pytest.fixture
def client(sample):
    return TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                        allowed_hosts=["testserver"]))


def rows(client, tx, account=None, params=None):
    body = {"params": params or {}} | ({"account": account} if account else {})
    r = client.post(f"/api/tx/{tx}", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    ds = out["datasets"]["main"]
    return [dict(zip(ds["columns"], row)) for row in ds["rows"]], out


def test_the_page_carries_the_accounts_to_pick_from_and_nothing_secret(client):
    page = client.get("/screen/STP-1100").text
    data = json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>', page, re.S).group(1))
    assert data["default_account"] == "A0001"
    assert [(a["account_id"], a["customer_id"], a["status"]) for a in data["accounts"]] == [
        ("A0001", "C0001", "OPEN"), ("A0002", "C0001", "OPEN"), ("A0003", "C0002", "OPEN")]
    assert set(data["accounts"][0]) == {"account_id", "alias", "customer_id", "status"}
    assert "account_no" not in page and "APP_KEY" not in page and "KIS_2" not in page


def test_every_transaction_answers_for_the_account_it_is_asked_about(client, conn):
    for acct in ("A0001", "A0002", "A0003"):
        held, body = rows(client, "STP1100R01", acct)
        assert body["account"] == acct
        want = conn.execute("select symbol, qty, book_cost from positions_v where account_id = %s", (acct,)).fetchall()
        assert sorted((h["symbol"], h["qty"], h["book_cost"]) for h in held) == sorted(want)
        summary, _ = rows(client, "STP4200R01", acct)
        assert summary[0]["account_id"] == acct
        assert summary[0]["cash_d0"] == conn.execute(
            "select balance from cash_balance_v where account_id = %s", (acct,)).fetchone()[0]
        events, _ = rows(client, "STP3100R01", acct)
        mine = {r[0] for r in conn.execute("select seq from ledger_events where account_id = %s", (acct,))}
        assert {e["seq"] for e in events} == mine
        trial, tb = rows(client, "STP3100R03", acct)
        assert tb["meta"]["balanced"] and trial
    _, body = rows(client, "STP1100R01")                          # no account given: the default one
    assert body["account"] == "A0001"


def test_an_event_of_another_account_cannot_be_traced_from_this_one(client, conn):
    seq = conn.execute("select min(seq) from ledger_events where account_id = 'A0002'").fetchone()[0]
    assert client.post("/api/tx/STP3100R02", json={"params": {"seq": seq}}).status_code == 400
    ok = client.post("/api/tx/STP3100R02", json={"params": {"seq": seq}, "account": "A0002"})
    assert ok.status_code == 200 and ok.json()["meta"]["balanced"]


@pytest.mark.parametrize("account, message", [("A0009", "등록되지 않은 계좌"), ("A0001'; --", "형식"),
                                              (["A0001"], "형식"), ("a0001", "형식")])
def test_a_bad_account_is_refused(client, account, message):
    r = client.post("/api/tx/STP1100R01", json={"params": {}, "account": account})
    assert r.status_code == 400 and message in r.json()["error"]


def test_account_list_and_status_history(client, conn, sample):
    accounts, body = rows(client, "STP4400R01", "A0003")          # the list is the same from any account
    assert body["meta"]["selected"] == "A0003"
    assert [(a["account_id"], a["customer_id"], a["status"]) for a in accounts] == [
        ("A0001", "C0001", "OPEN"), ("A0002", "C0001", "OPEN"), ("A0003", "C0002", "OPEN")]
    second = accounts[1]
    assert second["opened_on"] == sample.others[0].opened_on.isoformat() and second["credential_ref"] == "KIS_2"
    assert second["clean_streak"] == sample.others[0].streak and accounts[0]["clean_streak"] == sample.streak
    assert accounts[2]["events"] == conn.execute(
        "select count(*) from ledger_events where account_id = 'A0003'").fetchone()[0]
    text = json.dumps(body)
    assert "account_no" not in text and "app_key" not in text.lower()

    history, _ = rows(client, "STP4400R02", params={"account_id": "A0003"})
    assert [h["status"] for h in history] == ["OPEN", "SUSPENDED", "OPEN"]       # newest first
    assert history[1]["reason"].startswith("샘플:")
    assert client.post("/api/tx/STP4400R02", json={"params": {}}).status_code == 400


def test_the_account_list_shows_little_and_its_detail_is_on_the_right(client, conn):
    """왼쪽 목록, 오른쪽 상세: the list keeps to what identifies an account; the rest is one row away."""
    screen = load_screens()["STP-4400"]
    master, *details = screen.grids
    assert screen.layout == "split" and master.id == "accounts" and master.load == "open"
    assert [c.name for c in master.columns] == ["account_id", "alias", "customer_alias", "status", "cash_d0", "book_cost"]
    assert [(g.id, g.load, g.master, g.link) for g in details] == [
        ("positions", "master", "accounts", {"account_id": "account_id"}),
        ("status_history", "master", "accounts", {"account_id": "account_id"})]

    accounts, body = rows(client, "STP4400R01", "A0002")
    assert body["meta"]["selected"] == "A0002"                    # the row the screen opens on
    for a in accounts:                                            # asked from A0001, answered for the row
        held, _ = rows(client, "STP4400R03", params={"account_id": a["account_id"]})
        want = conn.execute("select symbol, qty, book_cost from positions_v where account_id = %s",
                            (a["account_id"],)).fetchall()
        assert sorted((h["symbol"], h["qty"], h["book_cost"]) for h in held) == sorted(want)
        assert len(held) == a["holdings"] and sum(h["book_cost"] for h in held) == a["book_cost"]
    assert client.post("/api/tx/STP4400R03", json={"params": {}}).status_code == 400
    nobody, _ = rows(client, "STP4400R03", params={"account_id": "A0009"})
    assert nobody == []


def test_customer_holdings_add_up_the_accounts_of_one_customer(client, conn):
    """STP-4500 고객별 보유: 고객 → 그 고객의 종목별 합산과 계좌."""
    screen = load_screens()["STP-4500"]
    assert screen.layout == "split"
    assert [(g.id, g.load, g.master) for g in screen.grids] == [
        ("customers", "open", None), ("positions", "master", "customers"), ("accounts", "master", "customers")]

    customers, _ = rows(client, "STP4500R01", "A0003")            # the same from any account
    assert [(c["customer_id"], c["accounts"]) for c in customers] == [("C0001", 2), ("C0002", 1)]
    for c in customers:
        mine, _ = rows(client, "STP4500R03", params={"customer_id": c["customer_id"]})
        want = [r[0] for r in conn.execute("select account_id from trading_accounts where customer_id = %s"
                                           " order by account_id", (c["customer_id"],))]
        assert [a["account_id"] for a in mine] == want
        assert sum(a["cash_d0"] for a in mine) == c["cash_d0"] and sum(a["book_cost"] for a in mine) == c["book_cost"]
        totals, _ = rows(client, "STP4500R02", params={"customer_id": c["customer_id"]})
        assert totals and {t["customer_id"] for t in totals} == {c["customer_id"]}
        assert sum(t["book_cost"] for t in totals) == c["book_cost"]
    assert client.post("/api/tx/STP4500R03", json={"params": {}}).status_code == 400

    totals, _ = rows(client, "STP4500R02")                        # no customer named: everyone
    want = conn.execute("select customer_id, symbol, qty, book_cost from customer_positions_v").fetchall()
    assert sorted((t["customer_id"], t["symbol"], t["qty"], t["book_cost"]) for t in totals) == sorted(want)
    both = [t for t in totals if t["customer_id"] == "C0001" and t["accounts"] == 2]
    for t in both:                                                # held in both accounts of one customer: added up
        parts = conn.execute("select sum(qty) from positions_v where account_id in ('A0001', 'A0002') and symbol = %s",
                             (t["symbol"],)).fetchone()[0]
        assert t["qty"] == parts


def test_a_drill_that_names_an_account_column_must_have_it(conn):
    screens = load_screens()
    catalog = erd.read_catalog(conn, "public")
    assert validate(screens, catalog) == []
    grid = screens["STP-4400"].grids[0]
    assert grid.drill == {"screen": "STP-4200", "account": "account_id"}
    grid.drill = {"screen": "STP-4200", "account": "no_such_column"}
    assert any("이동할 계좌 열 no_such_column" in e for e in validate(screens, catalog))
