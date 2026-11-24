"""주문 결재 (R-99~101): 권한, 요청·승인·반려·취소, 묶음 검증, run의 전송, 만료. DB가 순서를 지켜요."""
import datetime as dt
from decimal import Decimal

import pytest

from db.test_ops import CAL, D, URL, books, broker, conn  # noqa: F401
from ministp import approval, audit
from ministp.approval import ApprovalError, Line
from ministp.ledger import Side
from ministp.ops import open_books, place_order, session_for

psycopg = pytest.importorskip("psycopg")
DAY = D("2026-11-20")
NOW = dt.datetime(2026, 11, 20, 10, 0)
WIDE = Decimal(1000)      # 비중 한도를 사실상 끄고 현금만 볼 때
BUY, SELL = Side.BUY, Side.SELL


def as_(user):
    return audit.acting_as("CLI", user)


@pytest.fixture
def team(conn, broker):
    """kim: 권한 관리 + 작성 + 승인(1건 1천만 원, 셀프 승인 허용), lee: 작성만, park: 작성 + 승인(셀프 승인 없음)."""
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    with as_("kim"):
        approval.add_user(conn, "kim", "김", DAY)
        approval.grant(conn, "kim", "RIGHTS_ADMIN", DAY)
        for u, n in (("lee", "이"), ("park", "박")):
            approval.add_user(conn, u, n, DAY)
        for u in ("kim", "lee", "park"):
            approval.grant(conn, u, "ORDER_WRITE", DAY)
        approval.grant(conn, "kim", "ORDER_APPROVE", DAY, max_order_value=10_000_000, self_approve=True)
        approval.grant(conn, "park", "ORDER_APPROVE", DAY)
    return conn


def status(conn, rid):
    return approval.requests(conn, ids=[rid])[0]


def test_rights_start_from_one_admin_and_only_admins_change_them(conn, broker):
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    with as_("kim"):
        with pytest.raises(ApprovalError, match="권한 관리자가 아직 없어요"):
            approval.grant(conn, "kim", "ORDER_WRITE", DAY)
        approval.add_user(conn, "kim", "김", DAY)               # 첫 사용자는 누구나
        approval.grant(conn, "kim", "RIGHTS_ADMIN", DAY)         # 관리자가 없을 때는 권한 관리만
        approval.add_user(conn, "lee", "이", DAY)
    with as_("lee"), pytest.raises(ApprovalError, match="권한 관리 권한이 없어요"):
        approval.grant(conn, "lee", "ORDER_APPROVE", DAY, self_approve=True)
    with as_("kim"):
        with pytest.raises(ApprovalError, match="주문 승인 권한에만"):
            approval.grant(conn, "lee", "ORDER_WRITE", DAY, self_approve=True)
        approval.grant(conn, "lee", "ORDER_WRITE", DAY)
        approval.grant(conn, "lee", "ORDER_WRITE", DAY, account_id="A0001", revoke=True)   # 계좌 행이 앞서요
        approval.grant(conn, "lee", "ORDER_APPROVE", DAY + dt.timedelta(days=3), max_order_value=1_000_000,
                       effective_from=DAY + dt.timedelta(days=3))
    assert approval.rights_of(conn, "lee", "A0001", DAY) == {}
    later = approval.rights_of(conn, "lee", "A0001", DAY + dt.timedelta(days=3))
    assert set(later) == {"ORDER_APPROVE"} and later["ORDER_APPROVE"].max_order_value == 1_000_000
    actions = [r[0] for r in conn.execute("select action from audit_log where action like 'RIGHT%%' or action = 'USER_ADD'"
                                          " order by id")]
    assert actions == ["USER_ADD", "RIGHT_GRANT", "USER_ADD", "RIGHT_GRANT", "RIGHT_REVOKE", "RIGHT_GRANT"]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("update user_rights set granted = true")


