"""STP-1200 블로터와 결재 요청 (C-21): 주문마다 요청 번호, 고른 주문의 요청 전체와 체결률, 취소 요청."""
import pytest

from db.test_approval import DAY, status, team  # noqa: F401
from db.test_ops import books, broker, conn  # noqa: F401
from db.test_send_unknown import basket, flaky, send
from ministp import queries as q

psycopg = pytest.importorskip("psycopg")


def test_the_blotter_names_the_request_and_shows_the_whole_basket_of_a_picked_order(team, broker):
    key = basket(team, [1, 2, 3])
    flaky(broker, "ok", "ok", "reject")
    session, sent = send(team, broker)
    session.sync(DAY)
    b = books(team)
    rows = {r["request"]: r for r in q.blotter_view(q.order_blotter(team, DAY), b.ledger, DAY).records()}
    assert set(rows) == {f"{key.request_no}-1", f"{key.request_no}-2", f"{key.request_no}-3"}
    assert rows[f"{key.request_no}-3"]["stage"] == "접수 거부"
    assert [rows[f"{key.request_no}-{n}"]["stage"] for n in (1, 2)] == ["취소 요청"] * 2   # 바스켓 중단 (R-103)
    r = q.order_request(team, b.ledger, sent[1].client_id, DAY)
    head = r.request.records()[0]
    assert (head["request_no"], head["order_type"], head["line_count"]) == (key.request_no, "BASKET", 3)
    assert head["amount"] == 6 * 80_000 and head["filled_amount"] == 0 and str(head["fill_rate"]) == "0.00"
    lines = r.lines.records()
    assert [(x["line_no"], x["broker_answer"], x["filled_qty"]) for x in lines] == [(1, "접수", 0), (2, "접수", 0),
                                                                                (3, "거부", 0)]
    assert "APBK0919" in lines[2]["reason"]
    assert q.order_request(team, b.ledger, "S-없음", DAY).request.rows == ()


def test_a_single_request_is_named_by_its_number_alone():
    assert q.request_label({"request_no": 4, "line_no": 1, "order_type": "SINGLE"}) == "4"
    assert q.request_label({"request_no": 4, "line_no": 2, "order_type": "BASKET"}) == "4-2"
    assert q.request_label({"request_no": None}) is None
