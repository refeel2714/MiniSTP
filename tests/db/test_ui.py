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


def test_every_page_carries_what_the_jump_box_can_open(client, conn):
    """바로 가기 창(Ctrl+K): 모든 화면과, 화면을 열 수 있는 코드(종목)의 목록이 어느 페이지에나 실려요."""
    import json
    import re
    for path in ("/", "/screen/STP-2100"):
        data = json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                    client.get(path).text, re.S).group(1))
        assert [m["id"] for m in data["menu"]] == list(load_screens())
        (jump,) = data["jump"]
        assert jump["name"] == "symbol" and {t["id"]: t["param"] for t in jump["into"]} == {
            "STP-1300": "symbol", "STP-3100": "symbol", "STP-3200": "symbol"}
    listed = client.post(f"/api/tx/{jump['tx']}", json={"params": {}}).json()["datasets"]["main"]
    assert {jump["value"], jump["shows"]} <= set(listed["columns"])     # what the box matches on and shows
    at = listed["columns"].index(jump["value"])
    assert sorted(r[at] for r in listed["rows"]) == [r[0] for r in conn.execute("select symbol from instruments order by 1")]
    for t in jump["into"]:                                                # each target opens for that code
        screen = load_screens()[t["id"]]
        assert any(c.name == t["param"] and c.lookup == "symbol" for c in screen.conditions)
        assert client.get(f"/screen/{t['id']}?{t['param']}={listed['rows'][0][at]}").status_code == 200
    assert "openJump" in client.get("/static/stpui.js").text


def test_every_page_carries_the_menu_tree_and_a_screen_its_place_in_it(client, conn):
    """왼쪽 메뉴트리: 분류(대분류 > 중분류 > 화면)가 어느 페이지에나 실리고, 화면은 자기 분류를 알아요."""
    import json
    import re

    def data(path):
        return json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                    client.get(path).text, re.S).group(1))
    home, page = data("/"), data("/screen/STP-3200")
    assert home["tree"] == page["tree"] and home["path"] == [] and page["path"] == ["원장·회계", "분개"]
    assert [s["id"] for a in home["tree"] for g in a["groups"] for s in g["screens"]] == sorted(load_screens())
    script, index = client.get("/static/stpui.js").text, all_docs(load_screens(), erd.read_catalog(conn, "public"))
    assert "DATA.tree" in script and 'class: "tree"' in script
    assert "| **3 원장·회계**" in index["README.md"] and "| 32 분개 | [STP-3200](STP-3200.md) 분개 조회 |" in index["README.md"]
    assert "분류: 원장·회계 > 분개" in index["STP-3200.md"]


def test_server_refuses_to_start_when_a_screen_has_no_place_in_the_classification(sample, monkeypatch):
    from ministp.ui.definitions import load_menu
    menu = [a for a in load_menu() if a.id != "3"]
    monkeypatch.setattr(ui_app, "load_menu", lambda: menu)
    with pytest.raises(DefinitionError, match="STP-3100 원장 조회: 대분류 3이 menu.yaml에 없어요"):
        ui_app.create_app(URL, calendar=CAL, fees=FEES)


def test_screen_page_carries_the_resolved_definition(client):
    r = client.get("/screen/STP-3200")
    assert r.status_code == 200 and 'id="stpui-data"' in r.text
    assert "차변 금액" in r.text                       # label taken from the DB comment
    assert "차변 금액" not in client.get("/screen/STP-3100").text   # the journal is not on the ledger screen
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/screen/STP-0000").status_code == 404
    assert client.get("/static/stpui.js").status_code == 200


def test_event_trace_through_the_api_balances(client, conn):
    seq = conn.execute("select min(seq) from ledger_events where event_type = 'ExecutionEvent'").fetchone()[0]
    body = client.post("/api/tx/STP3200R02", json={"params": {"seq": seq}}).json()
    assert body["meta"]["balanced"] is True and set(body["datasets"]) == {"journal"}
    journal = body["datasets"]["journal"]
    rows = [dict(zip(journal["columns"], r)) for r in journal["rows"]]
    assert rows and sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows)
    ledger = client.post("/api/tx/STP3100R02", json={"params": {"seq": seq}}).json()["datasets"]
    assert set(ledger) == {"event", "positions", "cash"} and len(ledger["positions"]["rows"]) == 1


