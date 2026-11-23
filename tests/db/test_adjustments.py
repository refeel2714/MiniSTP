"""수동 조정 on the database (ERD v2.6, R-66): a difference the recon found, booked, and the recon clean again."""
import datetime as dt

import pytest

from ministp import __main__ as cli
from ministp import adjustments, audit, codes
from ministp.ops import OpsError, daily_close, open_books, resolve_break
from ministp.recon import Cause, Status
from ministp.store.integrity import integrity_report

from db.test_multi_account import fresh, two  # noqa: F401, E402
from db.test_ops import CAL, D, FEES, KODEX, URL, books, broker, conn  # noqa: F401, E402
from conftest import calendar, instruments  # noqa: F401, E402

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

FRI, MON, TUE = D("2026-11-20"), D("2026-11-23"), D("2026-11-24")
ENV = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
       "MINISTP_KIS_ACCOUNT": "12345678-01"}
OPENING = 99_919_450


def _opened(conn, broker, day=FRI):
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}


def _daily(conn, broker, day):
    broker.set_day(day)
    broker.orders.setdefault(day, {})
    return daily_close(books(conn), broker, day)


def _check(conn, key, account="A0001"):
    return next(c for c in integrity_report(conn, CAL, FEES, {KODEX.symbol: KODEX})
                if c.key == key and c.account == account)


def _breaks(report):
    return {r.item: r for r in report.results if r.status is Status.BREAK}


def test_a_fee_the_broker_charged_differently_breaks_every_day_until_it_is_adjusted(conn, broker):
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    broker.cash -= 30                                                  # the broker took 30 won more than the books say
    report = _daily(conn, broker, MON)
    assert not report.closed and set(_breaks(report)) == {"CASH_D0", "CASH_PROJECTED"}
    row = conn.execute("select id from recon_results where date = %s and item = 'CASH_D0' and status = 'BREAK'",
                       (MON,)).fetchone()[0]
    resolve_break(conn, row, Cause.FEE_DIFF, "증권사 거래내역: 수수료 30원 더 나감")   # the judgement (R-73)...
    assert set(_breaks(_daily(conn, broker, MON))) == {"CASH_D0", "CASH_PROJECTED"}  # ...does not change the books

    a = adjustments.add(books(conn), MON, "FEE", -30, memo="11-20 매수 수수료 차이", recon_result_id=row, today=MON)
    assert (a.ref, a.reason, a.cash, a.recon_result_id) == ("ADJ-1", "FEE", -30, row)
    assert books(conn).ledger.cash_balance() == OPENING - 30
    assert conn.execute("select date, reason, amount, ref_id from cash_ledger order by id desc limit 1").fetchone() == (
        MON, "ADJUSTMENT", -30, "ADJ-1")
    assert conn.execute("select adjustment_id, date, reason, amount, tax, cash_amount, memo, recon_result_id,"
                        " canceled_on from adjustments_v").fetchall() == [
        (1, MON, "FEE", -30, 0, -30, "11-20 매수 수수료 차이", row, None)]
    said = audit.rows(conn, limit=1)[0]
    assert (said.action, said.outcome, said.account_id, said.target) == ("ADJUSTMENT_ADD", "DONE", "A0001", "ADJ-1")
    assert said.detail == {"date": "2026-11-23", "reason": "FEE", "amount": -30, "cash": -30, "recon_result_id": row,
                           "memo": "11-20 매수 수수료 차이"}

    report = _daily(conn, broker, MON)                                 # struck again, clean, closed (R-75, R-77)
    assert report.closed and not _breaks(report)
    assert _daily(conn, broker, TUE).closed                            # and the next day no longer breaks
    check = _check(conn, "adjustments")
    assert check.ok and "수동 조정 1건(취소 0건) 일치, 예수금 반영 합계 -30원" in check.detail and codes.check(conn) == []


def test_interest_from_the_broker_is_booked_with_its_tax(conn, broker):
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    broker.cash += 10_450                                              # 12,340 of 예탁금 이용료 less 1,890 withheld
    a = adjustments.add(books(conn), MON, "INTEREST", 12_340, tax=1_890, memo="3분기 예탁금 이용료", today=MON)
    assert a.cash == 10_450
    assert dict(conn.execute("select code, balance from account_balances_v where code in ('4040', '5050')"
                             " and account_id = 'A0001'")) == {"4040": 12_340, "5050": 1_890}
    report = _daily(conn, broker, MON)
    assert report.closed and report.nav.net_assets == OPENING + 80_540 + 10_450   # it is income: the NAV moves up
    assert report.nav.units == OPENING + 80_540


