"""STP-2400 마감 점검 on a sample database: the screen's transactions and the API."""
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
STEPS = ["결제", "주문·체결", "시세", "기준가", "증권사 대사", "일 마감", "원장 내부 대사"]


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
    assert all((r["done"], r["steps"], r["attention"], r["state"]) == (7, 7, 0, "점검 완료") for r in days)
    recon = {r["date"]: r for r in q.recon_calendar(conn, CAL, sample.last_day, sample.first_day).records()}
    nav = {r["date"]: r["net_assets"] for r in tx(conn, sample.last_day, "STP2200R01").datasets["main"].records()}
    for r in days:
        steps = tx(conn, sample.last_day, "STP2400R02", date=r["date"]).datasets["main"].records()
        assert [s["step"] for s in steps] == STEPS and [s["no"] for s in steps] == [1, 2, 3, 4, 5, 6, 7]
        assert all(s["state"] in ("완료", "대상 없음") and s["date"] == r["date"] for s in steps)
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