def test_request_then_another_person_approves_or_the_writer_self_approves(team):
    b = books(team)
    with as_("lee"):
        (rid,), r = approval.submit(b, DAY, DAY, [Line("069500", BUY, 2, 80_000)], now=NOW)
        assert r.ok and status(team, rid).status == "PENDING"
        with pytest.raises(ApprovalError, match="주문 승인 권한이 없어요"):
            approval.approve(b, DAY, [rid], now=NOW)
    with as_("park"):
        approval.approve(b, DAY, [rid], now=NOW)
    s = status(team, rid)
    assert (s.status, s.approved_by, s.self_approved) == ("APPROVED", "park", False)

    with as_("park"):   # 셀프 승인 허용이 없으면 자기 요청은 승인하지 못해요
        (own,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000)], now=NOW)
        with pytest.raises(ApprovalError, match="셀프 승인 허용이 없어"):
            approval.approve(b, DAY, [own], now=NOW)
    with as_("kim"):
        (mine,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000)], self_approve=True, now=NOW)
        with pytest.raises(ApprovalError, match="1건 승인 한도"):    # 200주 = 1,600만 원 > 1천만 원
            approval.submit(b, DAY, DAY, [Line("069500", BUY, 200, 80_000)], self_approve=True, now=NOW)
    s = status(team, mine)
    assert (s.status, s.approved_by, s.self_approved) == ("APPROVED", "kim", True)
    # 한도에 걸린 요청은 승인과 함께 되돌려져 남지 않아요
    assert len(approval.requests(team)) == 3


def test_pending_requests_use_up_cash_so_the_next_request_is_checked_against_them(team):
    b = books(team)
    with as_("lee"):
        # 여러 줄을 한 번에 올리면 어느 줄에서 막히는지 알려 줘요 (4,800만 + 5,600만 > 약 9,990만)
        with pytest.raises(ApprovalError, match="2번째 줄 069500 매수 700주.*혼자는 통과, 같이 보내면 실패"):
            approval.submit(b, DAY, DAY, [Line("069500", BUY, 600, 80_000), Line("069500", BUY, 700, 80_000)],
                            weight_limit=WIDE, now=NOW)
        approval.submit(b, DAY, DAY, [Line("069500", BUY, 600, 80_000)], weight_limit=WIDE, now=NOW)  # 4,800만
        approval.submit(b, DAY, DAY, [Line("069500", BUY, 500, 80_000)], weight_limit=WIDE, now=NOW)  # 4,000만
        with pytest.raises(ApprovalError, match="주문가능금액보다 커요"):               # 혼자는 1,600만 원이라 통과
            approval.submit(b, DAY, DAY, [Line("069500", BUY, 200, 80_000)], weight_limit=WIDE, now=NOW)
        approval.submit(b, DAY, DAY, [Line("069500", SELL, 1, 80_000)], weight_limit=WIDE, now=NOW)  # 매도는 현금과 무관
    done = team.execute("select count(*) from audit_log where action = 'ORDER_REQUEST'").fetchone()[0]
    assert done == 3 and len(approval.requests(team)) == 3


def test_a_batch_is_approved_rejected_or_canceled_as_a_whole(team):
    b = books(team)
    with as_("lee"):
        ids, _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000), Line("069500", BUY, 2, 80_000)],
                                 batch="BK", now=NOW)
    reqs = approval.requests(team, ids=ids)
    assert {r.batch_id for r in reqs} == {"BK20261120-01"} and [r.line_no for r in reqs] == [1, 2]
    with as_("park"):
        with pytest.raises(ApprovalError, match="묶음 BK20261120-01은\\(는\\) 함께 승인해야"):
            approval.approve(b, DAY, ids[:1], now=NOW)
        with pytest.raises(ApprovalError, match="사유"):
            approval.reject(team, "A0001", DAY, ids, " ")
        approval.reject(team, "A0001", DAY, ids, "가격 재확인")
    assert {status(team, i).status for i in ids} == {"REJECTED"}
    with as_("lee"):
        (one,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000)], now=NOW)
    with as_("park"), pytest.raises(ApprovalError, match="작성자만"):
        approval.cancel(team, "A0001", [one])
    with as_("lee"):
        approval.cancel(team, "A0001", [one], "주문 철회")
        with pytest.raises(ApprovalError, match="취소할 수 없는 상태"):
            approval.cancel(team, "A0001", [one])
    assert status(team, one).status == "CANCELED"


