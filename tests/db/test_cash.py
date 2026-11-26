"""입출금·대체 on the database (ERD v2.5, R-56, R-61, R-64, R-65): the event, the rows, the audit line, the recon."""
import datetime as dt
from decimal import Decimal

import pytest

from ministp import __main__ as cli
from ministp import audit, cash, codes
from ministp.accounts import set_status
from ministp.ledger import Side
from ministp.ops import Books, OpsError, add_instrument, daily_close, load_books, open_books, place_order
from ministp.recon import Status
from ministp.store.integrity import integrity_report

from db.test_multi_account import _open_account, fresh, two  # noqa: F401, E402
from db.test_ops import CAL, D, FEES, KODEX, URL, SimBroker, books, broker, conn  # noqa: F401, E402
from conftest import calendar, instruments  # noqa: F401, E402

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

FRI, SAT, MON, TUE, WED = (D(f"2026-11-{n}") for n in (20, 21, 23, 24, 25))
ENV = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
       "MINISTP_KIS_ACCOUNT": "12345678-01"}
OPENING = 99_919_450   # the cash the paper account opens with (test_ops)


def _opened(conn, broker, day=FRI):
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}


def _daily(conn, broker, day):
    broker.set_day(day)
    broker.orders.setdefault(day, {})
    return daily_close(books(conn), broker, day)


def _check(conn, key, account="", insts=None):
    return next(c for c in integrity_report(conn, CAL, FEES, insts or {KODEX.symbol: KODEX})
                if c.key == key and c.account == account)


def _movements(conn):
    return conn.execute("select account_id, date, kind, amount, transfer_id, counter_account from cash_movements_v"
                        " order by event_seq").fetchall()


# ---------- money in ----------

def test_a_deposit_that_is_not_recorded_is_a_break_until_it_is(conn, broker):
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    broker.cash += 5_000_000                                           # the owner pays in at the broker on Monday
    report = _daily(conn, broker, MON)
    assert not report.closed and {r.item for r in report.results if r.status is Status.BREAK} == {
        "CASH_D0", "CASH_PROJECTED"}
    before = report.nav

    m = cash.deposit(books(conn), MON, 5_000_000, "월 적립", today=MON)
    assert (m.kind, m.amount, m.nav_per_1000) == ("DEPOSIT", 5_000_000, Decimal("1000.00"))   # Friday's NAV (R-61)
    assert m.units == 5_000_000 and books(conn).ledger.cash_balance() == OPENING + 5_000_000
    assert conn.execute("select date, reason, amount, ref_id from cash_ledger order by id desc limit 1").fetchone() == (
        MON, "DEPOSIT", 5_000_000, "")
    assert _movements(conn) == [("A0001", FRI, "DEPOSIT", OPENING, None, None),
                                ("A0001", MON, "DEPOSIT", 5_000_000, None, None)]
    row = audit.rows(conn, limit=1)[0]
    assert (row.action, row.outcome, row.account_id, row.target) == ("CASH_DEPOSIT", "DONE", "A0001", None)
    assert row.detail == {"date": "2026-11-23", "amount": 5_000_000, "units": 5_000_000, "nav_per_1000": "1000.00",
                          "memo": "월 적립"}

    report = _daily(conn, broker, MON)                                 # the day was not closed: struck again (R-75)
    assert report.closed and all(r.status is Status.MATCH for r in report.results)
    assert any("다시 산출" in n for n in report.notes)
    assert report.nav.net_assets == before.net_assets + 5_000_000 and report.nav.units == before.units + 5_000_000
    assert report.nav.nav_per_1000 == before.nav_per_1000              # money in does not move the NAV
    assert conn.execute("select units from day_closes where date = %s", (MON,)).fetchone()[0] == report.nav.units
    check = _check(conn, "movements", "A0001")
    assert check.ok and "입출금·대체 2건 일치" in check.detail and codes.check(conn) == []


