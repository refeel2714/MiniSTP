"""ERP 일반전표 내보내기 (spec D9, R-96): the journal, as a general-journal upload file for the company ERP.

Reads `journal_entries` / `journal_lines` and writes a CSV. What it writes to the DB is only the send history
(R-104, ERD v2.11): one `erp_exports` row per file and one `erp_export_docs` row per document in it, so a
document already exported is never exported again (the DB refuses it) until that export is voided. The file is
written under a temporary name and renamed when complete, then a control file (`.ctl`: count, totals, SHA-256)
follows it, so whatever picks the file up (an SFTP job, a person) never sees half a file.
Without a mapping file the documents carry MiniSTP's own chart (`accounts_chart`) as is. A mapping file
(YAML, not a table: the operating schema stays as it is until the gate) turns them into the company's account
codes; then a file is written only when every account in the range is mapped. Every document must balance.

Two shapes:
- 건별 (`entry`): one document per journal entry. 증빙번호 `MSTP-<이벤트 번호>-<k>` (k-th entry the event
  wrote), so the ERP can refuse a document it already has. A reversal points at the document it reverses.
- 일별 합계 (`day`): one document per day and account, net amount per ERP account. Only for days the account's
  books are closed through (R-77 일 마감, `day_closes`): a day still open can gain entries (a late fill, a
  dividend) and its total would change. A close locks every day up to its date, so one close covers them all.

One account at a time by default (the CLI's `--account`); `account_id=None` takes every account into one file.
Every line carries its account (계좌, 계좌명) and, when the entry is about one stock, the stock (종목코드): the
관리항목 an ERP keeps beside the account code, so one ERP account can still be split by brokerage account and stock.
"""
import csv
import datetime as dt
import hashlib
import io
import os
import pathlib
from collections import defaultdict
from dataclasses import dataclass

import psycopg
import yaml

from . import audit
from .accounting import Account
from .store import DEFAULT_ACCOUNT

BY = ("entry", "day")
UNITS = {"entry": "ENTRY", "day": "DAY"}          # erp_exports.unit (공통코드 ERP_EXPORT_UNIT)
EXPORT_STATUSES = ("EXPORTED", "VOIDED")          # erp_exports_v.status (공통코드 ERP_EXPORT_STATUS)
HEADER = ["회사코드", "전표유형", "증빙번호", "전표일자", "통화", "줄번호", "계정코드", "계정명", "차변", "대변", "적요",
          "계좌", "계좌명", "종목코드", "MiniSTP계정", "원장이벤트", "반대전표대상"]
KIND_NAMES = {"DEPOSIT": "본사 자금 입금", "OPENING": "기초 보유", "EXECUTION": "체결", "SETTLEMENT": "결제",
              "VALUATION": "공정가치 평가", "VALUATION_REVERSAL": "전일 평가 역분개",
              "LATE_EXECUTION": "마감 후 체결", "DIVIDEND_ACCRUAL": "배당 인식", "DIVIDEND": "배당 입금",
              "DIVIDEND_CANCEL": "배당 인식 취소", "WITHDRAWAL": "출금", "TRANSFER_OUT": "대체 출금",
              "TRANSFER_IN": "대체 입금", "ADJUSTMENT": "수동 조정", "ADJUSTMENT_CANCEL": "수동 조정 취소"}
# The 적요 of a reversal names the kind of the entry it is; the code table (JOURNAL_KIND) wins over these.
SIDE_NAMES = {"BUY": "매수", "SELL": "매도"}
MINISTP_NAMES = {a.code: a.value[1] for a in Account}


class ErpError(ValueError):
    pass


@dataclass(frozen=True)
class Mapping:
    company: str
    document_type: str
    currency: str
    accounts: dict[str, tuple[str, str]]       # MiniSTP code -> (ERP code, ERP name)
    sha256: str | None = None                  # of the mapping file; None for MiniSTP's own chart


