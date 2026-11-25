"""STP-2400 마감 점검 on a sample database: the screen's transactions and the API."""
import datetime as dt
import os
import re

import pytest

from ministp import queries as q
from ministp.ops import OpsError
from ministp.sample import build_sample
from ministp.ui import app as ui_app
from ministp.ui.transactions import TxContext, TxError, run_tx

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from db.test_integrity import damaged  # noqa: E402
from db.test_ops import CAL, FEES  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
STEPS = ["결제", "주문·체결", "배당", "시세", "기준가", "증권사 대사", "일 마감", "ERP 전송", "원장 내부 대사"]


@pytest.fixture(scope="module")
def sample():
    with psycopg.connect(URL, autocommit=True) as c:
        return build_sample(c, CAL, FEES, days=8, seed=5)


@pytest.fixture
def conn(sample):
    with psycopg.connect(URL, autocommit=True) as c:
        yield c


def tx(conn, today, tx_id, **params):
    return run_tx(tx_id, TxContext(conn, CAL, FEES, today), params)


def test_every_day_of_the_sample_is_finished_and_each_step_agrees_with_its_own_screen(conn, sample):
    res = tx(conn, sample.last_day, "STP2400R01")
    days = res.datasets["main"].records()
    assert len(days) == 8 and days[0]["date"] == sample.last_day and res.meta == {"attention_days": 0}
    assert all((r["done"], r["steps"], r["attention"], r["state"]) == (8, 8, 0, "점검 완료") for r in days)
    recon = {r["date"]: r for r in q.recon_calendar(conn, CAL, sample.last_day, sample.first_day).records()}
    nav = {r["date"]: r["net_assets"] for r in tx(conn, sample.last_day, "STP2200R01").datasets["main"].records()}
    for r in days:
        steps = tx(conn, sample.last_day, "STP2400R02", date=r["date"]).datasets["main"].records()
        assert [s["step"] for s in steps] == STEPS and [s["no"] for s in steps] == list(range(1, 10))
        assert all(s["state"] in ("완료", "대상 없음") and s["date"] == r["date"] for s in steps if s["kind"] == "필수")
        assert [s["step"] for s in steps if s["kind"] == "선택"] == ["ERP 전송"] and steps[7]["state"] == "대기"
        steps = [s for s in steps if s["step"] not in ("배당", "ERP 전송")]
        assert f"순자산 {nav[r['date']]:,}원" in steps[3]["detail"]
        assert recon[r["date"]]["state"] in ("일치", "판단 완료")      # the same reading as the 대사 달력
        assert f"대사 {recon[r['date']]['items']}개 항목" in steps[4]["detail"]
        assert ("판단을 마쳤어요" in steps[4]["detail"]) == (recon[r["date"]]["state"] == "판단 완료")


def test_the_next_business_day_is_in_progress_until_daily_runs(conn, sample):
    tomorrow = CAL.add_business_days(sample.last_day, 1)
    days = tx(conn, tomorrow, "STP2400R01").datasets["main"].records()
    assert (days[0]["date"], days[0]["state"], days[0]["attention"]) == (tomorrow, "진행 중", 0)
    assert days[1]["state"] == "점검 완료"
    steps = {s["step"]: s["state"] for s in tx(conn, tomorrow, "STP2400R02", date=tomorrow).datasets["main"].records()}
    assert [steps[n] for n in ("시세", "기준가", "증권사 대사", "일 마감", "원장 내부 대사")] == ["대기"] * 4 + ["완료"]
    # two days on, the day nobody closed is missing and the list says how many days need a look
    later = CAL.add_business_days(tomorrow, 1)
    res = tx(conn, later, "STP2400R01")
    assert res.meta == {"attention_days": 1} and res.datasets["main"].records()[1]["state"] == "확인 필요"
    only = tx(conn, later, "STP2400R01", date_from=tomorrow, date_to=tomorrow).datasets["main"].records()
    assert [r["date"] for r in only] == [tomorrow]


def test_the_board_opens_on_books_that_fail_verification_and_says_so_on_every_day(conn, sample):
    with damaged(conn, "alter table cash_ledger disable trigger cash_ledger_append_only",
                 "update cash_ledger set amount = amount + 7 where id = (select min(id) from cash_ledger)"):
        with pytest.raises(OpsError):
            tx(conn, sample.last_day, "STP1100R02")
        res = tx(conn, sample.last_day, "STP2400R01")
        assert res.meta == {"attention_days": 8}
        last = tx(conn, sample.last_day, "STP2400R02", date=sample.last_day).datasets["main"].records()[-1]
        assert (last["step"], last["state"], last["screen"]) == ("원장 내부 대사", "확인 필요", "STP-2110")
        assert re.search(r"점검 \d+개 중 2개가 맞지 않아요", last["detail"])
    assert tx(conn, sample.last_day, "STP2400R01").meta == {"attention_days": 0}


def test_events_that_cannot_be_replayed_leave_one_row_that_points_at_the_integrity_screen(conn, sample):
    with damaged(conn, "insert into ledger_events (seq, event_type, payload, account_id) select max(seq) + 1, 'ExecutionEvent',"
                       " '{\"exec_id\": \"X-1\", \"trade_date\": \"2026-11-02\", \"symbol\": \"NOPE\","
                       " \"side\": \"BUY\", \"qty\": 1, \"price\": 100}', 'A0001' from ledger_events"):
        days = tx(conn, sample.last_day, "STP2400R01").datasets["main"].records()
        assert [(r["date"], r["steps"], r["state"]) for r in days] == [(sample.last_day, 1, "확인 필요")]
        [step] = tx(conn, sample.last_day, "STP2400R02", date=sample.last_day).datasets["main"].records()
        assert (step["step"], step["state"], step["screen"]) == ("원장 내부 대사", "확인 필요", "STP-2110")


