"""The integrity checks as numbers side by side: what is stored, what it has to equal, the difference.

`integrity.py` answers each check with yes or no and a sentence. A screen that
shows a reconciliation wants the two values next to each other, the way the
broker reconciliation shows 내부 값 and 증권사 값 (STP-2110 원장 내부 대사).

A row here never decides a check: the verdict stays with `integrity.py`. A check
this module has no rows for is shown by its sentence alone, so a check added
there appears on the screen without a change here.

It only reads.
"""
from collections import Counter
from dataclasses import dataclass

import psycopg

from ..accounting import Account
from ..ledger import Ledger
from .integrity import APPEND_ONLY_TABLES, BALANCE_TRIGGERS
from .postgres import cash_rows, from_payload, position_row

REPLAY = "이벤트를 처음부터 다시 계산한 값"


@dataclass(frozen=True)
class Compared:
    item: str             # what is compared
    stored: int | None    # what the database holds
    expected: int | None  # what it has to equal
    basis: str            # where the expected value comes from

    @property
    def ok(self) -> bool:
        return self.stored == self.expected

    @property
    def diff(self) -> int | None:
        return None if self.stored is None or self.expected is None else self.stored - self.expected


def replay(conn: psycopg.Connection, calendar, fees, instruments) -> Ledger:
    """The books as the events alone give them. Raises what `Ledger.replay` raises on an event the rules reject."""
    events = [from_payload(t, p) for t, p in conn.execute(
        "select event_type, payload from ledger_events order by seq")]
    return Ledger.replay(events, calendar, fees, instruments)


def _unlike(stored: list[tuple], expected: list[tuple]) -> int:
    """Rows as multisets: how many are on one side only."""
    a, b = Counter(stored), Counter(expected)
    return sum(((a - b) + (b - a)).values())


# ---------- checks that need no replay ----------

def compare_events(conn: psycopg.Connection) -> list[Compared]:
    n, lo, hi = conn.execute("select count(*), min(seq), max(seq) from ledger_events").fetchone()
    if n == 0:
        return []
    return [Compared("이벤트 건수", n, hi, "마지막 순번 (빈 번호가 없으면 건수와 같음)"),
            Compared("첫 순번", lo, 1, "1번부터 시작")]


def compare_guards(conn: psycopg.Connection) -> list[Compared]:
    live = {name for name, enabled in conn.execute(
        "select t.tgname, t.tgenabled from pg_trigger t join pg_class c on c.oid = t.tgrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = current_schema() and not t.tgisinternal") if enabled in ("O", "A")}
    rows = [Compared(t, sum(f"{t}_{kind}" in live for kind in ("append_only", "no_truncate")), 2,
                     "수정·삭제 금지와 비우기 금지 트리거 2개") for t in APPEND_ONLY_TABLES]
    return rows + [Compared("분개 대차 검사", sum(t in live for t in BALANCE_TRIGGERS), len(BALANCE_TRIGGERS),
                            "전표와 분개 줄의 대차 검사 트리거")]


def compare_balanced(conn: psycopg.Connection) -> list[Compared]:
    bad, dr, cr = conn.execute(
        "select count(*) filter (where dr <> cr or lines = 0), coalesce(sum(dr), 0), coalesce(sum(cr), 0) from ("
        "  select e.entry_id, coalesce(sum(l.debit), 0) dr, coalesce(sum(l.credit), 0) cr, count(l.line_id) lines"
        "  from journal_entries e left join journal_lines l using (entry_id) group by e.entry_id) x").fetchone()
    return [Compared("차변 합계", int(dr), int(cr), "대변 합계"),
            Compared("대차가 맞지 않는 분개 수", bad, 0, "하나도 없어야 함")]


# ---------- stored rows against a replay of the events ----------

