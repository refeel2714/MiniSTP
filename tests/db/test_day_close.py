"""일 마감 on the database (ERD v2.2, R-77~79): `daily` closes a clean day, the database locks it,
a late fill becomes a correction, and the ABOR views agree with a replay up to the close."""
import datetime as dt

import pytest

from ministp import __main__ as cli
from ministp.ledger import DayClosedEvent, LateExecutionEvent, Side
from ministp.ops import OpsError, close_books, daily_close, open_books, place_order, resolve_break
from ministp.recon import Cause, Status
from ministp.store import migrate as mg
from ministp.store.integrity import integrity_report

from db.test_multi_account import _restore_v1_1  # noqa: E402
from db.test_ops import CAL, D, FEES, KODEX, URL, SimBroker, books, broker, conn  # noqa: F401, E402

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]

DAY, NEXT = D("2026-11-20"), D("2026-11-23")     # a Friday and the Monday after it


def _closes(conn):
    return conn.execute("select date, event_seq, net_assets, units, nav_per_1000 from day_closes order by date").fetchall()


def _opened(conn, broker, day=DAY):
    broker.set_day(day)
    open_books(books(conn), broker, day)
    broker.orders[day] = {}


def _check(conn, key):
    return next(c for c in integrity_report(conn, CAL, FEES, {KODEX.symbol: KODEX}) if c.key == key)


def test_daily_closes_a_clean_day_and_the_row_carries_its_nav(conn, broker):
    _opened(conn, broker)
    rep = daily_close(books(conn), broker, DAY)
    assert rep.clean and rep.closed
    led = books(conn).ledger
    assert led.closed_through == DAY and isinstance(led.events[-1], DayClosedEvent)
    seq = conn.execute("select max(seq) from ledger_events").fetchone()[0]
    assert _closes(conn) == [(DAY, seq, rep.nav.net_assets, rep.nav.units, rep.nav.nav_per_1000)]
    assert conn.execute("select date, net_assets from last_close_v").fetchall() == [(DAY, rep.nav.net_assets)]
    assert conn.execute("select count(*) from unclosed_events_v").fetchone()[0] == 0

    again = daily_close(books(conn), broker, DAY)                     # the same evening: recon only
    assert again.clean and not again.closed and again.nav is None and "이미 마감한 날" in again.notes[0]
    assert len(_closes(conn)) == 1

    broker.set_day(NEXT)
    assert daily_close(books(conn), broker, NEXT).closed
    assert [r[0] for r in _closes(conn)] == [DAY, NEXT] and books(conn).ledger.closed_through == NEXT
    assert _check(conn, "closes").ok and "마감 2일" in _check(conn, "closes").detail


def test_a_day_with_a_break_waits_for_the_owner_then_closes_by_hand(conn, broker):
    _opened(conn, broker)
    broker.cash_skew = -5
    rep = daily_close(books(conn), broker, DAY)
    assert not rep.clean and not rep.closed and "close로 마감" in rep.notes[-1]
    assert _closes(conn) == [] and books(conn).ledger.closed_through is None
    with pytest.raises(OpsError, match="판단하지 않은 불일치가 2건"):
        close_books(books(conn), DAY)
    with pytest.raises(OpsError, match="대사 기록이 없어요"):
        close_books(books(conn), NEXT)
    for (i,) in conn.execute("select id from recon_results where status = 'BREAK'").fetchall():
        resolve_break(conn, i, Cause.FEE_DIFF, "모의계좌 수수료 확인")
    nav = close_books(books(conn), DAY)                               # the day the gate counts is the day that closes
    assert nav == rep.nav and [r[0] for r in _closes(conn)] == [DAY]
    with pytest.raises(OpsError, match="이미 마감한 날"):
        close_books(books(conn), DAY)