def test_a_day_that_is_not_on_the_board_is_refused(conn, sample):
    with pytest.raises(TxError, match="영업일이 아니거나"):
        tx(conn, sample.last_day, "STP2400R02", date="2020-01-01")
    with pytest.raises(TxError, match="영업일이 필요"):
        tx(conn, sample.last_day, "STP2400R02")
    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def test_the_api_serves_the_screen_and_tells_the_page_where_each_step_leads(sample):
    client = TestClient(ui_app.create_app(URL, calendar=CAL, fees=FEES, today=sample.last_day,
                                          allowed_hosts=["testserver"]))
    page = client.get("/screen/STP-2400")
    assert page.status_code == 200 and '"by": "screen"' in page.text.replace('"by":"screen"', '"by": "screen"')
    body = client.post("/api/tx/STP2400R01", json={"params": {}}).json()
    assert body["meta"] == {"attention_days": 0} and len(body["datasets"]["main"]["rows"]) == 8
    assert client.post("/api/tx/STP2400R02", json={"params": {"date": "2020-01-01"}}).status_code == 400


# ---------- 단계 기준과 점검 이력 (R-105, ERD v2.12) ----------

def test_steps_are_changed_in_the_database_and_never_removed(conn, sample, capsys):
    from ministp import __main__ as cli
    from ministp import audit
    env = {"MINISTP_DATABASE_URL": URL}
    try:
        assert cli.run(["close-step", "set", "ERP_EXPORT", "--required", "--name", "ERP 업로드"], environ=env) == 0
        capsys.readouterr()
        steps = tx(conn, sample.last_day, "STP2400R02", date=sample.last_day).datasets["main"].records()
        erp_row = next(s for s in steps if s["step"] == "ERP 업로드")
        assert erp_row["kind"] == "필수" and erp_row["state"] == "대기"
        assert tx(conn, sample.last_day, "STP2400R01").datasets["main"].records()[0]["state"] == "진행 중"
        row = audit.rows(conn, limit=1)[0]
        assert (row.action, row.target, row.detail["required"]) == ("CLOSE_STEP_SET", "ERP_EXPORT", [False, True])
        assert cli.run(["close-step", "set", "PRICES", "--no-use"], environ=env) == 0
        names = [s["step"] for s in tx(conn, sample.last_day, "STP2400R02", date=sample.last_day).datasets["main"].records()]
        assert "시세" not in names and len(names) == 8
        for bad, said in [(["NOPE", "--name", "x"], "NOPE가 없어요"), (["NAV", "--screen", "STP-9999"], "열 수 있는 화면"),
                          (["NAV", "--asset-type", "BOND"], "BOND"), (["NAV"], "바뀐 값이 없어요")]:
            capsys.readouterr()
            assert cli.run(["close-step", "set", *bad], environ=env) == 1 and said in capsys.readouterr().out
        for change in ("delete from close_steps where step_code = 'NAV'",
                       "update close_steps set step_code = 'NAV2' where step_code = 'NAV'", "truncate close_steps cascade"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(change)
        assert cli.run(["code", "check"], environ=env) == 0
    finally:
        conn.execute("update close_steps set required = false, name = 'ERP 전송' where step_code = 'ERP_EXPORT'")
        conn.execute("update close_steps set in_use = true where step_code = 'PRICES'")
    assert cli.run(["close-step", "list"], environ=env) == 0 and "ERP_EXPORT" in capsys.readouterr().out


def test_a_check_is_kept_with_what_each_step_found_at_the_time(conn, sample, capsys):
    from ministp import __main__ as cli
    from ministp import closeboard as cb
    env = {"MINISTP_DATABASE_URL": URL}
    before = conn.execute("select count(*) from close_checks").fetchone()[0]
    assert cli.run(["close-check", "--date", sample.last_day.isoformat()], environ=env,
                   now=lambda: dt.datetime.combine(sample.last_day, dt.time(18))) == 0
    out = capsys.readouterr().out
    assert "점검 완료: 필수 단계 8개 중 8개 끝남" in out and "ERP 전송 (선택): 대기" in out
    check = conn.execute("select check_id, account_id, check_date, day_state, done, steps, command from close_checks"
                         " order by check_id desc limit 1").fetchone()
    assert check[1:] == ("A0001", sample.last_day, "DONE", 8, 8, "close-check")
    rows = conn.execute("select step_no, step_code, required, state from close_check_steps where check_id = %s"
                        " order by step_no", (check[0],)).fetchall()
    assert [r[1] for r in rows] == list(cb.STEP_CODES) and rows[7][2:] == (False, "WAIT")
    assert conn.execute("select count(*) from close_checks").fetchone()[0] == before + 1
    last = conn.execute("select check_id from close_checks_last_v where account_id = 'A0001' and check_date = %s",
                        (sample.last_day,)).fetchone()[0]
    assert last == check[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("update close_check_steps set state = 'DONE'")
    assert cli.run(["close-check", "--date", "2020-01-01"], environ=env) == 1
