"""현금배당 on the database (ERD v2.4, R-57~59): the register, what `daily` books from it, the cash against the broker."""
import datetime as dt
from decimal import Decimal

import pytest

from ministp import __main__ as cli
from ministp import audit, codes, rights
from ministp.accounting import Account
from ministp.ledger import Side
from ministp.ops import Books, OpsError, add_instrument, daily_close, open_books, place_order
from ministp.store.integrity import integrity_report

from db.test_multi_account import fresh, two  # noqa: F401, E402
from db.test_ops import CAL, D, FEES, KODEX, URL, books, broker, conn  # noqa: F401, E402
from conftest import calendar, instruments  # noqa: F401, E402

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

FRI, MON, TUE, WED = D("2026-11-20"), D("2026-11-23"), D("2026-11-24"), D("2026-11-25")
ENV = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
       "MINISTP_KIS_ACCOUNT": "12345678-01"}


def _opened(conn, broker, day=FRI):
    """The books open on Friday 11-20 with the one KODEX 200 share the broker holds."""
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}


def _daily(conn, broker, day):
    broker.set_day(day)
    broker.orders.setdefault(day, {})
    return daily_close(books(conn), broker, day)


def _check(conn, key, account=""):
    return next(c for c in integrity_report(conn, CAL, FEES, {KODEX.symbol: KODEX})
                if c.key == key and c.account == account)


def _dividends(conn):
    return conn.execute("select account_id, action_id, symbol, qty, accrued_gross, accrued_on, paid_gross, tax,"
                        " paid_on, canceled_on, status from dividends_v order by action_id, account_id").fetchall()


# ---------- the register ----------

