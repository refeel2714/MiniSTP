"""감사 로그 (ERD v2.3, R-85~87): every request leaves a row, done, refused or failed; the rows are chained."""
import datetime as dt
import getpass

import pytest

from ministp import __main__ as cli
from ministp import accounts, audit, codes
from ministp.broker import OrderRejected
from ministp.ledger import Side
from ministp.ops import OpsError, close_books, daily_close, open_books, place_order, resolve_break
from ministp.recon import Cause
from ministp.risk import REASON_TEXT
from ministp.store import migrate as mg
from ministp.store.integrity import integrity_report

from db.test_multi_account import _restore_v1_1  # noqa: E402
from db.test_ops import CAL, D, FEES, KODEX, URL, books, broker, conn, let_me_order  # noqa: F401, E402
from db.test_ui_manage import press  # noqa: F401, E402

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

DAY = D("2026-11-20")
ENV = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
       "MINISTP_KIS_ACCOUNT": "12345678-01"}


def at(h, m):
    return lambda: dt.datetime(2026, 11, 20, h, m)


def log(conn, **where):
    """(action, outcome, account, target) of every row, oldest first."""
    return [(r.action, r.outcome, r.account_id, r.target) for r in reversed(audit.rows(conn, limit=1000, **where))]


def last(conn) -> audit.Row:
    return audit.rows(conn, limit=1)[0]


def _check(conn):
    return next(c for c in integrity_report(conn, CAL, FEES, {KODEX.symbol: KODEX}) if c.key == "audit")


# ---------- the rows and their chain ----------

def test_the_database_numbers_stamps_and_links_every_row(conn):
    assert log(conn) == [("INSTRUMENT_ADD", "DONE", None, "069500")]     # the fixture registered KODEX 200
    first = last(conn)
    assert (first.id, first.channel, first.actor) == (1, "SYSTEM", "system")
    assert first.detail == {"name": "KODEX 200", "market": "KOSPI", "asset_type": "ETF"}
    with audit.acting_as("CLI", "tester"):
        n = audit.record(conn, "RESOLVE", target="7", detail={"date": DAY, "skip": None, "n": 3})
    # what the caller says about the number, the time and the hashes is not taken
    conn.execute("insert into audit_log (id, at, channel, actor, action, outcome, prev_hash, hash)"
                 " values (99, '2001-01-01', 'CLI', 'tester', 'CLOSE', 'DONE', 'x', 'y')")
    rows = conn.execute("select id, at > now() - interval '1 minute', prev_hash, hash, actor from audit_log"
                        " order by id").fetchall()
    assert n == 2 and [r[0] for r in rows] == [1, 2, 3] and all(r[1] for r in rows)
    assert rows[0][2] == "0" * 64 and rows[1][2] == rows[0][3] and rows[2][2] == rows[1][3]
    assert len({r[3] for r in rows}) == 3 and all(len(r[3]) == 64 for r in rows)
    assert audit.rows(conn, action="RESOLVE")[0].detail == {"date": "2026-11-20", "n": 3}
    assert audit.origin()[:2] == ("SYSTEM", "system")                 # acting_as gave the origin back
    assert audit.chain_problems(conn) == (3, rows[2][3], []) and _check(conn).ok
    assert "3줄" in _check(conn).detail and rows[2][3][:12] in _check(conn).detail


