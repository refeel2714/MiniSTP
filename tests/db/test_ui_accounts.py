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
    assert set(data["accounts"][0]) == {"account_id", "alias", "customer_id", "status", "env"}
    # 실전 테두리: the page is told each account's environment, so picking a LIVE one frames the screens
    # even when the shell that started the server points at 모의. The sample's accounts are all 모의.
    assert {a["env"] for a in data["accounts"]} == {"PAPER"}
    script = client.get("/static/stpui.js").text
    assert '.env === "LIVE"' in script and 'document.body.dataset.env = "live"' in script
    assert "account_no" not in page and "APP_KEY" not in page and "KIS_2" not in page


def test_every_transaction_answers_for_the_account_it_is_asked_about(client, conn):
    for acct in ("A0001", "A0002", "A0003"):
        held, body = rows(client, "STP1100R01", acct)
        assert body["account"] == acct
        want = conn.execute("select symbol, qty, book_cost from positions_v where account_id = %s", (acct,)).fetchall()
        assert sorted((h["symbol"], h["qty"], h["book_cost"]) for h in held) == sorted(want)
        summary, _ = rows(client, "STP3400R01", acct)
        assert summary[0]["account_id"] == acct
        assert summary[0]["cash_d0"] == conn.execute(
            "select balance from cash_balance_v where account_id = %s", (acct,)).fetchone()[0]
        events, _ = rows(client, "STP3100R01", acct)
        mine = {r[0] for r in conn.execute("select seq from ledger_events where account_id = %s", (acct,))}
        assert {e["seq"] for e in events} == mine
        journals, _ = rows(client, "STP3200R01", acct)   # 분개 조회의 목록과 분개도 그 계좌 것만
        assert journals and {e["seq"] for e in journals} <= mine
        assert client.post("/api/tx/STP3200R02", json={"params": {"seq": journals[0]["seq"]},
                                                        "account": acct}).json()["meta"]["balanced"]
        trial, tb = rows(client, "STP3300R01", acct)
        assert tb["meta"]["balanced"] and trial
        days, cal = rows(client, "STP2100R02", acct)     # 대사 달력도 그 계좌의 대사만
        ran = dict(conn.execute(
            "select r.date::text, count(*) from recon_results r join broker_snapshots b on b.snapshot_id = r.run_id"
            " where r.account_id = %s and b.kind = 'DAILY_RECON' and r.supersedes_id is null group by 1", (acct,)).fetchall())
        assert {d["date"]: d["items"] for d in days if d["items"] is not None} == ran
        assert cal["meta"]["clean_streak"] == summary[0]["clean_streak"]
    # 주문과 그 결제도 계좌별: 남의 계좌 주문은 이 계좌에서 없는 주문
    day, cid = conn.execute("select trade_date, client_id from oms_orders where account_id = 'A0002' order by created_at limit 1").fetchone()
    mine, _ = rows(client, "STP1200R01", "A0002", {"day": day.isoformat()})
    assert cid in {o["client_id"] for o in mine} and all(len(o["progress"]) == 4 for o in mine)
    assert client.post("/api/tx/STP1200R02", json={"params": {"client_id": cid}, "account": "A0002"}).status_code == 200
    other = client.post("/api/tx/STP1200R02", json={"params": {"client_id": cid}, "account": "A0003"})
    assert other.status_code == 400 and "없는 주문" in other.json()["error"]
    _, body = rows(client, "STP1100R01")                          # no account given: the default one
    assert body["account"] == "A0001"


def test_an_event_of_another_account_cannot_be_traced_from_this_one(client, conn):
    seq = conn.execute("select min(seq) from ledger_events where account_id = 'A0002'").fetchone()[0]
    for tx in ("STP3100R02", "STP3200R02"):                       # 보유·예수금 변동도, 분개도
        assert client.post(f"/api/tx/{tx}", json={"params": {"seq": seq}}).status_code == 400
        assert client.post(f"/api/tx/{tx}", json={"params": {"seq": seq}, "account": "A0002"}).status_code == 200
    ok = client.post("/api/tx/STP3200R02", json={"params": {"seq": seq}, "account": "A0002"})
    assert ok.json()["datasets"]["journal"]["rows"] and ok.json()["meta"]["balanced"]


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
    assert (second["broker"], second["env"], second["account_type"]) == ("KIS", "PAPER", "GENERAL")
    # reference data: nothing of what the account holds (that is STP-3400, STP-1100, STP-1400)
    assert not {"cash_d0", "book_cost", "holdings", "events", "clean_streak"} & set(second)
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
    assert [c.name for c in master.columns] == ["account_id", "alias", "customer_alias", "status", "broker", "env",
                                                "account_type", "opened_on"]
    assert not master.totals                                      # 기준정보: no balances to add up
    assert [(g.id, g.load, g.master, g.link) for g in details] == [
        ("status_history", "master", "accounts", {"account_id": "account_id"})]

    accounts, body = rows(client, "STP4400R01", "A0002")
    assert body["meta"]["selected"] == "A0002"                    # the row the screen opens on
    for a in accounts:                                            # asked from A0002, answered for the row
        history, _ = rows(client, "STP4400R02", params={"account_id": a["account_id"]})
        assert history and history[0]["status"] == a["status"]
    from ministp.ui.transactions import TRANSACTIONS
    assert "STP4400R03" not in TRANSACTIONS                       # 계좌 보유는 이 화면에 없어요


def test_customer_holdings_add_up_the_accounts_of_one_customer(client, conn):
    """STP-1400 고객별 보유: 고객 → 그 고객의 종목별 합산과 계좌."""
    screen = load_screens()["STP-1400"]
    assert screen.layout == "split"
    assert [(g.id, g.load, g.master) for g in screen.grids] == [
        ("customers", "open", None), ("positions", "master", "customers"), ("accounts", "master", "customers")]

    customers, _ = rows(client, "STP1400R01", "A0003")            # the same from any account
    assert [(c["customer_id"], c["accounts"]) for c in customers] == [("C0001", 2), ("C0002", 1)]
    for c in customers:
        mine, _ = rows(client, "STP1400R03", params={"customer_id": c["customer_id"]})
        want = [r[0] for r in conn.execute("select account_id from trading_accounts where customer_id = %s"
                                           " order by account_id", (c["customer_id"],))]
        assert [a["account_id"] for a in mine] == want
        assert sum(a["cash_d0"] for a in mine) == c["cash_d0"] and sum(a["book_cost"] for a in mine) == c["book_cost"]
        totals, _ = rows(client, "STP1400R02", params={"customer_id": c["customer_id"]})
        assert totals and {t["customer_id"] for t in totals} == {c["customer_id"]}
        kinds = dict(conn.execute("select symbol, asset_type from instruments").fetchall())
        assert all(t["asset_type"] == kinds[t["symbol"]] for t in totals)      # 배지 모양이 되는 자산 유형
        assert sum(t["book_cost"] for t in totals) == c["book_cost"]
    assert client.post("/api/tx/STP1400R03", json={"params": {}}).status_code == 400

    totals, _ = rows(client, "STP1400R02")                        # no customer named: everyone
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
    assert grid.drill == {"screen": "STP-3400", "account": "account_id"}
    grid.drill = {"screen": "STP-3400", "account": "no_such_column"}
    assert any("이동할 계좌 열 no_such_column" in e for e in validate(screens, catalog))
