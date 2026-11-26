"""R-94, R-95: `master load` puts the fetched market master into the database, one row per change."""
import datetime as dt

import psycopg
import pytest

from db.test_ops import URL, conn  # noqa: F401
from ministp import __main__ as cli
from ministp import codes, master, master_db
from ministp.instruments import Market
from ministp.store.integrity import check_master
from unit.test_master import KOSDAQ, KOSPI, downloads, line

D1, D2, D3 = dt.date(2026, 11, 24), dt.date(2026, 11, 25), dt.date(2026, 11, 26)


def at(day):
    return lambda: dt.datetime.combine(day, dt.time(8, 30))


@pytest.fixture
def env(tmp_path):
    return {"MINISTP_DATABASE_URL": URL, "MINISTP_MASTER_DIR": str(tmp_path)}


def fetch(env, day, kospi, kosdaq=KOSDAQ):
    assert cli.run(["master", "fetch"], environ=env, now=at(day), master_download=downloads(kospi, kosdaq)) == 0


def history(conn):  # noqa: F811
    return conn.execute("select symbol, valid_from, listed, name, halted from instrument_master"
                        " order by symbol, valid_from").fetchall()


def test_the_first_load_holds_every_code_and_the_next_only_what_changed(conn, env, capsys):  # noqa: F811
    fetch(env, D1, KOSPI)
    capsys.readouterr()
    assert cli.run(["master", "load"], environ=env, now=at(D1)) == 0
    out = capsys.readouterr().out
    assert "[2026-11-24] 적재 ML-1: 5종목 중 신규 5, 변경 0, 빠짐 0" in out and "등록 종목 대조: 모두 일치" in out
    assert len(history(conn)) == 5

    # the next day: prices move (not history), 삼성전자우 leaves, a code is renamed and one is halted, one is new
    fetch(env, D2, [line(Market.KOSPI, "005930", "삼성전자", base_price=56_000, market_cap=3_300_000),
                    line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=81_000),
                    line(Market.KOSPI, "000660", "SK하이닉스", base_price=500_000)],
          [line(Market.KOSDAQ, "247540", "에코프로비엠", base_price=150_000, warning="01", halted="Y"),
           line(Market.KOSDAQ, "0088M0", "에이치엠씨스팩5호", spac="Y", base_price=2_000, halted="Y")])
    capsys.readouterr()
    assert cli.run(["master", "load"], environ=env, now=at(D2)) == 0
    assert "[2026-11-25] 적재 ML-2: 5종목 중 신규 1, 변경 2, 빠짐 1" in capsys.readouterr().out
    rows = history(conn)
    assert len(rows) == 9
    assert ("005935", D2, False, "삼성전자우", False) in rows             # left the file: a row that says so
    assert [r for r in rows if r[0] == "005930"] == [("005930", D1, True, "삼성전자", False)]   # price only: no row
    assert ("247540", D2, True, "에코프로비엠", True) in rows and ("0088M0", D2, True, "에이치엠씨스팩5호", True) in rows
    assert master_db.history(conn, "0088M0")[0][4] == "에이치엠씨제5호스팩"

    # 삼성전자우 comes back: a new listed row, the old ones stay
    fetch(env, D3, [line(Market.KOSPI, "005935", "삼성전자우", preferred="1", base_price=46_000)], KOSDAQ[:1])
    capsys.readouterr()
    assert cli.run(["master", "load"], environ=env, now=at(D3)) == 0
    assert [r[1:3] for r in history(conn) if r[0] == "005935"] == [(D1, True), (D2, False), (D3, True)]
    assert "069500 KODEX 200: DELISTED" in capsys.readouterr().out    # the books' instrument left the file

    check = check_master(conn)
    assert check.ok and "적재 3번" in check.detail and "ML-3" in check.detail
    assert codes.check(conn) == []                                   # every stored group and market has a name
    log = conn.execute("select action, outcome, target from audit_log where action = 'MASTER_LOAD' order by id")
    assert log.fetchall() == [("MASTER_LOAD", "DONE", f"ML-{i}") for i in (1, 2, 3)]


def test_a_day_is_loaded_once_and_in_order(conn, env, capsys):  # noqa: F811
    fetch(env, D1, KOSPI[:2])
    fetch(env, D2, KOSPI)
    assert cli.run(["master", "load", "--date", D2.isoformat()], environ=env, now=at(D2)) == 0
    capsys.readouterr()
    assert cli.run(["master", "load", "--date", D2.isoformat()], environ=env, now=at(D2)) == 0
    assert "이미 적재한 파일이에요 (ML-1" in capsys.readouterr().out
    assert cli.run(["master", "load", "--date", D1.isoformat()], environ=env, now=at(D2)) == 1
    assert "마지막 적재(ML-1, 2026-11-25)보다 늦어야" in capsys.readouterr().out
    assert cli.run(["master", "load"], environ=env, now=at(D2)) == 0
    assert "넣을 날이 없어요" in capsys.readouterr().out
    assert conn.execute("select count(*) from master_loads").fetchone()[0] == 1
    rows = conn.execute("select outcome, target from audit_log where action = 'MASTER_LOAD' order by id").fetchall()
    assert rows == [("DONE", "ML-1"), ("REFUSED", D1.isoformat())]


def test_the_history_cannot_be_changed_and_a_forged_row_is_found(conn, env):  # noqa: F811
    fetch(env, D1, KOSPI)
    assert cli.run(["master", "load"], environ=env, now=at(D1)) == 0
    for stmt in ("update instrument_master set name = 'x'", "delete from master_loads",
                 "truncate instrument_master cascade"):
        with pytest.raises(psycopg.Error):
            conn.execute(stmt)
    assert check_master(conn).ok
    with conn.transaction(force_rollback=True):   # a row no load wrote
        conn.execute("insert into instrument_master (symbol, valid_from, load_id, listed, isin, name, market,"
                     " sec_group, lot, par_value, listed_shares_k, preferred, spac, halted, liquidation, managed,"
                     " warning) select '999990', valid_from, load_id, listed, isin, name, market, sec_group, lot,"
                     " par_value, listed_shares_k, preferred, spac, halted, liquidation, managed, warning"
                     " from instrument_master limit 1")
        bad = check_master(conn)
        assert not bad.ok and "ML-1 신규: 기록 5, 이력 행 6" in bad.detail


def test_the_match_view_names_what_differs(conn, env):  # noqa: F811
    assert master_db.matches(conn) == [("069500", "KODEX 200", None, "MISSING")]   # nothing loaded yet
    fetch(env, D1, [line(Market.KOSPI, "069500", "KODEX200", group="ST")])
    assert cli.run(["master", "load"], environ=env, now=at(D1)) == 0
    assert master_db.matches(conn) == [("069500", "KODEX 200", "KODEX200", "DIFFERENT")]   # ETF registered, ST here
    fetch(env, D2, [line(Market.KOSPI, "069500", "KODEX 코스피200", group="EF")])
    assert cli.run(["master", "load"], environ=env, now=at(D2)) == 0
    assert master_db.matches(conn) == [("069500", "KODEX 200", "KODEX 코스피200", "RENAMED")]
    rows = master.load_day(master.default_dir(env), D2)
    assert master_db.changes(master_db.current(conn), rows) == ([], [], [])   # loaded state = the file