def test_cash_is_recorded_only_where_the_books_can_take_it(conn, broker):
    with pytest.raises(OpsError, match="아직 시작되지 않았어요"):
        cash.deposit(books(conn), FRI, 1_000, today=FRI)
    _opened(conn, broker)
    assert _daily(conn, broker, FRI).closed
    for day, today, said in ((FRI, MON, "이미 마감한 날"), (TUE, MON, "아직 오지 않은 날짜")):
        with pytest.raises(OpsError, match=said):
            cash.deposit(books(conn), day, 1_000, today=today)
    with pytest.raises(OpsError, match="0보다 커야"):
        cash.withdraw(books(conn), MON, 0, today=MON)
    cash.deposit(books(conn), SAT, 1_000, today=MON)                   # a weekend transfer: any calendar day
    broker.set_day(MON)
    broker.orders[MON] = {}
    place_order(books(conn), broker, MON, "069500", Side.BUY, 100, limit_price=80_000)
    broker.fill("0000000001", 80_000)
    books(conn).ledger.execute("20261123-0000000001-1", MON, "069500", Side.BUY, 100, 80_000)   # the fill, booked
    with pytest.raises(OpsError, match="2026-11-23 날짜의 기록이 이미 있어요"):
        cash.deposit(books(conn), SAT, 1_000, today=MON)
    led = books(conn).ledger
    spoken_for = led.pending_settlements()[0].net
    assert led.withdrawable() == led.cash_balance() - spoken_for       # R-64: the buy settles on Wednesday
    with pytest.raises(OpsError, match=f"출금가능금액은 {led.withdrawable():,}원"):
        cash.withdraw(books(conn), MON, led.withdrawable() + 1, today=MON)
    refused = [r for r in audit.rows(conn, limit=20) if r.action.startswith("CASH")]
    assert [(r.action, r.outcome) for r in refused] == [("CASH_DEPOSIT", "DONE")]   # refusals are the caller's to log
    out = cash.withdraw(books(conn), MON, 1_000_000, "생활비", today=MON)
    assert (out.kind, out.amount, out.units) == ("WITHDRAWAL", -1_000_000, -1_000_000)
    assert conn.execute("select reason, amount from cash_ledger order by id desc limit 1").fetchone() == (
        "WITHDRAWAL", -1_000_000)
    assert audit.rows(conn, limit=1)[0].detail["memo"] == "생활비"
    assert _check(conn, "movements", "A0001").ok and _check(conn, "cash", "A0001").ok


def test_a_suspended_account_takes_money_in_but_lets_none_out(conn, broker):
    _opened(conn, broker)
    now = dt.date.today()                                               # a status starts no earlier than the last one
    set_status(conn, "A0001", "SUSPENDED", now, "본인 확인")
    cash.deposit(books(conn), FRI, 10_000, today=FRI)
    with pytest.raises(OpsError, match="정지 상태"):
        cash.withdraw(books(conn), FRI, 10_000, today=FRI)
    set_status(conn, "A0001", "OPEN", now, "확인 완료")
    cash.withdraw(books(conn), FRI, OPENING + 10_000, today=FRI)       # the cash goes, the holding stays
    assert books(conn).ledger.cash_balance() == 0 and books(conn).ledger.units > 0


# ---------- between two accounts ----------

def _books_of(two_, acct):   # noqa: ANN001
    conn, stores, ledgers, (_, _, insts) = two_
    return Books(conn, stores[acct], ledgers[acct], insts, acct)


def test_a_transfer_is_written_to_both_accounts_in_one_piece(two):   # noqa: F811
    conn, stores, ledgers, (cal, fees, insts) = two
    for inst in insts.values():
        add_instrument(conn, inst)
    day = dt.date(2026, 11, 2)
    a1, a2, a3 = (_books_of(two, a) for a in ("A0001", "A0002", "A0003"))
    room = a1.ledger.withdrawable()
    assert room == 10_000_000 - sum(s.net for s in a1.ledger.pending_settlements())   # two buys wait to settle

    t, sent, got = cash.transfer(a1, a2, day, 2_000_000, "계좌 정리", today=day)
    assert (t.ref, t.from_account, t.to_account, t.amount, t.date) == ("TR-1", "A0001", "A0002", 2_000_000, day)
    assert (sent.kind, sent.amount, sent.counter_account) == ("TRANSFER_OUT", -2_000_000, "A0002")
    assert (got.kind, got.amount, got.counter_account) == ("TRANSFER_IN", 2_000_000, "A0001")
    assert (a1.ledger.cash_balance(), a2.ledger.cash_balance()) == (8_000_000, 7_000_000)
    assert cash.list_transfers(conn) == [t]
    assert _movements(conn)[-2:] == [("A0001", day, "TRANSFER_OUT", -2_000_000, 1, "A0002"),
                                     ("A0002", day, "TRANSFER_IN", 2_000_000, 1, "A0001")]
    assert conn.execute("select account_id, reason, amount, ref_id from cash_ledger order by id desc limit 2"
                        ).fetchall() == [("A0002", "TRANSFER_IN", 2_000_000, "TR-1"),
                                         ("A0001", "TRANSFER_OUT", -2_000_000, "TR-1")]
    row = audit.rows(conn, limit=1)[0]
    assert (row.action, row.outcome, row.account_id, row.target) == ("CASH_TRANSFER", "DONE", "A0001", "TR-1")
    assert row.detail["to"] == "A0002" and row.detail["amount"] == 2_000_000 and row.detail["memo"] == "계좌 정리"

    for src, dst, amount, said in ((a1, a1, 1, "같아요"), (a1, a3, 1, "같은 법인의 계좌 사이에서만"),
                                   (a1, a2, room, "출금가능금액")):
        with pytest.raises(OpsError, match=said):
            cash.transfer(src, dst, day, amount, today=day)
    assert cash.transfer(a2, a1, day, 500_000, today=day)[0].ref == "TR-2"
    report = integrity_report(conn, cal, fees, insts)
    assert all(c.ok for c in report), [c for c in report if not c.ok]
    assert "대체 2건의 보낸 쪽과 받은 쪽이 맞음 (합계 2,500,000원)" in next(c for c in report if c.key == "transfers").detail