def load_mapping(path: str | pathlib.Path) -> Mapping:
    p = pathlib.Path(path)
    if not p.exists():
        raise ErpError(f"계정 매핑 파일 {p}이 없어요. docs/erp/mapping.example.yaml을 복사해 회사 계정코드로 고쳐 쓰세요.")
    data = p.read_bytes()
    raw = yaml.safe_load(data.decode("utf-8")) or {}
    missing = [k for k in ("company", "document_type", "accounts") if not raw.get(k)]
    if missing:
        raise ErpError(f"매핑 파일에 {', '.join(missing)} 항목이 없어요.")
    accounts = {}
    for code, v in raw["accounts"].items():
        if not isinstance(v, dict) or not v.get("code"):
            raise ErpError(f"매핑 파일의 계정 {code}에 code가 없어요.")
        accounts[str(code)] = (str(v["code"]), str(v.get("name", "")))
    return Mapping(str(raw["company"]), str(raw["document_type"]), str(raw.get("currency", "KRW")), accounts,
                   hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class DocLine:
    doc_no: str
    date: dt.date
    line_no: int
    erp_code: str
    erp_name: str
    debit: int
    credit: int
    memo: str
    ministp_accounts: str
    events: str
    reverses: str = ""
    account_id: str = ""
    account_name: str = ""
    symbol: str = ""


@dataclass
class Export:
    by: str
    lines: list[DocLine]
    mapping: Mapping
    account_id: str | None = DEFAULT_ACCOUNT     # None: every account

    @property
    def documents(self) -> int:
        return len({ln.doc_no for ln in self.lines})

    @property
    def debit(self) -> int:
        return sum(ln.debit for ln in self.lines)

    @property
    def credit(self) -> int:
        return sum(ln.credit for ln in self.lines)

    def by_account(self) -> list[tuple[str, str, int, int]]:
        """(ERP code, name, debit, credit) per ERP account: the totals to tie out against the ERP ledger."""
        acc: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        for ln in self.lines:
            acc[(ln.erp_code, ln.erp_name)][0] += ln.debit
            acc[(ln.erp_code, ln.erp_name)][1] += ln.credit
        return [(c, n, dr, cr) for (c, n), (dr, cr) in sorted(acc.items())]


_LINES_SQL = """
with numbered as (
    select entry_id, event_seq, date, kind, source_id, reverses_entry_id, account_id,
           row_number() over (partition by event_seq order by entry_id) as k
      from journal_entries)
select n.entry_id, n.event_seq, n.k, n.date, n.kind, n.source_id,
       r.event_seq as rev_seq, r.k as rev_k, l.account_code, l.debit, l.credit, n.account_id
  from numbered n
  join journal_lines l using (entry_id)
  left join numbered r on r.entry_id = n.reverses_entry_id
 where n.date between %(f)s and %(t)s
   and (%(a)s::text is null or n.account_id = %(a)s)
 order by n.account_id, n.date, n.event_seq, n.entry_id, l.line_id
"""


def doc_no(seq: int, k: int) -> str:
    """Event numbers run across all accounts, so a 건별 number is unique without the account in it."""
    return f"MSTP-{seq}-{k}"


def day_doc_no(date: dt.date, account_id: str) -> str:
    return f"MSTP-D{date:%Y%m%d}-{account_id}"


def _symbols(conn: psycopg.Connection, seqs: set[int]) -> dict[int, str]:
    """The stock an event is about (관리항목 종목), if it is about one: its own `symbol`, or a settlement's fill's."""
    if not seqs:
        return {}
    out, exec_of = {}, {}
    for seq, t, p in conn.execute("select seq, event_type, payload from ledger_events where seq = any(%s)", (sorted(seqs),)):
        if p.get("symbol"):
            out[seq] = p["symbol"]
        elif t == "SettlementEvent" and p.get("exec_id"):
            exec_of[seq] = p["exec_id"]
    if exec_of:
        sym = dict(conn.execute("select payload->>'exec_id', payload->>'symbol' from ledger_events"
                                " where event_type in ('ExecutionEvent', 'LateExecutionEvent')"
                                " and payload->>'exec_id' = any(%s)", (sorted(set(exec_of.values())),)).fetchall())
        out.update({seq: sym[e] for seq, e in exec_of.items() if e in sym})
    return out


def _aliases(conn: psycopg.Connection) -> dict[str, str]:
    return dict(conn.execute("select account_id, alias from trading_accounts").fetchall())


def _memos(conn: psycopg.Connection, seqs: set[int]) -> dict[int, str]:
    """A short 적요 per event: what happened, which stock, how many at what price."""
    if not seqs:
        return {}
    names = dict(conn.execute("select symbol, name from instruments").fetchall())
    events = {s: (t, p) for s, t, p in conn.execute(
        "select seq, event_type, payload from ledger_events where seq = any(%s)", (sorted(seqs),))}
    exec_ids = [p["exec_id"] for t, p in events.values() if t == "SettlementEvent"]
    fills = {p["exec_id"]: p for (p,) in conn.execute(
        "select payload from ledger_events where event_type in ('ExecutionEvent', 'LateExecutionEvent')"
        " and payload->>'exec_id' = any(%s)",
        (exec_ids,))} if exec_ids else {}

    def stock(sym: str) -> str:
        return f"{sym} {names.get(sym, '')}".strip()

    reasons = dict(conn.execute("select code, name from common_codes where group_id = 'ADJUSTMENT_REASON'").fetchall())

    out = {}
    for seq, (t, p) in events.items():
        if t in ("ExecutionEvent", "LateExecutionEvent"):
            late = f" (마감 후 체결, 매매일 {p['trade_date']})" if t == "LateExecutionEvent" else ""
            out[seq] = f"{SIDE_NAMES.get(p['side'], p['side'])} 체결 {stock(p['symbol'])} {p['qty']:,}주 @{p['price']:,}{late}"
        elif t == "SettlementEvent":
            f = fills.get(p["exec_id"])
            what = f"{SIDE_NAMES.get(f['side'], f['side'])} 결제 {stock(f['symbol'])} {f['qty']:,}주" if f else "결제"
            out[seq] = f"{what} (체결 {p['exec_id']})"
        elif t == "DepositEvent":
            out[seq] = "본사 자금 입금"
        elif t == "OpeningPositionEvent":
            out[seq] = f"기초 보유 {stock(p['symbol'])} {p['qty']:,}주"
        elif t in ("NavEvent", "RevaluationEvent"):
            out[seq] = f"{p['date']} 종가 공정가치 평가"
        elif t == "WithdrawalEvent":
            out[seq] = "본사 자금 출금"
        elif t == "TransferOutEvent":
            out[seq] = f"계좌 대체 출금 → {p['to_account']} (대체 {p['transfer_id']})"
        elif t == "TransferInEvent":
            out[seq] = f"계좌 대체 입금 ← {p['from_account']} (대체 {p['transfer_id']})"
        elif t == "DividendAccrualEvent":
            out[seq] = f"배당 인식 {stock(p['symbol'])} {p['qty']:,}주 (권리 {p['action_id']})"
        elif t == "DividendPaymentEvent":
            out[seq] = f"배당 입금 {stock(p['symbol'])} (권리 {p['action_id']}, 원천징수 {p['tax']:,})"
        elif t == "DividendCancelEvent":
            out[seq] = f"배당 인식 취소 {stock(p['symbol'])} (권리 {p['action_id']})"
        elif t == "AdjustmentEvent":
            out[seq] = f"수동 조정 {reasons.get(p['reason'], p['reason'])} (조정 {p['adjustment_id']}): {p['memo']}"
        elif t == "AdjustmentCancelEvent":
            out[seq] = f"수동 조정 취소 (조정 {p['adjustment_id']}): {p['memo']}"
    return out


def _closed_through(conn: psycopg.Connection) -> dict[str, dt.date]:
    """Per account, the last day its books are closed (R-77). Every day up to it is closed too."""
    return dict(conn.execute("select account_id, max(date) from day_closes group by account_id").fetchall())


def _kind_names(conn: psycopg.Connection) -> dict[str, str]:
    """전표 유형 names from the code table (JOURNAL_KIND), so a kind added later is named without a release."""
    names = dict(KIND_NAMES)
    names.update(conn.execute("select code, name from common_codes where group_id = 'JOURNAL_KIND'").fetchall())
    return names


def own_chart(conn: psycopg.Connection) -> Mapping:
    """MiniSTP's chart as the mapping: each account to itself. Used when no mapping file is given."""
    return Mapping("", "", "KRW", {c: (c, n) for c, n in conn.execute("select code, name from accounts_chart")})


def export(conn: psycopg.Connection, mapping: Mapping | None, date_from: dt.date, date_to: dt.date,
           by: str = "entry", account_id: str | None = DEFAULT_ACCOUNT) -> Export:
    """The documents for [date_from, date_to] of one account (`account_id`), or of all of them (None).
    Raises ErpError instead of returning a file that would mislead. `mapping` None means MiniSTP's own codes."""
    mapping = mapping or own_chart(conn)
    if by not in BY:
        raise ErpError(f"by는 {', '.join(BY)} 중 하나예요.")
    if date_from > date_to:
        raise ErpError("시작일이 종료일보다 늦어요.")
    aliases = _aliases(conn)
    if account_id is not None and account_id not in aliases:
        raise ErpError(f"계좌 {account_id}가 없어요. account list로 확인하세요.")
    rows = conn.execute(_LINES_SQL, {"f": date_from, "t": date_to, "a": account_id}).fetchall()
    unmapped = sorted({r[8] for r in rows} - set(mapping.accounts))
    if unmapped:
        raise ErpError("매핑이 없는 계정이 있어 파일을 만들지 않았어요: "
                       + ", ".join(f"{c} {MINISTP_NAMES.get(c, '')}".strip() for c in unmapped))
    if by == "entry":
        result = Export(by, _by_entry(conn, rows, mapping, aliases), mapping, account_id)
    else:
        through = _closed_through(conn)
        open_days = sorted({(r[11], r[3]) for r in rows if r[3] > through.get(r[11], dt.date.min)})
        if open_days:
            raise ErpError("일 마감 전인 날은 일별 합계를 만들 수 없어요(마감 후 체결·배당으로 전표가 더 생길 수 있어요). "
                           "close 명령이나 daily로 마감한 뒤 내보내세요: "
                           + ", ".join(f"{a} {d.isoformat()}" for a, d in open_days))
        result = Export(by, _by_day(rows, mapping, aliases), mapping, account_id)
    _check_balanced(result.lines)
    return result


def _by_entry(conn, rows, mapping: Mapping, aliases: dict[str, str]) -> list[DocLine]:
    seqs = {r[1] for r in rows}
    memos, symbols, kinds = _memos(conn, seqs), _symbols(conn, seqs), _kind_names(conn)
    out, line_no, current = [], 0, None
    for entry_id, seq, k, date, kind, source_id, rev_seq, rev_k, code, dr, cr, acct in rows:
        if entry_id != current:
            current, line_no = entry_id, 0
        line_no += 1
        erp_code, erp_name = mapping.accounts[code]
        memo = memos.get(seq) or kinds.get(kind, kind)
        reverses = doc_no(rev_seq, rev_k) if rev_seq is not None else ""
        if reverses:
            memo = f"{kinds.get(kind, kind)} (반대전표: {reverses})"
        out.append(DocLine(doc_no(seq, k), date, line_no, erp_code, erp_name, dr, cr, memo, code, str(seq), reverses,
                           acct, aliases.get(acct, ""), symbols.get(seq, "")))
    return out


def _by_day(rows, mapping: Mapping, aliases: dict[str, str]) -> list[DocLine]:
    """One document per (account, day): an ERP posts each brokerage account's day on its own."""
    days: dict[tuple[str, dt.date], dict] = {}
    for entry_id, seq, k, date, kind, source_id, rev_seq, rev_k, code, dr, cr, acct in rows:
        d = days.setdefault((acct, date), {"net": defaultdict(int), "src": defaultdict(set), "entries": set(), "seqs": set()})
        erp = mapping.accounts[code]
        d["net"][erp] += dr - cr
        d["src"][erp].add(code)
        d["entries"].add(entry_id)
        d["seqs"].add(seq)
    out = []
    for acct, date in sorted(days):
        d = days[(acct, date)]
        no = day_doc_no(date, acct)
        memo = f"MiniSTP {acct} {date.isoformat()} 일계 (전표 {len(d['entries'])}건)"
        events = f"{min(d['seqs'])}~{max(d['seqs'])}"
        line_no = 0
        for (erp_code, erp_name), net in sorted(d["net"].items()):
            if net == 0:
                continue
            line_no += 1
            out.append(DocLine(no, date, line_no, erp_code, erp_name, max(net, 0), max(-net, 0), memo,
                               ",".join(sorted(d["src"][(erp_code, erp_name)])), events, "", acct, aliases.get(acct, "")))
    return out


def _check_balanced(lines: list[DocLine]) -> None:
    docs: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for ln in lines:
        docs[ln.doc_no][0] += ln.debit
        docs[ln.doc_no][1] += ln.credit
    bad = [f"{no} 차변 {dr:,} 대변 {cr:,}" for no, (dr, cr) in docs.items() if dr != cr]
    if bad:
        raise ErpError("차대가 맞지 않는 전표가 있어 파일을 만들지 않았어요: " + "; ".join(bad))


def render_csv(result: Export) -> bytes:
    """The upload file: UTF-8 with BOM so Excel opens the Korean headers."""
    mapping = result.mapping
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(HEADER)
    for ln in result.lines:
        w.writerow([mapping.company, mapping.document_type, ln.doc_no, ln.date.isoformat(), mapping.currency,
                    ln.line_no, ln.erp_code, ln.erp_name, ln.debit, ln.credit, ln.memo,
                    ln.account_id, ln.account_name, ln.symbol, ln.ministp_accounts, ln.events, ln.reverses])
    return buf.getvalue().encode("utf-8-sig")


def _refuse_existing(*paths: pathlib.Path) -> None:
    for p in paths:
        if p.exists():
            raise ErpError(f"{p}이 이미 있어요. 같은 전표를 두 번 올리지 않도록 덮어쓰지 않아요. 지우거나 --out으로 다른 이름을 주세요.")


def _publish(path: pathlib.Path, data: bytes) -> pathlib.Path:
    """Write under `<name>.tmp`, flush to disk, then give it its name in one step. Never replaces a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.link(tmp, path)                 # fails if the name is taken, unlike a rename on POSIX
    except FileExistsError:
        raise ErpError(f"{path}이 이미 있어요. 덮어쓰지 않아요.") from None
    finally:
        tmp.unlink(missing_ok=True)
    return path


def write_csv(result: Export, path: str | pathlib.Path) -> pathlib.Path:
    """The upload file alone, written then renamed. Refuses to overwrite: a second file for the same range is
    how a document gets posted twice. `deliver` is what the command uses: it also keeps the send history."""
    p = pathlib.Path(path)
    _refuse_existing(p)
    return _publish(p, render_csv(result))


def control_path(path: str | pathlib.Path) -> pathlib.Path:
    p = pathlib.Path(path)
    return p.with_suffix(".ctl")


def control_text(result: Export, file_name: str, sha256: str, size: int, export_id: int, date_from: dt.date,
                 date_to: dt.date, created_at: dt.datetime) -> str:
    """The control file: what the receiver checks before posting. Plain `key=value` lines, UTF-8 without BOM."""
    rows = [("file", file_name), ("sha256", sha256), ("bytes", size), ("export_id", export_id),
            ("account", result.account_id or "ALL"), ("from", date_from.isoformat()), ("to", date_to.isoformat()),
            ("unit", UNITS[result.by]), ("company", result.mapping.company),
            ("document_type", result.mapping.document_type), ("documents", result.documents),
            ("lines", len(result.lines)), ("debit", result.debit), ("credit", result.credit),
            ("created_at", created_at.isoformat(timespec="seconds"))]
    return "".join(f"{k}={v}\n" for k, v in rows)


@dataclass(frozen=True)
class Clash:
    doc_no: str             # the document of this export
    sent_as: str            # the document already exported
    export_id: int
    same_document: bool     # False: the same account and day went out in the other unit


def _docs(result: Export) -> dict[str, tuple[str, dt.date]]:
    """doc_no -> (account, date) of each document of the export."""
    out: dict[str, tuple[str, dt.date]] = {}
    for ln in result.lines:
        out.setdefault(ln.doc_no, (ln.account_id, ln.date))
    return out


def clashes(conn: psycopg.Connection, result: Export) -> list[Clash]:
    """Documents of `result` that an export not voided already holds, or whose account and day it holds in the
    other unit (건별 and 일별 합계 of the same day would post that day twice)."""
    docs = _docs(result)
    if not docs:
        return []
    nos = list(docs)
    rows = conn.execute(
        "select x.doc_no, d.doc_no, d.export_id, d.doc_no = x.doc_no"
        "  from unnest(%s::text[], %s::text[], %s::date[]) as x (doc_no, account_id, doc_date)"
        "  join erp_export_docs d on d.doc_no = x.doc_no or (d.account_id = x.account_id and d.doc_date = x.doc_date)"
        "  join erp_exports e on e.export_id = d.export_id"
        " where not exists (select 1 from erp_export_voids v where v.export_id = d.export_id)"
        "   and (d.doc_no = x.doc_no or e.unit <> %s)"
        " order by x.doc_no, d.export_id",
        (nos, [docs[n][0] for n in nos], [docs[n][1] for n in nos], UNITS[result.by])).fetchall()
    return [Clash(*r) for r in rows]


def without(result: Export, doc_nos: set[str]) -> Export:
    return Export(result.by, [ln for ln in result.lines if ln.doc_no not in doc_nos], result.mapping, result.account_id)


@dataclass(frozen=True)
class Delivered:
    export_id: int
    result: Export
    csv: pathlib.Path
    control: pathlib.Path
    sha256: str
    skipped: int            # documents left out because they were already exported (`new_only`)


def deliver(conn: psycopg.Connection, result: Export, path: str | pathlib.Path, date_from: dt.date, date_to: dt.date,
            new_only: bool = False) -> Delivered:
    """`erp export`: the upload file, its control file and the send history, all or nothing (R-104).

    A document an export not voided already holds stops the export, unless `new_only`, which leaves those out.
    The same account and day in the other unit always stops it. The files are published inside the
    transaction and removed again if it does not commit, so a file on disk always has its history row."""
    p = pathlib.Path(path)
    ctl = control_path(p)
    _refuse_existing(p, ctl)
    skipped = 0
    with conn.transaction():
        found = clashes(conn, result)
        other_unit = [c for c in found if not c.same_document]
        if other_unit:
            raise ErpError("같은 계좌·날짜를 다른 단위(건별/일별 합계)로 이미 내보냈어요. ERP에 그날이 두 번 잡혀요: "
                           + _some([f"{c.doc_no} ↔ {c.sent_as} (내보내기 {c.export_id})" for c in other_unit])
                           + ". 먼저 낸 파일을 ERP가 받지 않았다면 erp void로 무효로 하세요.")
        if found and not new_only:
            raise ErpError(f"이미 내보낸 전표 {len({c.doc_no for c in found})}건이 들어 있어요: "
                           + _some([f"{c.doc_no} (내보내기 {c.export_id})" for c in found])
                           + ". 새 전표만 내려면 --new-only, ERP가 받지 않은 파일이면 erp void로 무효로 하세요.")
        if found:
            sent = {c.doc_no for c in found}
            skipped, result = len(sent), without(result, sent)
            if not result.lines:
                raise ErpError(f"새로 낼 전표가 없어요. 이 기간의 전표 {skipped}건은 모두 이미 내보냈어요.")
        data = render_csv(result)
        digest = hashlib.sha256(data).hexdigest()
        _, actor, _ = audit.origin()
        export_id, created_at = conn.execute(
            "insert into erp_exports (account_id, date_from, date_to, unit, company_code, document_type, mapping_sha256,"
            " documents, lines, debit_total, credit_total, file_name, file_sha256, created_by)"
            " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning export_id, created_at",
            (result.account_id, date_from, date_to, UNITS[result.by], result.mapping.company,
             result.mapping.document_type, result.mapping.sha256, result.documents, len(result.lines),
             result.debit, result.credit, p.name, digest, actor)).fetchone()
        with conn.cursor() as cur:
            cur.executemany("insert into erp_export_docs (export_id, doc_no, account_id, doc_date) values (%s, %s, %s, %s)",
                            [(export_id, no, a, d) for no, (a, d) in _docs(result).items()])
        audit.record(conn, "ERP_EXPORT", account_id=result.account_id, target=export_ref(export_id),
                     detail={"file": p.name, "sha256": digest, "unit": UNITS[result.by], "from": date_from,
                             "to": date_to, "documents": result.documents, "lines": len(result.lines),
                             "debit": result.debit, "skipped": skipped, "mapping_sha256": result.mapping.sha256})
        written = []
        try:
            written.append(_publish(p, data))
            written.append(_publish(ctl, control_text(result, p.name, digest, len(data), export_id, date_from,
                                                      date_to, created_at).encode("utf-8")))
        except BaseException:
            for w in written:
                w.unlink(missing_ok=True)
            raise
    return Delivered(export_id, result, p, ctl, digest, skipped)


def export_ref(export_id: int) -> str:
    return f"ERP-{export_id}"


def _some(items: list[str], n: int = 5) -> str:
    return ", ".join(items[:n]) + (f" 외 {len(items) - n}건" if len(items) > n else "")


def void(conn: psycopg.Connection, export_id: int, reason: str) -> None:
    """`erp void`: the ERP did not take this file, so its documents may be exported again (R-104).
    The history row stays; only a void row is added. Removing the file itself is the person's job."""
    if not (reason or "").strip():
        raise ErpError("무효 사유를 적어 주세요 (--reason).")
    with conn.transaction():
        row = conn.execute("select account_id, status from erp_exports_v where export_id = %s", (export_id,)).fetchone()
        if row is None:
            raise ErpError(f"내보내기 {export_id}번이 없어요. erp history로 확인하세요.")
        if row[1] == "VOIDED":
            raise ErpError(f"내보내기 {export_id}번은 이미 무효예요.")
        _, actor, _ = audit.origin()
        conn.execute("insert into erp_export_voids (export_id, reason, voided_by) values (%s, %s, %s)",
                     (export_id, reason.strip(), actor))
        audit.record(conn, "ERP_EXPORT_VOID", account_id=row[0], target=export_ref(export_id),
                     detail={"reason": reason.strip()})


def history(conn: psycopg.Connection, limit: int = 20) -> list[tuple]:
    """(번호, 계좌, 시작일, 종료일, 단위, 전표 수, 차변 합계, 파일, 상태, 내보낸 사람, 시각, 무효 사유), newest first."""
    return conn.execute(
        "select export_id, account_id, date_from, date_to, unit, documents, debit_total, file_name, status,"
        " created_by, created_at, void_reason from erp_exports_v order by export_id desc limit %s", (limit,)).fetchall()


def problems(conn: psycopg.Connection) -> list[str]:
    """Where the send history does not hold together: an export whose document rows do not match its count,
    or a document in two exports that are not voided (the trigger refuses it; this finds drift)."""
    bad = [f"내보내기 {i}: 전표 {n}건인데 전표 행 {m}개" for i, n, m in conn.execute(
        "select e.export_id, e.documents, count(d.doc_no) from erp_exports e"
        " left join erp_export_docs d using (export_id) group by e.export_id, e.documents"
        " having count(d.doc_no) <> e.documents order by 1")]
    bad += [f"{no}: 내보내기 {ids}" for no, ids in conn.execute(
        "select d.doc_no, string_agg(d.export_id::text, ', ' order by d.export_id) from erp_export_docs d"
        " where not exists (select 1 from erp_export_voids v where v.export_id = d.export_id)"
        " group by d.doc_no having count(*) > 1 order by 1")]
    return bad


def default_out(date_from: dt.date, date_to: dt.date, by: str, account_id: str | None = DEFAULT_ACCOUNT) -> pathlib.Path:
    kind = "건별" if by == "entry" else "일별"
    span = f"{date_from:%Y%m%d}" if date_from == date_to else f"{date_from:%Y%m%d}-{date_to:%Y%m%d}"
    return pathlib.Path("erp-export") / f"전표_{account_id or '전체계좌'}_{span}_{kind}.csv"


# ---------- ERP 3자 대사 (R-106): MiniSTP 장부, ERP 총계정원장, 증권사 ----------
# The ERP keeps the company's books; MiniSTP keeps one brokerage account's part of them; the broker keeps the
# account itself. They agree when, account by ERP account, the three say the same amount.
# - 잔액 (balance on the as-of date) for assets and liabilities, 기간 (amount booked in the period) for the rest:
#   the ERP closes its income accounts every year, MiniSTP does not. 선납세금 (1040) is a period amount too: the
#   ERP sets it off against the year's corporate tax at year end. 본사 자금 대체 (3010) is what moved between the
#   company's bank and the brokerage account in the period.
# - The broker's side exists for two accounts only: 증권예치금 (1010) against the broker's 예수금 and FVPL 금융자산
#   (1100) against its 매입금액, both from the last daily recon of the day (R-70). The rest the broker does not keep.
# Amounts are signed debit-positive (차변 +, 대변 −), the way an ERP trial balance lists them. Nothing is written.

PERIOD_ACCOUNTS = {"1040"}           # assets read as a period amount (see above)
BROKER_ITEMS = {"1010": "CASH_D0", "1100": "BOOK_COST"}
ERP_HEADERS = {"code": ("계정코드", "ERP계정", "code"), "name": ("계정명", "name"), "amount": ("잔액", "금액", "amount"),
               "debit": ("차변", "debit"), "credit": ("대변", "credit")}


def _is_period(ministp_code: str) -> bool:
    return ministp_code[0] in "345" or ministp_code in PERIOD_ACCOUNTS


@dataclass(frozen=True)
class ReconLine:
    erp_code: str
    erp_name: str
    basis: str                  # "잔액" or "기간"
    ministp: int
    erp: int | None             # None: the ERP file does not have the account
    broker: int | None          # None: the broker does not keep this account (or no recon that day)
    ministp_accounts: str

    @property
    def erp_diff(self) -> int | None:
        return None if self.erp is None else self.erp - self.ministp

    @property
    def broker_diff(self) -> int | None:
        return None if self.broker is None else self.broker - self.ministp

    @property
    def ok(self) -> bool:
        return self.erp_diff == 0 and self.broker_diff in (None, 0)


@dataclass
class Recon:
    as_of: dt.date
    period_from: dt.date
    account_id: str | None
    lines: list[ReconLine]
    erp_only: list[tuple[str, str, int]]    # ERP rows of a mapped code with no MiniSTP amount... or not mapped at all
    broker_note: str

    @property
    def ok(self) -> bool:
        return all(ln.ok for ln in self.lines)


def load_erp_balances(path: str | pathlib.Path) -> dict[str, tuple[str, int]]:
    """The ERP side: a CSV of ERP account code and amount (잔액, or 차변 and 대변), debit-positive, as the ERP's
    계정별 원장/시산표 exports it. Codes that appear twice are added up. UTF-8 with or without BOM."""
    p = pathlib.Path(path)
    if not p.exists():
        raise ErpError(f"ERP 잔액 파일 {p}이 없어요.")
    rows = list(csv.reader(p.read_text(encoding="utf-8-sig").splitlines()))
    if not rows:
        raise ErpError(f"{p}이 비어 있어요.")
    head = [h.strip() for h in rows[0]]

    def col(key):
        return next((head.index(h) for h in ERP_HEADERS[key] if h in head), None)
    code_i, name_i, amt_i, dr_i, cr_i = col("code"), col("name"), col("amount"), col("debit"), col("credit")
    if code_i is None or (amt_i is None and (dr_i is None or cr_i is None)):
        raise ErpError(f"{p}의 머리글에 계정코드와 잔액(또는 차변·대변) 열이 있어야 해요: 지금 {', '.join(head)}")

    def num(v: str, line: int) -> int:
        t = (v or "").strip().replace(",", "")
        if t in ("", "-"):
            return 0
        try:
            return int(t)
        except ValueError:
            raise ErpError(f"{p} {line}번째 줄의 금액 {v!r}이 원 단위 정수가 아니에요.") from None
    out: dict[str, tuple[str, int]] = {}
    for i, r in enumerate(rows[1:], start=2):
        if not r or not r[code_i].strip():
            continue
        code = r[code_i].strip()
        amount = num(r[amt_i], i) if amt_i is not None else num(r[dr_i], i) - num(r[cr_i], i)
        name = r[name_i].strip() if name_i is not None and name_i < len(r) else ""
        prev = out.get(code, (name, 0))
        out[code] = (prev[0] or name, prev[1] + amount)
    return out


def reconcile(conn: psycopg.Connection, mapping: Mapping | None, erp_rows: dict[str, tuple[str, int]],
              as_of: dt.date, period_from: dt.date | None = None, account_id: str | None = None) -> Recon:
    """`erp recon`: MiniSTP against the ERP file and the broker, per ERP account (R-106). `account_id` None takes
    every brokerage account together, as the ERP keeps them."""
    mapping = mapping or own_chart(conn)
    period_from = period_from or dt.date(as_of.year, 1, 1)
    if period_from > as_of:
        raise ErpError("기간 시작일이 기준일보다 늦어요.")
    if account_id is not None and account_id not in _aliases(conn):
        raise ErpError(f"계좌 {account_id}가 없어요. account list로 확인하세요.")
    rows = conn.execute(
        "select l.account_code, coalesce(sum(l.debit - l.credit), 0),"
        "       coalesce(sum(l.debit - l.credit) filter (where e.date >= %(f)s), 0)"
        "  from journal_lines l join journal_entries e using (entry_id)"
        " where e.date <= %(t)s and (%(a)s::text is null or e.account_id = %(a)s)"
        " group by l.account_code", {"f": period_from, "t": as_of, "a": account_id}).fetchall()
    unmapped = sorted({c for c, bal, per in rows if (bal or per)} - set(mapping.accounts))
    if unmapped:
        raise ErpError("매핑이 없는 계정이 있어 대사하지 않았어요: "
                       + ", ".join(f"{c} {MINISTP_NAMES.get(c, '')}".strip() for c in unmapped))
    erp_side: dict[tuple[str, str], dict] = {}
    for code, balance, period in rows:
        if code not in mapping.accounts:
            continue
        erp_code, erp_name = mapping.accounts[code]
        basis = "기간" if _is_period(code) else "잔액"
        d = erp_side.setdefault((erp_code, erp_name), {"basis": basis, "amount": 0, "src": set()})
        if d["basis"] != basis:
            raise ErpError(f"ERP 계정 {erp_code}에 잔액으로 볼 계정과 기간으로 볼 계정이 함께 매핑돼 있어요. "
                           "3자 대사를 하려면 매핑을 나눠 주세요.")
        d["amount"] += int(period if basis == "기간" else balance)
        d["src"].add(code)
    broker, note = _broker_side(conn, as_of, account_id)
    lines = []
    for (erp_code, erp_name), d in sorted(erp_side.items()):
        src = sorted(d["src"])
        legs = [broker[c] for c in src if c in BROKER_ITEMS and c in broker]
        has_broker = bool(legs) and all(c in BROKER_ITEMS for c in src)
        e = erp_rows.get(erp_code)
        lines.append(ReconLine(erp_code, erp_name or (e[0] if e else ""), d["basis"], d["amount"],
                               e[1] if e else None, sum(legs) if has_broker else None, ",".join(src)))
    seen = {ln.erp_code for ln in lines}
    mapped = {v[0] for v in mapping.accounts.values()}
    erp_only = [(c, n, a) for c, (n, a) in sorted(erp_rows.items()) if c not in seen and c in mapped and a]
    return Recon(as_of, period_from, account_id, lines, erp_only, note)


def _broker_side(conn: psycopg.Connection, as_of: dt.date, account_id: str | None) -> tuple[dict[str, int], str]:
    """The broker's 예수금 and 매입금액 from each account's last daily recon of the as-of date (R-70)."""
    rows = conn.execute(
        "with last as (select account_id, max(run_id) as run_id from recon_results"
        "               where date = %(d)s and item in ('CASH_D0', 'BOOK_COST')"
        "                 and (%(a)s::text is null or account_id = %(a)s) group by account_id)"
        " select r.item, sum(r.broker_value), count(distinct r.account_id)"
        "   from recon_results r join last using (account_id, run_id)"
        "  where r.item in ('CASH_D0', 'BOOK_COST') and r.supersedes_id is null group by r.item",
        {"d": as_of, "a": account_id}).fetchall()
    wanted = 1 if account_id else conn.execute("select count(*) from trading_accounts").fetchone()[0]
    got = {item: (int(v), n) for item, v, n in rows}
    if not got:
        return {}, f"{as_of} 일 마감 대사가 없어 증권사 쪽은 비워 뒀어요."
    out = {}
    for code, item in BROKER_ITEMS.items():
        if item in got and got[item][1] == wanted:
            out[code] = got[item][0]
    if not out:
        return {}, f"{as_of} 일 마감 대사가 모든 계좌에 있지 않아 증권사 쪽은 비워 뒀어요."
    return out, f"{as_of} 일 마감 대사의 증권사 예수금·매입금액"


RECON_HEADER = ["ERP계정", "계정명", "기준", "MiniSTP", "ERP", "ERP차이", "증권사", "증권사차이", "결과", "MiniSTP계정"]


def write_recon_csv(recon: Recon, path: str | pathlib.Path) -> pathlib.Path:
    p = pathlib.Path(path)
    _refuse_existing(p)
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(RECON_HEADER)
    for ln in recon.lines:
        w.writerow([ln.erp_code, ln.erp_name, ln.basis, ln.ministp, "" if ln.erp is None else ln.erp,
                    "" if ln.erp_diff is None else ln.erp_diff, "" if ln.broker is None else ln.broker,
                    "" if ln.broker_diff is None else ln.broker_diff, "일치" if ln.ok else "불일치", ln.ministp_accounts])
    return _publish(p, buf.getvalue().encode("utf-8-sig"))
