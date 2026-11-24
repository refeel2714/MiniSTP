"""주문 결재 화면 (STP-1210 주식 주문, STP-5100 주문 결재, STP-5200 결재 권한; R-98~101).

화면은 읽기만 하고, 줄마다 단건·묶음 결과와 버튼이 잠기는 이유를 같은 계산에서 보여 줘요. 버튼은 CLI와 같은 결재 함수를
부르고, 잠겨 있던 버튼을 억지로 눌러도 서버가 같은 검사로 거부해요.
"""
import json
import os
import re

import pytest

from ministp import approval, audit
from ministp.sample import SAMPLE_USERS, build_sample
from ministp.store import DEFAULT_ACCOUNT
from ministp.ui import app as ui_app
from ministp.ui.definitions import load_screens, split_sides
from ministp.ui.transactions import TxContext, TxError, parse_lines, run_tx

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
ME = audit.os_user()


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=8, seed=5)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


@pytest.fixture
def ctx(conn, sample):
    with audit.acting_as("UI"):      # 화면 서버처럼: 읽는 사람도 PC 로그인 이름
        yield TxContext(conn, CAL, FEES, sample.last_day)


def writer(sample) -> TestClient:
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"], write_dsn=URL))
    data = json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                client.get("/screen/STP-1210").text, re.S).group(1))
    client.headers["X-STPUI-Token"] = data["token"]
    return client


def rows(res, name="main") -> list[dict]:
    return res.datasets[name].records()


def cash_and_price(ctx) -> tuple[int, str, int]:
    state = approval.account_state(ctx.books, ctx.today)
    sym = sorted(state.prices)[0]
    return state.available_cash, sym, state.prices[sym]


def test_the_sample_has_users_rights_and_requests_to_show(conn, sample):
    users = dict(conn.execute("select user_id, name from app_users").fetchall())
    assert {u for u, _, _ in SAMPLE_USERS} | {ME} == set(users)
    statuses = [r.status for r in approval.requests(conn, account_id=DEFAULT_ACCOUNT)]
    assert sorted(statuses) == ["APPROVED", "PENDING", "PENDING", "REJECTED"]
    made = approval.requests(conn, account_id=DEFAULT_ACCOUNT)
    # 요청 번호는 주문일마다 1부터인 순번이고, 바스켓은 종목이 둘이어도 요청 1건이에요
    assert [(str(r.key), r.order_type, len(r.lines)) for r in made] == \
        [(f"{sample.last_day:%Y%m%d}-{n}", t, k) for n, t, k in
         ((1, "SINGLE", 1), (2, "SINGLE", 1), (3, "SINGLE", 1), (4, "BASKET", 2))]


def test_order_lines_are_parsed_and_checked_strictly():
    lines = parse_lines("N1:005930:buy:3:61200,N2:069500:SELL:1::BK")
    assert [(d.key, d.line.symbol, d.line.side.value, d.line.qty, d.line.limit_price, d.group) for d in lines] == \
        [("N1", "005930", "BUY", 3, 61200, ""), ("N2", "069500", "SELL", 1, None, "BK")]
    for bad in ("N1:005930:HOLD:3:1", "N1:005930:BUY:0:1", "N1:005930:BUY:x:1", "N1:005930:BUY:1:1:ZZ",
                "N1:005930:BUY:1:1,N1:069500:BUY:1:1"):
        with pytest.raises(TxError):
            parse_lines(bad)


def test_each_line_alone_passes_but_the_two_together_sell_more_than_is_held(ctx):
    state = approval.account_state(ctx.books, ctx.today)
    sym, held = next((s, q) for s, q in sorted(state.held_qty.items()) if q >= 2)
    price = state.prices[sym]
    each = held // 2 + 1               # 한 줄씩은 보유 안이고, 두 줄을 더하면 보유보다 많아요
    lines = f"N1:{sym}:SELL:{each}:{price},N2:{sym}:SELL:{each}:{price}"
    res = run_tx("STP1210R01", ctx, {"lines": lines, "picked": "N1,N2"})
    drafts = [r for r in rows(res) if r["pickable"]]
    assert [(r["key"], r["status"]) for r in drafts] == [("N1", "작성 중"), ("N2", "작성 중")]
    assert [r["alone"] for r in drafts] == ["통과", "통과"]
    assert drafts[0]["together"] == "통과" and drafts[1]["together"] == "매도가능 초과"
    assert res.meta["request_lock"].startswith("묶음 검증에 걸려요") and res.meta["send_lock"]
    one = run_tx("STP1210R01", ctx, {"lines": lines, "picked": "N1"})
    assert one.meta["request_lock"] == "" and one.meta["send_lock"] == ""
    assert [r["together"] for r in rows(one) if r["pickable"]] == ["통과", "선택 안 함"]
    summary = {r["item"]: r for r in rows(run_tx("STP1210R02", ctx, {"lines": lines, "picked": "N1,N2"}))}
    assert summary["매도가능수량"]["result"] == "초과" and summary["매도가능수량"]["ok"] is False
    assert summary["예수금"]["result"] == "통과"
    verdict = run_tx("STP1210R02", ctx, {"lines": lines, "picked": "N1,N2"}).meta["verdict"]
    assert verdict.startswith("묶음 검증 불합격: 2번째 줄")
    assert rows(run_tx("STP1210R02", ctx, {"lines": lines}))[0]["result"] == "선택 안 함"


