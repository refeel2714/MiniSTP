"""Real-time IBOR runner (spec D6) against the simulated paper account."""
import datetime as dt
import queue

import pytest

from db.test_ops import CAL, D, FEES, URL, SimBroker, books, conn, broker  # noqa: F401
from ministp import __main__ as cli
from ministp.broker.kis_ws import FillNotice
from ministp.ops import IntradayRunner, OpsError, acquire_writer, clean_streak, daily_close, open_books

psycopg = pytest.importorskip("psycopg")
pytestmark = [pytest.mark.db, pytest.mark.skipif(not URL, reason="MINISTP_TEST_DATABASE_URL not set")]
DAY = D("2026-11-23")


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def opened(conn, broker):  # noqa: F811
    broker.set_day(D("2026-11-20"))
    open_books(books(conn), broker, D("2026-11-20"))
    broker.set_day(DAY)
    broker.orders[DAY] = {}
    return conn, broker


def runner(conn, broker, clock, out):  # noqa: F811
    return IntradayRunner(books(conn), broker, DAY, queue.Queue(), monotonic=clock,
                          out=out.append, settle_wait=lambda: None,
                          now=lambda: dt.datetime(2026, 11, 23, 10, 0))


def notice(no, qty, price, fill=True):
    return FillNotice(no, "069500", "BUY", qty, price, "100001", fill, False)


def test_fill_notice_books_immediately(opened):
    conn, broker = opened
    clock, out = Clock(), []
    r = runner(conn, broker, clock, out)
    assert r.on_command("buy 069500 2") is True
    assert broker.calls[-1] == ("order", "069500", "BUY", 2)
    broker.fill("0000000001", 80_000)
    r.events.put(("notice", notice("0000000001", 2, 80_000), dt.datetime(2026, 11, 23, 10, 0, 1)))
    r.step(timeout=0)
    assert r.books.ledger.position("069500").qty == 3          # IBOR moved at once
    assert r.stats.fills_booked == 1 and len(r.stats.latencies_ms) == 1
    kind, = conn.execute("select kind from broker_snapshots order by snapshot_id desc limit 1").fetchone()
    assert kind == "FILL_NOTICE"
    # duplicate notice: nothing more to book
    r.events.put(("notice", notice("0000000001", 2, 80_000), dt.datetime(2026, 11, 23, 10, 0, 2)))
    r.step(timeout=0)
    assert r.books.ledger.position("069500").qty == 3
    # IBOR == broker intraday
    assert all(x.status.value == "MATCH" for x in r.recon())
    r.print_status()
    assert any("주문가능" in line for line in out)


def test_missed_notice_is_caught_by_poll_and_reconnect(opened):
    conn, broker = opened
    clock, out = Clock(), []
    r = runner(conn, broker, clock, out)
    r.on_command("buy 069500 1")
    broker.fill("0000000001", 80_100)  # notice lost
    r.events.put(("feed", "down", "lost", dt.datetime(2026, 11, 23, 10, 1)))
    r.step(timeout=0)
    assert r.books.ledger.position("069500").qty == 1
    clock.t = 61
    r.step(timeout=0)                  # periodic poll
    assert r.books.ledger.position("069500").qty == 2 and r.stats.polls_booked == 1


def test_intraday_recon_does_not_count_for_the_gate(opened):
    conn, broker = opened
    clock, out = Clock(), []
    broker.cash_skew = -5
    r = runner(conn, broker, clock, out)
    clock.t = 301
    r.step(timeout=0)
    assert r.stats.recon_breaks == 2
    broker.cash_skew = 0
    rep = daily_close(books(conn), broker, DAY)
    assert rep.clean
    assert clean_streak(conn, CAL, DAY) == 1


def test_console_commands(opened):
    conn, broker = opened
    r = runner(conn, broker, Clock(), out := [])
    assert r.on_command("buy 999999 1") is True and "종목 등록" in out[-1]
    assert r.on_command("hello") is True and "명령:" in out[-1]
    assert r.on_command("buy 069500 x") is True and "오류" in out[-1]
    assert r.on_command("quit") is False


def test_single_writer(opened):
    conn, _ = opened
    with psycopg.connect(URL, autocommit=True) as a, psycopg.connect(URL, autocommit=True) as b:
        acquire_writer(a)
        with pytest.raises(OpsError, match="다른 MiniSTP"):
            acquire_writer(b)


def test_cli_run(opened, capsys):
    conn, broker = opened
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01", "MINISTP_KIS_HTS_ID": "myhts"}
    times = iter([dt.datetime(2026, 11, 23, 10, 0)] * 3 + [dt.datetime(2026, 11, 23, 15, 31)] * 50)

    class Feed:
        def __init__(self, q):
            self.q = q

        def start(self):
            self.q.put(("feed", "up", "ok", dt.datetime(2026, 11, 23, 10, 0)))

        def stop(self):
            pass

    def console(q):
        q.put(("command", "status", dt.datetime(2026, 11, 23, 10, 0)))

    rc = cli.run(["run"], environ=env, client_factory=lambda cfg: broker, now=lambda: next(times),
                 feed_factory=Feed, console=console)
    assert rc == 0
    out = capsys.readouterr().out
    assert "장중 실행 시작" in out and "장중 실행 종료" in out