def test_a_transfer_that_cannot_reach_the_other_account_leaves_nothing(two):   # noqa: F811
    conn, stores, ledgers, _ = two
    day = dt.date(2026, 11, 2)
    a1, a2 = _books_of(two, "A0001"), _books_of(two, "A0002")
    events = conn.execute("select count(*) from ledger_events").fetchone()[0]
    a2.status = "CLOSED"                                               # the receiving side cannot take it
    with pytest.raises(OpsError, match="해지된 계좌"):
        cash.transfer(a1, a2, day, 1_000, today=day)
    a2.status = "OPEN"
    conn.execute("alter table cash_ledger add constraint no_transfer_in check (reason <> 'TRANSFER_IN')")
    with pytest.raises(Exception):                                     # the second half fails inside the database
        cash.transfer(a1, a2, day, 1_000, today=day)
    conn.execute("alter table cash_ledger drop constraint no_transfer_in")
    assert conn.execute("select count(*) from ledger_events").fetchone()[0] == events   # the first half went with it
    assert cash.list_transfers(conn) == [] and not [r for r in audit.rows(conn, limit=5) if r.action == "CASH_TRANSFER"]


def test_verify_finds_a_transfer_whose_halves_do_not_match(two):   # noqa: F811
    conn, stores, ledgers, (cal, fees, insts) = two
    for inst in insts.values():
        add_instrument(conn, inst)
    day = dt.date(2026, 11, 2)
    ledgers["A0001"].transfer_out(day, 1, "A0002", 1_000)             # written around `cash.transfer`
    bad = _check(conn, "transfers", insts=insts)
    assert not bad.ok and "TR-1 보낸 쪽과 받은 쪽이 하나씩이 아님" in bad.detail
    ledgers["A0002"].transfer_in(day, 1, "A0001", 1_000)
    assert _check(conn, "transfers", insts=insts).ok
    ledgers["A0001"].transfer_out(day, 2, "A0002", 1_000)
    ledgers["A0002"].transfer_in(day, 2, "A0001", 900)
    assert "TR-2 보낸 금액·날짜와 받은 금액·날짜가 다름" in _check(conn, "transfers", insts=insts).detail
    ledgers["A0001"].transfer_out(day, 3, "A0003", 1_000)
    ledgers["A0003"].transfer_in(day, 3, "A0001", 1_000)
    assert "TR-3 법인이 다른 계좌 사이의 대체" in _check(conn, "transfers", insts=insts).detail
    with pytest.raises(Exception):                                     # one number, one sending event (the index)
        ledgers["A0001"].transfer_out(day, 3, "A0002", 1)


# ---------- an account that starts empty ----------

def test_an_empty_account_opens_with_no_event_and_starts_with_a_transfer(conn, broker):
    _opened(conn, broker)
    _open_account(conn, "A0002")
    empty = SimBroker(cash=0)
    empty.set_day(FRI)
    b2 = lambda: load_books(conn, CAL, FEES, "A0002")  # noqa: E731
    with pytest.raises(OpsError, match="아직 시작되지 않았어요"):
        cash.transfer(books(conn), b2(), FRI, 1_000_000, today=FRI)
    open_books(b2(), empty, FRI)
    assert b2().ledger.events == ()
    with pytest.raises(OpsError, match="이미 시작됐어요"):
        open_books(b2(), empty, FRI)
    cash.transfer(books(conn), b2(), FRI, 1_000_000, today=FRI)
    empty.cash += 1_000_000
    assert b2().ledger.cash_balance() == 1_000_000 and b2().ledger.units == 1_000_000
    empty.orders[FRI] = {}
    report = daily_close(b2(), empty, FRI)
    assert report.closed and report.nav.nav_per_1000 == Decimal("1000.00")


# ---------- the console ----------

