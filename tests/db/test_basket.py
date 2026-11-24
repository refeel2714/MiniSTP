"""바스켓 주문 시뮬레이션 명령 (R-97): DB에서 계좌 모습을 읽어 계산만 하고, DB에는 아무것도 쓰지 않아요."""
import datetime as dt
from decimal import Decimal

from db.test_erp import DAY, _a_day
from db.test_ops import URL, books, broker, conn  # noqa: F401
from ministp import __main__ as cli
from ministp.basket import load_portfolio, simulate, default_targets


def _counts(conn):
    return conn.execute("select (select count(*) from ledger_events), (select count(*) from broker_snapshots),"
                        " (select count(*) from oms_orders)").fetchone()


def test_portfolio_from_the_db_and_default_basket(conn, broker):
    _a_day(conn, broker)
    pf = load_portfolio(books(conn), conn, DAY, dt.datetime(2026, 11, 20, 16))
    assert pf.held_qty == {"069500": books(conn).ledger.position("069500").qty} and pf.prices["069500"] > 0
    assert pf.available_cash == books(conn).ledger.projected_cash()
    targets = default_targets(pf)
    assert [(t.symbol, t.weight) for t in targets] == [("069500", Decimal("100.00"))]
    r = simulate(pf, targets, 1_000_000)
    assert r.lines[0].qty == 1_000_000 // r.lines[0].order_price


def test_command_prints_the_plan_saves_csv_and_writes_nothing(conn, broker, tmp_path, capsys):
    _a_day(conn, broker)
    before = _counts(conn)
    basket = tmp_path / "b.csv"
    basket.write_text("종목코드,편입비\n069500,100\n", encoding="utf-8")
    out = tmp_path / "plan.csv"
    rc = cli.run(["basket", "simulate", str(basket), "--amount", "1000000", "--limit", "100", "--out", str(out)],
                 environ={"MINISTP_DATABASE_URL": URL}, now=lambda: dt.datetime(2026, 11, 20, 16))
    text = capsys.readouterr().out
    assert rc == 0 and out.exists() and "069500" in text and "남는 금액" in text
    assert _counts(conn) == before
    rc = cli.run(["basket", "simulate", "--amount", "40000000"], environ={"MINISTP_DATABASE_URL": URL},
                 now=lambda: dt.datetime(2026, 11, 20, 16))
    text = capsys.readouterr().out
    assert rc == 2 and "현재 편입비 그대로" in text and "종목당 한도" in text   # 4천만 원을 한 종목에 더하면 한도 30%에 걸려요
    basket.write_text("종목코드,편입비\n069500,50\n", encoding="utf-8")
    rc = cli.run(["basket", "simulate", str(basket), "--amount", "1000000"], environ={"MINISTP_DATABASE_URL": URL})
    assert rc == 1 and "100%가 아니에요" in capsys.readouterr().out