def test_an_adjustment_is_refused_unless_it_says_what_and_why(conn, broker):
    with pytest.raises(OpsError, match="아직 시작되지 않았어요"):
        adjustments.add(books(conn), FRI, "FEE", -30, memo="x", today=FRI)
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    for kw, said in ((dict(reason="BONUS", amount=10, memo="x"), "조정 사유는"),
                     (dict(reason="FEE", amount=-30), "메모가 있어야"),
                     (dict(reason="FEE", amount=0, memo="x"), "0이 아니어야"),
                     (dict(reason="INTEREST", amount=-5, memo="x"), "늘어나는 조정만"),
                     (dict(reason="FEE", amount=50, tax=5, memo="x"), "INTEREST에만"),
                     (dict(reason="OTHER", amount=-OPENING - 1, memo="x"), "출금가능금액"),
                     (dict(reason="FEE", amount=-30, memo="x", recon_result_id=999), "불일치 행이 아니에요"),
                     (dict(reason="FEE", amount=-30, memo="x", day=FRI), "이미 마감한 날"),
                     (dict(reason="FEE", amount=-30, memo="x", day=TUE), "아직 오지 않은 날짜")):
        day = kw.pop("day", MON)
        with pytest.raises(OpsError, match=said):
            adjustments.add(books(conn), day, today=MON, **kw)
    match = conn.execute("select id from recon_results where status = 'MATCH' limit 1").fetchone()[0]
    with pytest.raises(OpsError, match="불일치 행이 아니에요"):          # a row that matched has nothing to settle
        adjustments.add(books(conn), MON, "FEE", -30, memo="x", recon_result_id=match, today=MON)
    assert adjustments.list_adjustments(conn) == [] and _check(conn, "adjustments").detail == "수동 조정이 아직 없어요"


def test_a_difference_past_the_line_is_booked_only_when_confirmed(conn, broker):
    """Nothing is tolerated (R-71: 0). The line makes the owner look again before a large "difference" is booked."""
    _opened(conn, broker)
    for reason, amount in (("FEE", -1_001), ("TAX", -1_001), ("DIVIDEND", 1_001), ("WITHHOLDING", -1_001), ("OTHER", 1_001)):
        with pytest.raises(OpsError, match="큰 금액이에요.*--confirm"):
            adjustments.add(books(conn), FRI, reason, amount, memo="x", today=FRI)
    assert adjustments.list_adjustments(conn) == []
    adjustments.add(books(conn), FRI, "FEE", -1_000, memo="기준과 같은 금액", today=FRI)             # at the line: as before
    said = audit.rows(conn, limit=1)[0]
    assert "confirmed" not in said.detail
    a = adjustments.add(books(conn), FRI, "FEE", -1_001, memo="거래내역 확인함", today=FRI, confirmed=True)
    said = audit.rows(conn, limit=1)[0]
    assert (a.ref, said.target, said.detail["confirmed"]) == ("ADJ-2", "ADJ-2", True)
    adjustments.add(books(conn), FRI, "INTEREST", 500_000, tax=77_000, memo="이자는 실제로 받은 돈", today=FRI)
    assert [r[4] for r in adjustments.list_adjustments(conn)] == [-1_000, -1_001, 500_000]


def test_the_break_row_is_a_reference_and_its_judgement_stays_apart(conn, broker):
    """판단과 반영은 독립 (R-66): an adjustment does not judge the break, a judgement does not book."""
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    broker.cash -= 30
    assert not _daily(conn, broker, MON).closed
    row = conn.execute("select id from recon_results where date = %s and item = 'CASH_D0' and status = 'BREAK'",
                       (MON,)).fetchone()[0]
    before = conn.execute("select count(*), max(id) from recon_results").fetchone()
    adjustments.add(books(conn), MON, "FEE", -30, memo="수수료 차이", recon_result_id=row, today=MON)
    assert conn.execute("select count(*), max(id) from recon_results").fetchone() == before   # no RESOLVED row was added
    assert conn.execute("select status from recon_results where id = %s", (row,)).fetchone()[0] == "BREAK"
    assert conn.execute("select count(*) from recon_results where supersedes_id = %s", (row,)).fetchone()[0] == 0


