"""STP-2210 기준가 이력 on a sample database: the screen's transactions and the API."""
import os

import pytest

from ministp.sample import build_sample
from ministp.ui import app as ui_app
from ministp.ui.transactions import TxContext, TxError, run_tx

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
        return build_sample(c, CAL, FEES, days=8, seed=5)


@pytest.fixture
def ctx(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield TxContext(c, CAL, FEES, sample.last_day)


def test_every_day_of_the_sample_is_explained_by_its_journal(ctx):
    res = run_tx("STP2210R01", ctx, {})
    listed = res.datasets["main"].records()
    assert len(listed) == 8 and res.meta == {"all_explained": True, "unexplained_days": 0}
    assert {r["result"] for r in listed} == {"일치"}
    nav = {r["date"]: (r["net_assets"], r["nav_per_1000"], r["units"])
           for r in run_tx("STP2200R01", ctx, {}).datasets["main"].records()}
    assert {r["date"]: (r["net_assets"], r["nav_per_1000"], r["units"]) for r in listed} == nav   # as the NAV screen
    assert all(r["struck"] == 1 for r in listed) and listed[-1]["nav_change"] is None
    for r in listed:
        bridge = run_tx("STP2210R02", ctx, {"date": r["date"]}).datasets["main"].records()
        assert bridge[-1]["running"] == r["net_assets"] and bridge[-2]["amount"] == 0
        assert bridge[0]["running"] + sum(b["amount"] or 0 for b in bridge) == r["net_assets"]
        assert all(b["code"][0] in "345" for b in bridge[1:-2])     # only equity, income and expense explain


def test_the_period_narrows_the_list_and_a_day_without_a_nav_is_refused(ctx, sample):
    one = run_tx("STP2210R01", ctx, {"date_from": sample.last_day, "date_to": sample.last_day})
    assert [r["date"] for r in one.datasets["main"].records()] == [sample.last_day]
    with pytest.raises(TxError, match="기준가가 없어요"):
        run_tx("STP2210R02", ctx, {"date": "2020-01-01"})
    with pytest.raises(TxError, match="기준일이 필요"):
        run_tx("STP2210R02", ctx, {})


def test_the_api_serves_the_screen(sample):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"]))
    assert client.get("/screen/STP-2210").status_code == 200
    body = client.post("/api/tx/STP2210R01", json={"params": {}}).json()
    assert body["meta"]["all_explained"] is True
    assert client.post("/api/tx/STP2210R02", json={"params": {"date": "2020-01-01"}}).status_code == 400