def test_the_journal_list_counts_what_each_event_wrote_and_both_screens_open_on_one_event(client, conn):
    """STP-3200 왼쪽 목록: 분개를 만든 이벤트만, 전표 수와 분개 줄 수와 차변 합계와 함께. seq 조건은 다른 화면이 고른 이벤트로 열 때 써요."""
    listed = client.post("/api/tx/STP3200R01", json={"params": {}}).json()["datasets"]["main"]
    rows = [dict(zip(listed["columns"], r)) for r in listed["rows"]]
    want = conn.execute("select j.event_seq, count(distinct j.entry_id), count(*), sum(l.debit) from journal_entries j"
                        " join journal_lines l using (entry_id) group by 1 order by 1 desc").fetchall()
    assert [(r["seq"], r["entries"], r["lines"], r["amount"]) for r in rows] == [tuple(w) for w in want]
    assert max(r["entries"] for r in rows) == 2 and max(r["lines"] for r in rows) >= 5   # 평가는 전표 둘, 매도는 다섯 줄
    silent = conn.execute("select count(*) from ledger_events e where not exists"
                          " (select 1 from journal_entries j where j.event_seq = e.seq)").fetchone()[0]
    assert silent > 0 and len(rows) + silent == conn.execute("select count(*) from ledger_events").fetchone()[0]
    big = max(rows, key=lambda r: r["lines"])
    for tx in ("STP3200R01", "STP3100R01"):
        one = client.post(f"/api/tx/{tx}", json={"params": {"seq": big["seq"]}}).json()["datasets"]["main"]
        assert [r[one["columns"].index("seq")] for r in one["rows"]] == [big["seq"]]
    sells = client.post("/api/tx/STP3200R01", json={"params": {"event_type": "ExecutionEvent", "symbol": "005930"}})
    assert {r[1] for r in sells.json()["datasets"]["main"]["rows"]} == {"ExecutionEvent"}
    for sid in ("STP-3100", "STP-3200"):
        assert client.get(f"/screen/{sid}?seq={big['seq']}").status_code == 200


def test_the_t_accounts_of_every_event_balance_entry_by_entry(client, conn):
    """STP-3200 분개의 T계정 보기: 전표마다 차변 합계와 대변 합계가 같고, T에 놓이는 금액은 분개 줄 그대로예요."""
    grid = next(g for g in client.get("/api/screen/STP-3200").json()["grids"] if g["id"] == "journal")
    t = grid["taccount"]
    assert grid["view"] == "taccount" and set(t.values()) <= {c["name"] for c in grid["columns"]}
    seen = 0
    for (seq,) in conn.execute("select seq from ledger_events order by seq").fetchall():
        ds = client.post("/api/tx/STP3200R02", json={"params": {"seq": seq}}).json()["datasets"]["journal"]
        lines = [dict(zip(ds["columns"], r)) for r in ds["rows"]]
        entries = {}
        for ln in lines:
            assert (ln[t["debit"]] == 0) != (ln[t["credit"]] == 0)          # a line sits on one side of its T
            entries.setdefault(ln[t["entry"]], []).append(ln)
        for entry_id, mine in entries.items():
            assert sum(ln[t["debit"]] for ln in mine) == sum(ln[t["credit"]] for ln in mine), (seq, entry_id)
            want = conn.execute("select account_code, debit, credit from journal_lines where entry_id = %s"
                                " order by line_id", (entry_id,)).fetchall()
            assert sorted((ln[t["account"]], ln[t["debit"]], ln[t["credit"]]) for ln in mine) == sorted(want)
        seen += len(entries)
    assert seen == conn.execute("select count(*) from journal_entries").fetchone()[0] > 0


