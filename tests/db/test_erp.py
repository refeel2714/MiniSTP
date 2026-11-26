"""ERP 일반전표 내보내기 (R-96): the journal, read as upload documents. Nothing is written to the DB."""
import csv
import hashlib
import pathlib

import psycopg
import pytest

from db.test_ops import CAL, FEES, URL, D, books, broker, conn  # noqa: F401
from ministp import __main__ as cli
from ministp import audit, erp, rights
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


def test_daily_totals_wait_for_the_day_close_not_the_valuation(conn, broker):
    """R-96 v1.42: 일별 합계는 일 마감(day_closes) 기준. 평가는 했지만 대사 불일치로 마감 못 한 날은 아직 열린 날."""
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    broker.cash_skew = -5                                             # the broker shows 5 won less: a break
    rep = daily_close(books(conn), broker, DAY)
    assert not rep.closed and rep.nav is not None                    # valued, NAV struck, yet not closed
    with pytest.raises(erp.ErpError, match=r"일 마감 전.*A0001 2026-11-20"):
        erp.export(conn, None, DAY, DAY, by="day")
    broker.cash_skew = 0
    assert daily_close(books(conn), broker, DAY).closed               # the break is gone: daily closes the day
    assert erp.export(conn, None, DAY, DAY, by="day").documents == 1


def test_days_before_the_last_close_count_as_closed_for_every_account(conn):
    s = build_sample(conn, CAL, FEES, days=3, seed=5, accounts=2)
    first = conn.execute("select min(date) from journal_entries").fetchone()[0]
    through = dict(conn.execute("select account_id, max(date) from day_closes group by 1").fetchall())
    assert set(through.values()) == {s.last_day}
    r = erp.export(conn, None, first, s.last_day, by="day", account_id=None)
    assert {ln.account_id for ln in r.lines} == set(through)
    n = conn.execute("select count(distinct (account_id, date)) from journal_entries").fetchone()[0]
    assert r.documents == n                                           # every day up to the last close, 개시일 included


def test_dividend_entries_have_a_memo_and_a_kind_name(conn, broker):
    """새 전표 유형(배당·출금·대체·수동 조정·늦은 체결)도 적요와 전표 유형 이름이 붙는다."""
    fri, mon, tue, wed = DAY, D("2026-11-23"), D("2026-11-24"), D("2026-11-25")
    broker.set_day(fri)
    open_books(books(conn), broker, fri)
    broker.orders[fri] = {}
    rights.add_dividend(conn, "069500", fri, wed, 450)
    for day in (fri, mon, tue, wed):
        broker.set_day(day)
        broker.orders.setdefault(day, {})
        if day == wed:
            broker.cash += 381
        assert daily_close(books(conn), broker, day).closed
    r = erp.export(conn, None, fri, wed)
    memos = {ln.memo for ln in r.lines}
    assert "배당 인식 069500 KODEX 200 1주 (권리 1)" in memos
    assert "배당 입금 069500 KODEX 200 (권리 1, 원천징수 69)" in memos
    assert all(ln.symbol == "069500" for ln in r.lines if ln.memo.startswith("배당"))
    names = erp._kind_names(conn)
    assert names["DIVIDEND_ACCRUAL"] != "DIVIDEND_ACCRUAL" and names["DIVIDEND"] != "DIVIDEND"


def test_every_entry_of_the_sample_gets_a_memo(conn):
    build_sample(conn, CAL, FEES, days=12, seed=5, accounts=3)
    first, last = conn.execute("select min(date), max(date) from journal_entries").fetchone()
    r = erp.export(conn, None, first, last, account_id=None)
    kinds = {k for (k,) in conn.execute("select distinct kind from journal_entries")}
    assert {"WITHDRAWAL", "ADJUSTMENT"} <= kinds
    assert all(ln.memo and not any(k in ln.memo for k in kinds) for ln in r.lines), {ln.memo for ln in r.lines}
    assert any(ln.memo.startswith("투자원금 출금") for ln in r.lines)
    assert any(ln.memo.startswith("수동 조정 ") and "(조정 1)" in ln.memo for ln in r.lines)
    names = erp._kind_names(conn)
    assert all(names.get(k, k) != k for k in kinds)


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


