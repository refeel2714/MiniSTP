"""ERP 일반전표 내보내기 (R-96): the journal, read as upload documents. Nothing is written to the DB."""
import csv
import pathlib

import pytest

from db.test_ops import CAL, FEES, URL, D, books, broker, conn  # noqa: F401
from ministp import __main__ as cli
from ministp import erp
from ministp.ledger import Side
from ministp.ops import daily_close, open_books, place_order
from ministp.sample import build_sample

DAY = D("2026-11-20")
EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "docs" / "erp" / "mapping.example.yaml"


def _a_day(conn, broker):
    """Open from the broker, buy 2 at 80,000, close the day."""
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    place_order(books(conn), broker, DAY, "069500", Side.BUY, 2, limit_price=80_000)
    broker.fill("0000000001", 80_000)
    daily_close(books(conn), broker, DAY)


def _rows(conn):
    return conn.execute("select count(*) from journal_entries").fetchone()[0]


def test_one_document_per_journal_entry_with_minstp_codes_by_default(conn, broker):
    _a_day(conn, broker)
    before = _rows(conn)
    r = erp.export(conn, None, DAY, DAY)
    assert _rows(conn) == before                                    # read only
    n_entries = conn.execute("select count(*) from journal_entries where date = %s", (DAY,)).fetchone()[0]
    assert r.documents == n_entries and r.debit == r.credit > 0
    fill = [ln for ln in r.lines if "매수 체결" in ln.memo]
    assert fill and fill[0].memo.startswith("매수 체결 069500") and "2주 @80,000" in fill[0].memo
    assert {ln.erp_code for ln in fill} == {"1100", "5030", "2010"}  # FVPL 주식, 수수료 / 매수미지급금
    assert all(ln.erp_code == ln.ministp_accounts for ln in r.lines)
    seq = int(fill[0].events)
    assert fill[0].doc_no == f"MSTP-{seq}-1" and [ln.line_no for ln in fill] == list(range(1, len(fill) + 1))


def test_reversal_points_at_the_document_it_reverses(conn):
    build_sample(conn, CAL, FEES, days=5, seed=3)
    first, last = conn.execute("select min(date), max(date) from journal_entries").fetchone()
    r = erp.export(conn, erp.load_mapping(EXAMPLE), first, last)
    docs = {ln.doc_no for ln in r.lines}
    reversals = [ln for ln in r.lines if ln.reverses]
    assert reversals and all(ln.reverses in docs and ln.reverses != ln.doc_no for ln in reversals)
    assert all("반대전표" in ln.memo for ln in reversals)
    for no in docs:                                                 # every document balances after mapping
        lines = [ln for ln in r.lines if ln.doc_no == no]
        assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
    assert "1130100" in {ln.erp_code for ln in r.lines}              # mapped: FVPL 주식 → 회사 계정


def test_an_unmapped_account_stops_the_export(conn, broker, tmp_path):
    _a_day(conn, broker)
    m = erp.load_mapping(EXAMPLE)
    partial = erp.Mapping(m.company, m.document_type, m.currency, {k: v for k, v in m.accounts.items() if k != "5030"})
    with pytest.raises(erp.ErpError, match="5030 지급수수료"):
        erp.export(conn, partial, DAY, DAY)


def test_daily_totals_only_for_closed_days_and_net_per_account(conn):
    build_sample(conn, CAL, FEES, days=4, seed=5)
    days = [d for (d,) in conn.execute("select distinct date from journal_entries order by date")]
    r = erp.export(conn, None, days[0], days[-1], by="day")
    assert r.documents == len(days)
    for no in {ln.doc_no for ln in r.lines}:
        lines = [ln for ln in r.lines if ln.doc_no == no]
        assert sum(ln.debit for ln in lines) == sum(ln.credit for ln in lines)
        assert len({ln.erp_code for ln in lines}) == len(lines)      # one net line per account
        assert all((ln.debit == 0) != (ln.credit == 0) for ln in lines)
    by_entry = erp.export(conn, None, days[0], days[-1])
    def net(res):
        acc = {}
        for ln in res.lines:
            acc[ln.erp_code] = acc.get(ln.erp_code, 0) + ln.debit - ln.credit
        return {k: v for k, v in acc.items() if v}
    assert net(r) == net(by_entry)                                    # same balances either way


def test_a_day_still_open_has_no_daily_total(conn, broker):
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)                              # opening entries, no daily close yet
    assert _rows(conn) > 0
    with pytest.raises(erp.ErpError, match="일 마감 전"):
        erp.export(conn, None, DAY, DAY, by="day")
    assert erp.export(conn, None, DAY, DAY).documents > 0              # 건별 is fine: each entry is final