def test_values_are_json_ready(client, sample):
    nav = client.post("/api/tx/STP2200R01", json={"params": {}}).json()
    assert nav["meta"] == {"clean_streak": sample.streak, "gate_days": 20}
    first = dict(zip(nav["datasets"]["main"]["columns"], nav["datasets"]["main"]["rows"][0]))
    assert first["nav_per_1000"] == 1000 and isinstance(first["date"], str)


def test_bad_requests_get_a_plain_message(client):
    r = client.post("/api/tx/STP3300R01", json={"params": {"x": 1}})
    assert r.status_code == 400 and "없는 조회조건" in r.json()["error"]
    for tx in ("STP3100R02", "STP3200R02"):
        r = client.post(f"/api/tx/{tx}", json={"params": {"seq": 999999}})
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


# ---------- STP-4100 종목정보·STP-4300 거래 기준, STP-3400 계좌정보 ----------

def _rows(client, tx, params=None, dataset="main"):
    body = client.post(f"/api/tx/{tx}", json={"params": params or {}}).json()
    ds = body["datasets"][dataset]
    return [dict(zip(ds["columns"], r)) for r in ds["rows"]], body["meta"]


def test_instrument_master_holds_nothing_of_the_account_and_shows_the_rules_the_system_uses(client):
    rows, _ = _rows(client, "STP4100R01")
    # reference data is the same whichever account is looked at: no holdings, no closes the books took
    assert set(rows[0]) == {"symbol", "instrument_name", "market", "asset_type", "sell_tax"}
    kospi_stock = next(r for r in rows if r["market"] == "KOSPI" and r["asset_type"] == "STOCK")
    assert kospi_stock["sell_tax"] == "0.2%"          # 0.05% 거래세 + 0.15% 농특세
    rules, _ = _rows(client, "STP4100R02", {"symbol": kospi_stock["symbol"]})
    by_item = {r["item"]: r["value"] for r in rules}
    assert (by_item["위탁수수료율"], by_item["증권거래세율"], by_item["농어촌특별세율"]) == ("0.015%", "0.05%", "0.15%")
    assert by_item["결제주기"] == "T+2" and by_item["2,000원 미만"] == "1원" and by_item["500,000원 이상"] == "1,000원"
    etf = next(r for r in rows if r["asset_type"] == "ETF")
    etf_rules, _ = _rows(client, "STP4100R02", {"symbol": etf["symbol"]})
    assert [(r["item"], r["value"]) for r in etf_rules if r["kind"] == "호가단위"] == [("2,000원 미만", "1원"), ("2,000원 이상", "5원")]
    assert {r["item"]: r["value"] for r in etf_rules}["증권거래세율"] == "0%"
    assert client.post("/api/tx/STP4100R02", json={"params": {"symbol": "999999"}}).status_code == 400
    screen = client.get("/api/screen/STP-4100").json()
    assert not any(c["name"] in ("qty", "book_cost", "last_close") for g in screen["grids"] for c in g["columns"])


def test_account_ladder_ends_at_the_cash_after_every_settlement(client):
    (acct,), _ = _rows(client, "STP3400R01")
    ladder, _ = _rows(client, "STP3400R02")
    pending, _ = _rows(client, "STP3400R03")
    assert [r["day_label"] for r in ladder[:3]] == ["D+0", "D+1", "D+2"]   # D+2까지는 결제가 없어도 보여요
    assert ladder[0]["cash_after"] == acct["cash_d0"] + ladder[0]["cash_in"] - ladder[0]["cash_out"]
    assert ladder[-1]["cash_after"] == acct["projected_cash"]
    assert acct["cash_d0"] + sum(p["amount"] for p in pending) == acct["projected_cash"]
    assert acct["receivables"] - acct["payables"] == sum(p["amount"] for p in pending)
    assert acct["unsettled"] == len(pending)
    assert all(p["settle_date"] > p["trade_date"] for p in pending)


