"""R-90~92: KIS market master files, built here line by line with the layout KIS documents."""
import datetime as dt
import io
import json
import zipfile

import pytest

from ministp import __main__ as cli
from ministp import master
from ministp.instruments import AssetType, Instrument, Market
from ministp.master import BAD, OK, WARN, MasterError

DAY = dt.date(2026, 11, 24)
DEFAULTS = {"group": "ST", "base_price": 0, "lot": 1, "halted": "N", "liquidation": "N", "managed": "N",
            "warning": "00", "lock": "00", "margin": 40, "spac": "N", "preferred": "0", "prev_volume": 0,
            "par_value": 100, "listed_on": "19750611", "listed_shares": 0, "market_cap": 0}
NUMERIC = {"base_price", "lot", "margin", "prev_volume", "par_value", "listed_shares", "market_cap"}


def line(market, symbol, name, isin=None, tail_len=None, **fields):
    f = DEFAULTS | fields
    tail = [" "] * master.TAIL[market]
    for key, (start, width) in master._FIELDS[market].items():
        v = f[key]
        text = str(v).rjust(width, "0") if key in NUMERIC else str(v).ljust(width)
        assert len(text) == width, key
        tail[start:start + width] = text
    tail = "".join(tail)
    if tail_len is not None:          # a layout that moved by a column or two
        tail = tail[:tail_len] if tail_len < len(tail) else tail + " " * (tail_len - len(tail))
    isin = isin or ("KR7" + symbol + "000")[:12].ljust(12, "0")
    return symbol.ljust(9) + isin + name + tail


def mst(market, *lines) -> bytes:
    return ("\n".join(lines) + "\n").encode("cp949")


def zipped(market, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(master.FILES[market] + ".mst", data)
    return buf.getvalue()


KOSPI = [line(Market.KOSPI, "005930", "삼성전자", base_price=55_000, market_cap=3_283_000),
         line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=80_540),
         line(Market.KOSPI, "005935", "삼성전자우", preferred="1", base_price=46_000)]
KOSDAQ = [line(Market.KOSDAQ, "247540", "에코프로비엠", base_price=150_000, warning="01"),
          line(Market.KOSDAQ, "0088M0", "에이치엠씨제5호스팩", spac="Y", base_price=2_000, halted="Y")]


def downloads(kospi=KOSPI, kosdaq=KOSDAQ):
    files = {master.BASE_URL + "kospi_code.mst.zip": zipped(Market.KOSPI, mst(Market.KOSPI, *kospi)),
             master.BASE_URL + "kosdaq_code.mst.zip": zipped(Market.KOSDAQ, mst(Market.KOSDAQ, *kosdaq))}
    return files.__getitem__


def test_a_kospi_line_reads_every_field():
    r = master.parse_line(KOSPI[0], Market.KOSPI)
    assert (r.symbol, r.isin, r.name, r.market, r.group, r.group_name) == (
        "005930", "KR7005930000", "삼성전자", Market.KOSPI, "ST", "주권")
    assert (r.base_price, r.lot, r.margin, r.par_value, r.market_cap) == (55_000, 1, 40, 100, 3_283_000)
    assert r.listed_on == dt.date(1975, 6, 11) and not (r.halted or r.managed or r.spac)


def test_a_kosdaq_line_uses_its_own_offsets():
    r = master.parse_line(KOSDAQ[1], Market.KOSDAQ)
    assert (r.symbol, r.name, r.spac, r.halted, r.base_price) == ("0088M0", "에이치엠씨제5호스팩", True, True, 2_000)
    assert master.parse_line(KOSDAQ[0], Market.KOSDAQ).warning == "01"


def test_names_of_any_length_do_not_shift_the_fixed_block():
    long = line(Market.KOSPI, "123456", "아주아주긴한글종목명과 English Mix 2호", base_price=1_234)
    assert master.parse_line(long, Market.KOSPI).base_price == 1_234


def test_bad_lines_are_reported_with_their_reason_not_dropped():
    parsed = master.parse(mst(Market.KOSPI, *KOSPI, line(Market.KOSPI, "00$930", "깨진줄")), Market.KOSPI)
    assert len(parsed.rows) == 3 and parsed.bad == [(4, "단축코드='00$930'")]
    assert not parsed.layout_ok          # 1 of 4 is far above the 1% allowed