def test_csv_has_korean_header_and_is_never_overwritten(conn, broker, tmp_path):
    _a_day(conn, broker)
    r = erp.export(conn, erp.load_mapping(EXAMPLE), DAY, DAY)
    path = erp.write_csv(r, tmp_path / "out.csv")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                           # BOM: Excel reads the Korean header
    rows = list(csv.reader(path.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == erp.HEADER and len(rows) == len(r.lines) + 1
    assert rows[1][0] == "1000" and rows[1][1] == "SA"
    with pytest.raises(erp.ErpError, match="두 번"):
        erp.write_csv(r, path)


def test_mapping_file_errors_are_plain(tmp_path):
    with pytest.raises(erp.ErpError, match="mapping.example.yaml"):
        erp.load_mapping(tmp_path / "없음.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("company: '1'\ndocument_type: SA\naccounts:\n  '1010': {name: x}\n", encoding="utf-8")
    with pytest.raises(erp.ErpError, match="1010"):
        erp.load_mapping(bad)


def test_command_writes_the_file_and_prints_account_totals(conn, broker, tmp_path, capsys):
    _a_day(conn, broker)
    out = tmp_path / "e.csv"
    rc = cli.run(["erp", "export", "--from", DAY.isoformat(), "--out", str(out), "--mapping", str(EXAMPLE)],
                 environ={"MINISTP_DATABASE_URL": URL})
    text = capsys.readouterr().out
    assert rc == 0 and out.exists() and "차변" in text and "1130100" in text
    rc = cli.run(["erp", "export", "--from", DAY.isoformat(), "--out", str(out)], environ={"MINISTP_DATABASE_URL": URL})
    assert rc == 1 and "내보내지 않았어요" in capsys.readouterr().out


def test_one_account_at_a_time_and_every_line_names_its_account_and_stock(conn):
    """다계좌 (R-96 v1.41): 기본은 계좌 하나, --all-accounts는 모든 계좌를 한 파일로. 줄마다 계좌·종목 관리항목."""
    s = build_sample(conn, CAL, FEES, days=4, seed=5, accounts=3)
    ids = [a for (a,) in conn.execute("select account_id from trading_accounts order by account_id")]
    first = conn.execute("select min(date) from journal_entries").fetchone()[0]
    each = {a: erp.export(conn, None, first, s.last_day, account_id=a) for a in ids}
    everyone = erp.export(conn, None, first, s.last_day, account_id=None)
    for a, r in each.items():
        assert r.lines and {ln.account_id for ln in r.lines} == {a} and r.debit == r.credit
        n = conn.execute("select count(*) from journal_entries where account_id = %s and date between %s and %s",
                         (a, first, s.last_day)).fetchone()[0]
        assert r.documents == n
    assert sorted(ln.doc_no for ln in everyone.lines) == sorted(ln.doc_no for r in each.values() for ln in r.lines)
    alias = dict(conn.execute("select account_id, alias from trading_accounts").fetchall())
    assert all(ln.account_name == alias[ln.account_id] for ln in everyone.lines)
    fills = [ln for ln in everyone.lines if "체결" in ln.memo and "반대전표" not in ln.memo]
    assert fills and all(ln.symbol and ln.memo.split()[2] == ln.symbol for ln in fills)
    settles = [ln for ln in everyone.lines if "결제" in ln.memo]
    assert settles and all(ln.symbol for ln in settles)
    assert not any(ln.symbol for ln in everyone.lines if "평가" in ln.memo)    # 계좌 전체의 평가는 종목 없음
    days = erp.export(conn, None, first, s.last_day, by="day", account_id=None)
    assert {ln.doc_no for ln in days.lines} == {erp.day_doc_no(ln.date, ln.account_id) for ln in days.lines}
    assert len({(ln.account_id, ln.date) for ln in days.lines}) == days.documents
    with pytest.raises(erp.ErpError, match="A9999"):
        erp.export(conn, None, first, s.last_day, account_id="A9999")


def test_command_takes_the_account_and_names_it_in_the_file(conn, tmp_path, capsys):
    s = build_sample(conn, CAL, FEES, days=3, seed=5, accounts=2)
    first = conn.execute("select min(date) from journal_entries").fetchone()[0]
    out = tmp_path / "a2.csv"
    rc = cli.run(["erp", "export", "--account", "A0002", "--from", first.isoformat(), "--to", s.last_day.isoformat(),
                  "--out", str(out)], environ={"MINISTP_DATABASE_URL": URL})
    assert rc == 0 and "A0002" in capsys.readouterr().out
    rows = list(csv.DictReader(out.read_text(encoding="utf-8-sig").splitlines()))
    assert rows and {r["계좌"] for r in rows} == {"A0002"}
    assert erp.default_out(first, first, "entry", None).name.startswith("전표_전체계좌_")