def test_account_screen_never_carries_an_account_number(client):
    page = client.get("/screen/STP-3400").text
    body = client.post("/api/tx/STP3400R01", json={"params": {}}).text
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
    assert ticks[-2:] == [{"asset_type": "ETF", "price_from": 0, "price_below": 2_000, "tick": 1},
                          {"asset_type": "ETF", "price_from": 2_000, "price_below": None, "tick": 5}]


# ---------- 셀 표시 유형 (실시간 잔고) ----------

def test_holdings_are_valued_at_the_last_close_when_run_has_no_quotes(client, conn, sample):
    """R-68: `run`이 오늘 현재가를 남기지 않았으면 원장의 최근 종가로 평가해요. 계좌정보의 평가금액과 같은 값이에요."""
    from ministp.ledger import Ledger
    from ministp.ops import load_books
    from ministp.queries import Dataset, holdings_view, last_closes
    rows, meta = _rows(client, "STP1100R01")
    close_day, closes = last_closes(load_books(conn, CAL, FEES).ledger)
    assert rows and all(r["symbol"] in closes for r in rows)
    for r in rows:
        assert r["price"] == closes[r["symbol"]] and r["price_at"] == f"종가 {close_day:%m-%d}"
        assert r["market_value"] == r["qty"] * closes[r["symbol"]]
        assert r["unrealized_pnl"] == r["market_value"] - r["book_cost"]
        assert r["pnl_rate"] == f"{float(r['unrealized_pnl']) / float(r['book_cost']) * 100 + 0.0:+.2f}%"
    assert meta == {"basis": f"종가 {close_day:%m-%d}"}
    # 비중: 평가금액 ÷ 평가금액 합계
    total = sum(r["market_value"] for r in rows)
    assert sum(r["weight"] for r in rows) == pytest.approx(1, abs=0.001)
    assert all(r["weight"] == pytest.approx(r["market_value"] / total, abs=0.0001) for r in rows)
    account = _rows(client, "STP3400R01")[0][0]
    assert total == account["market_value"]

    # a holding the ledger has no price for yet (bought today, before the day's close) is left unvalued, not guessed
    bare = holdings_view(Dataset(("symbol", "qty", "book_cost"), (("999999", 3, 30_000),)), Ledger(CAL, FEES, {}))
    row = dict(zip(bare.columns, bare.rows[0]))
    assert row["price"] is row["market_value"] is row["unrealized_pnl"] is row["weight"] is None


def test_holdings_take_todays_quotes_and_say_which_price(conn, sample):
    """R-68: 오늘 받은 현재가가 종가보다 늦으면 현재가로. 목록 위 평가 기준은 가장 오래된 쪽을 말해요."""
    import datetime as dt
    from ministp.ops import load_books
    from ministp.queries import Dataset, holdings_view, last_closes, valuation_basis
    from ministp.quotes import Quote
    ledger = load_books(conn, CAL, FEES).ledger
    close_day, closes = last_closes(ledger)
    sym, other = sorted(closes)[:2]
    at = dt.datetime.combine(close_day + dt.timedelta(days=3), dt.time(10, 32))
    quotes = {sym: Quote(sym, closes[sym] + 100, at)}
    base = Dataset(("symbol", "qty", "book_cost"), ((sym, 2, 1_000), (other, 1, 1_000)))
    rows = [dict(zip(v.columns, r)) for v in [holdings_view(base, ledger, quotes, at)] for r in v.rows]
    assert rows[0]["price"] == closes[sym] + 100 and rows[0]["price_at"] == "10:32"
    assert rows[0]["market_value"] == 2 * (closes[sym] + 100)
    assert rows[1]["price"] == closes[other] and rows[1]["price_at"] == f"종가 {close_day:%m-%d}"
    assert valuation_basis(ledger, [sym, other], quotes, at) == f"종가 {close_day:%m-%d}"    # 섞이면 오래된 쪽
    assert valuation_basis(ledger, [sym], quotes, at) == "현재가 10:32"
    later = at + dt.timedelta(minutes=6)
    assert valuation_basis(ledger, [sym], quotes, later) == "현재가 10:32 · 갱신 멈춤"
    assert holdings_view(base, ledger, quotes, later).rows[0][4] == "10:32 갱신 멈춤"
    assert valuation_basis(ledger, [], quotes, at) == "가격 없음"