def compare_journal(conn: psycopg.Connection, ledger: Ledger) -> list[Compared]:
    stored: dict[int, tuple] = {}
    for eid, date, kind, src, acct, dr, cr in conn.execute(
            "select e.entry_id, e.date, e.kind, e.source_id, l.account_code, l.debit, l.credit"
            " from journal_entries e join journal_lines l using (entry_id) order by e.entry_id, l.line_id"):
        stored.setdefault(eid, ((date, kind, src), []))[1].append((acct, dr, cr))
    db = [(head, sorted(lines)) for head, lines in stored.values()]
    mem = [((e.date, e.kind, e.source_id), sorted((ln.account.code, ln.debit, ln.credit) for ln in e.lines))
           for e in ledger.journal]
    unlike = sum(a != b for a, b in zip(db, mem)) + abs(len(db) - len(mem))
    return [Compared("분개 건수", len(db), len(mem), REPLAY),
            Compared("분개 줄 수", sum(len(lines) for _, lines in db), sum(len(lines) for _, lines in mem), REPLAY),
            Compared("차변 합계", int(sum(dr for _, lines in db for _, dr, _ in lines)),
                     sum(dr for _, lines in mem for _, dr, _ in lines), REPLAY),
            Compared("순서대로 견주어 내용이 다른 분개 수", unlike, 0, "하나도 없어야 함")]


def compare_accounts(conn: psycopg.Connection, ledger: Ledger) -> list[Compared]:
    stored = dict(conn.execute("select code, balance from account_balances_v"))
    return [Compared(f"{a.code} {a.value[1]}", int(stored.get(a.code, 0)), ledger.account_balance(a), REPLAY)
            for a in Account]


def compare_positions(conn: psycopg.Connection, ledger: Ledger) -> list[Compared]:
    stored = conn.execute("select trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                          " from position_ledger").fetchall()
    expected = [r for r in (position_row(ledger, ev) for ev in ledger.events) if r is not None]
    totals = {s: (int(q), int(c)) for s, q, c in conn.execute("select symbol, qty, book_cost from positions_v")}
    mem = {s: (p.qty, p.book_cost) for s, p in ledger.positions().items()}
    rows = [Compared("보유 원장 행 수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 보유 원장 행 수", _unlike(stored, expected), 0, "하나도 없어야 함")]
    for s in sorted(set(totals) | set(mem)):
        (q, c), (mq, mc) = totals.get(s, (None, None)), mem.get(s, (None, None))
        rows += [Compared(f"{s} 보유수량", q, mq, REPLAY), Compared(f"{s} 장부금액", c, mc, REPLAY)]
    return rows


def compare_cash(conn: psycopg.Connection, ledger: Ledger) -> list[Compared]:
    stored = conn.execute("select date, reason, amount, ref_id from cash_ledger").fetchall()
    expected = [r for e in ledger.journal for r in cash_rows(e)]
    balance = int(conn.execute("select balance from cash_balance_v").fetchone()[0])
    return [Compared("예수금", balance, ledger.cash_balance(), REPLAY),
            Compared("현금 원장 행 수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 현금 원장 행 수", _unlike(stored, expected), 0, "하나도 없어야 함")]


PLAIN = {"events": compare_events, "guards": compare_guards, "balanced": compare_balanced}
REPLAYED = {"journal": compare_journal, "accounts": compare_accounts, "positions": compare_positions,
            "cash": compare_cash}


def compared(conn: psycopg.Connection, key: str, calendar, fees, instruments) -> list[Compared]:
    """The numbers behind check `key`. Empty for a check that has none here (its sentence says it all)
    and for a replay check when the events cannot be replayed (the 이벤트 재생 check says why)."""
    top = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with conn.transaction():
        if top:   # one snapshot, as in integrity_report
            conn.execute("set transaction isolation level repeatable read read only")
        if key in PLAIN:
            return PLAIN[key](conn)
        if key not in REPLAYED:
            return []
        try:
            ledger = replay(conn, calendar, fees, instruments)
        except Exception:
            return []
        return REPLAYED[key](conn, ledger)