def test_the_requests_already_made_take_their_share_and_cannot_be_checked_again(ctx):
    res = run_tx("STP1210R01", ctx, {})
    sent = rows(res)
    assert sent and all(not r["pickable"] and r["together"] == "보낸 요청" for r in sent)
    assert res.meta["request_lock"] == "결재 요청할 줄을 체크하세요"


def test_buttons_lock_with_the_reason_for_someone_without_the_right(ctx):
    _, sym, price = cash_and_price(ctx)
    lines = f"N1:{sym}:BUY:1:{price}"
    with audit.acting_as("UI", "kim.trader"):       # 작성만 있는 사람: 결재 요청은 되고 승인하고 전송은 잠겨요
        res = run_tx("STP1210R01", ctx, {"lines": lines, "picked": "N1"})
    assert res.meta["request_lock"] == "" and "주문 승인 권한이 없어요" in res.meta["send_lock"]
    with audit.acting_as("UI", "nobody"):
        res = run_tx("STP1210R01", ctx, {"lines": lines, "picked": "N1"})
    assert "주문 작성 권한이 없어요" in res.meta["request_lock"] == res.meta["send_lock"]
    with audit.acting_as("UI", "kim.trader"):
        pending = run_tx("STP5100R01", ctx, {"status": "PENDING"})
        some = next(r["request_key"] for r in rows(pending) if r["order_type"] == "SINGLE")
        res = run_tx("STP5100R01", ctx, {"status": "PENDING", "picked": some})
    assert "주문 승인 권한이 없어요" in res.meta["approve_lock"] and res.meta["cancel_lock"] == ""
    with audit.acting_as("UI", "lee.fin"):          # 남의 요청은 취소할 수 없어요
        res = run_tx("STP5100R01", ctx, {"status": "PENDING", "picked": some})
    assert res.meta["approve_lock"] == "" and "작성자만" in res.meta["cancel_lock"]


def test_approving_checks_the_set_and_the_limit_of_the_approver(ctx):
    pending = rows(run_tx("STP5100R01", ctx, {"status": "PENDING"}))
    basket = [r for r in pending if r["order_type"] == "BASKET"]
    assert len(basket) == 1 and all(r["together"] == "선택 안 함" for r in pending)
    bk = basket[0]                     # 바스켓은 한 행: 줄 수와 "외 N종목", 금액은 줄 합계
    assert bk["line_count"] == 2 and bk["instrument_name"].endswith("외 1종목") and bk["request_no"] == 4
    lines = rows(run_tx("STP5100R04", ctx, {"request_key": bk["request_key"]}))
    assert [r["line_no"] for r in lines] == [1, 2] and bk["amount"] == sum(r["amount"] for r in lines)
    assert all(r["send_outcome"] is None for r in lines)
    res = run_tx("STP5100R01", ctx, {"status": "PENDING", "picked": bk["request_key"]})
    assert [r["together"] for r in rows(res) if r["order_type"] == "BASKET"] == ["통과"]
    with audit.acting_as("UI", "lee.fin"):
        mine = run_tx("STP5100R02", ctx, {"status": "PENDING", "picked": bk["request_key"]})
    limit_row = rows(mine)[-1]
    assert limit_row["item"] == "내 승인 한도" and "lee.fin" in limit_row["detail"] and limit_row["ok"] is True
    nothing = run_tx("STP5100R01", ctx, {"status": "PENDING"})
    assert nothing.meta["approve_lock"] == "결재 대기 요청을 체크하세요"


def test_the_rights_screen_shows_today_and_the_history(ctx):
    users = {r["user_id"]: r for r in rows(run_tx("STP5200R01", ctx, {}))}
    assert users["lee.fin"]["rights"] == "주문 승인 5,000,000원, 셀프 · 주문 작성"
    assert users[ME]["me"] is True
    hist = rows(run_tx("STP5200R02", ctx, {"user_id": "choi.cfo"}))
    assert [(r["right_code"], r["granted"], r["state"]) for r in hist] == \
        [("ORDER_APPROVE", "부여", "적용 중"), ("RIGHTS_ADMIN", "부여", "적용 중")]
    with audit.acting_as("UI", "kim.trader"):
        assert "권한 관리 권한이 없어요" in run_tx("STP5200R01", ctx, {}).meta["admin_lock"]