def test_a_layout_that_moved_is_refused():
    moved = [line(Market.KOSPI, s, n, tail_len=master.TAIL[Market.KOSPI] + 1, base_price=1)
             for s, n in (("005930", "삼성전자"), ("000660", "SK하이닉스"))]
    parsed = master.parse(mst(Market.KOSPI, *moved), Market.KOSPI)
    assert parsed.rows == [] and not parsed.layout_ok


def test_fetch_keeps_the_zips_as_received_and_a_manifest(tmp_path):
    now = dt.datetime(2026, 11, 24, 8, 30)
    got = master.fetch(tmp_path, DAY, now, downloads())
    assert got.changed and got.rows == {"KOSPI": 3, "KOSDAQ": 2}
    m = json.loads((tmp_path / "2026-11-24" / "manifest.json").read_text(encoding="utf-8"))
    assert m["fetched_at"] == "2026-11-24T08:30:00" and m["files"]["kospi_code.mst.zip"]["rows"] == 3
    assert master.kept_days(tmp_path) == [DAY] and set(master.load_day(tmp_path, DAY)) == {
        "005930", "069500", "005935", "247540", "0088M0"}

    again = master.fetch(tmp_path, DAY, now.replace(hour=9), downloads())
    assert not again.changed

    newer = master.fetch(tmp_path, DAY, now.replace(hour=10), downloads(kospi=KOSPI[:2]))
    assert newer.changed and newer.rows["KOSPI"] == 2
    folder = tmp_path / "2026-11-24"
    assert (folder / "kospi_code.mst.zip.083000").exists()           # the earlier bytes are kept beside
    m = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert [h["fetched_at"] for h in m["history"]] == ["2026-11-24T08:30:00"]


def test_fetch_refuses_a_file_it_cannot_read_and_keeps_nothing(tmp_path):
    broken = [line(Market.KOSPI, "005930", "삼성전자", tail_len=200)]
    with pytest.raises(MasterError, match="형식이 예상과 달라요"):
        master.fetch(tmp_path, DAY, dt.datetime(2026, 11, 24, 8), downloads(kospi=broken))
    assert master.kept_days(tmp_path) == []


def test_a_kept_file_changed_afterwards_is_caught(tmp_path):
    master.fetch(tmp_path, DAY, dt.datetime(2026, 11, 24, 8), downloads())
    (tmp_path / "2026-11-24" / "kosdaq_code.mst.zip").write_bytes(zipped(Market.KOSDAQ, mst(Market.KOSDAQ, KOSDAQ[0])))
    with pytest.raises(MasterError, match="SHA-256"):
        master.load_day(tmp_path, DAY)


def rows_of(kospi=KOSPI, kosdaq=KOSDAQ):
    out = {}
    for market, lines in ((Market.KOSPI, kospi), (Market.KOSDAQ, kosdaq)):
        out |= {r.symbol: r for r in master.parse(mst(market, *lines), market).rows}
    return out


KODEX = Instrument("069500", "KODEX 200", Market.KOSPI, AssetType.ETF)


def test_check_counts_and_compares_with_the_day_before():
    before = rows_of(kospi=KOSPI[:2] + [line(Market.KOSPI, "000000", "폐지종목")],
                     kosdaq=[line(Market.KOSDAQ, "247540", "에코프로비엠(구)")] + KOSDAQ[1:])
    rep = master.check(DAY, rows_of(), dt.date(2026, 11, 23), before, {}, set(), {})
    assert rep.counts[("KOSPI", "주권")] == 2 and rep.counts[("KOSPI", "ETF")] == 1
    assert rep.flags["거래정지"] == 1 and rep.flags["시장경고"] == 1
    assert [r.symbol for r in rep.new] == ["005935"] and [r.symbol for r in rep.gone] == ["000000"]
    assert [(a.name, b.name) for a, b in rep.renamed] == [("에코프로비엠(구)", "에코프로비엠")]


def test_many_symbols_missing_at_once_means_a_cut_file():
    rep = master.check(DAY, rows_of(kospi=KOSPI[:1], kosdaq=[]), None, rows_of(), {}, set(), {})
    assert rep.level == BAD and "잘렸을" in rep.findings[0].text