def test_account_codes_outside_the_numbering_rule_are_refused(conn):
    for code in ("6010", "101", "10100", "A010", "0010"):
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into accounts_chart (code, name, normal_side) values (%s, '규칙 밖', 'D')", (code,))
    conn.execute("insert into accounts_chart (code, name, normal_side) values ('5041', '증권거래세', 'D')")   # a 세목 fits


def test_a_cancel_adds_the_opposite_entry_and_links_it(conn, broker):
    _opened(conn, broker)
    a = adjustments.add(books(conn), FRI, "OTHER", 5_000, memo="잘못 들어온 돈으로 보임", today=FRI, confirmed=True)
    with pytest.raises(OpsError, match="ADJ-9 조정이 없어요"):
        adjustments.cancel(books(conn), 9, FRI, "없는 번호", today=FRI)
    with pytest.raises(OpsError, match="취소 사유"):
        adjustments.cancel(books(conn), a.adjustment_id, FRI, " ", today=FRI)
    c = adjustments.cancel(books(conn), a.adjustment_id, FRI, "증권사가 회수함", today=FRI)
    assert (c.canceled_on, c.cancel_memo) == (FRI, "증권사가 회수함") and books(conn).ledger.cash_balance() == OPENING
    kinds = conn.execute("select e.kind, e.source_id, r.kind from journal_entries e"
                         " left join journal_entries r on r.entry_id = e.reverses_entry_id"
                         " order by e.entry_id desc limit 2").fetchall()
    assert kinds == [("ADJUSTMENT_CANCEL", "ADJ-1", "ADJUSTMENT"), ("ADJUSTMENT", "ADJ-1", None)]
    assert adjustments.list_adjustments(conn, "A0001") == [
        ("A0001", 1, FRI, "OTHER", 5_000, 0, 5_000, "잘못 들어온 돈으로 보임", None, FRI, "증권사가 회수함")]
    with pytest.raises(OpsError, match="이미 취소됐어요"):
        adjustments.cancel(books(conn), a.adjustment_id, FRI, "두 번", today=FRI)
    said = audit.rows(conn, limit=1)[0]
    assert (said.action, said.target, said.detail["why"]) == ("ADJUSTMENT_CANCEL", "ADJ-1", "증권사가 회수함")
    assert "수동 조정 1건(취소 1건) 일치, 예수금 반영 합계 +0원" in _check(conn, "adjustments").detail
    assert adjustments.add(books(conn), FRI, "OTHER", 1, memo="다음 번호", today=FRI).ref == "ADJ-2"


def test_adjustment_numbers_are_one_series_for_every_account(two):   # noqa: F811
    from ministp.ops import Books, add_instrument
    conn, stores, ledgers, (cal, fees, insts) = two
    for inst in insts.values():
        add_instrument(conn, inst)
    day = dt.date(2026, 11, 2)
    b = {a: Books(conn, stores[a], ledgers[a], insts, a) for a in ledgers}
    assert [adjustments.add(b[a], day, "OTHER", 10, memo="번호", today=day).ref for a in ("A0002", "A0001", "A0003")] == [
        "ADJ-1", "ADJ-2", "ADJ-3"]
    with pytest.raises(OpsError, match="A0001에 ADJ-1 조정이 없어요"):   # another account's adjustment
        adjustments.cancel(b["A0001"], 1, day, "남의 조정", today=day)
    with pytest.raises(Exception):                                     # one number, one adjustment (the index)
        ledgers["A0001"].adjust(day, 3, "OTHER", 10, memo="같은 번호")
    report = integrity_report(conn, cal, fees, insts)
    ledgers["A0002"].adjust(day, 7, "FEE", -5, memo="남의 불일치", recon_result_id=12345)   # around `adjustments.add`
    bad = next(c for c in integrity_report(conn, cal, fees, insts) if c.key == "adjustments" and c.account == "A0002")
    assert not bad.ok and "불일치 행 12345이 이 계좌의 불일치가 아님" in bad.detail
    assert [c.ok for c in report if c.key == "adjustments"] == [True, True, True]


