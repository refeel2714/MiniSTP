"""법인·계좌 등록, 계좌 상태, and the daily commands run for one account at a time (`--account`)."""
import datetime as dt
import os

import pytest

from ministp import __main__ as cli
from ministp.accounts import add_account, add_company, erp_code_on, get_account, list_accounts, set_erp_code, set_status
from ministp.ledger import Side
from ministp.ops import OpsError, add_instrument, clean_streak, daily_close, load_books, open_books, place_order
from ministp.queries import order_check
from ministp.store import PostgresLedgerStore

psycopg = pytest.importorskip("psycopg")

from db.test_ops import CAL, FEES, KODEX, D, SimBroker, let_me_order  # noqa: E402

URL = os.environ.get("MINISTP_TEST_DATABASE_URL")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

DAY = D("2026-11-20")
ENV = {"MINISTP_DATABASE_URL": URL,
       "MINISTP_KIS_APP_KEY": "k1", "MINISTP_KIS_APP_SECRET": "s1", "MINISTP_KIS_ACCOUNT": "11111111-01",
       "MINISTP_KIS_2_APP_KEY": "k2", "MINISTP_KIS_2_APP_SECRET": "s2", "MINISTP_KIS_2_ACCOUNT": "22222222-01"}


@pytest.fixture
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("drop schema if exists public cascade; create schema public")
        PostgresLedgerStore(c).init_schema()
        add_instrument(c, KODEX)
        yield c


@pytest.fixture
def second(conn):
    """A second account of the default company, registered but not opened."""
    return add_account(conn, "A0002", "C0001", "두 번째 계좌", credential_ref="KIS_2", opened_on=DAY)


def _broker(cash) -> SimBroker:
    b = SimBroker(cash)
    b.set_day(DAY)
    b.orders[DAY] = {}
    return b


def at(h, m):
    return lambda: dt.datetime(2026, 11, 20, h, m)


# ---------- registering ----------

def test_register_a_company_and_an_account(conn):
    add_company(conn, "C0002", "두 번째 법인")
    a = add_account(conn, "A0002", "C0002", "연습 계좌", credential_ref="KIS_2", opened_on=DAY)
    assert (a.company_id, a.broker, a.env, a.account_type, a.status, a.status_from) == (
        "C0002", "KIS", "PAPER", "GENERAL", "OPEN", DAY)
    assert a.env_prefix == "MINISTP_KIS_2"
    assert [x.account_id for x in list_accounts(conn)] == ["A0001", "A0002"]
    assert not load_books(conn, CAL, FEES, "A0002").ledger.events     # its own, empty books


@pytest.mark.parametrize("args, message", [
    (("B0002", "C0001", "x", "KIS_2"), "계좌 ID는 A와 숫자 4자리"),
    (("A0002", "C0009", "x", "KIS_2"), "등록되지 않은 법인"),
    (("A0001", "C0001", "x", "KIS_2"), "이미 있는 계좌"),
    (("A0002", "C0001", " ", "KIS_2"), "별칭"),
    (("A0002", "C0001", "x", "KIS"), "계좌 A0001가 쓰고 있어요"),          # one key set, one account
    (("A0002", "C0001", "x", "12345678-01"), "계좌번호나 키를 넣는 자리가 아니에요"),
])
def test_account_registration_refuses(conn, args, message):
    account_id, company_id, alias, ref = args
    with pytest.raises(OpsError, match=message):
        add_account(conn, account_id, company_id, alias, credential_ref=ref, opened_on=DAY)
    assert len(list_accounts(conn)) == 1
    assert conn.execute("select count(*) from account_status_history").fetchone()[0] == 1


def test_only_paper_accounts_and_well_formed_companies(conn):
    with pytest.raises(OpsError, match="모의투자"):
        add_account(conn, "A0002", "C0001", "x", credential_ref="KIS_2", opened_on=DAY, env="LIVE")
    with pytest.raises(OpsError, match="법인 ID는 C와 숫자 4자리"):
        add_company(conn, "홍길동", "x")
    with pytest.raises(OpsError, match="이미 있는 법인"):
        add_company(conn, "C0001", "x")
    with pytest.raises(OpsError, match="등록되지 않은 계좌"):
        load_books(conn, CAL, FEES, "A0009")