def test_rows_cannot_be_changed_and_a_forced_change_breaks_the_chain(conn):
    for i in range(4):
        audit.record(conn, "CLOSE", target=f"2026-11-{20 + i}")
    for forbidden in ("update audit_log set message = 'x' where id = 2", "delete from audit_log where id = 2",
                      "truncate audit_log"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(forbidden)
    for tamper, found in [("update audit_log set target = '2026-12-31' where id = 3", [3]),        # a row rewritten
                          ("update audit_log set at = at - interval '1 day' where id = 2", [2]),   # a row re-dated
                          ("delete from audit_log where id = 3", [4, 5]),                          # a row removed
                          ("delete from audit_log where id = 1", [2, 3, 4, 5]),                    # the beginning removed
                          ("update audit_log set hash = repeat('a', 64) where id = 5", [5])]:      # the end re-sealed by hand
        with conn.transaction(force_rollback=True):
            conn.execute("alter table audit_log disable trigger user")
            conn.execute(tamper)
            assert audit.chain_problems(conn, 9)[2] == found, tamper
            assert not _check(conn).ok and str(found[0]) in _check(conn).detail
    assert audit.chain_problems(conn)[2] == [] and _check(conn).ok
    with conn.transaction(force_rollback=True):                       # the trigger that makes the chain is a guard too
        conn.execute("alter table audit_log disable trigger audit_log_chain")
        guards = next(c for c in integrity_report(conn, CAL, FEES, {}) if c.key == "guards")
        assert not guards.ok and "audit_log_chain" in guards.detail


def test_two_sessions_writing_at_once_still_make_one_chain(conn):
    import threading
    with psycopg.connect(URL, autocommit=True) as other:
        done = threading.Event()

        def second():
            audit.record(other, "CLOSE", target="second")             # waits for the first to finish its row
            done.set()
        with conn.transaction():
            audit.record(conn, "CLOSE", target="first")
            t = threading.Thread(target=second)
            t.start()
            assert not done.wait(0.3)                                 # the chain has one end: one writer at a time
        t.join(5)
        assert done.is_set()
    assert [r[3] for r in log(conn)] == ["069500", "first", "second"] and audit.chain_problems(conn)[2] == []


def test_unknown_codes_never_reach_the_table(conn):
    with pytest.raises(ValueError, match="행위 코드"):
        audit.record(conn, "ORDER_EDIT")
    with pytest.raises(ValueError, match="결과 코드"):
        audit.record(conn, "ORDER", "MAYBE")
    with pytest.raises(ValueError):
        audit.set_origin("PHONE")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into audit_log (channel, actor, action, outcome) values ('PHONE', 'x', 'ORDER', 'DONE')")
    assert codes.check(conn) == []                                   # the three code groups agree with the program
    assert set(codes.names(conn, "AUDIT_ACTION")) == set(audit.ACTIONS)


# ---------- what the operations leave ----------

def test_a_day_of_operation_leaves_one_row_per_thing_done(conn, broker):
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    _, cid = place_order(books(conn), broker, DAY, "069500", Side.BUY, 2, 80_000)
    broker.fill("0000000001", 80_000)
    broker.cash_skew = 5                                              # the broker's cash is 5 won off: a break
    rep = daily_close(books(conn), broker, DAY)
    assert not rep.clean and not rep.closed
    break_id = conn.execute("select id from recon_results where status = 'BREAK' limit 1").fetchone()[0]
    for rid in [r[0] for r in conn.execute("select id from recon_results where status = 'BREAK' order by id")]:
        resolve_break(conn, rid, Cause.TIMING, "조회 시점")
    close_books(books(conn), DAY)
    rows = log(conn)[1:]
    assert rows[0] == ("OPEN", "DONE", "A0001", "2026-11-20") and rows[1] == ("ORDER", "DONE", "A0001", cid)
    assert rows[2] == ("DAILY", "DONE", "A0001", "2026-11-20")
    assert rows[3] == ("RESOLVE", "DONE", "A0001", str(break_id)) and rows[-1] == ("CLOSE", "DONE", "A0001", "2026-11-20")
    order = audit.rows(conn, action="ORDER")[0]
    assert order.detail == {"symbol": "069500", "side": "BUY", "qty": 2, "limit_price": 80_000, "order_no": "0000000001"}
    daily = audit.rows(conn, action="DAILY")[0]
    assert daily.detail["booked"] == 1 and daily.detail["breaks"] >= 1 and daily.detail["closed"] is False
    assert audit.rows(conn, action="RESOLVE")[0].detail["cause"] == "TIMING"
    assert audit.rows(conn, account_id="A0001", outcome="REFUSED") == []
    assert audit.chain_problems(conn)[2] == []


def test_an_order_refused_before_the_broker_is_in_no_order_table_but_in_the_log(conn, broker):
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    session, cid = place_order(books(conn), broker, DAY, "069500", Side.SELL, 500)   # one share held
    assert cid is None and broker.calls == []
    assert conn.execute("select count(*) from oms_orders").fetchone()[0] == 0
    row = last(conn)
    assert (row.action, row.outcome, row.account_id, row.target) == ("ORDER", "REFUSED", "A0001", None)
    assert row.detail["reason"] in REASON_TEXT and row.detail["reason"] in row.message
    assert row.detail == {"symbol": "069500", "side": "SELL", "qty": 500, "date": "2026-11-20",
                          "reason": row.detail["reason"]}
    assert row.message == session.anomalies[-1].detail.replace(f"risk: {row.detail['reason']}", row.message)

    with pytest.raises(OpsError, match="종목 등록"):                    # refused by the command itself: the caller's row
        with audit.refusals(conn, lambda: ("ORDER", "A0001", None, {"symbol": "999999"}), refused=(OpsError,)):
            place_order(books(conn), broker, DAY, "999999", Side.BUY, 1)
    assert (last(conn).outcome, last(conn).detail, last(conn).message) == (
        "REFUSED", {"symbol": "999999"}, "종목 등록이 필요해요: 999999")

    def down(*a):
        raise OrderRejected("APBK0919 주문가능금액 초과", "APBK0919")
    broker.place_order = down                                         # the broker says no: the intent is stored, so FAILED names it
    _, cid = place_order(books(conn), broker, DAY, "069500", Side.BUY, 1)
    row, stored = last(conn), conn.execute("select client_id from oms_orders").fetchall()
    assert cid is None and len(stored) == 1
    assert (row.outcome, row.target, row.message) == ("FAILED", stored[0][0], "broker: APBK0919 주문가능금액 초과")
    assert [r[1] for r in log(conn, action="ORDER")] == ["REFUSED", "REFUSED", "FAILED"]


def test_a_change_that_cannot_be_logged_does_not_happen(conn, monkeypatch):
    monkeypatch.setattr(audit, "ACTIONS", {k: v for k, v in audit.ACTIONS.items() if k != "COMPANY_ADD"})
    with pytest.raises(ValueError, match="행위 코드"):
        accounts.add_company(conn, "C0002", "둘째")
    assert conn.execute("select count(*) from companies where company_id = 'C0002'").fetchone()[0] == 0
    monkeypatch.undo()
    accounts.add_company(conn, "C0002", "둘째")
    accounts.add_account(conn, "A0002", "C0002", "둘째 계좌", credential_ref="KIS_2", opened_on=DAY)
    accounts.set_status(conn, "A0002", "SUSPENDED", DAY, "점검")
    assert log(conn)[1:] == [("COMPANY_ADD", "DONE", None, "C0002"), ("ACCOUNT_ADD", "DONE", "A0002", "A0002"),
                             ("ACCOUNT_STATUS", "DONE", "A0002", "A0002")]
    assert last(conn).detail == {"from": "OPEN", "to": "SUSPENDED", "effective_from": "2026-11-20", "reason": "점검"}
    assert "12345678" not in str(conn.execute("select detail from audit_log").fetchall())


def test_a_refusal_about_an_account_that_does_not_exist_is_still_kept(conn):
    with pytest.raises(OpsError):
        with audit.refusals(conn, lambda: ("ACCOUNT_STATUS", "A0009", "A0009", {"to": "CLOSED"}), refused=(OpsError,)):
            accounts.set_status(conn, "A0009", "CLOSED", DAY, "없는 계좌")
    row = last(conn)
    assert (row.outcome, row.account_id, row.target) == ("REFUSED", None, "A0009")
    assert row.detail == {"to": "CLOSED", "account": "A0009"} and "등록되지 않은 계좌" in row.message
    with pytest.raises(KeyError):                                     # not an error the log is told to keep: no row
        with audit.refusals(conn, lambda: ("CLOSE", None, None, None), refused=(OpsError,)):
            raise KeyError("x")
    assert last(conn).id == row.id
    with audit.refusals(conn, lambda: None, refused=(OpsError,)):     # nothing raised, nothing written
        pass
    with pytest.raises(OpsError):                                     # a read: named None
        with audit.refusals(conn, lambda: None, refused=(OpsError,)):
            raise OpsError("조회 실패")
    assert last(conn).id == row.id


# ---------- the console ----------

def test_commands_leave_rows_as_the_console_user_and_audit_shows_them(conn, broker, capsys):
    broker.set_day(DAY)
    kw = dict(environ=ENV, client_factory=lambda cfg: broker, sleep=lambda s: None)
    assert cli.run(["open"], now=at(8, 0), **kw) == 0
    assert cli.run(["open"], now=at(8, 1), **kw) == 1                 # once only
    me = let_me_order(conn)                                           # 주문은 결재를 거쳐요 (R-99~101)
    assert cli.run(["order", "069500", "buy", "1"], now=at(8, 30), **kw) == 2   # before the market opens
    broker.orders[DAY] = {}
    assert cli.run(["order", "069500", "buy", "1", "--limit", "80000"], now=at(10, 0), **kw) == 0
    assert cli.run(["order", "069500", "sell", "900"], now=at(10, 1), **kw) == 1
    assert cli.run(["daily"], now=at(15, 0), **kw) == 2               # too early
    assert cli.run(["resolve", "999", "TIMING", "없는 행"], now=at(15, 1), **kw) == 1
    assert cli.run(["code", "add", "RECON_STATUS", "MAYBE", "글쎄"], now=at(15, 2), **kw) == 1
    assert cli.run(["account", "status", "A0009", "CLOSED", "없는 계좌"], now=at(15, 3), **kw) == 1
    assert cli.run(["daily"], now=at(16, 0), **kw) == 0
    assert cli.run(["status"], now=at(16, 1), **kw) == 0              # a read leaves nothing
    capsys.readouterr()
    cid = conn.execute("select client_id from oms_orders").fetchone()[0]
    rid = "{:%Y%m%d}-{}".format(*conn.execute("select trade_date, request_no from order_requests").fetchone())
    assert log(conn)[1:] == [
        ("OPEN", "DONE", "A0001", "2026-11-20"), ("OPEN", "REFUSED", "A0001", "2026-11-20"),
        ("USER_ADD", "DONE", None, me), ("RIGHT_GRANT", "DONE", None, me), ("RIGHT_GRANT", "DONE", None, me),
        ("RIGHT_GRANT", "DONE", None, me),
        ("ORDER", "REFUSED", "A0001", None), ("ORDER_REQUEST", "DONE", "A0001", rid),
        ("ORDER_APPROVE", "DONE", "A0001", rid), ("ORDER", "DONE", "A0001", cid), ("ORDER", "REFUSED", "A0001", None),
        ("DAILY", "REFUSED", "A0001", "2026-11-20"), ("RESOLVE", "REFUSED", None, "999"),
        ("CODE_ADD", "REFUSED", None, "RECON_STATUS.MAYBE"), ("ACCOUNT_STATUS", "REFUSED", None, "A0009"),
        ("DAILY", "DONE", "A0001", "2026-11-20")]
    mine = audit.rows(conn, limit=10)
    assert {(r.channel, r.actor) for r in mine} == {("CLI", getpass.getuser())}
    assert audit.rows(conn, action="DAILY")[0].detail["closed"] is True
    refused = audit.rows(conn, action="ORDER", outcome="REFUSED")
    assert refused[1].message == "장중(영업일 09:00~15:20)에만 주문해요." and "묶음 검증" in refused[0].message
    assert "매도가능수량보다 많아요" in refused[0].message                # 요청 전에 걸러서 요청도 남지 않아요
    assert "k" not in {v for r in audit.rows(conn, limit=100) for v in r.detail.values() if isinstance(v, str)}

    assert cli.run(["audit"], **kw) == 0
    out = capsys.readouterr().out
    assert f"명령 {getpass.getuser()}  거부  원장 시작  A0001 2026-11-20" in out and "→ 원장이 이미 시작됐어요" in out
    assert f"처리  주문  A0001 {cid}" in out and "symbol=069500  side=BUY  qty=1  limit_price=80000" in out
    assert f"전체 17줄, 마지막 줄 17번의 해시 {audit.chain_problems(conn)[1][:16]}" in out
    assert cli.run(["audit", "--outcome", "REFUSED", "--action", "ORDER", "--limit", "1"], **kw) == 0
    out = capsys.readouterr().out
    assert out.count("거부  주문") == 1 and "원장 시작" not in out
    assert cli.run(["audit", "--account", "A0009"], **kw) == 0 and "조건에 맞는 줄이 없어요" in capsys.readouterr().out
    assert log(conn)[-1][0] == "DAILY"                                # looking at the log is not logged
    assert cli.run(["verify"], **kw) == 0 and "감사 로그: 17줄의 번호와 해시 사슬이 이어짐" in capsys.readouterr().out


def test_applying_a_migration_is_the_first_row_of_a_migrated_database(capsys):
    _restore_v1_1()
    env = {"MINISTP_DATABASE_URL": URL}
    assert cli.run(["open"], environ=env) == 1 and "db-migrate" in capsys.readouterr().out   # schema behind: no log yet, no crash
    assert cli.run(["db-migrate", "--dry-run"], environ=env) == 0
    with psycopg.connect(URL, autocommit=True) as old:
        assert not audit.has_log(old)                                 # a rehearsal leaves nothing, not even the table
    assert cli.run(["db-migrate", "--apply"], environ=env) == 0
    capsys.readouterr()
    with psycopg.connect(URL, autocommit=True) as c:
        assert list(mg.applied(c)) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        assert log(c) == [("DB_MIGRATE", "DONE", None, "ERD v2.14")]
        assert len(last(c).detail["applied"]) == 14 and last(c).channel == "CLI"
        assert codes.check(c) == [] and audit.chain_problems(c)[2] == []


# ---------- the screens ----------

def test_a_button_leaves_rows_that_name_the_screen_transaction(press, conn):
    assert press("STP1400U01", company_id="C0002", alias="둘째")[0] == 200
    row = last(conn)
    assert (row.channel, row.actor, row.action, row.outcome, row.target) == (
        "UI", getpass.getuser(), "COMPANY_ADD", "DONE", "C0002")
    assert row.detail == {"alias": "둘째", "tx": "STP1400U01"}

    status, said = press("STP4600U04", group_id="ACCOUNT_STATUS", code="CLOSED", in_use="사용 안 함")
    row = last(conn)
    assert status == 400 and (row.action, row.outcome, row.target, row.message) == (
        "CODE_USE", "REFUSED", "ACCOUNT_STATUS.CLOSED", said)
    assert row.detail == {"group_id": "ACCOUNT_STATUS", "code": "CLOSED", "in_use": "사용 안 함", "tx": "STP4600U04"}
    assert press("STP4600U03", group_id="ACCOUNT_STATUS", code="OPEN")[0] == 400      # refused before the command: no name
    assert (last(conn).action, last(conn).outcome, last(conn).message) == ("CODE_RENAME", "REFUSED", "name 값을 넣어 주세요")
    assert press("STP4400U02", account_id="A0009", status="CLOSED", reason="없는 계좌")[0] == 400
    row = last(conn)
    assert (row.action, row.account_id, row.target, row.detail["account"]) == ("ACCOUNT_STATUS", None, "A0009", "A0009")
    n = row.id
    assert press("STP9999U01", x="1")[0] == 400 and last(conn).id == n   # not a 처리 transaction: nothing to name
    assert audit.origin()[:2] == ("SYSTEM", "system") and audit.chain_problems(conn)[2] == []
    assert {r.channel for r in audit.rows(conn, limit=4)} == {"UI"}
