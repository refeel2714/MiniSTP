"""처리 버튼: what a screen may change, who may ask for it, and that it is the CLI's own command."""
import json
import os
import re

import pytest

from ministp import __main__ as cli
from ministp.ops import acquire_writer
from ministp.sample import build_sample
from ministp.store import erd
from ministp.ui import app as ui_app
from ministp.ui.definitions import load_lookups, load_screens, lookup_problems, resolve
from ministp.ui.spec import screen_markdown

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
RESOLVE = "/api/action/STP2100U01"


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=8, seed=5)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


def page_data(client) -> dict:
    return json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                client.get("/screen/STP-2100").text, re.S).group(1))


@pytest.fixture
def reader(sample):
    return TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                        allowed_hosts=["testserver"]))


@pytest.fixture
def writer(sample):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"], write_dsn=URL))
    client.headers["X-STPUI-Token"] = page_data(client)["token"]
    return client


def a_break(conn) -> int:
    return conn.execute("select min(id) from recon_results where status = 'BREAK'").fetchone()[0]


def count(conn) -> int:
    return conn.execute("select count(*) from recon_results").fetchone()[0]


def judge(conn, **over) -> dict:
    return {"params": {"result_id": a_break(conn), "cause": "TIMING", "note": "증권사 반영이 하루 늦음", **over}}


def test_a_server_opened_to_read_refuses_every_button(reader, conn):
    data = page_data(reader)
    assert data["writable"] is False and data["token"] == ""
    before = count(conn)
    r = reader.post(RESOLVE, json=judge(conn))
    assert r.status_code == 403 and "조회 전용" in r.json()["error"]
    assert count(conn) == before


def test_a_button_is_pressed_only_from_this_servers_own_page(writer, conn):
    before = count(conn)
    body = judge(conn)
    assert page_data(writer)["writable"] is True and len(writer.headers["X-STPUI-Token"]) >= 24
    for headers in ({"X-STPUI-Token": ""}, {"X-STPUI-Token": "guess"},
                    {"Origin": "http://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        r = writer.post(RESOLVE, json=body, headers=headers)
        assert r.status_code == 403 and "이 화면에서 보낸 요청이 아니에요" in r.json()["error"], headers
    # a plain form post (no preflight in a browser) is not taken either
    r = writer.post(RESOLVE, content=json.dumps(body), headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    assert writer.post(RESOLVE, content="[1]", headers={"Content-Type": "application/json"}).status_code == 400
    assert count(conn) == before
    other = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, allowed_hosts=["testserver"], write_dsn=URL))
    assert page_data(other)["token"] != writer.headers["X-STPUI-Token"]        # a new token every start


def test_judging_a_break_on_the_screen_is_the_resolve_command(writer, conn):
    rid = a_break(conn)
    original = conn.execute("select status, cause_code, note from recon_results where id = %s", (rid,)).fetchone()
    r = writer.post(RESOLVE, json=judge(conn), headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200, r.text
    new_id = conn.execute("select max(id) from recon_results").fetchone()[0]
    assert f"새 행 {new_id}" in r.json()["message"]
    assert conn.execute("select status, cause_code, note, supersedes_id from recon_results where id = %s",
                        (new_id,)).fetchone() == ("RESOLVED", "TIMING", "증권사 반영이 하루 늦음", rid)
    assert conn.execute("select status, cause_code, note from recon_results where id = %s",
                        (rid,)).fetchone() == original                          # the break itself is never edited
    shown = writer.post("/api/tx/STP2100R01", json={"params": {"only_breaks": True}}).json()["datasets"]["main"]
    row = next(dict(zip(shown["columns"], r)) for r in shown["rows"] if r[shown["columns"].index("id")] == rid)
    assert (row["resolved_cause"], row["resolved_note"]) == ("TIMING", "증권사 반영이 하루 늦음")


def test_bad_input_is_refused_in_plain_words_and_nothing_is_written(writer, conn):
    before = count(conn)
    match = conn.execute("select min(id) from recon_results where status = 'MATCH'").fetchone()[0]
    for url, body, problem in [
        (RESOLVE, judge(conn, note=""), "note 값을 넣어"),
        (RESOLVE, judge(conn, cause="NOPE"), "없는 원인 코드"),
        (RESOLVE, judge(conn, result_id=match), "불일치 행이 아니에요"),
        (RESOLVE, judge(conn, extra=1), "없는 입력 항목"),
        ("/api/action/STP2100U99", judge(conn), "없는 처리 트랜잭션"),
        ("/api/action/STP2100R01", judge(conn), "없는 처리 트랜잭션"),      # a read id is not a button
    ]:
        r = writer.post(url, json=body)
        assert r.status_code == 400 and problem in r.json()["error"], (problem, r.text)
    assert count(conn) == before


def test_a_button_waits_its_turn_behind_the_single_writer(writer, conn, sample):
    before = count(conn)
    with psycopg.connect(URL, autocommit=True) as running:      # e.g. `run` during market hours
        acquire_writer(running)
        r = writer.post(RESOLVE, json=judge(conn))
        assert r.status_code == 400 and "원장을 쓰고 있어요" in r.json()["error"]
        assert count(conn) == before
    assert writer.post(RESOLVE, json=judge(conn)).status_code == 200            # free again once it ends
    assert writer.post(RESOLVE, json=judge(conn)).status_code == 200            # and a button lets go of the lock


def test_buttons_and_lookups_reach_the_runtime_and_the_screen_spec(conn):
    catalog = erd.read_catalog(conn, "public")
    lookups = load_lookups()
    assert lookup_problems(lookups, catalog) == []
    screens = load_screens()
    results = next(g for g in resolve(screens["STP-2100"], catalog, lookups)["grids"] if g["id"] == "results")
    button = results["actions"][0]
    assert (button["label"], button["tx"], button["title"], button["needs_row"]) == (
        "원인 판단", "STP2100U01", "불일치 원인 판단", True)
    assert [(f["name"], f["label"], f["required"]) for f in button["fields"]] == [
        ("result_id", "행 번호", True), ("cause", "원인 코드", True), ("note", "판단 메모", True)]
    popup = resolve(screens["STP-1300"], catalog, lookups)["lookups"]["symbol"]
    assert (popup["title"], popup["value"], popup["shows"]) == ("종목 찾기", "symbol", "instrument_name")
    assert [c["label"] for c in popup["columns"]][:2] == ["종목코드", "종목명"]
    spec = screen_markdown(screens["STP-2100"], catalog)
    assert "### 처리 버튼 `resolve` 원인 판단" in spec and "| `STP2100U01` | 불일치 원인 판단 |" in spec
    assert "## 찾기 팝업" in screen_markdown(screens["STP-1300"], catalog)


def test_the_ui_command_turns_buttons_on_only_when_asked(sample, capsys):
    apps = []

    def writable(**kw) -> bool:
        env = {"MINISTP_UI_DATABASE_URL": URL, "MINISTP_SAMPLE_DATABASE_URL": URL}
        assert cli._ui(URL, env, CAL, FEES, port=9999, serve=apps.append, **kw) == 0
        return page_data(TestClient(apps[-1], base_url="http://127.0.0.1"))["writable"]

    assert writable(sample=False) is False and "조회 전용" in capsys.readouterr().out
    assert writable(sample=False, write=True) is True and "처리 버튼 켜짐" in capsys.readouterr().out
    assert writable(sample=True) is True                                       # the sample DB is for trying things