def test_registered_instruments_must_match_the_master():
    instruments = {"069500": KODEX,
                   "005930": Instrument("005930", "삼성 전자", Market.KOSDAQ, AssetType.ETF),
                   "999999": Instrument("999999", "없는종목", Market.KOSPI, AssetType.STOCK),
                   "0088M0": Instrument("0088M0", "에이치엠씨제5호스팩", Market.KOSDAQ, AssetType.STOCK)}
    rep = master.check(DAY, rows_of(), None, None, instruments, set(), {})
    texts = [(f.level, f.text) for f in rep.findings]
    assert (BAD, "005930 삼성 전자: 시장 KOSDAQ → 마스터 KOSPI, 구분 ETF → 마스터 주권") in texts
    assert not any("005930: 등록 이름" in t for _, t in texts)       # spacing alone is not a different name
    assert (BAD, "999999 없는종목: 마스터에 없어요 (상장폐지 또는 잘못된 코드)") in texts
    assert (WARN, "0088M0 에이치엠씨제5호스팩: 거래정지") in texts      # not held: a warning
    assert not any("069500" in t for _, t in texts)
    held = master.check(DAY, rows_of(), None, None, {"0088M0": instruments["0088M0"]}, {"0088M0"}, {})
    assert held.findings[0].level == BAD                              # held: needs a look


def test_the_base_price_is_a_second_source_for_the_books_close():
    closes = {dt.date(2026, 11, 20): {"069500": 79_000}, dt.date(2026, 11, 23): {"069500": 80_540}}
    rep = master.check(DAY, rows_of(), None, None, {"069500": KODEX}, {"069500"}, closes)
    assert [(f.level, f.text) for f in rep.findings] == [(OK, "069500 기준가 80,540 = 원장 2026-11-23 종가")]
    assert rep.level == OK

    stale = rows_of(kospi=[line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=79_000)])
    rep = master.check(DAY, stale, None, None, {"069500": KODEX}, {"069500"}, closes)
    assert rep.level == BAD and "파일이 하루 전 것일 수 있어요" in rep.findings[0].text

    locked = rows_of(kospi=[line(Market.KOSPI, "069500", "KODEX 200", group="EF", base_price=78_000, lock="03")])
    rep = master.check(DAY, locked, None, None, {"069500": KODEX}, {"069500"}, closes)
    assert rep.level == WARN and "권리 조정" in rep.findings[0].text

    later = {**closes, DAY: {"069500": 81_000}}                       # a close of the day itself is not the base
    assert master.check(DAY, rows_of(), None, None, {"069500": KODEX}, {"069500"}, later).level == OK


def test_cli_fetch_and_check_without_the_db(tmp_path, capsys):
    env = {"MINISTP_MASTER_DIR": str(tmp_path)}
    at = lambda: dt.datetime(2026, 11, 24, 8, 30)   # noqa: E731
    assert cli.run(["master", "check", "--no-db"], environ=env, now=at) == 1
    assert "먼저 python -m ministp master fetch" in capsys.readouterr().out
    assert cli.run(["master", "fetch", "--csv"], environ=env, now=at, master_download=downloads()) == 0
    out = capsys.readouterr().out
    assert "받음:" in out and "KOSPI 3종목, KOSDAQ 2종목" in out
    csv = (tmp_path / "2026-11-24" / "master.csv").read_text(encoding="utf-8-sig").splitlines()
    assert csv[0].startswith("단축코드,표준코드,종목명") and len(csv) == 6
    assert cli.run(["master", "check", "--no-db"], environ=env, now=at) == 0
    out = capsys.readouterr().out
    assert "  KOSPI  주권" + " " * 12 + "      2" in out and "비교할 이전 파일이 없어요" in out and "결과: 정상" in out


def test_the_count_table_lines_up_with_korean_names():
    assert master.ljust_wide("주권", 8) == "주권    " and master.ljust_wide("ETF", 8) == "ETF     "
    assert master.ljust_wide("인프라투융자회사", 8) == "인프라투융자회사"


def test_cli_fetch_reports_a_network_failure(tmp_path, capsys):
    import urllib.error

    def down(url):
        raise urllib.error.URLError("timed out")
    env = {"MINISTP_MASTER_DIR": str(tmp_path)}
    assert cli.run(["master", "fetch"], environ=env, now=lambda: dt.datetime(2026, 11, 24, 8), master_download=down) == 1
    assert "오류:" in capsys.readouterr().out and master.kept_days(tmp_path) == []
