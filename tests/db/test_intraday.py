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


def test_quotes_are_recorded_every_minute_for_ibor_only(opened):
    """R-68: 현재가는 증권사 기록에만 남고, 기준가는 일일 마감의 종가로 내요."""
    from ministp.quotes import latest_quotes
    conn, broker = opened
    clock, out = Clock(), []
    r = runner(conn, broker, clock, out)
    events = conn.execute("select count(*) from ledger_events").fetchone()[0]
    broker.prices["069500"] = 81_000
    clock.t = 59
    r.step(timeout=0)
    assert r.stats.quotes == 0
    clock.t = 60
    r.step(timeout=0)
    assert r.stats.quotes == 1 and r.quotes["069500"].price == 81_000
    payload, = conn.execute("select payload from broker_snapshots where kind = 'INTRADAY_PRICE'").fetchone()
    assert payload == {"at": "2026-11-23T10:00:00", "prices": {"069500": 81_000}, "failed": {}}
    assert conn.execute("select count(*) from ledger_events").fetchone()[0] == events   # 원장에는 없음
    r.print_status()
    assert any("현재가 10:00 81,000" in line and "평가손익 +460" in line for line in out)   # 1주 @80,540
    # 다시 띄워도 오늘 받은 가격부터 시작해요
    assert latest_quotes(conn, DAY)["069500"].price == 81_000
    assert runner(conn, broker, Clock(), []).quotes["069500"].price == 81_000
    # 기준가는 종가로: 마감 때 시세가 80,700이면 그 값이 원장에 들어가요
    broker.prices["069500"] = 80_700
    daily_close(books(conn), broker, DAY)
    from ministp.queries import last_closes
    assert last_closes(books(conn).ledger) == (DAY, {"069500": 80_700})


def test_a_failing_quote_does_not_stop_the_runner(opened):
    conn, broker = opened
    clock, out = Clock(), []
    r = runner(conn, broker, clock, out)

    def broken(symbol):
        raise TimeoutError("시세 응답 없음")
    good, broker.get_price = broker.get_price, broken
    clock.t = 60
    assert r.step(timeout=0) is True
    clock.t = 120
    r.step(timeout=0)
    assert r.stats.quotes == 2 and r.stats.quote_failures == 2 and r.quotes == {}
    assert sum("현재가를 받지 못했어요: 069500" in line for line in out) == 1      # 한 번만 알려요
    failed, = conn.execute("select payload -> 'failed' from broker_snapshots where kind = 'INTRADAY_PRICE'"
                           " order by snapshot_id desc limit 1").fetchone()
    assert failed == {"069500": "시세 응답 없음"}
    broker.get_price = good
    clock.t = 180
    r.step(timeout=0)
    assert r.quotes["069500"].price == 80_540 and any("현재가 다시 받는 중" in line for line in out)


def test_cancel_in_the_runner(opened):
    """R-45: cancel asks the broker first, then waits for the broker's confirmed quantity."""
    conn, broker = opened
    r = runner(conn, broker, Clock(), out := [])
    r.on_command("buy 069500 5 79000")
    cid = next(iter(r.session.oms.orders))
    assert r.on_command("cancel 7") is True and "찾지 못했어요" in out[-1]
    assert r.on_command("cancel 1") is True
    assert ("cancel", "0000000001") in broker.calls
    assert r.session.oms.orders[cid].status.value == "CANCELED"          # the poll right after saw it
    assert any("취소 요청을 증권사가 받았어요" in line for line in out)
    assert r.session.open_orders() == []                                 # no cash reserved any more
    assert r.on_command("cancel 1") is True and "CANCELED 상태라 취소할 수 없어요" in out[-1]
    kind, = conn.execute("select count(*) from broker_snapshots where kind = 'CANCEL_REQUEST'").fetchone()
    assert kind == 1


def test_a_cancel_refused_by_the_broker_changes_nothing(opened):
    conn, broker = opened
    r = runner(conn, broker, Clock(), out := [])
    r.on_command("buy 069500 2 79000")
    broker.fill("0000000001", 79_000)                 # filled before the cancel got there
    r.on_command("cancel 1")
    assert "취소하지 않았어요" in out[-1] and "FILLED" in out[-1]       # the poll first booked the fill
    assert r.books.ledger.position("069500").qty == 3


def test_a_restarted_process_remembers_a_pending_cancel(opened):
    conn, broker = opened
    r = runner(conn, broker, Clock(), [])
    r.on_command("buy 069500 4 79000")
    broker.cancel_order = lambda ack: broker.calls.append(("cancel", ack.order_no))   # confirmation comes later
    r.on_command("cancel 1")
    assert r.session.oms.orders["S20261123-0001"].status.value == "CANCEL_PENDING"
    again = runner(conn, broker, Clock(), out := [])
    assert again.session.oms.orders["S20261123-0001"].status.value == "CANCEL_PENDING"
    again.print_status()
    assert any("(취소 요청 중)" in line for line in out)


def test_cli_cancel(opened, capsys):
    conn, broker = opened
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_KIS_APP_KEY": "k", "MINISTP_KIS_APP_SECRET": "s",
           "MINISTP_KIS_ACCOUNT": "12345678-01"}
    at = lambda: dt.datetime(2026, 11, 23, 10, 0)  # noqa: E731
    assert cli.run(["order", "069500", "buy", "3", "--limit", "79000"], environ=env,
                   client_factory=lambda cfg: broker, now=at, sleep=lambda s: None) == 0
    capsys.readouterr()
    assert cli.run(["cancel", "1"], environ=env, client_factory=lambda cfg: broker, now=at,
                   sleep=lambda s: None) == 0
    assert "취소 확인: 체결 0주, 취소 3주" in capsys.readouterr().out
    assert cli.run(["cancel", "1"], environ=env, client_factory=lambda cfg: broker, now=at,
                   sleep=lambda s: None) == 1
    assert "취소할 수 없어요" in capsys.readouterr().out
def test_settlements_due_today_are_booked_when_the_runner_starts(opened):
    """Found by the random operations test: on a settlement day the broker's 예수금 D+0 already
    includes the settlement, but the ledger only settled at `daily`, so every intraday recon broke."""
    conn, broker = opened
    r = runner(conn, broker, Clock(), [])
    r.on_command("buy 069500 2")
    broker.fill("0000000001", 80_000)
    r.poll()
    daily_close(books(conn), broker, DAY)
    settle_day = D("2026-11-25")                      # T+2
    broker.set_day(settle_day)
    broker.orders[settle_day] = {}
    out = []
    r2 = IntradayRunner(books(conn), broker, settle_day, queue.Queue(), monotonic=Clock(), out=out.append,
                        settle_wait=lambda: None, now=lambda: dt.datetime(2026, 11, 25, 9, 1))
    r2.poll("시작 시 확인")
    assert any(line.startswith("결제 1건 반영") for line in out)
    assert all(x.status.value == "MATCH" for x in r2.recon())        # CASH_D0 matches the broker
    rep = daily_close(books(conn), broker, settle_day)
    assert rep.settled == [] and rep.clean                            # nothing left for the close
