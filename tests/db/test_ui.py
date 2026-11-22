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
            c.execute("insert into ledger_events (event_type, payload, account_id) values ('X', '{}', 'A0001')")


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


def test_order_capacity_says_no_on_a_closed_day(sample):
    """The screen and the order path share one check, so the screen also knows the market is closed."""
    day = sample.last_day
    while CAL.is_business_day(day):
        day += dt.timedelta(days=1)
    app = ui_app.create_app(URL, calendar=CAL, fees=FEES, today=day, allowed_hosts=["testserver"])
    ok, row = _check(TestClient(app), symbol="005930", side="BUY", qty=1, limit_price=60_000)
    assert not ok and row["reason"] == "NOT_BUSINESS_DAY" and "영업일" in row["reason_text"]


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
        conn.execute("insert into oms_orders (client_id, trade_date, symbol, side, qty, account_id)"
                     " values ('LIVE-1', %s, 'LIVE01', 'BUY', 1, 'A0001')", (sample.last_day,))
        assert ui_app.read_head(ro)["orders"] == before["orders"] + 1


def test_live_endpoint_is_an_event_stream(client):
    route = next(r for r in client.app.routes if getattr(r, "path", "") == "/api/live")
    assert "GET" in route.methods


# ---------- STP-4100·4200·4300 기준정보 ----------

def _rows(client, tx, params=None, dataset="main"):
    body = client.post(f"/api/tx/{tx}", json={"params": params or {}}).json()
    ds = body["datasets"][dataset]
    return [dict(zip(ds["columns"], r)) for r in ds["rows"]], body["meta"]


def test_instrument_master_shows_the_tick_and_tax_the_system_uses(client):
    from ministp.instruments import AssetType
    from ministp.ticks import tick_size
    rows, _ = _rows(client, "STP4100R01")
    priced = [r for r in rows if r["last_close"] is not None]
    assert priced
    for r in priced:
        assert r["tick"] == tick_size(r["last_close"], AssetType(r["asset_type"]))
    kospi_stock = next(r for r in rows if r["market"] == "KOSPI" and r["asset_type"] == "STOCK")
    assert kospi_stock["sell_tax"] == "0.2%"          # 0.05% 거래세 + 0.15% 농특세
    events, _ = _rows(client, "STP4100R02", {"symbol": kospi_stock["symbol"]})
    assert all(e["payload"].get("symbol") == kospi_stock["symbol"] for e in events)


def test_account_ladder_ends_at_the_cash_after_every_settlement(client):
    (acct,), _ = _rows(client, "STP4200R01")
    ladder, _ = _rows(client, "STP4200R02")
    pending, _ = _rows(client, "STP4200R03")
    assert [r["day_label"] for r in ladder[:3]] == ["D+0", "D+1", "D+2"]   # D+2까지는 결제가 없어도 보여요
    assert ladder[0]["cash_after"] == acct["cash_d0"] + ladder[0]["cash_in"] - ladder[0]["cash_out"]
    assert ladder[-1]["cash_after"] == acct["projected_cash"]
    assert acct["cash_d0"] + sum(p["amount"] for p in pending) == acct["projected_cash"]
    assert acct["receivables"] - acct["payables"] == sum(p["amount"] for p in pending)
    assert acct["unsettled"] == len(pending)
    assert all(p["settle_date"] > p["trade_date"] for p in pending)


def test_account_screen_never_carries_an_account_number(client):
    page = client.get("/screen/STP-4200").text
    body = client.post("/api/tx/STP4200R01", json={"params": {}}).text
    for text in (page, body):
        assert "account_no" not in text and "CANO" not in text


def test_reference_data_is_read_from_what_the_ledger_uses(client):
    fees, _ = _rows(client, "STP4300R01")
    assert {f["commission_rate"] for f in fees} == {"0.015%"} and {f["commission_unit"] for f in fees} == {10}
    rules, _ = _rows(client, "STP4300R02")
    assert [r["status"] for r in rules] == ["적용 중"] and rules[0]["cycle"] == "T+2"
    holidays, meta = _rows(client, "STP4300R04")
    assert [h["date"] for h in holidays] == [d.isoformat() for d in CAL.holidays]
    assert meta["until"] == CAL.verified_until.isoformat() == "2027-01-31"   # 휴장일이 확인된 날까지만
    ticks, _ = _rows(client, "STP4300R03")
    assert ticks[-1] == {"asset_type": "ETF", "price_from": 0, "price_below": None, "tick": 5}


# ---------- 셀 표시 유형 (실시간 잔고) ----------

def test_holdings_carry_the_weight_and_the_closing_prices_the_ledger_recorded(client, conn, sample):
    from ministp.ops import load_books
    from ministp.queries import close_trends, last_closes
    rows, _ = _rows(client, "STP1100R01")
    assert rows, "the sample holds something"
    # 비중: 장부금액 ÷ 장부금액 합계, so the column adds up to one
    total = sum(r["book_cost"] for r in rows)
    assert sum(r["weight"] for r in rows) == pytest.approx(1, abs=0.001)
    assert all(r["weight"] == pytest.approx(r["book_cost"] / total, abs=0.0001) for r in rows)
    # 추세선: 원장이 남긴 종가만, 오래된 날부터. 마지막 값은 다른 화면이 쓰는 최근 종가와 같아요
    ledger = load_books(conn, CAL, FEES).ledger
    trends, (_, closes) = close_trends(ledger), last_closes(ledger)
    for r in rows:
        assert r["trend"] == trends[r["symbol"]]
        assert 2 <= len(r["trend"]) <= 20 and r["trend"][-1] == closes[r["symbol"]]


def test_close_trends_keep_the_last_days_only(conn, sample):
    from ministp.ops import load_books
    from ministp.queries import close_trends
    ledger = load_books(conn, CAL, FEES).ledger
    full, short = close_trends(ledger), close_trends(ledger, days=3)
    assert all(len(v) <= 3 for v in short.values())
    assert all(full[sym][-len(v):] == v for sym, v in short.items())


def test_holdings_screen_draws_the_badge_bar_meter_and_trend(client):
    screen = client.get("/api/screen/STP-1100").json()
    grid = next(g for g in screen["grids"] if g["id"] == "holdings")
    cols = {c["name"]: c for c in grid["columns"]}
    assert cols["instrument_name"]["display"] == "badge"
    assert cols["symbol"]["inside"] == cols["asset_type"]["inside"] == "instrument_name"
    assert [cols[n]["display"] for n in ("book_cost", "weight", "trend")] == ["bar", "meter", "spark"]
    # 그림을 더한 만큼 열을 줄여요: 일곱 열이던 그리드가 여섯 열로 보여요
    assert len([c for c in grid["columns"] if not c["inside"]]) == 6
    spec = client.get("/spec/STP-1100").text
    assert "배지 안에 표시" in spec and "추세선" in spec
