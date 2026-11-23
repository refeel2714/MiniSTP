"""STP-2300 시세 추이 on a sample database: the screen's transactions and the API."""
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


def test_every_symbol_the_sample_valued_has_its_closes_over_the_period(sample):
    ctx = context(sample)
    with ctx.conn:
        res = run_tx("STP2300R01", ctx, {})
        symbols = res.datasets["main"].records()
        assert symbols and all(r["instrument_name"] and r["last_date"] == sample.last_day for r in symbols)
        assert {k: res.meta[k] for k in ("warn_change", "limit_change", "warn_exec_gap")} == {
            "warn_change": "15%", "limit_change": "30%", "warn_exec_gap": "10%"}
        assert all(r["level"] in ("정상", "주의") for r in symbols)             # sample prices sit on the grid
        flagged = [r["symbol"] for r in symbols if r["level"] == "주의"]
        assert res.meta.get("selected") == (flagged[0] if flagged else None)
        nav_days = len(run_tx("STP2200R01", ctx, {}).datasets["main"].records())
        for r in symbols:
            detail = run_tx("STP2300R02", ctx, {"symbol": r["symbol"]})
            rows = detail.datasets["main"].records()
            assert len(rows) == r["days"] <= nav_days and rows[0]["date"] == r["last_date"]
            assert [x["close"] for x in reversed(rows)] == r["trend"] and rows[0]["close"] == r["last_close"]
            assert detail.meta["high"].startswith(f"{r['high']:,} (") and detail.meta["low"].startswith(f"{r['low']:,} (")
            assert all(x["diff"] == x["close"] - x["prev_close"] for x in rows if x["prev_close"])


def test_the_period_narrows_both_sides_and_one_day_reads_against_the_day_before(sample):
    ctx = context(sample)
    with ctx.conn:
        day = {"date_from": sample.last_day, "date_to": sample.last_day}
        symbols = run_tx("STP2300R01", ctx, day).datasets["main"].records()
        assert symbols and all(r["days"] == 1 and r["date_from"] == sample.last_day for r in symbols)
        for r in symbols:
            [row] = run_tx("STP2300R02", ctx, {"symbol": r["symbol"], **day}).datasets["main"].records()
            assert (row["date"], row["close"]) == (sample.last_day, r["last_close"])
            assert r["base_close"] == (row["prev_close"] or row["close"]) and r["change"] == (row["change"] or 0.0)
        assert run_tx("STP2300R01", ctx, {"date_from": "2020-01-01", "date_to": "2020-01-31"}).datasets["main"].records() == []
        empty = run_tx("STP2300R02", ctx, {"symbol": symbols[0]["symbol"], "date_to": "2020-01-31"})
        assert empty.datasets["main"].records() == [] and empty.meta == {}
        with pytest.raises(TxError, match="종목코드가 필요"):
            run_tx("STP2300R02", ctx, {})


def test_the_api_serves_the_screen_and_the_detail_takes_the_period_from_the_row(sample):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"]))
    assert client.get("/screen/STP-2300").status_code == 200
    body = client.post("/api/tx/STP2300R01", json={"params": {}}).json()
    ds = body["datasets"]["main"]
    first = dict(zip(ds["columns"], ds["rows"][0]))
    assert body["meta"]["limit_change"] == "30%" and first["date_from"] is None
    # the screen sends the row's own period, empty when the whole history was asked for
    sent = {"symbol": first["symbol"], "date_from": first["date_from"], "date_to": first["date_to"]}
    rows = client.post("/api/tx/STP2300R02", json={"params": sent}).json()["datasets"]["main"]["rows"]
    assert len(rows) == first["days"]
    grid = next(g for g in client.get("/api/screen/STP-2300").json()["grids"] if g["id"] == "closes")
    assert grid["chart"] == {"x": "date", "y": "close"} and grid["link"]["date_from"] == "date_from"
