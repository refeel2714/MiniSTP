"""관리 화면의 처리 버튼: 코드 관리(STP-4600), 계좌 관리(STP-4400), 고객 등록(STP-1400).

Each button is the CLI command of the same name, so the rules are tested once in
test_codes.py and test_accounts.py. Here: the screen reaches them, says their
refusals in the same words, and writes nothing when refused.
"""
import datetime as dt
import json
import os
import re

import pytest

from ministp import accounts, codes
from ministp.store import PostgresLedgerStore, erd
from ministp.ui import app as ui_app
from ministp.ui.definitions import load_lookups, load_screens, resolve

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
TODAY = dt.date(2026, 11, 20)


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        PostgresLedgerStore(c).init_schema()
        yield c


def page_data(client, screen="STP-4400") -> dict:
    return json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                client.get(f"/screen/{screen}").text, re.S).group(1))


@pytest.fixture
def press(conn):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=TODAY, allowed_hosts=["testserver"],
                                          write_dsn=URL))
    client.headers["X-STPUI-Token"] = page_data(client)["token"]

    def press(tx, **params):
        r = client.post(f"/api/action/{tx}", json={"params": params})
        return r.status_code, r.json().get("message") or r.json().get("error")
    press.client = client
    return press


def test_code_buttons_are_the_code_command(press, conn):
    assert press("STP4600U03", group_id="RECON_CAUSE", code="TIMING", name="조회 시점 차이") == (
        200, "코드명을 바꿨어요: RECON_CAUSE TIMING '시점 차이' → '조회 시점 차이'. 코드와 저장된 값은 그대로예요.")
    assert codes.names(conn, "RECON_CAUSE")["TIMING"] == "조회 시점 차이"
    assert codes.check(conn) == []                                   # a name is free to change

    assert press("STP4600U01", group_id="MEMO_KIND", name="메모 분류")[0] == 200
    assert press("STP4600U02", group_id="MEMO_KIND", code="CHECK", name="확인 필요")[0] == 200
    assert press("STP4600U02", group_id="MEMO_KIND", code="DONE", name="처리 완료", description="더 볼 것 없음")[0] == 200
    assert press("STP4600U04", group_id="MEMO_KIND", code="CHECK", in_use="사용 안 함")[0] == 200
    assert codes.list_codes(conn, "MEMO_KIND") == [("CHECK", "확인 필요", None, False),
                                                   ("DONE", "처리 완료", "더 볼 것 없음", True)]
    assert press("STP4600U04", group_id="MEMO_KIND", code="CHECK", in_use="사용")[0] == 200
    assert codes.list_codes(conn, "MEMO_KIND")[0][3] is True


def test_code_buttons_refuse_what_the_command_refuses_and_write_nothing(press, conn):
    before = conn.execute("select count(*), max(updated_at) from common_codes").fetchone()
    for tx, params, problem in [
        ("STP4600U02", dict(group_id="ACCOUNT_STATUS", code="FROZEN", name="동결"), "프로그램이 정한 코드"),
        ("STP4600U04", dict(group_id="ACCOUNT_STATUS", code="CLOSED", in_use="사용 안 함"), "끌 수 없어요"),
        ("STP4600U04", dict(group_id="ACCOUNT_STATUS", code="CLOSED", in_use="off"), "사용 여부는"),
        ("STP4600U03", dict(group_id="ACCOUNT_STATUS", code="FROZEN", name="동결"), "없는 코드예요"),
        ("STP4600U03", dict(group_id="NOPE", code="X", name="이름"), "없는 코드 그룹"),
        ("STP4600U03", dict(group_id="ACCOUNT_STATUS", code="OPEN"), "name 값을 넣어"),
        ("STP4600U01", dict(group_id="memo kind", name="메모 분류"), "코드 그룹 ID는"),
        ("STP4600U03", dict(group_id="accounts_chart", code="1010", name="현금"), "계정과목은"),   # 전용 테이블
        ("STP4600U02", dict(group_id="accounts_chart", code="9999", name="새 계정"), "계정과목은"),
    ]:
        status, said = press(tx, **params)
        assert status == 400 and problem in said, (tx, params, said)
    assert conn.execute("select count(*), max(updated_at) from common_codes").fetchone() == before
    assert conn.execute("select name from accounts_chart where code = '1010'").fetchone()[0] == "예수금"
    assert conn.execute("select count(*) from code_groups").fetchone()[0] == 35


