"""STP-UI on a sample database: definitions, transactions, the API and the read-only guarantee."""
import datetime as dt
import os
from pathlib import Path

import pytest

from ministp import __main__ as cli
from ministp.sample import build_sample
from ministp.store import erd
from ministp.ui import app as ui_app
from ministp.ui.definitions import Column, DefinitionError, load_screens, validate
from ministp.ui.spec import all_docs
from ministp.ui.transactions import TxContext, output_problems

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
DOCS = Path(__file__).resolve().parents[2] / "docs" / "screens"


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        s = build_sample(c, CAL, FEES, days=8, seed=5)
    return s


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


@pytest.fixture
def client(sample):
    app = ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day, allowed_hosts=["testserver"])
    return TestClient(app)


def test_shipped_definitions_match_the_schema(conn):
    assert validate(load_screens(), erd.read_catalog(conn, "public")) == []


def test_every_grid_column_comes_from_its_transaction(conn, sample):
    assert output_problems(load_screens(), TxContext(conn, CAL, FEES, sample.last_day)) == []


def test_committed_screen_docs_are_current(conn):
    fresh = all_docs(load_screens(), erd.build_catalog(conn, erd.schema_sql()))
    for name, text in fresh.items():
        path = DOCS / name
        assert path.exists() and path.read_text(encoding="utf-8") == text, \
            f"docs/screens/{name}가 오래됐어요: python scripts/gen_screen_docs.py 로 다시 만들어 주세요"


def test_ui_connection_cannot_write(sample):
    with ui_app.connect_read_only(URL) as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("insert into ledger_events (event_type, payload) values ('X', '{}')")


def test_server_refuses_to_start_when_a_screen_drifts_from_the_schema(sample, monkeypatch):
    screens = load_screens()
    screens["STP-3100"].grids[0].columns.append(Column("gone", "ledger_events.gone"))
    monkeypatch.setattr(ui_app, "load_screens", lambda: screens)
    with pytest.raises(DefinitionError, match="ledger_events.gone"):
        ui_app.create_app(URL, calendar=CAL, fees=FEES)


def test_screen_page_carries_the_resolved_definition(client):
    r = client.get("/screen/STP-3100")
    assert r.status_code == 200 and 'id="stpui-data"' in r.text
    assert "차변 금액" in r.text                       # label taken from the DB comment
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/screen/STP-0000").status_code == 404
    assert client.get("/static/stpui.js").status_code == 200


def test_event_trace_through_the_api_balances(client, conn):
    seq = conn.execute("select min(seq) from ledger_events where event_type = 'ExecutionEvent'").fetchone()[0]
    body = client.post("/api/tx/STP3100R02", json={"params": {"seq": seq}}).json()
    assert body["meta"]["balanced"] is True
    journal = body["datasets"]["journal"]
    rows = [dict(zip(journal["columns"], r)) for r in journal["rows"]]
    assert rows and sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows)
    assert len(body["datasets"]["positions"]["rows"]) == 1


def test_values_are_json_ready(client, sample):
    nav = client.post("/api/tx/STP2200R01", json={"params": {}}).json()
    assert nav["meta"] == {"clean_streak": sample.streak, "gate_days": 20}
    first = dict(zip(nav["datasets"]["main"]["columns"], nav["datasets"]["main"]["rows"][0]))
    assert first["nav_per_1000"] == 1000 and isinstance(first["date"], str)


def test_bad_requests_get_a_plain_message(client):
    r = client.post("/api/tx/STP3100R03", json={"params": {"x": 1}})
    assert r.status_code == 400 and "없는 조회조건" in r.json()["error"]
    r = client.post("/api/tx/STP3100R02", json={"params": {"seq": 999999}})
    assert r.status_code == 400 and "999999" in r.json()["error"]


def test_only_local_hosts_are_answered(sample):
    app = ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day)
    assert TestClient(app).get("/").status_code == 400                   # Host: testserver
    assert TestClient(app, base_url="http://127.0.0.1").get("/").status_code == 200


def test_spec_page_is_generated(client):
    r = client.get("/spec/STP-2100")
    assert r.status_code == 200 and r.text.startswith("# STP-2100 일일 대사 결과")


def test_sample_today_is_the_last_booked_day(sample):
    assert ui_app.sample_today(URL)() == sample.last_day


def test_ui_command_serves_on_localhost(sample, capsys):
    served = []
    assert cli._ui(URL, {}, CAL, FEES, sample=False, port=9999, serve=served.append) == 0
    assert served and "http://127.0.0.1:9999" in capsys.readouterr().out
    assert cli.ui_database_url(URL, {"MINISTP_UI_DATABASE_URL": "postgresql:///ro"}, False) == "postgresql:///ro"
    assert "ministp_sample" in cli.ui_database_url(URL, {}, True)
    assert isinstance(sample.last_day, dt.date)


def _check(client, **params):
    body = client.post("/api/tx/STP1300R02", json={"params": params}).json()
    return body["meta"]["ok"], dict(zip(body["datasets"]["main"]["columns"], body["datasets"]["main"]["rows"][0]))


def test_order_capacity_boundary_is_the_order_checks_boundary(client):
    caps = client.post("/api/tx/STP1300R01", json={"params": {}}).json()["datasets"]["main"]
    first = dict(zip(caps["columns"], caps["rows"][0]))
    sym, most, sellable = first["symbol"], first["max_buy_qty"], first["sellable_qty"]
    ok, row = _check(client, symbol=sym, side="BUY", qty=most)
    assert ok and row["required_cash"] <= row["available_cash"]
    ok, row = _check(client, symbol=sym, side="BUY", qty=most + 1)
    assert not ok and row["reason"] == "INSUFFICIENT_CASH"
    assert _check(client, symbol=sym, side="SELL", qty=sellable)[0]
    ok, row = _check(client, symbol=sym, side="SELL", qty=sellable + 1)
    assert not ok and row["reason"] == "SHORT_SELL"


def test_order_capacity_uses_the_limit_price_and_checks_ticks(client):
    ok, row = _check(client, symbol="005930", side="BUY", qty=1, limit_price=60_000)
    assert ok and row["ref_price"] == 60_000 and row["ref_source"] == "지정가"
    ok, row = _check(client, symbol="005930", side="BUY", qty=1, limit_price=60_050)
    assert not ok and row["reason"] == "BAD_TICK"


def test_order_capacity_needs_a_known_symbol(client):
    r = client.post("/api/tx/STP1300R02", json={"params": {"symbol": "999999", "side": "BUY", "qty": 1}})
    assert r.status_code == 400 and "등록되지 않은 종목" in r.json()["error"]
    r = client.post("/api/tx/STP1300R02", json={"params": {"qty": 1}})
    assert r.status_code == 400 and "종목코드" in r.json()["error"]


def test_db_head_moves_when_the_writer_records(sample, conn):
    with ui_app.connect_read_only(URL) as ro:
        before = ui_app.read_head(ro)
        conn.execute("insert into instruments (symbol, name, market, asset_type) values ('LIVE01', '테스트', 'KOSPI', 'ETF')")
        assert ui_app.read_head(ro) == before            # instruments are not on the head
        conn.execute("insert into oms_orders (client_id, trade_date, symbol, side, qty)"
                     " values ('LIVE-1', %s, 'LIVE01', 'BUY', 1)", (sample.last_day,))
        assert ui_app.read_head(ro)["orders"] == before["orders"] + 1


def test_live_endpoint_is_an_event_stream(client):
    route = next(r for r in client.app.routes if getattr(r, "path", "") == "/api/live")
    assert "GET" in route.methods