def test_close_trends_keep_the_last_days_only(conn, sample):
    from ministp.ops import load_books
    from ministp.queries import close_trends
    ledger = load_books(conn, CAL, FEES).ledger
    full, short = close_trends(ledger), close_trends(ledger, days=3)
    assert all(len(v) <= 3 for v in short.values())
    assert all(full[sym][-len(v):] == v for sym, v in short.items())


def test_holdings_screen_draws_the_badge_meter_and_the_lines_under_prices(client):
    screen = client.get("/api/screen/STP-1100").json()
    grid = next(g for g in screen["grids"] if g["id"] == "holdings")
    cols = {c["name"]: c for c in grid["columns"]}
    assert cols["instrument_name"]["display"] == "badge"
    assert cols["symbol"]["inside"] == cols["asset_type"]["inside"] == "instrument_name"
    assert cols["weight"]["display"] == "meter"
    # 가격 시각과 손익률은 열을 따로 차지하지 않고 가격·손익 칸 아래에 작게 적혀요
    assert cols["price"]["sub"] == "price_at" and cols["price_at"]["inside"] == "price"
    assert cols["unrealized_pnl"]["sub"] == "pnl_rate" and cols["pnl_rate"]["inside"] == "unrealized_pnl"
    # 평가 열 셋을 보이게 한 대신 최근 종가 추세 열을 뺐어요 (UI-02, UI-12)
    assert "trend" not in cols
    shown = [c["name"] for c in grid["columns"] if not c["inside"] and not c["hidden"]]
    assert shown == ["instrument_name", "qty", "price", "market_value", "unrealized_pnl", "book_cost", "weight", "today_qty"]
    assert grid["meta"] == {"basis": "평가 기준"} and grid["totals"] == ["market_value", "unrealized_pnl", "book_cost"]
    assert grid["map"] == {"label": "instrument_name", "size": "book_cost", "tone": "unrealized_pnl"}
    spec = client.get("/spec/STP-1100").text
    assert "배지 안에 표시" in spec and "칸 아래에 작게 표시" in spec


# ---------- 대사 달력 ----------

def test_recon_calendar_opens_on_the_latest_day_and_counts_the_gate(client, sample):
    days, meta = _rows(client, "STP2100R02")
    last = sample.last_day.isoformat()
    assert days[0]["date"] == last
    assert meta == {"clean_streak": sample.streak, "gate_left": 20 - sample.streak, "selected": last}
    states = {r["state"] for r in days}
    assert "판단 완료" in states and states <= {"일치", "판단 완료", "휴장"}   # 샘플은 불일치를 모두 판단해 둬요
    # 오른쪽 상세는 달력의 한 칸이 가리키는 그날의 대사 결과
    judged = next(r for r in days if r["state"] == "판단 완료")
    detail, _ = _rows(client, "STP2100R01", {"date_from": judged["date"], "date_to": judged["date"], "kind": judged["kind"]})
    assert len(detail) == judged["items"] and sum(r["status"] == "BREAK" for r in detail) == judged["breaks"]
    assert all(r["resolved_cause"] for r in detail if r["status"] == "BREAK") and judged["open_breaks"] == 0
    r = client.post("/api/tx/STP2100R02", json={"params": {"date_from": "2020-01-01"}})
    assert r.status_code == 400 and "두 해" in r.json()["error"]
    r = client.post("/api/tx/STP2100R02", json={"params": {"date_from": last, "date_to": "2026-01-02"}})
    assert r.status_code == 400 and "시작일" in r.json()["error"]