def test_account_buttons_are_the_customer_and_account_commands(press, conn):
    assert press("STP1400U01", customer_id="C0002", alias="두 번째 고객") == (200, "고객을 등록했어요: C0002 두 번째 고객")
    status, said = press("STP4400U01", account_id="A0002", customer_id="C0002", alias="연금 계좌", credential_ref="KIS_2")
    assert status == 200 and "환경변수 MINISTP_KIS_2_*" in said and "개설일 2026-11-20" in said   # opened on the screen's day
    a = accounts.get_account(conn, "A0002")
    assert (a.customer_id, a.alias, a.env, a.status, a.opened_on) == ("C0002", "연금 계좌", "PAPER", "OPEN", TODAY)
    assert [x["account_id"] for x in page_data(press.client)["accounts"]] == ["A0001", "A0002"]   # the top bar picks it up

    status, said = press("STP4400U02", account_id="A0002", status="SUSPENDED", reason="고객 요청")   # no date: today
    assert status == 200 and "2026-11-20부터" in said
    assert press("STP4400U02", account_id="A0002", status="CLOSED", effective_from="2026-11-21", reason="해지 요청")[0] == 200
    assert conn.execute("select status, effective_from, reason from account_status_history where account_id = 'A0002'"
                        " order by id").fetchall() == [("OPEN", TODAY, "계좌 등록"), ("SUSPENDED", TODAY, "고객 요청"),
                                                       ("CLOSED", dt.date(2026, 11, 21), "해지 요청")]
    assert page_data(press.client)["accounts"][1]["status"] == "CLOSED"


def test_account_buttons_refuse_what_the_commands_refuse_and_write_nothing(press, conn):
    def rows():
        return conn.execute("select (select count(*) from customers), (select count(*) from trading_accounts),"
                            " (select count(*) from account_status_history)").fetchone()
    before = rows()
    kis = conn.execute("select credential_ref from trading_accounts where account_id = 'A0001'").fetchone()[0]
    for tx, params, problem in [
        ("STP1400U01", dict(customer_id="C0001", alias="또"), "이미 있는 고객"),
        ("STP1400U01", dict(customer_id="고객2", alias="별칭"), "고객 ID는"),
        ("STP4400U01", dict(account_id="A0002", customer_id="C0009", alias="계좌", credential_ref="KIS_2"),
         "등록되지 않은 고객"),
        ("STP4400U01", dict(account_id="A0002", customer_id="C0001", alias="계좌", credential_ref=kis), "쓰고 있어요"),
        ("STP4400U01", dict(account_id="A0002", customer_id="C0001", alias="계좌", credential_ref="12345678-01"),
         "계좌번호나 키를 넣는 자리가 아니에요"),
        ("STP4400U01", dict(account_id="A0002", customer_id="C0001", alias="계좌"), "credential_ref 값을 넣어"),
        ("STP4400U02", dict(account_id="A0001", status="FROZEN", reason="동결"), "상태는"),
        ("STP4400U02", dict(account_id="A0001", status="OPEN", reason="다시"), "이미 OPEN"),
        ("STP4400U02", dict(account_id="A0009", status="SUSPENDED", reason="없는 계좌"), "등록되지 않은 계좌"),
        ("STP4400U02", dict(account_id="A0001", status="SUSPENDED", effective_from="어제", reason="날짜"), "올바르지 않아요"),
    ]:
        status, said = press(tx, **params)
        assert status == 400 and problem in said, (tx, params, said)
    assert rows() == before


