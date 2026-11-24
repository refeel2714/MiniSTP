"""주문 결과 미확인 (R-103): 증권사가 거부한 줄과 답이 없는 줄을 나눠요.

거부는 그 줄만 실패이고 다음 줄은 나가요. 답이 없으면 다시 보내지 않고, 그 주문을 증권사 주문내역에서 찾거나 사람이
확인할 때까지 이 계좌는 아무것도 보내지 않아요. 프로세스를 다시 띄워도 같은 상태에서 이어져요.
"""
import pytest

from db.test_approval import BUY, DAY, NOW, WIDE, as_, status, team  # noqa: F401
from db.test_ops import CAL, URL, books, broker, conn  # noqa: F401
from ministp import __main__ as cli
from ministp import approval, audit
from ministp.approval import Line
from ministp.broker import OrderOutcomeUnknown, OrderRejected
from ministp.ops import OpsError, confirm_order, session_for, stored_open_orders

psycopg = pytest.importorskip("psycopg")


def flaky(broker, *outcomes):
    """The next place_order calls answer as told: ok, reject (the broker says no), lost (placed, answer lost),
    unreachable (nothing placed, no answer). Then back to normal."""
    real, queue = broker.place_order, list(outcomes)

    def place(symbol, side, qty, limit_price):
        what = queue.pop(0) if queue else "ok"
        if what == "reject":
            raise OrderRejected("APBK0919 주문가능금액 초과", "APBK0919")
        if what == "unreachable":
            raise OrderOutcomeUnknown("timed out")
        ack = real(symbol, side, qty, limit_price)
        if what == "lost":
            raise OrderOutcomeUnknown("timed out")
        return ack
    broker.place_order = place
    return broker


def basket(team, qtys):
    with as_("kim"):
        (key,), _ = approval.submit(books(team), DAY, DAY, [Line("069500", BUY, q, 80_000) for q in qtys],
                                    basket=True, self_approve=True, weight_limit=WIDE, now=NOW)
    return key


def send(team, broker, session=None):
    session = session or session_for(books(team), broker, DAY)
    return session, approval.send_approved(books(team), session, DAY, weight_limit=WIDE, now=NOW)


def test_a_line_the_broker_refuses_fails_alone_and_the_rest_of_the_basket_goes(team, broker):
    key = basket(team, [1, 2, 3])
    flaky(broker, "ok", "reject")
    _, sent = send(team, broker)
    assert [(s.line_no, bool(s.client_id), s.unknown) for s in sent] == [(1, True, False), (2, False, False),
                                                                         (3, True, False)]
    assert "APBK0919" in sent[1].detail
    r = status(team, key)
    assert r.status == "SENT" and [ln.ack_outcome for ln in r.lines] == ["ACCEPTED", "REJECTED", "ACCEPTED"]
    row = team.execute("select accepted_lines, rejected_lines, unknown_lines, last_reason from order_requests_v"
                       " where request_no = %s", (key.request_no,)).fetchone()
    assert row[:3] == (2, 1, 0) and "APBK0919" in row[3]
    assert [c[3] for c in broker.calls if c[0] == "order"] == [1, 3]     # 거부된 줄을 다시 보내지 않아요


def test_every_line_refused_is_a_failed_send(team, broker):
    key = basket(team, [1, 2])
    flaky(broker, "reject", "reject")
    send(team, broker)
    assert status(team, key).status == "SEND_FAILED"


def test_no_answer_stops_sending_until_the_order_is_found_in_the_brokers_list(team, broker):
    key = basket(team, [1, 2, 3])
    flaky(broker, "ok", "lost")
    session, sent = send(team, broker)
    assert [(s.line_no, s.unknown) for s in sent] == [(1, False), (2, True)]     # 3번째 줄은 멈춰요
    assert session.unacked == [sent[1].client_id]
    r = status(team, key)
    assert r.status == "SEND_UNKNOWN" and r.lines[1].awaiting_ack and r.lines[2].send_outcome is None
    # 답이 없는 주문도 현금을 잡고 있어요(나갔을 수 있으니까)
    assert sorted(o.remaining_qty for o in stored_open_orders(books(team), DAY)) == [1, 2]
    # 다시 띄워도 그대로 멈춰 있고, 같은 줄을 다시 보내지 않아요
    again = session_for(books(team), broker, DAY)
    assert again.unacked == [sent[1].client_id]
    assert approval.send_approved(books(team), again, DAY, weight_limit=WIDE, now=NOW) == []
    assert len([c for c in broker.calls if c[0] == "order"]) == 2
    # 조회가 증권사 주문내역에서 찾아요: 종목·매매·수량이 맞는 주문이 하나뿐이라서
    again.sync(DAY)
    assert again.unacked == [] and any(a.kind == "UNACKED_FOUND" for a in again.anomalies)
    ack = team.execute("select outcome, order_no, detail from oms_order_acks where client_id = %s",
                       (sent[1].client_id,)).fetchone()
    assert ack == ("ACCEPTED", "0000000002", "증권사 주문내역에서 찾음")
    _, rest = send(team, broker, again)
    assert [(s.line_no, bool(s.client_id)) for s in rest] == [(3, True)]
    r = status(team, key)
    assert r.status == "SENT" and [ln.ack_outcome for ln in r.lines] == ["ACCEPTED"] * 3