# ---------- 전송 이력, 중복 차단, 통제 파일 (R-104, ERD v2.11) ----------

def _sent(conn):
    return conn.execute("select export_id, unit, documents, status from erp_exports_v order by export_id").fetchall()


def test_an_export_leaves_the_file_its_control_file_and_the_history(conn, broker, tmp_path):
    _a_day(conn, broker)
    r = erp.export(conn, erp.load_mapping(EXAMPLE), DAY, DAY)
    done = erp.deliver(conn, r, tmp_path / "e.csv", DAY, DAY)
    assert done.csv.exists() and done.control == tmp_path / "e.ctl" and not list(tmp_path.glob("*.tmp"))
    data = done.csv.read_bytes()
    ctl = dict(line.split("=", 1) for line in done.control.read_text(encoding="utf-8").splitlines())
    assert ctl["file"] == "e.csv" and ctl["sha256"] == hashlib.sha256(data).hexdigest() == done.sha256
    assert (int(ctl["bytes"]), int(ctl["documents"]), int(ctl["lines"]), int(ctl["debit"]), int(ctl["credit"])) == (
        len(data), r.documents, len(r.lines), r.debit, r.credit)
    assert (ctl["export_id"], ctl["account"], ctl["unit"], ctl["company"]) == (str(done.export_id), "A0001", "ENTRY", "1000")
    row = conn.execute("select account_id, unit, documents, lines, debit_total, file_name, file_sha256, mapping_sha256,"
                       " created_by from erp_exports").fetchone()
    assert row[:7] == ("A0001", "ENTRY", r.documents, len(r.lines), r.debit, "e.csv", done.sha256)
    assert row[7] == hashlib.sha256(EXAMPLE.read_bytes()).hexdigest() and row[8]
    docs = conn.execute("select doc_no from erp_export_docs order by doc_no").fetchall()
    assert sorted(d for (d,) in docs) == sorted({ln.doc_no for ln in r.lines})
    last = audit.rows(conn, limit=1)[0]
    assert (last.action, last.target, last.detail["sha256"]) == ("ERP_EXPORT", f"ERP-{done.export_id}", done.sha256)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("delete from erp_export_docs")


def test_a_document_goes_out_once_until_its_export_is_voided(conn, broker, tmp_path):
    _a_day(conn, broker)
    first = erp.deliver(conn, erp.export(conn, None, DAY, DAY), tmp_path / "a.csv", DAY, DAY)
    with pytest.raises(erp.ErpError, match=r"이미 내보낸 전표 \d+건.*--new-only"):
        erp.deliver(conn, erp.export(conn, None, DAY, DAY), tmp_path / "b.csv", DAY, DAY)   # another name: still refused
    assert not (tmp_path / "b.csv").exists() and len(_sent(conn)) == 1
    with pytest.raises(erp.ErpError, match="새로 낼 전표가 없어요"):
        erp.deliver(conn, erp.export(conn, None, DAY, DAY), tmp_path / "b.csv", DAY, DAY, new_only=True)
    nxt = CAL.add_business_days(DAY, 1)                                 # the next day adds entries of its own
    broker.set_day(nxt)
    broker.orders[nxt] = {}
    daily_close(books(conn), broker, nxt)
    again = erp.deliver(conn, erp.export(conn, None, DAY, nxt), tmp_path / "b.csv", DAY, nxt, new_only=True)
    assert again.skipped == first.result.documents and {ln.date for ln in again.result.lines} == {nxt}
    with pytest.raises(erp.ErpError, match=r"다른 단위.*erp void"):     # 건별 already out: the day total would double it
        erp.deliver(conn, erp.export(conn, None, DAY, DAY, by="day"), tmp_path / "d.csv", DAY, DAY)
    erp.void(conn, first.export_id, "ERP 업로드 오류로 받지 않음")
    day = erp.deliver(conn, erp.export(conn, None, DAY, DAY, by="day"), tmp_path / "d.csv", DAY, DAY)
    assert _sent(conn) == [(first.export_id, "ENTRY", first.result.documents, "VOIDED"),
                           (again.export_id, "ENTRY", again.result.documents, "EXPORTED"), (day.export_id, "DAY", 1, "EXPORTED")]
    assert erp.problems(conn) == []