def test_a_company_keeps_the_history_of_its_erp_company_code(conn):
    """v2.14: 법인마다 ERP 회사코드. 바꿀 때는 새 행, 날짜마다 그날의 코드."""
    assert erp_code_on(conn, "C0001", DAY) is None                     # 기본 법인: 아직 없음
    add_company(conn, "C0002", "계열사 B", erp_code="2000", effective_from=DAY)
    set_erp_code(conn, "C0001", "1000", DAY, "ERP 연결")
    set_erp_code(conn, "C0001", "1100", DAY + dt.timedelta(days=10), "ERP 회사 재편")
    assert [erp_code_on(conn, "C0001", d) for d in (DAY - dt.timedelta(days=1), DAY, DAY + dt.timedelta(days=9),
                                                   DAY + dt.timedelta(days=10))] == [None, "1000", "1000", "1100"]
    assert erp_code_on(conn, "C0002", DAY) == "2000"
    for args, said in ((("C0002", "1100", DAY + dt.timedelta(days=20), "x"), "법인 C0001가 쓰고 있어요"),
                       (("C0002", "1000", DAY, "x"), "법인 C0001가 쓰고 있어요"),       # 지난 날의 코드도
                       (("C0001", "1100", DAY + dt.timedelta(days=30), "x"), "이미 1100"),
                       (("C0001", "1200", DAY, "x"), "시작일"),
                       (("C0001", "12 00", DAY + dt.timedelta(days=30), "x"), "영문·숫자"),
                       (("C0001", "1200", DAY + dt.timedelta(days=30), " "), "사유"),
                       (("C0009", "1200", DAY + dt.timedelta(days=30), "x"), "등록되지 않은 법인")):
        with pytest.raises(OpsError, match=said):
            set_erp_code(conn, *args)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):           # 고치지 않고 새 행
        conn.execute("update company_erp_codes set erp_company_code = 'X'")
    assert conn.execute("select action, target from audit_log where action like 'COMPANY%' order by id").fetchall() == [
        ("COMPANY_ADD", "C0002"), ("COMPANY_ERP_CODE", "C0002"), ("COMPANY_ERP_CODE", "C0001"), ("COMPANY_ERP_CODE", "C0001")]