def test_an_order_that_never_arrived_is_confirmed_by_a_person_and_then_the_rest_goes(team, broker):
    key = basket(team, [1, 2])
    flaky(broker, "unreachable")
    session, sent = send(team, broker)
    cid = sent[0].client_id
    assert sent[0].unknown and session.unacked == [cid]
    session.sync(DAY)
    assert session.unacked == [cid]                     # 증권사에 없으니 찾지 못해요
    with as_("kim"):
        with pytest.raises(OpsError, match="오늘 증권사 주문내역에 9999"):
            confirm_order(books(team), broker, DAY, cid, "9999")
        session, done, how = confirm_order(books(team), broker, DAY, cid, None)
    assert (done, how) == (cid, "not_placed") and session.unacked == []
    row = audit.rows(team, limit=1, action="ORDER_CONFIRM")[0]
    assert (row.outcome, row.target, row.actor) == ("FAILED", cid, "kim")
    _, rest = send(team, broker, session)
    assert [(s.line_no, bool(s.client_id)) for s in rest] == [(2, True)]
    r = status(team, key)
    assert r.status == "SENT" and [ln.ack_outcome for ln in r.lines] == ["REJECTED", "ACCEPTED"]
    assert "사람이 확인" in r.lines[0].ack_detail


def test_look_alike_orders_are_never_guessed_between(team, broker):
    with as_("kim"):
        keys = [approval.submit(books(team), DAY, DAY, [Line("069500", BUY, 1, 80_000)], self_approve=True,
                                weight_limit=WIDE, now=NOW)[0][0] for _ in range(2)]
    flaky(broker, "lost")
    session, sent = send(team, broker)
    assert [s.unknown for s in sent] == [True]          # 첫 줄에서 멈춰요
    lost = sent[0].client_id
    flaky(broker)                                       # 같은 모양의 주문이 밖에서도 하나 나갔다면
    broker.place_order("069500", "BUY", 1, 80_000)
    session.sync(DAY)
    assert session.unacked == [lost]                    # 둘 중 무엇인지 모르니 고르지 않아요
    with as_("kim"):
        with pytest.raises(OpsError, match="같은 모양의 주문"):
            confirm_order(books(team), broker, DAY, lost, None)
        with pytest.raises(OpsError, match="없어요"):
            confirm_order(books(team), broker, DAY, lost, "0000000099")
        session, _, how = confirm_order(books(team), broker, DAY, lost, "0000000001")   # HTS에서 본 번호
    assert how == "confirmed" and session.unacked == [] and status(team, keys[0]).status == "SENT"
    assert status(team, keys[0]).lines[0].order_no == "0000000001"
    assert any(a.kind == "UNKNOWN_ORDER" and a.ref == "0000000002" for a in session.anomalies)   # 밖에서 낸 것


def test_an_unanswered_order_cannot_be_canceled_and_the_confirm_command_settles_it(team, broker, capsys):
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01"}
    kw = dict(environ=env, client_factory=lambda cfg: broker, sleep=lambda s: None, now=lambda: NOW)
    first = basket(team, [1])
    flaky(broker, "unreachable")
    session, sent = send(team, broker)
    from ministp.oms import InvalidTransition
    with pytest.raises(InvalidTransition, match="접수 결과를 아직 몰라서"):
        session.cancel(sent[0].client_id)
    assert cli.run(["confirm", "1", "--not-placed"], **kw) == 0
    assert "증권사에 없는 주문으로 닫았어요" in capsys.readouterr().out
    row = audit.rows(team, limit=1, action="ORDER_CONFIRM")[0]
    assert (row.outcome, row.target, row.channel) == ("FAILED", sent[0].client_id, "CLI")
    assert status(team, first).status == "SEND_FAILED"
    # 답만 잃어버린 주문은 confirm이 먼저 조회해서 찾아요
    second = basket(team, [2])
    flaky(broker, "lost")
    _, sent = send(team, broker)
    assert cli.run(["confirm", sent[0].client_id, "--order-no", "0000000001"], **kw) == 0
    assert "조회로 이미 찾았어요" in capsys.readouterr().out
    assert status(team, second).status == "SENT"
    assert cli.run(["confirm", sent[0].client_id, "--not-placed"], **kw) == 1     # 이미 찾은 주문
    assert "결과를 기다리는 주문이 아니에요" in capsys.readouterr().out