def test_a_request_from_the_screen_goes_through_the_same_function_and_is_audited(sample, conn, ctx):
    client = writer(sample)
    _, sym, price = cash_and_price(ctx)
    lines = f"N1:{sym}:BUY:1:{price},N2:{sym}:BUY:1:{price}"
    before = len(approval.requests(conn, account_id=DEFAULT_ACCOUNT))
    r = client.post("/api/action/STP1210U01", json={"params": {"lines": lines, "picked": "N2"}})
    assert r.status_code == 200, r.json()
    assert "단건 1건을 결재 요청했어요" in r.json()["message"]
    made = approval.requests(conn, account_id=DEFAULT_ACCOUNT)
    assert len(made) == before + 1
    new = max(made, key=lambda x: x.key)
    assert f"요청 {new.request_no}" in r.json()["message"]
    assert (new.status, new.created_by, new.order_type, new.lines[0].symbol) == ("PENDING", ME, "SINGLE", sym)
    row = audit.rows(conn, limit=1, action="ORDER_REQUEST")[0]
    assert (row.channel, row.actor, row.detail["tx"]) == ("UI", ME, "STP1210U01")
    # 승인과 반려는 체크한 요청으로. 본인 요청이라도 셀프 승인 허용이 있으면 돼요
    r = client.post("/api/action/STP5100U01", json={"params": {"picked": str(new.key)}})
    assert r.status_code == 200, r.json()
    assert approval.requests(conn, keys=[new.key])[0].status == "APPROVED"


def test_a_locked_button_pressed_anyway_is_refused_by_the_same_check(sample, conn, ctx, monkeypatch):
    client = writer(sample)
    basket = next(r for r in approval.requests(conn, account_id=DEFAULT_ACCOUNT, statuses=("PENDING",))
                  if r.created_by == "kim.trader" and r.order_type == "BASKET")
    monkeypatch.setattr(audit, "os_user", lambda: "kim.trader")   # 작성만 있는 사람이 승인을 누르면
    r = client.post("/api/action/STP5100U01", json={"params": {"picked": str(basket.key)}})
    assert r.status_code == 400 and "주문 승인 권한이 없어요" in r.json()["error"]
    refused = audit.rows(conn, limit=1, action="ORDER_APPROVE", outcome="REFUSED")[0]
    assert (refused.channel, refused.actor) == ("UI", "kim.trader")
    assert approval.requests(conn, keys=[basket.key])[0].status == "PENDING"
    r = client.post("/api/action/STP5100U01", json={"params": {"picked": "x-1"}})
    assert r.status_code == 400 and "요청 번호는" in r.json()["error"]


def test_mixed_basket_and_single_lines_are_refused(sample, ctx):
    client = writer(sample)
    _, sym, price = cash_and_price(ctx)
    r = client.post("/api/action/STP1210U01", json={"params": {
        "lines": f"N1:{sym}:BUY:1:{price}:BK,N2:{sym}:BUY:1:{price}", "picked": "N1,N2"}})
    assert r.status_code == 400 and "바스켓 줄끼리" in r.json()["error"]


def test_approval_buttons_do_not_wait_for_the_writer_lock(sample, conn, ctx):
    from ministp.ops import acquire_writer
    client = writer(sample)
    _, sym, price = cash_and_price(ctx)
    with psycopg.connect(URL, autocommit=True) as held:      # run이 떠 있는 것처럼 작성자 잠금을 잡아 둬요
        acquire_writer(held)
        r = client.post("/api/action/STP1210U01", json={"params": {"lines": f"N1:{sym}:BUY:1:{price}", "picked": "N1"}})
        assert r.status_code == 200, r.json()
        r = client.post("/api/action/STP4600U01", json={"params": {"group_id": "MEMO_KIND", "name": "메모"}})
        assert r.status_code == 400 and "run" in r.json()["error"]       # 원장 쪽 처리는 여전히 기다려요


def test_the_screens_sit_in_the_new_area_and_split_as_drawn():
    screens = load_screens()
    left, right, below = split_sides(screens["STP-1210"])
    assert [g.id for g in left] == ["lines"] and [g.id for g in right] == ["set", "history"] and below == []
    left, right, _ = split_sides(screens["STP-5100"])
    assert [g.id for g in left] == ["requests"] and [g.id for g in right] == ["set", "lines", "history"]
    assert screens["STP-1500"].grids[1].handoff["screen"] == "STP-1210"


def test_the_basket_hands_its_lines_to_the_order_screen(ctx):
    plan = run_tx("STP1500R02", ctx, {"amount": 3_000_000, "side": "BUY", "basis": "현재가 지정가"})
    lines = [r["line"] for r in rows(plan) if r["line"]]
    assert lines and all(x.endswith(":BK") for x in lines)
    keyed = ",".join(f"N{i}:{x}" for i, x in enumerate(lines, 1))
    assert {d.group for d in parse_lines(keyed)} == {"BK"}


def test_the_top_bar_names_the_acting_user(sample):
    client = writer(sample)
    data = json.loads(re.search(r'<script id="stpui-data" type="application/json">(.*?)</script>',
                                client.get("/screen/STP-5200").text, re.S).group(1))
    assert data["user"] == {"id": ME, "name": "이 PC 사용자"}