def test_company_commands(conn, capsys):
    kw = dict(environ=ENV, now=at(8, 0))
    assert cli.run(["company", "add", "C0002", "계열사 B", "--erp-code", "2000"], **kw) == 0
    assert cli.run(["company", "erp-code", "C0001", "1000", "ERP 연결"], **kw) == 0
    assert cli.run(["company", "erp-code", "C0001", "2000", "겹침"], **kw) == 1
    out = capsys.readouterr().out
    assert "법인 등록: C0002 계열사 B (ERP 회사코드 2000, 2026-11-20부터)" in out and "법인 C0002가 쓰고 있어요" in out
    assert cli.run(["company", "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "C0001 기본 법인: 계좌 1개, ERP 회사코드 1000" in out and "C0002 계열사 B: 계좌 0개, ERP 회사코드 2000" in out
    assert conn.execute("select action, outcome from audit_log where action = 'COMPANY_ERP_CODE' order by id").fetchall() == [
        ("COMPANY_ERP_CODE", "DONE"), ("COMPANY_ERP_CODE", "DONE"), ("COMPANY_ERP_CODE", "REFUSED")]


# ---------- status (R-39) ----------

def test_a_suspended_account_takes_no_orders_but_still_closes_the_day(conn, second):
    broker = _broker(10_000_000)
    open_books(load_books(conn, CAL, FEES, "A0002"), broker, DAY)
    set_status(conn, "A0002", "SUSPENDED", DAY, "테스트 정지")
    books = load_books(conn, CAL, FEES, "A0002")
    assert books.status == "SUSPENDED"
    session, cid = place_order(books, broker, DAY, "069500", Side.BUY, 1)
    assert cid is None and session.anomalies[-1].detail == "risk: ACCOUNT_NOT_OPEN"
    assert broker.calls == [] and conn.execute("select count(*) from oms_orders").fetchone()[0] == 0
    assert order_check(books, DAY, "069500", "BUY", 1, 80_000).rows[0][-2] == "ACCOUNT_NOT_OPEN"   # the screen agrees
    assert daily_close(books, broker, DAY).clean                 # settlement, NAV and recon go on

    set_status(conn, "A0002", "OPEN", DAY, "정지 해제")
    _, cid = place_order(load_books(conn, CAL, FEES, "A0002"), broker, DAY, "069500", Side.BUY, 1)
    assert cid == "A0002-S20261120-0001"                         # the account is part of the order id
    assert [r[0] for r in conn.execute("select status from account_status_history where account_id = 'A0002'"
                                       " order by id")] == ["OPEN", "SUSPENDED", "OPEN"]


def test_status_changes_are_checked(conn, second):
    with pytest.raises(OpsError, match="이미 OPEN"):
        set_status(conn, "A0002", "OPEN", DAY, "x")
    with pytest.raises(OpsError, match="사유"):
        set_status(conn, "A0002", "SUSPENDED", DAY, " ")
    with pytest.raises(OpsError, match="시작일"):
        set_status(conn, "A0002", "SUSPENDED", D("2026-11-19"), "x")
    open_books(load_books(conn, CAL, FEES, "A0002"), _broker(5_000_000), DAY)
    with pytest.raises(OpsError, match="예수금 5,000,000원.*해지할 수 없어요"):
        set_status(conn, "A0002", "CLOSED", DAY, "x")
    assert get_account(conn, "A0002").status == "OPEN"


def test_an_empty_account_can_be_closed_and_stays_closed(conn, second):
    assert set_status(conn, "A0002", "CLOSED", DAY, "쓰지 않는 계좌").status == "CLOSED"
    with pytest.raises(OpsError, match="다시 열지 않아요"):
        set_status(conn, "A0002", "OPEN", DAY, "x")
    with pytest.raises(OpsError, match="정상 상태가 아니에요"):
        open_books(load_books(conn, CAL, FEES, "A0002"), _broker(1), DAY)


# ---------- the commands, one account at a time ----------

def test_cli_two_accounts_from_registration_to_the_daily_close(conn, capsys):
    brokers = {"11111111": _broker(100_000_000), "22222222": _broker(30_000_000)}
    seen = []

    def factory(cfg):
        seen.append((cfg.app_key, cfg.account_no, cfg.token_cache.name))
        return brokers[cfg.account_no]

    kw = dict(environ=ENV, client_factory=factory, sleep=lambda s: None)
    assert cli.run(["company", "add", "C0002", "두 번째 법인"], now=at(8, 0), **kw) == 0
    assert cli.run(["account", "add", "A0002", "C0002", "연습 계좌", "--credential", "KIS_2"], now=at(8, 0), **kw) == 0
    out = capsys.readouterr().out
    assert "MINISTP_KIS_2_APP_KEY" in out and "open --account A0002" in out

    assert cli.run(["open"], now=at(8, 0), **kw) == 0
    assert cli.run(["open", "--account", "A0002"], now=at(8, 0), **kw) == 0
    assert seen == [("k1", "11111111", "kis_token.json"), ("k2", "22222222", "kis_token_kis_2.json")]
    out = capsys.readouterr().out
    assert "원장 시작: 예수금 100,000,000" in out and "원장 시작: 예수금 30,000,000" in out
    assert "계좌 A0002 연습 계좌 (법인 C0002, OPEN)" in out

    let_me_order(conn)                                    # 주문은 결재를 거쳐요 (셀프 승인, R-99~101)
    assert cli.run(["order", "069500", "buy", "2", "--account", "A0002"], now=at(10, 0), **kw) == 0
    assert cli.run(["order", "069500", "buy", "1"], now=at(10, 1), **kw) == 0
    assert brokers["22222222"].calls == [("order", "069500", "BUY", 2)]
    assert brokers["11111111"].calls == [("order", "069500", "BUY", 1)]
    brokers["22222222"].fill("0000000001", 80_000)       # both brokers number their orders from 1
    brokers["11111111"].fill("0000000001", 80_100)
    assert cli.run(["daily", "--account", "A0002"], now=at(16, 0), **kw) == 0
    assert cli.run(["daily"], now=at(16, 1), **kw) == 0
    capsys.readouterr()

    assert conn.execute("select client_id, account_id from oms_orders order by 1").fetchall() == [
        ("A0002-S20261120-0001", "A0002"), ("S20261120-0001", "A0001")]
    assert conn.execute("select account_id, exec_id, qty_delta from position_ledger order by 1").fetchall() == [
        ("A0001", "20261120-0000000001-1", 1), ("A0002", "20261120-0000000001-1", 2)]
    assert conn.execute("select account_id, count(*) from recon_results group by 1 order by 1").fetchall()[0][0] == "A0001"
    assert clean_streak(conn, CAL, DAY, "A0001") == 1 and clean_streak(conn, CAL, DAY, "A0002") == 1

    assert cli.run(["status", "--account", "A0002"], now=at(16, 5), **kw) == 0
    out = capsys.readouterr().out
    assert "069500 KODEX 200: 2주" in out and "무오류 연속 영업일: 1 / 20" in out
    assert cli.run(["account", "list"], now=at(16, 5), **kw) == 0
    out = capsys.readouterr().out
    assert "A0001 기본 계좌: 법인 C0001" in out and "A0002 연습 계좌: 법인 C0002" in out and "MINISTP_KIS_2_*" in out
    assert cli.run(["verify"], now=at(16, 6), **kw) == 0
    out = capsys.readouterr().out
    assert "[A0001] 현금" in out and "[A0002] 현금" in out and "문제" not in out


def test_streak_of_one_account_does_not_count_the_other(conn, second):
    b1, b2 = _broker(1_000_000), _broker(2_000_000)
    open_books(load_books(conn, CAL, FEES), b1, DAY)
    open_books(load_books(conn, CAL, FEES, "A0002"), b2, DAY)
    b2.cash_skew = 500                                            # only the second account breaks
    assert daily_close(load_books(conn, CAL, FEES), b1, DAY).clean
    assert not daily_close(load_books(conn, CAL, FEES, "A0002"), b2, DAY).clean
    assert clean_streak(conn, CAL, DAY) == 1 and clean_streak(conn, CAL, DAY, "A0002") == 0


def test_cli_refuses_two_ledger_accounts_on_one_brokerage_account(conn, second, capsys):
    env = {**ENV, "MINISTP_KIS_2_ACCOUNT": "11111111-01"}
    rc = cli.run(["open", "--account", "A0002"], environ=env, client_factory=lambda cfg: _broker(1), now=at(8, 0))
    assert rc == 1 and "같은 증권사 계좌" in capsys.readouterr().out
    assert conn.execute("select count(*) from ledger_events").fetchone()[0] == 0


def test_cli_status_change_and_unknown_account(conn, second, capsys):
    kw = dict(environ=ENV, client_factory=lambda cfg: _broker(1), now=at(9, 30))
    assert cli.run(["account", "status", "A0002", "SUSPENDED", "점검"], **kw) == 0
    assert "상태: SUSPENDED (2026-11-20부터)" in capsys.readouterr().out
    assert cli.run(["status", "--account", "A0002"], **kw) == 0
    assert "SUSPENDED" in capsys.readouterr().out
    assert cli.run(["status", "--account", "A0009"], **kw) == 1
    assert "등록되지 않은 계좌" in capsys.readouterr().out
    assert cli.run(["account", "add", "A0003", "C0001", "x", "--credential", "KIS_2"], **kw) == 1
    assert "계좌 A0002가 쓰고 있어요" in capsys.readouterr().out