def test_the_database_keeps_the_order_of_the_flow(team):
    b = books(team)
    with as_("lee"):
        (rid,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000)], now=NOW)
    with pytest.raises(psycopg.errors.CheckViolation, match="SEND after REQUEST"):
        team.execute("insert into order_approvals (request_id, action, client_id) values (%s, 'SEND', null)", (rid,))
    with pytest.raises(psycopg.errors.CheckViolation, match="only its writer"):
        team.execute("insert into order_approvals (request_id, action, actor) values (%s, 'CANCEL', 'kim')", (rid,))
    team.execute("insert into order_approvals (request_id, action, actor, self_approved) values (%s, 'APPROVE', 'park', true)",
                 (rid,))   # 셀프 승인 표시는 DB가 작성자와 비교해 다시 채워요
    assert status(team, rid).self_approved is False
    with pytest.raises(psycopg.errors.CheckViolation, match="REJECT after APPROVE"):
        team.execute("insert into order_approvals (request_id, action, actor, reason) values (%s, 'REJECT', 'park', 'x')",
                     (rid,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        team.execute("delete from order_approvals")


def test_run_sends_approved_requests_once_and_rechecks_the_set_right_before(team, broker):
    b = books(team)
    with as_("kim"):
        (a,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 2, 80_000)], self_approve=True, now=NOW)
        (c,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 100, 80_000)], weight_limit=WIDE, now=NOW)
    with as_("park"):
        approval.approve(b, DAY, [c], weight_limit=WIDE, now=NOW)
    # 승인 뒤에 다른 경로로 현금이 쓰이면(여기서는 직접 주문) 보내기 직전 검증에서 걸러요
    place_order(b, broker, DAY, "069500", BUY, 1_150, limit_price=80_000)
    session = session_for(books(team), broker, DAY)
    sent = approval.send_approved(books(team), session, DAY, weight_limit=WIDE, now=NOW)
    assert [(s.request_id, bool(s.client_id)) for s in sent] == [(a, True), (c, False)]
    assert "주문가능금액" in sent[1].detail
    sa, sc = status(team, a), status(team, c)
    assert (sa.status, sa.client_id) == ("SENT", sent[0].client_id) and sc.status == "SEND_FAILED"
    assert [call[3] for call in broker.calls if call[0] == "order"] == [1_150, 2]
    assert approval.send_approved(books(team), session_for(books(team), broker, DAY), DAY, now=NOW) == []  # 다시 안 나가요
    with as_("kim"):
        (d,), _ = approval.submit(books(team), DAY, DAY, [Line("069500", SELL, 1, 80_000)], self_approve=True, now=NOW)
    with pytest.raises(psycopg.errors.UniqueViolation):   # 주문 하나에 전송 기록 하나
        team.execute("insert into order_approvals (request_id, action, client_id) values (%s, 'SEND', %s)",
                     (d, sa.client_id))


def test_unsent_requests_expire_after_their_day(team):
    b = books(team)
    with as_("lee"):
        (rid,), _ = approval.submit(b, DAY, DAY, [Line("069500", BUY, 1, 80_000)], now=NOW)
        (tomorrow,), _ = approval.submit(b, DAY, D("2026-11-23"), [Line("069500", BUY, 1, 80_000)], now=NOW)
    assert approval.expire(team, "A0001", DAY) == []
    assert approval.expire(team, "A0001", DAY, through_today=True) == [rid]
    assert status(team, rid).status == "EXPIRED" and status(team, tomorrow).status == "PENDING"
    with as_("park"), pytest.raises(ApprovalError, match="승인할 수 없는 상태"):
        approval.approve(b, DAY, [rid], now=NOW)


# ---------- 명령과 run ----------