def test_the_database_refuses_a_second_copy_whatever_writes_it(conn, broker, tmp_path):
    _a_day(conn, broker)
    done = erp.deliver(conn, erp.export(conn, None, DAY, DAY), tmp_path / "a.csv", DAY, DAY)
    doc = conn.execute("select doc_no, account_id, doc_date from erp_export_docs limit 1").fetchone()
    other = conn.execute(
        "insert into erp_exports (account_id, date_from, date_to, unit, company_code, document_type, documents, lines,"
        " debit_total, credit_total, file_name, file_sha256, created_by) select account_id, date_from, date_to, unit,"
        " company_code, document_type, 1, 1, 1, 1, 'x.csv', file_sha256, 'test' from erp_exports returning export_id"
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.UniqueViolation, match="already exported"):
        conn.execute("insert into erp_export_docs values (%s, %s, %s, %s)", (other, *doc))
    conn.execute("insert into erp_export_voids (export_id, reason, voided_by) values (%s, 'x', 'test')", (done.export_id,))
    with pytest.raises(psycopg.errors.CheckViolation, match="already void"):
        conn.execute("insert into erp_export_docs values (%s, 'MSTP-999-1', %s, %s)", (done.export_id, doc[1], doc[2]))


def test_a_file_that_cannot_be_written_leaves_no_history(conn, broker, tmp_path, monkeypatch):
    _a_day(conn, broker)
    r = erp.export(conn, None, DAY, DAY)
    (tmp_path / "a.ctl").write_text("x")
    with pytest.raises(erp.ErpError, match="a.ctl이 이미 있어요"):
        erp.deliver(conn, r, tmp_path / "a.csv", DAY, DAY)
    real = erp._publish
    monkeypatch.setattr(erp, "_publish", lambda p, data: real(p, data) if p.suffix == ".csv" else (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        erp.deliver(conn, r, tmp_path / "b.csv", DAY, DAY)
    assert not (tmp_path / "b.csv").exists() and _sent(conn) == []
    assert conn.execute("select count(*) from erp_export_docs").fetchone()[0] == 0


def test_void_needs_a_reason_and_happens_once(conn, broker, tmp_path):
    _a_day(conn, broker)
    done = erp.deliver(conn, erp.export(conn, None, DAY, DAY), tmp_path / "a.csv", DAY, DAY)
    for export_id, reason, problem in [(done.export_id, " ", "사유"), (999, "x", "999번이 없어요")]:
        with pytest.raises(erp.ErpError, match=problem):
            erp.void(conn, export_id, reason)
    erp.void(conn, done.export_id, "ERP 반려")
    with pytest.raises(erp.ErpError, match="이미 무효"):
        erp.void(conn, done.export_id, "또")
    assert audit.rows(conn, limit=1)[0].action == "ERP_EXPORT_VOID"


def test_history_void_and_preview_commands(conn, broker, tmp_path, capsys):
    _a_day(conn, broker)
    env = {"MINISTP_DATABASE_URL": URL}
    out = tmp_path / "e.csv"
    assert cli.run(["erp", "export", "--from", DAY.isoformat(), "--preview"], environ=env) == 0
    assert "미리 보기" in capsys.readouterr().out and _sent(conn) == []
    assert cli.run(["erp", "export", "--from", DAY.isoformat(), "--out", str(out)], environ=env) == 0
    text = capsys.readouterr().out
    assert "통제 파일" in text and "ERP-1" in text and out.with_suffix(".ctl").exists()
    assert cli.run(["erp", "export", "--from", DAY.isoformat(), "--preview"], environ=env) == 0
    assert "겹쳐요" in capsys.readouterr().out
    assert cli.run(["erp", "history"], environ=env) == 0
    assert "ERP-1" in (h := capsys.readouterr().out) and "내보냄" in h
    assert cli.run(["erp", "void", "1"], environ=env) == 1 and "사유" in capsys.readouterr().out
    assert cli.run(["erp", "void", "1", "--reason", "ERP 반려"], environ=env) == 0
    capsys.readouterr()
    assert cli.run(["erp", "history"], environ=env) == 0 and "무효 (ERP 반려)" in capsys.readouterr().out
    assert cli.run(["verify"], environ=env) == 0 and "ERP 전송 이력" in capsys.readouterr().out
