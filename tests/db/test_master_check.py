"""R-91, R-92 against a real books DB: `master check` reads the instruments and closes, writes nothing."""
import datetime as dt

from db.test_intraday import DAY, opened  # noqa: F401
from db.test_ops import URL, books, broker, conn  # noqa: F401
from ministp import __main__ as cli
from ministp.instruments import Market
from ministp.ops import daily_close
from ministp.pricecheck import closes_by_day
from unit.test_master import KOSDAQ, KOSPI, downloads, line


def test_master_check_compares_the_books_with_the_market_master(opened, tmp_path, capsys):  # noqa: F811
    conn, broker = opened
    daily_close(books(conn), broker, DAY)
    close = closes_by_day(books(conn).ledger)[DAY][0]["069500"]
    count = conn.execute("select count(*) from broker_snapshots").fetchone()[0]

    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_MASTER_DIR": str(tmp_path)}
    nxt = lambda: dt.datetime(2026, 11, 24, 8, 30)  # noqa: E731
    kodex = line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=close)
    assert cli.run(["master", "fetch"], environ=env, now=nxt,
                   master_download=downloads(kospi=[KOSPI[0], kodex], kosdaq=KOSDAQ)) == 0
    capsys.readouterr()
    assert cli.run(["master", "check"], environ=env, now=nxt) == 0
    out = capsys.readouterr().out
    assert f"[정상] 기준가 대조: 069500 기준가 {close:,} = 원장 {DAY} 종가" in out and "결과: 정상" in out

    wrong = line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=close + 5)
    later = lambda: dt.datetime(2026, 11, 25, 8, 30)  # noqa: E731
    assert cli.run(["master", "fetch"], environ=env, now=later,
                   master_download=downloads(kospi=[wrong], kosdaq=KOSDAQ)) == 0
    capsys.readouterr()
    assert cli.run(["master", "check"], environ=env, now=later) == 1
    out = capsys.readouterr().out
    assert "≠ 원장" in out and "빠짐 1" in out and "- 005930 삼성전자" in out
    assert conn.execute("select count(*) from broker_snapshots").fetchone()[0] == count   # nothing written