def test_recon_screen_is_a_calendar_with_that_days_results_next_to_it(client):
    screen = client.get("/api/screen/STP-2100").json()
    days, results = screen["grids"]
    assert screen["layout"] == "split" and [c["name"] for c in screen["conditions"]] == ["date_from", "date_to", "kind"]
    assert (days["view"], days["calendar"], days["key"]) == ("calendar", {"date": "date", "state": "state"}, "date")
    assert (results["load"], results["master"]) == ("master", "days")
    assert results["link"] == {"date_from": "date", "date_to": "date", "kind": "kind"}
    assert [a["tx"] for a in results["actions"]] == ["STP2100U01"]     # 원인 판단 버튼은 그날의 결과 쪽에
    spec = client.get("/spec/STP-2100").text
    assert "- 표시: 달력" in spec and "`STP2100R02` | 날짜별 대사 요약" in spec


# ---------- 주문 진행 단계와 결제 ----------

def test_blotter_shows_where_each_order_stands_and_its_settlements(client, conn, sample):
    screen = client.get("/api/screen/STP-1200").json()
    orders, order, fills = screen["grids"]
    cols = {c["name"]: c for c in orders["columns"]}
    assert screen["layout"] == "split" and (order["view"], order["dataset"], fills["dataset"]) == ("cards", "order", "fills")
    assert cols["progress"]["steps"] == ["의도", "접수", "체결", "결제"] and cols["qty"]["inside"] == "filled_qty"
    assert cols["client_id"]["hidden"] and orders["key"] == "client_id"
    # 그림을 더한 만큼 열을 줄여요: 열네 열이던 주문 목록이 여섯 열로 보여요
    assert len([c for c in orders["columns"] if not c["inside"] and not c["hidden"]]) == 6
    day = conn.execute("select min(trade_date) from oms_orders").fetchone()[0]     # 샘플의 첫 주문일: 결제까지 끝난 날
    rows, _ = _rows(client, "STP1200R01", {"day": day.isoformat()})
    assert rows and all(len(r["progress"]) == 4 and r["stage"] for r in rows)
    filled = next(r for r in rows if r["filled_qty"])
    assert (filled["progress"], filled["stage"], filled["settled_qty"]) == (["done"] * 4, "결제 완료", filled["filled_qty"])
    body = client.post("/api/tx/STP1200R02", json={"params": {"client_id": filled["client_id"]}}).json()
    sent = dict(zip(body["datasets"]["order"]["columns"], body["datasets"]["order"]["rows"][0]))
    execs = [dict(zip(body["datasets"]["fills"]["columns"], r)) for r in body["datasets"]["fills"]["rows"]]
    assert sent["order_no"] == filled["order_no"] and sum(e["qty"] for e in execs) == filled["filled_qty"]
    assert all(e["settle_state"] == "결제 완료" and e["settled_on"] == e["settle_date"] for e in execs)
    r = client.post("/api/tx/STP1200R02", json={"params": {"client_id": "S-없는주문"}})
    assert r.status_code == 400 and "없는 주문" in r.json()["error"]
    spec = client.get("/spec/STP-1200").text
    assert "진행 단계(의도 → 접수 → 체결 → 결제" in spec and "`filled_qty` 막대 안에 표시" in spec and "숨김" in spec


def test_returns_screen_reads_the_nav_month_by_month(client):
    months, meta = _rows(client, "STP6100R01")
    assert months and {"since_start", "year_to_date", "one_month", "last_date"} <= set(meta)
    assert meta["since_start"] == f"{months[0]['cum_return']:+.2f}%"     # the newest month's 누적 runs from the start
    assert sum(m["flows"] for m in months) > 0                             # the opening deposit, not a return
    days, _ = _rows(client, "STP6100R02", {"month": months[0]["month"]})
    assert days[0]["month_to_date"] == months[0]["month_return"] and days[0]["date"] == months[0]["last_date"]
    assert client.post("/api/tx/STP6100R02", json={"params": {"month": "11월"}}).status_code == 400
