"""STP-2300 시세 점검 on a sample database: the screen's transactions and the API."""
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


def context(sample, today=None):
    return TxContext(psycopg.connect(URL, autocommit=True), CAL, FEES, today or sample.last_day)


def test_every_close_the_sample_took_is_checked_and_shown_with_the_rules(sample):
    ctx = context(sample)
    with ctx.conn:
        res = run_tx("STP2300R01", ctx, {})
        days = res.datasets["main"].records()
        assert len(days) == 8 and days[0]["date"] == sample.last_day
        assert {k: res.meta[k] for k in ("warn_change", "limit_change", "warn_exec_gap")} == {
            "warn_change": "15%", "limit_change": "30%", "warn_exec_gap": "10%"}
        assert all(r["result"] in ("정상", "주의") and r["flagged"] == 0 for r in days)   # sample prices sit on the grid
        if any(r["result"] == "주의" for r in days):
            assert res.meta["selected"] == next(r["date"] for r in days if r["result"] == "주의").isoformat()
        else:
            assert "selected" not in res.meta
        for r in days:
            rows = run_tx("STP2300R02", ctx, {"date": r["date"]}).datasets["main"].records()
            assert len(rows) == r["symbols"] and all(x["close"] > 0 and x["instrument_name"] for x in rows)
            assert sum(x["level"] == "주의" for x in rows) == r["warned"]


def test_the_next_business_day_waits_as_not_valued(sample):
    tomorrow = CAL.add_business_days(sample.last_day, 1)
    ctx = context(sample, today=tomorrow)
    with ctx.conn:
        days = run_tx("STP2300R01", ctx, {}).datasets["main"].records()
        assert (days[0]["date"], days[0]["result"], days[0]["flagged"]) == (tomorrow, "평가 전", None)
        rows = run_tx("STP2300R02", ctx, {"date": tomorrow}).datasets["main"].records()
        assert len(rows) == days[0]["symbols"]
        assert all(r["close"] is None and r["prev_close"] and r["level"] == "평가 전" for r in rows)
        with pytest.raises(TxError, match="기준일이 필요"):
            run_tx("STP2300R02", ctx, {})


def test_the_api_serves_the_screen(sample):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"]))
    assert client.get("/screen/STP-2300").status_code == 200
    body = client.post("/api/tx/STP2300R01", json={"params": {}}).json()
    assert body["meta"]["limit_change"] == "30%" and len(body["datasets"]["main"]["rows"]) == 8
