"""ERP 일반전표 내보내기 (spec D9, R-96): the journal, as a general-journal upload file for the company ERP.

Reads `journal_entries` / `journal_lines` and writes a CSV; never writes to the DB, so it may run beside `run`.
Without a mapping file the documents carry MiniSTP's own chart (`accounts_chart`) as is. A mapping file
(YAML, not a table: the operating schema stays as it is until the gate) turns them into the company's account
codes; then a file is written only when every account in the range is mapped. Every document must balance.

Two shapes:
- 건별 (`entry`): one document per journal entry. 증빙번호 `MSTP-<이벤트 번호>-<k>` (k-th entry the event
  wrote), so the ERP can refuse a document it already has. A reversal points at the document it reverses.
- 일별 합계 (`day`): one document per day and account, net amount per ERP account. Only for days whose daily
  close is done (NAV recorded), since a day still open can gain entries and its total would change.

One account at a time by default (the CLI's `--account`); `account_id=None` takes every account into one file.
Every line carries its account (계좌, 계좌명) and, when the entry is about one stock, the stock (종목코드): the
관리항목 an ERP keeps beside the account code, so one ERP account can still be split by brokerage account and stock.
"""
import csv
import datetime as dt
import pathlib
from collections import defaultdict
from dataclasses import dataclass

import psycopg
import yaml

from .accounting import Account
from .store import DEFAULT_ACCOUNT

BY = ("entry", "day")
HEADER = ["회사코드", "전표유형", "증빙번호", "전표일자", "통화", "줄번호", "계정코드", "계정명", "차변", "대변", "적요",
          "계좌", "계좌명", "종목코드", "MiniSTP계정", "원장이벤트", "반대전표대상"]
KIND_NAMES = {"DEPOSIT": "투자원금 입금", "OPENING": "기초 보유", "EXECUTION": "체결", "SETTLEMENT": "결제",
              "VALUATION": "공정가치 평가", "VALUATION_REVERSAL": "전일 평가 역분개"}
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


def load_mapping(path: str | pathlib.Path) -> Mapping:
    p = pathlib.Path(path)
    if not p.exists():
        raise ErpError(f"계정 매핑 파일 {p}이 없어요. docs/erp/mapping.example.yaml을 복사해 회사 계정코드로 고쳐 쓰세요.")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    missing = [k for k in ("company", "document_type", "accounts") if not raw.get(k)]
    if missing:
        raise ErpError(f"매핑 파일에 {', '.join(missing)} 항목이 없어요.")
    accounts = {}
    for code, v in raw["accounts"].items():
        if not isinstance(v, dict) or not v.get("code"):
            raise ErpError(f"매핑 파일의 계정 {code}에 code가 없어요.")
        accounts[str(code)] = (str(v["code"]), str(v.get("name", "")))
    return Mapping(str(raw["company"]), str(raw["document_type"]), str(raw.get("currency", "KRW")), accounts)


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
        "select payload from ledger_events where event_type = 'ExecutionEvent' and payload->>'exec_id' = any(%s)",
        (exec_ids,))} if exec_ids else {}

    def stock(sym: str) -> str:
        return f"{sym} {names.get(sym, '')}".strip()

    out = {}
    for seq, (t, p) in events.items():
        if t == "ExecutionEvent":
            out[seq] = f"{SIDE_NAMES.get(p['side'], p['side'])} 체결 {stock(p['symbol'])} {p['qty']:,}주 @{p['price']:,}"
        elif t == "SettlementEvent":
            f = fills.get(p["exec_id"])
            what = f"{SIDE_NAMES.get(f['side'], f['side'])} 결제 {stock(f['symbol'])} {f['qty']:,}주" if f else "결제"
            out[seq] = f"{what} (체결 {p['exec_id']})"
        elif t == "DepositEvent":
            out[seq] = "투자원금 입금"
        elif t == "OpeningPositionEvent":
            out[seq] = f"기초 보유 {stock(p['symbol'])} {p['qty']:,}주"
        elif t in ("NavEvent", "RevaluationEvent"):
            out[seq] = f"{p['date']} 종가 공정가치 평가"
    return out


def _closed_days(conn: psycopg.Connection, f: dt.date, t: dt.date) -> set[tuple[str, dt.date]]:
    """(account, day) pairs whose daily close is done."""
    return {(a, dt.date.fromisoformat(d)) for a, d in conn.execute(
        "select account_id, payload->>'date' from ledger_events where event_type = 'NavEvent'"
        " and (payload->>'date')::date between %s and %s", (f, t))}


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
        closed = _closed_days(conn, date_from, date_to)
        open_days = sorted({(r[11], r[3]) for r in rows} - closed)
        if open_days:
            raise ErpError("일 마감 전인 날은 일별 합계를 만들 수 없어요(전표가 더 생길 수 있어요): "
                           + ", ".join(f"{a} {d.isoformat()}" for a, d in open_days))
        result = Export(by, _by_day(rows, mapping, aliases), mapping, account_id)
    _check_balanced(result.lines)
    return result


def _by_entry(conn, rows, mapping: Mapping, aliases: dict[str, str]) -> list[DocLine]:
    seqs = {r[1] for r in rows}
    memos, symbols = _memos(conn, seqs), _symbols(conn, seqs)
    out, line_no, current = [], 0, None
    for entry_id, seq, k, date, kind, source_id, rev_seq, rev_k, code, dr, cr, acct in rows:
        if entry_id != current:
            current, line_no = entry_id, 0
        line_no += 1
        erp_code, erp_name = mapping.accounts[code]
        memo = memos.get(seq) or KIND_NAMES.get(kind, kind)
        reverses = doc_no(rev_seq, rev_k) if rev_seq is not None else ""
        if reverses:
            memo = f"{KIND_NAMES.get(kind, kind)} (반대전표: {reverses})"
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


def write_csv(result: Export, path: str | pathlib.Path) -> pathlib.Path:
    """UTF-8 with BOM so Excel opens the Korean headers. Refuses to overwrite: a second file for the same
    range is how a document gets posted twice."""
    p = pathlib.Path(path)
    if p.exists():
        raise ErpError(f"{p}이 이미 있어요. 같은 전표를 두 번 올리지 않도록 덮어쓰지 않아요. 지우거나 --out으로 다른 이름을 주세요.")
    p.parent.mkdir(parents=True, exist_ok=True)
    mapping = result.mapping
    with p.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for ln in result.lines:
            w.writerow([mapping.company, mapping.document_type, ln.doc_no, ln.date.isoformat(), mapping.currency,
                        ln.line_no, ln.erp_code, ln.erp_name, ln.debit, ln.credit, ln.memo,
                        ln.account_id, ln.account_name, ln.symbol, ln.ministp_accounts, ln.events, ln.reverses])
    return p


def default_out(date_from: dt.date, date_to: dt.date, by: str, account_id: str | None = DEFAULT_ACCOUNT) -> pathlib.Path:
    kind = "건별" if by == "entry" else "일별"
    span = f"{date_from:%Y%m%d}" if date_from == date_to else f"{date_from:%Y%m%d}-{date_to:%Y%m%d}"
    return pathlib.Path("erp-export") / f"전표_{account_id or '전체계좌'}_{span}_{kind}.csv"