def test_the_database_itself_refuses_an_entry_on_a_closed_day(conn, broker):
    _opened(conn, broker)
    daily_close(books(conn), broker, DAY)
    seq = conn.execute("select max(seq) from ledger_events").fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation, match="inside a closed day"):
        conn.execute("insert into journal_entries (event_seq, date, kind, source_id, account_id)"
                     " values (%s, %s, 'DEPOSIT', 'x', 'A0001')", (seq, DAY))
    with pytest.raises(psycopg.errors.CheckViolation, match="inside a closed day"):
        conn.execute("insert into cash_ledger (event_seq, date, reason, amount, ref_id, account_id)"
                     " values (%s, %s, 'DEPOSIT', 1, 'x', 'A0001')", (seq, DAY - dt.timedelta(days=3)))
    with pytest.raises(psycopg.errors.CheckViolation, match="inside a closed day"):   # no second or earlier close
        conn.execute("insert into day_closes (account_id, date, event_seq, net_assets, units, nav_per_1000)"
                     " values ('A0001', %s, %s, 1, 1, 1000)", (DAY, seq))
    for change in ("update day_closes set net_assets = 0", "delete from day_closes", "truncate day_closes"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(change)
    conn.execute("insert into cash_ledger (event_seq, date, reason, amount, ref_id, account_id)"   # the open day is open
                 " values (%s, %s, 'DEPOSIT', 0, 'x', 'A0001')", (seq, NEXT))


def test_a_fill_reported_after_the_close_is_booked_as_a_correction(conn, broker):
    _opened(conn, broker)
    _, cid = place_order(books(conn), broker, DAY, "069500", Side.BUY, 2, limit_price=80_000)
    first = daily_close(books(conn), broker, DAY)                     # nothing filled yet: clean, closed
    assert first.closed and first.booked == []
    broker.fill("0000000001", 80_000)                                 # the fill turns up after the close

    rep = daily_close(books(conn), broker, DAY)
    assert rep.booked == ["20261120-0000000001-1"] and rep.clean and not rep.closed
    assert [a.kind for a in rep.anomalies] == ["LATE_FILL"]
    assert any("마감한 날이라 2026-11-23 장부에 정정으로 기록" in n for n in rep.notes)
    led = books(conn).ledger                                          # reloaded: the stored rows verify against a replay
    late = led.events[-1]
    assert isinstance(late, LateExecutionEvent) and (late.trade_date, late.booked_on) == (DAY, NEXT)
    assert conn.execute("select date, kind from journal_entries order by entry_id desc limit 1").fetchone() == (
        NEXT, "LATE_EXECUTION")
    assert conn.execute("select trade_date, qty_delta from position_ledger order by id desc limit 1").fetchone() == (
        DAY, 2)

    # ABOR: the closed day is as it was. IBOR has the fill. The difference is exactly the unclosed event.
    assert conn.execute("select closed_date, symbol, qty from abor_positions_v").fetchall() == [(DAY, "069500", 1)]
    assert conn.execute("select symbol, qty from positions_v").fetchall() == [("069500", 3)]
    assert conn.execute("select event_type from unclosed_events_v").fetchall() == [("LateExecutionEvent",)]
    assert led.abor().positions()["069500"].qty == 1 and _closes(conn)[0][2] == first.nav.net_assets
    assert _check(conn, "closes").ok and "그 뒤 이벤트 1건" in _check(conn, "closes").detail

    broker.set_day(NEXT)                                              # the next day values it and closes as usual
    rep = daily_close(books(conn), broker, NEXT)
    assert rep.clean and rep.closed and rep.nav.market_value == 3 * 80_540
    broker.set_day(D("2026-11-24"))                                   # T+2 from the day it really traded
    assert daily_close(books(conn), broker, D("2026-11-24")).settled == ["20261120-0000000001-1"]


def test_before_the_close_a_late_fill_is_an_ordinary_one_and_the_nav_is_struck_again(conn, broker):
    _opened(conn, broker)
    _, cid = place_order(books(conn), broker, DAY, "069500", Side.BUY, 2, limit_price=80_000)
    broker.cash_skew = -5                                             # a break keeps the day open
    first = daily_close(books(conn), broker, DAY)
    assert not first.closed
    broker.cash_skew = 0
    broker.fill("0000000001", 80_000)
    rep = daily_close(books(conn), broker, DAY)
    assert rep.booked and rep.anomalies == [] and "기준가를 다시 산출" in " ".join(rep.notes)
    assert rep.nav.market_value == first.nav.market_value + 2 * 80_540
    assert rep.clean and rep.closed                                   # this run is clean, so it closes
    led = books(conn).ledger
    assert not isinstance(led.events[-2], LateExecutionEvent) and [r.date for r in led.nav_history()] == [DAY]
    assert _closes(conn)[0][2] == rep.nav.net_assets                  # closed at the NAV that includes the fill


def test_the_integrity_report_finds_a_close_that_is_not_what_the_events_say(conn, broker):
    _opened(conn, broker)
    daily_close(books(conn), broker, DAY)
    with conn.transaction(force_rollback=True):
        conn.execute("alter table day_closes disable trigger user")
        conn.execute("update day_closes set nav_per_1000 = nav_per_1000 + 1")
        bad = _check(conn, "closes")
        assert not bad.ok and "저장에만 2026-11-20" in bad.detail
    with conn.transaction(force_rollback=True):
        conn.execute("alter table journal_entries disable trigger journal_entries_closed_day")
        assert not _check(conn, "guards").ok
    assert _check(conn, "closes").ok and _check(conn, "guards").ok


def test_close_and_status_commands(conn, broker, capsys):
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01"}
    broker.set_day(DAY)
    kw = dict(environ=env, client_factory=lambda cfg: broker, sleep=lambda s: None)
    at = lambda h, m: (lambda: dt.datetime(2026, 11, 20, h, m))  # noqa: E731
    assert cli.run(["open"], now=at(8, 0), **kw) == 0
    assert cli.run(["status"], now=at(8, 5), **kw) == 0
    assert "장부 마감: 아직 마감한 날이 없어요" in capsys.readouterr().out
    broker.cash_skew = -5
    assert cli.run(["daily"], now=at(16, 0), **kw) == 3
    assert "장부를 마감하지 않았어요" in capsys.readouterr().out
    assert cli.run(["close"], now=at(16, 5), **kw) == 1
    assert "판단하지 않은 불일치가 2건" in capsys.readouterr().out
    for (i,) in conn.execute("select id from recon_results where status = 'BREAK'").fetchall():
        assert cli.run(["resolve", str(i), "FEE_DIFF", "수수료 확인"], now=at(16, 6), **kw) == 0
    assert cli.run(["close", "--date", "2026-11-20"], now=at(16, 10), **kw) == 0
    assert "[2026-11-20] 장부 마감: 순자산" in capsys.readouterr().out
    assert cli.run(["status"], now=at(16, 15), **kw) == 0
    assert "장부 마감: 2026-11-20까지 (1일), 그 뒤 이벤트 0건" in capsys.readouterr().out
    broker.cash_skew = 0
    broker.set_day(NEXT)
    monday = lambda: dt.datetime(2026, 11, 23, 16, 0)  # noqa: E731
    assert cli.run(["daily"], now=monday, **kw) == 0
    assert "장부 마감: 2026-11-23까지. 이 날짜로는 더 기록되지 않아요." in capsys.readouterr().out


def test_a_migrated_ledger_closes_its_next_day_and_keeps_the_old_days_open_to_nothing(conn):
    """0003 on the v1.1 sample: no close exists yet, so the lock holds nothing until the first `daily`."""
    _restore_v1_1()
    with psycopg.connect(URL, autocommit=True) as old:
        assert [r.migration.version for r in mg.migrate(old)] == [1, 2, 3, 4, 5]
        assert old.execute("select count(*) from day_closes").fetchone()[0] == 0
        events = old.execute("select count(*) from ledger_events").fetchone()[0]
        assert old.execute("select count(*) from unclosed_events_v").fetchone()[0] == events
        from ministp.sample import SAMPLE_INSTRUMENTS
        from ministp.broker.sim import SimBroker as Sim
        led = books(old).ledger
        last = max(e.date for e in led.events if hasattr(e, "date"))
        day = CAL.add_business_days(last, 1)
        sim = Sim(CAL, FEES, {i.symbol: i for i, _ in SAMPLE_INSTRUMENTS}, led.cash_balance(),
                  {i.symbol: p for i, p in SAMPLE_INSTRUMENTS}, day)
        sim.positions = {s: [p.qty, p.book_cost] for s, p in led.positions().items()}
        sim.pending = [(s.settle_date, s.signed) for s in led.pending_settlements()]
        sim.set_day(day)                                              # what is due by then has settled at the broker
        rep = daily_close(books(old), sim, day)
        assert rep.clean and rep.closed, (rep.results, rep.notes)
        assert old.execute("select date from last_close_v").fetchone() == (day,)
        assert old.execute("select count(*) from unclosed_events_v").fetchone()[0] == 0
        assert all(c.ok for c in integrity_report(old, CAL, FEES, books(old).instruments))