def test_management_screens_put_their_buttons_on_the_grid_they_act_on(conn):
    catalog = erd.read_catalog(conn, "public")
    screens, lookups = load_screens(), load_lookups()

    def buttons(sid):
        return {g["id"]: [(b["label"], b["tx"], b["needs_row"]) for b in g["actions"]]
                for g in resolve(screens[sid], catalog, lookups)["grids"] if g["actions"]}

    assert buttons("STP-4600") == {
        "groups": [("그룹 추가", "STP4600U01", False)],
        "codes": [("코드 추가", "STP4600U02", False), ("코드명 변경", "STP4600U03", True), ("사용 여부", "STP4600U04", True)]}
    assert buttons("STP-4400") == {"accounts": [("계좌 등록", "STP4400U01", False), ("상태 변경", "STP4400U02", True)]}
    assert buttons("STP-1400") == {"customers": [("고객 등록", "STP1400U01", False)]}

    r = resolve(screens["STP-4400"], catalog, lookups)
    status = next(f for b in r["grids"][0]["actions"] if b["id"] == "status" for f in b["fields"] if f["name"] == "status")
    assert (status["label"], status["lookup"], status["lookup_params"]) == ("상태", "code", {"group_id": "ACCOUNT_STATUS"})
    assert set(r["lookups"]) == {"code", "customer"} and r["lookups"]["code"]["shows"] == "name"
    results = next(g for g in resolve(screens["STP-2100"], catalog, lookups)["grids"] if g["id"] == "results")
    cause = results["actions"][0]["fields"][1]
    assert (cause["lookup"], cause["lookup_params"]) == ("code", {"group_id": "RECON_CAUSE"})   # 공통코드 팝업
    for sid in ("STP-4400", "STP-1400", "STP-4600"):                 # nothing secret is ever asked for
        fields = [f["name"] for g in resolve(screens[sid], catalog, lookups)["grids"] for b in g["actions"] for f in b["fields"]]
        assert not {"app_key", "app_secret", "account_no", "password"} & set(fields)


def test_the_page_carries_code_names_and_a_renamed_code_shows_on_the_next_open(press, conn):
    """코드명 표시: screens draw a code by its name. The names come from the table, not from the program."""
    data = page_data(press.client, "STP-3100")
    assert data["codes"]["EVENT_TYPE"]["ExecutionEvent"] == "체결" and data["codes"]["RECON_ITEM"]["CASH_D0"] == "예수금"
    assert data["codes"] == {g: codes.names(conn, g) for g in data["codes"]}
    assert set(data["codes"]) == {g.group_id for g in codes.SEED}
    cols = {c["name"]: c for c in next(g for g in data["screen"]["grids"] if g["id"] == "events")["columns"]}
    assert cols["event_type"]["codes"] == "EVENT_TYPE" and cols["symbol"]["codes"] is None
    assert next(c for c in data["screen"]["conditions"] if c["name"] == "event_type")["codes"] == "EVENT_TYPE"
    assert press("STP4600U03", group_id="EVENT_TYPE", code="ExecutionEvent", name="매매 체결")[0] == 200
    assert page_data(press.client, "STP-3100")["codes"]["EVENT_TYPE"]["ExecutionEvent"] == "매매 체결"
    # a code switched off keeps its name: rows recorded with it are still read
    assert press("STP4600U01", group_id="MEMO_KIND", name="메모 종류")[0] == 200
    assert press("STP4600U02", group_id="MEMO_KIND", code="CALL", name="통화")[0] == 200
    assert press("STP4600U04", group_id="MEMO_KIND", code="CALL", in_use="사용 안 함")[0] == 200
    assert page_data(press.client, "STP-3100")["codes"]["MEMO_KIND"] == {"CALL": "통화"}