def test_commands_for_users_rights_and_requests(conn, broker, capsys):
    import getpass

    from ministp import __main__ as cli
    from db.test_ops import URL as url
    env = {"MINISTP_DATABASE_URL": url, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01"}
    kw = dict(environ=env, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda h, m: (lambda: dt.datetime(2026, 11, 20, h, m))  # noqa: E731
    me = getpass.getuser()
    broker.set_day(DAY)
    assert cli.run(["open"], now=at(8, 0), **kw) == 0
    assert cli.run(["right", "list"], **kw) == 0 and "권한이 아직 없어요" in capsys.readouterr().out
    assert cli.run(["user", "add", me, "운영자"], now=at(8, 1), **kw) == 0
    assert cli.run(["right", "grant", me, "ORDER_WRITE"], now=at(8, 1), **kw) == 1     # 관리자부터
    for args in (["RIGHTS_ADMIN"], ["ORDER_WRITE"], ["ORDER_APPROVE", "--max-value", "1000000"]):
        assert cli.run(["right", "grant", me, *args], now=at(8, 2), **kw) == 0
    assert cli.run(["user", "add", "lee", "이"], now=at(8, 3), **kw) == 0
    assert cli.run(["right", "grant", "lee", "ORDER_APPROVE"], now=at(8, 3), **kw) == 0
    assert cli.run(["right", "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "1건 한도 1,000,000원, 셀프 승인 안 됨" in out and "1건 한도 없음" in out

    # 장 전에 올린 요청은 오늘 주문, 장 마감 뒤에 올린 요청은 다음 영업일 주문
    assert cli.run(["request", "add", "--line", "069500:buy:2:80000", "--line", "069500:buy:1:80000", "--batch"],
                   now=at(8, 30), **kw) == 0
    assert cli.run(["request", "add", "--line", "069500:buy:1:80500"], now=at(16, 0), **kw) == 0
    assert cli.run(["request", "add", "--line", "069500:buy:1"], now=at(16, 0), **kw) == 1   # 시장가인데 현재가 없음
    assert cli.run(["request", "add", "--line", "069500:hold:1"], now=at(16, 0), **kw) == 1
    assert cli.run(["request", "add", "--line", "069500:sell:5:80000"], now=at(16, 0), **kw) == 1   # 보유 1주
    assert cli.run(["request", "list"], now=at(16, 1), **kw) == 0
    out = "".join(capsys.readouterr())
    assert "Q20261120-0001 2026-11-20 B20261120-01" in out and "Q20261120-0003 2026-11-23" in out
    assert "@   80,500 결재 대기" in out and "현재가가 없어요" in out and "매도가능수량" in out
    assert cli.run(["request", "approve", "Q20261120-0001", "Q20261120-0002"], now=at(8, 40), **kw) == 1  # 셀프 승인 없음
    assert "셀프 승인 허용이 없어" in "".join(capsys.readouterr())
    with pytest.raises(SystemExit):                                                              # 반려 사유는 필수
        cli.run(["request", "reject", "Q20261120-0001"], now=at(8, 40), **kw)
    assert cli.run(["request", "cancel", "Q20261120-0001", "Q20261120-0002", "--reason", "보류"],
                   now=at(8, 41), **kw) == 0
    with as_("lee"):
        approval.approve(books(conn), DAY, ["Q20261120-0003"], now=NOW, prices={"069500": 80_000})
    assert [r.status for r in approval.requests(conn)] == ["CANCELED", "CANCELED", "APPROVED"]
    kinds = [r[0] for r in conn.execute("select action from audit_log where outcome = 'REFUSED' order by id")]
    assert kinds == ["RIGHT_GRANT", "ORDER_REQUEST", "ORDER_REQUEST", "ORDER_REQUEST", "ORDER_APPROVE"]


def test_run_sends_what_another_person_approves_and_expires_the_rest_at_the_close(team, broker):
    import queue

    from ministp.ops import IntradayRunner
    clock = [0.0]
    out = []
    times = iter([dt.datetime(2026, 11, 20, 10, 0)] * 6 + [dt.datetime(2026, 11, 20, 15, 21)] * 50)
    with as_("system"):
        r = IntradayRunner(books(team), broker, DAY, queue.Queue(), monotonic=lambda: clock[0], out=out.append,
                           settle_wait=lambda: None, now=lambda: next(times))
    with as_("lee"):
        (a,), _ = approval.submit(books(team), DAY, DAY, [Line("069500", BUY, 2, 80_000)], now=NOW)
        (b,), _ = approval.submit(books(team), DAY, DAY, [Line("069500", BUY, 1, 80_000)], now=NOW)
    with as_("park"):
        approval.approve(books(team), DAY, [a], now=NOW)
    clock[0] = 10.0
    r.step(timeout=0)                     # 승인된 요청을 집어 보내요
    assert any(f"요청 {a} → 주문" in line for line in out)
    assert status(team, a).status == "SENT" and broker.calls[-1] == ("order", "069500", "BUY", 2)
    r.run(dt.time(15, 20))                # 장 마감: 나가지 않은 요청은 만료
    assert status(team, b).status == "EXPIRED" and any("만료했어요" in line for line in out)