def test_cash_commands(conn, broker, capsys):
    broker.set_day(FRI)
    kw = dict(environ=ENV, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda day, h: (lambda: dt.datetime(day.year, day.month, day.day, h, 0))  # noqa: E731
    assert cli.run(["cash", "deposit", "1000"], now=at(FRI, 8), **kw) == 1
    assert "먼저 open --account A0001" in capsys.readouterr().out
    assert cli.run(["open"], now=at(FRI, 8), **kw) == 0
    capsys.readouterr()
    assert cli.run(["cash", "deposit", "5000000", "--memo", "월 적립"], now=at(FRI, 9), **kw) == 0
    out = capsys.readouterr().out
    assert "[2026-11-20] 입금 5,000,000원 (A0001): 예수금 104,919,450원, 좌수 +5,000,000좌 (기준가 1000.00 적용)" in out
    assert "다음 daily의 대사" in out
    assert cli.run(["cash", "withdraw", "200000000"], now=at(FRI, 9), **kw) == 1
    assert "출금가능금액은 104,919,450원" in capsys.readouterr().out
    assert cli.run(["cash", "withdraw", "919450", "--date", "2026-11-20"], now=at(MON, 9), **kw) == 0
    assert "[2026-11-20] 출금 919,450원 (A0001): 예수금 104,000,000원, 출금가능 104,000,000원, 좌수 -919,450좌" in (
        capsys.readouterr().out)
    assert cli.run(["company", "add", "C0002", "둘째"], **kw) == 0
    assert cli.run(["account", "add", "A0002", "C0001", "두 번째 계좌", "--credential", "KIS_2"], now=at(FRI, 9), **kw) == 0
    assert cli.run(["account", "add", "A0003", "C0002", "남의 계좌", "--credential", "KIS_3"], now=at(FRI, 9), **kw) == 0
    capsys.readouterr()
    assert cli.run(["cash", "transfer", "4000000", "--to", "A0003"], now=at(FRI, 10), **kw) == 1
    assert cli.run(["cash", "transfer", "4000000", "--to", "A0009"], now=at(FRI, 10), **kw) == 1
    assert cli.run(["cash", "transfer", "4000000", "--to", "A0002"], now=at(FRI, 10), **kw) == 1
    assert "A0002의 원장이 아직 시작되지 않았어요" in capsys.readouterr().out
    conn.execute("insert into broker_snapshots (date, kind, payload, account_id) values (%s, 'OPENING_BALANCE', '[]', 'A0002')",
                 (FRI,))                                               # `open --account A0002` on an empty account
    assert cli.run(["cash", "transfer", "4000000", "--to", "A0002", "--memo", "나눠 담기"], now=at(FRI, 10), **kw) == 0
    out = capsys.readouterr().out
    assert "[2026-11-20] 대체 TR-1: A0001 → A0002 4,000,000원" in out
    assert "  A0001: 예수금 100,000,000원, 좌수 -4,000,000좌 (기준가 1000.00 적용)" in out
    assert "  A0002: 예수금 4,000,000원, 좌수 +4,000,000좌 (기준가 1000.00 적용)" in out
    assert cli.run(["cash", "list"], **kw) == 0
    out = capsys.readouterr().out
    assert "예수금 D+0 100,000,000  결제 후 100,000,000  출금가능 100,000,000  좌수 100,080,540" in out
    assert "  2026-11-20  입금  +5,000,000원  좌수 +5,000,000좌 (기준가 1000.00 적용)" in out
    assert "  2026-11-20  출금  -919,450원  좌수 -919,450좌 (기준가 1000.00 적용)" in out
    assert "  2026-11-20  대체 출금 TR-1 → A0002  -4,000,000원  좌수 -4,000,000좌 (기준가 1000.00 적용)" in out
    assert cli.run(["cash", "list", "--account", "A0002"], **kw) == 0
    assert "  2026-11-20  대체 입금 TR-1 ← A0001  +4,000,000원" in capsys.readouterr().out
    assert cli.run(["status"], now=at(FRI, 10), **kw) == 0
    assert "예수금 D+0 100,000,000  결제 후 100,000,000  출금가능 100,000,000" in capsys.readouterr().out
    said = [(r.action, r.outcome, r.account_id, r.target) for r in audit.rows(conn, limit=30) if r.action.startswith("CASH")]
    assert said[::-1] == [("CASH_DEPOSIT", "REFUSED", "A0001", None), ("CASH_DEPOSIT", "DONE", "A0001", None),
                          ("CASH_WITHDRAW", "REFUSED", "A0001", None), ("CASH_WITHDRAW", "DONE", "A0001", None),
                          ("CASH_TRANSFER", "REFUSED", "A0001", None), ("CASH_TRANSFER", "REFUSED", "A0001", None),
                          ("CASH_TRANSFER", "REFUSED", "A0001", None), ("CASH_TRANSFER", "DONE", "A0001", "TR-1")]
    refused = [r for r in audit.rows(conn, limit=30) if r.action == "CASH_TRANSFER" and r.outcome == "REFUSED"][-1]
    assert refused.detail == {"date": "2026-11-20", "amount": 4_000_000, "to": "A0003"}
    assert cli.run(["verify"], **kw) == 0
    out = capsys.readouterr().out
    assert "대체: 대체 1건의 보낸 쪽과 받은 쪽이 맞음" in out and "입출금: 입출금·대체 4건 일치" in out