def test_a_dividend_is_registered_once_and_never_edited(conn):
    a = rights.add_dividend(conn, "069500", FRI, WED, "450", "분기 분배금")
    assert (a.action_id, a.symbol, a.record_date, a.pay_date, a.amount_per_share, a.note, a.canceled) == (
        1, "069500", FRI, WED, Decimal("450.00"), "분기 분배금", False)
    assert a.ref == "CA-1" and a.accrual_date(CAL) == MON             # R-57: the business day after the record date
    row = audit.rows(conn, limit=1)[0]
    assert (row.action, row.outcome, row.target) == ("CORPORATE_ACTION_ADD", "DONE", "CA-1")
    assert row.detail == {"symbol": "069500", "record_date": "2026-11-20", "pay_date": "2026-11-25",
                          "amount_per_share": "450", "note": "분기 분배금"}
    for bad, problem in [(("999999", FRI, WED, 450), "종목 등록"), (("069500", FRI, WED, 0), "0보다 크고"),
                         (("069500", FRI, WED, "1.234"), "소수 둘째"), (("069500", WED, FRI, 450), "보다 빨라요"),
                         (("069500", FRI, WED, 500), "이미 있어요 \\(CA-1\\)")]:
        with pytest.raises(OpsError, match=problem):
            rights.add_dividend(conn, *bad)
    assert len(rights.list_actions(conn)) == 1
    for change in ("update corporate_actions set amount_per_share = 500", "delete from corporate_actions",
                   "truncate corporate_actions cascade"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(change)
    for shapeless in ("insert into corporate_actions (symbol, action_type) values ('069500', 'CASH_DIVIDEND')",
                      "insert into corporate_actions (symbol, action_type) values ('069500', 'CANCEL')",
                      "insert into corporate_actions (symbol, action_type, record_date, pay_date, amount_per_share)"
                      " values ('069500', 'SPLIT', '2026-11-20', '2026-11-25', 1)"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(shapeless)
    assert codes.check(conn) == [] and _check(conn, "rights").ok


def test_a_wrong_action_is_cancelled_by_a_row_and_can_be_registered_again(conn):
    rights.add_dividend(conn, "069500", FRI, WED, 45)                 # a slip: 45 for 450
    with pytest.raises(OpsError, match="사유"):
        rights.cancel_action(conn, 1, " ")
    gone = rights.cancel_action(conn, 1, "주당 금액 오타")
    assert gone.canceled and gone.cancel_reason == "주당 금액 오타" and gone.amount_per_share == Decimal("45.00")
    assert conn.execute("select action_id, action_type, cancels_id, note from corporate_actions order by 1").fetchall() == [
        (1, "CASH_DIVIDEND", None, None), (2, "CANCEL", 1, "주당 금액 오타")]
    with pytest.raises(OpsError, match="이미 취소된"):
        rights.cancel_action(conn, 1, "또")
    with pytest.raises(OpsError, match="등록되지 않은 권리"):
        rights.cancel_action(conn, 2, "취소 행은 권리가 아니다")
    again = rights.add_dividend(conn, "069500", FRI, WED, 450)        # the same record date, now that the first is void
    assert again.action_id == 3 and [a.action_id for a in rights.list_actions(conn)] == [1, 3]
    assert [r.action for r in audit.rows(conn, limit=3)] == [
        "CORPORATE_ACTION_ADD", "CORPORATE_ACTION_CANCEL", "CORPORATE_ACTION_ADD"]


# ---------- what daily books ----------

def test_daily_recognises_the_dividend_then_books_the_cash_on_the_pay_date(conn, broker):
    _opened(conn, broker)
    place_order(books(conn), broker, FRI, "069500", Side.BUY, 2, 80_000)
    broker.fill("0000000001", 80_000)                                 # bought on the record date: settles 11-24, too late
    rights.add_dividend(conn, "069500", FRI, WED, 450)
    first = _daily(conn, broker, FRI)
    assert first.clean and first.closed and _dividends(conn) == []    # the record date itself: nothing yet (R-57)

    rep = _daily(conn, broker, MON)
    assert rep.clean and rep.closed
    assert "배당 인식 CA-1 069500: 기준일 2026-11-20 보유 1주 × 450원 = 450원 (지급일 2026-11-25)" in rep.notes
    assert _dividends(conn) == [("A0001", 1, "069500", 1, 450, MON, None, None, None, None, "ACCRUED")]
    assert rep.nav.receivables == 450 and rep.nav.net_assets == first.nav.net_assets + 450   # R-60, prices unchanged
    assert conn.execute("select date, kind, source_id from journal_entries where kind like 'DIVIDEND%'").fetchall() == [
        (MON, "DIVIDEND_ACCRUAL", "CA-1")]
    assert audit.rows(conn, action="DAILY", limit=1)[0].detail["dividends"] == 1
    assert _daily(conn, broker, MON).notes == ["2026-11-23은 이미 마감한 날이라 대사만 다시 실행했어요."]

    assert _dividends(conn) == _dividends(conn) and _daily(conn, broker, TUE).clean   # nothing on the day between
    cash = books(conn).ledger.cash_balance()
    broker.set_day(WED)
    broker.cash += 381                                                # the broker pays 450 less 63 + 6 tax (R-23)
    rep = _daily(conn, broker, WED)
    assert rep.clean and rep.closed, rep.results
    assert "배당 입금 CA-1 069500: 450원에서 세금 69원을 떼고 381원" in rep.notes
    led = books(conn).ledger
    assert led.cash_balance() == cash + 381 and led.account_balance(Account.DIVIDEND_RECEIVABLE) == 0
    assert led.account_balance(Account.DISTRIBUTION_INCOME) == 450 and led.account_balance(Account.PREPAID_TAX) == 69
    assert _dividends(conn) == [("A0001", 1, "069500", 1, 450, MON, 450, 69, WED, None, "PAID")]
    assert conn.execute("select date, reason, amount, ref_id from cash_ledger where reason = 'DIVIDEND'").fetchall() == [
        (WED, "DIVIDEND", 381, "CA-1")]
    assert _check(conn, "rights").ok and "권리 1건, 계좌별 배당 1건" in _check(conn, "rights").detail
    assert _check(conn, "dividends", "A0001").ok and "배당 1건(입금 1건)" in _check(conn, "dividends", "A0001").detail
    with pytest.raises(OpsError, match="A0001에 이미 입금으로 기록돼서"):
        rights.cancel_action(conn, 1, "늦었다")


def test_an_action_registered_late_is_booked_on_the_day_it_is_first_seen(conn, broker):
    _opened(conn, broker)
    for day in (FRI, MON, TUE):
        assert _daily(conn, broker, day).closed
    rights.add_dividend(conn, "069500", FRI, TUE, 450)                # learned on Wednesday: both dates are closed days
    broker.set_day(WED)
    broker.cash += 381
    rep = _daily(conn, broker, WED)
    assert rep.clean and rep.closed
    assert [n[:5] for n in rep.notes] == ["배당 인식", "배당 입금"]
    assert conn.execute("select date, kind from journal_entries where kind like 'DIVIDEND%' order by entry_id").fetchall() == [
        (WED, "DIVIDEND_ACCRUAL"), (WED, "DIVIDEND")]                 # never inside a closed day (R-78)
    assert _dividends(conn)[0][-1] == "PAID"


def test_an_account_that_held_nothing_on_the_record_date_gets_nothing(conn, broker):
    _opened(conn, broker)
    rights.add_dividend(conn, "069500", D("2026-11-19"), WED, 450)    # the books opened the day after
    for day in (FRI, MON, TUE, WED):
        assert _daily(conn, broker, day).clean
    assert _dividends(conn) == [] and books(conn).ledger.dividends() == {}
    assert _check(conn, "dividends", "A0001").detail == "배당 기록이 아직 없어요"


def test_a_cancelled_action_is_taken_back_by_the_next_daily(conn, broker):
    _opened(conn, broker)
    rights.add_dividend(conn, "069500", FRI, WED, 45)
    _daily(conn, broker, FRI)
    assert _daily(conn, broker, MON).nav.receivables == 45
    rights.cancel_action(conn, 1, "주당 금액 오타")
    rights.add_dividend(conn, "069500", FRI, WED, 450)                # registered again as CA-3
    rep = _daily(conn, broker, TUE)
    assert rep.clean and rep.closed and rep.nav.receivables == 450
    assert [n[:8] for n in rep.notes] == ["배당 인식 취소", "배당 인식 CA"]
    assert [(r[1], r[4], r[-1]) for r in _dividends(conn)] == [(1, 45, "CANCELED"), (3, 450, "ACCRUED")]
    assert _dividends(conn)[0][9] == TUE and books(conn).ledger.account_balance(Account.DISTRIBUTION_INCOME) == 450
    assert _check(conn, "rights").ok and _check(conn, "dividends", "A0001").ok


def test_the_actual_payment_can_be_recorded_by_hand(conn, broker):
    _opened(conn, broker)
    rights.add_dividend(conn, "069500", FRI, WED, 450)
    rights.add_dividend(conn, "069500", D("2026-10-30"), WED, 300)    # CA-2: a record date before the books opened
    for day in (FRI, MON, TUE):
        _daily(conn, broker, day)
    with pytest.raises(OpsError, match="이미 마감한 날"):
        rights.record_payment(books(conn), 1, TUE)
    with pytest.raises(OpsError, match="인식된 적이 없어요"):
        rights.record_payment(books(conn), 2, WED)
    with pytest.raises(OpsError, match="세금은 0 이상"):
        rights.record_payment(books(conn), 1, WED, tax=450)
    paid = rights.record_payment(books(conn), 1, WED, gross=460, tax=70)   # the broker paid 10 won more than expected
    assert (paid.accrued, paid.paid_gross, paid.tax, paid.status) == (450, 460, 70, "PAID")
    never = rights.record_payment(books(conn), 2, WED, gross=300, tax=46)   # the holding from before the ledger
    assert (never.qty, never.accrued, never.paid_gross) == (0, 0, 300)
    with pytest.raises(OpsError, match="이미 입금으로"):
        rights.record_payment(books(conn), 1, WED)
    led = books(conn).ledger
    assert led.account_balance(Account.DISTRIBUTION_INCOME) == 760 and led.account_balance(Account.PREPAID_TAX) == 116
    row = audit.rows(conn, action="DIVIDEND_PAY", limit=2)[1]
    assert (row.account_id, row.target) == ("A0001", "CA-1")
    assert row.detail == {"symbol": "069500", "date": "2026-11-25", "gross": 460, "tax": 70, "accrued": 450}
    broker.set_day(WED)
    broker.cash += 390 + 254
    rep = _daily(conn, broker, WED)                                   # daily finds both already paid and the cash agrees
    assert rep.clean and rep.closed and not any("배당" in n for n in rep.notes)
    assert [(r[1], r[3], r[4], r[6], r[7], r[-1]) for r in _dividends(conn)] == [
        (1, 1, 450, 460, 70, "PAID"), (2, None, 0, 300, 46, "PAID")]
    assert _check(conn, "rights").ok and _check(conn, "dividends", "A0001").ok


def test_each_account_is_entitled_to_what_it_held(two, calendar):   # noqa: F811
    conn, stores, ledgers, (cal, fees, insts) = two
    for inst in insts.values():
        add_instrument(conn, inst)
    record, seen = dt.date(2026, 11, 4), dt.date(2026, 11, 5)        # bought 11-02, settled on the record date
    ledgers["A0003"].execute("X10", dt.date(2026, 11, 3), "E", Side.SELL, 7, 10_000)   # sold, but leaves on 11-05
    rights.add_dividend(conn, "E", record, dt.date(2026, 11, 9), "12.5")
    rights.add_dividend(conn, "A", record, dt.date(2026, 11, 9), 361)
    for acct in ("A0001", "A0002", "A0003"):
        said = rights.apply_due(Books(conn, stores[acct], ledgers[acct], insts, acct), seen)
        assert len(said) == (2 if acct == "A0001" else 1), said
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in _dividends(conn)] == [
        ("A0001", 1, "E", 100, 1_250), ("A0002", 1, "E", 30, 375), ("A0003", 1, "E", 7, 87),   # 87.5: cut to the won
        ("A0001", 2, "A", 10, 3_610)]
    assert ledgers["A0003"].position("E").qty == 0                   # no longer holds it, still entitled (R-58)
    report = integrity_report(conn, cal, fees, insts)
    assert all(c.ok for c in report) and "권리 2건, 계좌별 배당 4건" in next(c for c in report if c.key == "rights").detail


def test_verify_finds_a_dividend_that_does_not_follow_from_the_register(conn, broker):
    _opened(conn, broker)
    rights.add_dividend(conn, "069500", FRI, WED, 450)
    _daily(conn, broker, FRI)
    _daily(conn, broker, MON)
    for tamper, finding in [
            ("update corporate_actions set amount_per_share = 500 where action_id = 1", "인식 금액이 수량 × 주당 배당금과 다름"),
            ("delete from corporate_actions where action_id = 1", "권리 원장에 없음")]:
        with conn.transaction(force_rollback=True):
            conn.execute("alter table corporate_actions disable trigger user")
            conn.execute(tamper)
            bad = _check(conn, "rights")
            assert not bad.ok and "A0001 CA-1" in bad.detail and finding in bad.detail
    assert _check(conn, "rights").ok


# ---------- the console ----------

def test_dividend_commands(conn, broker, capsys):
    broker.set_day(FRI)
    kw = dict(environ=ENV, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda day, h: (lambda: dt.datetime(day.year, day.month, day.day, h, 0))  # noqa: E731
    assert cli.run(["open"], now=at(FRI, 8), **kw) == 0
    assert cli.run(["dividend", "list"], **kw) == 0 and "등록된 권리가 없어요" in capsys.readouterr().out
    assert cli.run(["dividend", "add", "069500", "--record", "2026-11-20", "--pay", "2026-11-25",
                    "--per-share", "450", "--note", "분기 분배금"], **kw) == 0
    out = capsys.readouterr().out
    assert "권리 등록 CA-1: 069500 현금배당 주당 450원, 기준일 2026-11-20, 지급일 2026-11-25" in out
    assert "daily가 2026-11-23부터" in out
    assert cli.run(["dividend", "add", "069500", "--record", "2026-11-20", "--pay", "2026-11-25",
                    "--per-share", "abc"], **kw) == 1 and "숫자예요" in capsys.readouterr().out
    assert cli.run(["dividend", "add", "000000", "--record", "2026-11-20", "--pay", "2026-11-25",
                    "--per-share", "1"], **kw) == 1
    broker.orders[FRI] = {}
    assert cli.run(["daily"], now=at(FRI, 16), **kw) == 0
    broker.set_day(MON)
    broker.orders[MON] = {}
    assert cli.run(["daily"], now=at(MON, 16), **kw) == 0
    assert "배당 인식 CA-1 069500: 기준일 2026-11-20 보유 1주 × 450원 = 450원" in capsys.readouterr().out
    broker.set_day(TUE)
    broker.orders[TUE] = {}
    assert cli.run(["dividend", "pay", "1", "--gross", "450", "--tax", "60"], now=at(TUE, 10), **kw) == 0
    assert "[2026-11-24] 배당 입금 CA-1 069500: 세전 450원, 세금 60원, 예수금 +390원" in capsys.readouterr().out
    assert cli.run(["dividend", "pay", "1"], now=at(TUE, 10), **kw) == 1
    assert cli.run(["dividend", "cancel", "1", "늦은 취소"], **kw) == 1
    assert "A0001에 이미 입금으로 기록돼서 취소할 수 없어요" in capsys.readouterr().out
    assert cli.run(["dividend", "add", "069500", "--record", "2026-12-30", "--pay", "2027-01-05",
                    "--per-share", "12.5"], **kw) == 0
    assert cli.run(["dividend", "cancel", "2", "일정 변경"], **kw) == 0
    capsys.readouterr()
    assert cli.run(["dividend", "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "CA-1  069500 KODEX 200: 주당 450원, 기준일 2026-11-20, 지급일 2026-11-25  (분기 분배금)" in out
    assert "    A0001: 1주, 인식 450원 (2026-11-23), 입금 390원 (세전 450, 세금 60, 2026-11-24)" in out
    assert "CA-2  069500 KODEX 200: 주당 12.5원, 기준일 2026-12-30, 지급일 2027-01-05  [취소: 일정 변경]" in out
    said = [(r.action, r.outcome, r.target) for r in audit.rows(conn, limit=20) if "DIVIDEND" in r.action or "CORP" in r.action]
    assert said[::-1] == [("CORPORATE_ACTION_ADD", "DONE", "CA-1"), ("CORPORATE_ACTION_ADD", "REFUSED", None),
                          ("CORPORATE_ACTION_ADD", "REFUSED", None), ("DIVIDEND_PAY", "DONE", "CA-1"),
                          ("DIVIDEND_PAY", "REFUSED", "CA-1"), ("CORPORATE_ACTION_CANCEL", "REFUSED", "CA-1"),
                          ("CORPORATE_ACTION_ADD", "DONE", "CA-2"), ("CORPORATE_ACTION_CANCEL", "DONE", "CA-2")]
    assert cli.run(["verify"], **kw) == 0
    out = capsys.readouterr().out
    assert "권리: 권리 2건, 계좌별 배당 1건이 권리 원장과 맞음" in out and "배당: 배당 1건(입금 1건) 일치, 미수배당금 0원" in out