def test_adjust_commands(conn, broker, capsys):
    broker.set_day(FRI)
    kw = dict(environ=ENV, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda day, h: (lambda: dt.datetime(day.year, day.month, day.day, h, 0))  # noqa: E731
    assert cli.run(["open"], now=at(FRI, 8), **kw) == 0
    assert cli.run(["adjust", "list"], **kw) == 0 and "수동 조정이 아직 없어요" in capsys.readouterr().out
    assert cli.run(["adjust", "add", "interest", "12340", "--tax", "1890", "--memo", "3분기 예탁금 이용료"],
                   now=at(FRI, 9), **kw) == 0
    out = capsys.readouterr().out
    assert "[2026-11-20] 조정 ADJ-1 예탁금 이용료: 세전 +12,340원, 세금 1,890원, 예수금 +10,450원 (A0001)" in out
    assert "예수금 99,929,900원" in out
    assert cli.run(["adjust", "add", "FEE", "-30", "--memo", "수수료 차이"], now=at(FRI, 9), **kw) == 0
    assert "조정 ADJ-2 수수료 차이: 예수금 -30원" in capsys.readouterr().out
    assert cli.run(["adjust", "add", "FEE", "-30", "--memo", " "], now=at(FRI, 9), **kw) == 1
    assert "메모가 있어야" in capsys.readouterr().out
    assert cli.run(["adjust", "add", "FEE", "-30", "--memo", "x", "--break", "77"], now=at(FRI, 9), **kw) == 1
    capsys.readouterr()
    assert cli.run(["adjust", "add", "OTHER", "5000", "--memo", "출처 모르는 입금"], now=at(FRI, 9), **kw) == 1
    assert "OTHER 조정으로는 큰 금액이에요 (+5,000원, 확인 기준 1,000원)" in capsys.readouterr().out
    assert cli.run(["adjust", "add", "OTHER", "5000", "--memo", "출처 모르는 입금", "--confirm"], now=at(FRI, 9), **kw) == 0
    assert cli.run(["adjust", "cancel", "3", "증권사가 회수함"], now=at(FRI, 10), **kw) == 0
    assert cli.run(["adjust", "cancel", "2", "증권사가 돌려줌"], now=at(FRI, 10), **kw) == 0
    assert "[2026-11-20] 조정 취소 ADJ-2 (A0001): 예수금 +30원" in capsys.readouterr().out
    assert cli.run(["adjust", "cancel", "2", "두 번"], now=at(FRI, 10), **kw) == 1
    capsys.readouterr()
    assert cli.run(["adjust", "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "  2026-11-20  ADJ-1 예탁금 이용료: 세전 +12,340원, 세금 1,890원, 예수금 +10,450원  (3분기 예탁금 이용료)" in out
    assert "  2026-11-20  ADJ-2 수수료 차이: 예수금 -30원  (수수료 차이)  [취소 2026-11-20: 증권사가 돌려줌]" in out
    said = [(r.action, r.outcome, r.target) for r in audit.rows(conn, limit=40) if r.action.startswith("ADJUSTMENT")]
    assert said[::-1] == [("ADJUSTMENT_ADD", "DONE", "ADJ-1"), ("ADJUSTMENT_ADD", "DONE", "ADJ-2"),
                          ("ADJUSTMENT_ADD", "REFUSED", None), ("ADJUSTMENT_ADD", "REFUSED", None),
                          ("ADJUSTMENT_ADD", "REFUSED", None), ("ADJUSTMENT_ADD", "DONE", "ADJ-3"),
                          ("ADJUSTMENT_CANCEL", "DONE", "ADJ-3"),
                          ("ADJUSTMENT_CANCEL", "DONE", "ADJ-2"), ("ADJUSTMENT_CANCEL", "REFUSED", "ADJ-2")]
    assert cli.run(["verify"], **kw) == 0
    assert "조정: 수동 조정 3건(취소 2건) 일치, 예수금 반영 합계 +10,450원" in capsys.readouterr().out
    assert cli.run(["code", "list", "ADJUSTMENT_REASON"], **kw) == 0
    assert "INTEREST" in capsys.readouterr().out
