"""Ledger integrity report: is what the database holds exactly what the events say?

Every command already refuses to start when the stored rows differ from a replay
(`load_books`). This module is the same comparison made readable, plus the
checks a replay cannot make: that the event numbers have no gap, that the
guards which keep the ledger append-only are still switched on, and that every
journal entry balances.

It only reads. Run it before and after a migration, or whenever someone asks
"how do you know the ledger is right?".

ERD v2.0: the event numbers, guards and balances are checked for the whole
database; the replay comparison is made account by account, because each
account is its own set of books inside the one event stream.
"""
from collections import Counter
from dataclasses import dataclass, replace

import psycopg

from .. import audit
from ..accounting import Account
from ..ledger import Ledger
from ..ledger import DayClosedEvent
from .postgres import DEFAULT_ACCOUNT, cash_rows, close_row, from_payload, position_row

# Tables no one may UPDATE, DELETE or TRUNCATE (R-04). Same list as the loop in schema.sql;
# tests/unit/test_integrity_tables.py keeps the two in step.
APPEND_ONLY_TABLES = ("ledger_events", "journal_entries", "journal_lines", "position_ledger", "cash_ledger",
                      "instruments", "oms_orders", "oms_order_acks", "broker_snapshots", "recon_results",
                      "customers", "trading_accounts", "account_status_history", "day_closes", "audit_log")
BALANCE_TRIGGERS = ("journal_entries_balanced", "journal_lines_balanced")
# R-78 in the database: no entry, cash row or close dated inside a closed day (ERD v2.2).
CLOSED_DAY_TRIGGERS = ("journal_entries_closed_day", "cash_ledger_closed_day", "day_closes_closed_day")
AUDIT_CHAIN_TRIGGER = "audit_log_chain"   # numbers, stamps and links each audit row (ERD v2.3)
SHOWN = 3   # how many differing items a detail line names


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    ok: bool
    detail: str
    account: str = ""    # empty: a check of the whole database


def _some(items) -> str:
    items = [str(i) for i in items]
    return ", ".join(items[:SHOWN]) + (f" 외 {len(items) - SHOWN}건" if len(items) > SHOWN else "")


# ---------- checks that need no replay ----------

def check_event_stream(conn: psycopg.Connection) -> Check:
    n, lo, hi = conn.execute("select count(*), min(seq), max(seq) from ledger_events").fetchone()
    if n == 0:
        return Check("events", "이벤트 순번", True, "이벤트가 아직 없어요")
    if (lo, hi) != (1, n):
        missing = [r[0] for r in conn.execute(
            "select g from generate_series(1, %s) g where not exists"
            " (select 1 from ledger_events where seq = g) order by g limit %s", (hi, SHOWN + 1))]
        return Check("events", "이벤트 순번", False, f"{n:,}건인데 번호가 {lo}~{hi}예요. 빈 번호: {_some(missing)}")
    return Check("events", "이벤트 순번", True, f"1~{n:,}, 빈 번호 없음")


def check_guards(conn: psycopg.Connection) -> Check:
    """The triggers that forbid changing ledger rows exist and are enabled."""
    live = {name for name, enabled in conn.execute(
        "select t.tgname, t.tgenabled from pg_trigger t join pg_class c on c.oid = t.tgrelid"
        " join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = current_schema() and not t.tgisinternal") if enabled in ("O", "A")}
    want = [f"{t}_{kind}" for t in APPEND_ONLY_TABLES for kind in ("append_only", "no_truncate")]
    want += BALANCE_TRIGGERS + CLOSED_DAY_TRIGGERS + (AUDIT_CHAIN_TRIGGER,)
    off = [w for w in want if w not in live]
    if off:
        return Check("guards", "수정·삭제 방지", False, f"꺼져 있거나 없는 트리거: {_some(off)}")
    return Check("guards", "수정·삭제 방지", True,
                 f"{len(APPEND_ONLY_TABLES)}개 테이블의 수정·삭제·비우기 금지, 분개 대차 검사, 마감일 잠금, 감사 로그 사슬이 켜져 있음")


def check_balanced(conn: psycopg.Connection) -> Check:
    bad, n, dr, cr = conn.execute(
        "select count(*) filter (where dr <> cr or lines = 0), count(*),"
        " coalesce(sum(dr), 0), coalesce(sum(cr), 0) from ("
        "  select e.entry_id, coalesce(sum(l.debit), 0) dr, coalesce(sum(l.credit), 0) cr, count(l.line_id) lines"
        "  from journal_entries e left join journal_lines l using (entry_id) group by e.entry_id) x").fetchone()
    if bad or dr != cr:
        ids = [r[0] for r in conn.execute(
            "select e.entry_id from journal_entries e left join journal_lines l using (entry_id)"
            " group by e.entry_id having coalesce(sum(l.debit), 0) <> coalesce(sum(l.credit), 0)"
            " or count(l.line_id) = 0 order by e.entry_id limit %s", (SHOWN + 1,))]
        return Check("balanced", "분개 대차", False, f"대차가 맞지 않는 분개 {bad:,}건 (분개번호 {_some(ids)})")
    return Check("balanced", "분개 대차", True, f"분개 {n:,}건 모두 차변 = 대변 (합계 {int(dr):,})")


def check_audit(conn: psycopg.Connection) -> Check:
    """R-87: the audit rows are numbered 1..n and each carries the hash of its content and of the row before."""
    n, last, bad = audit.chain_problems(conn, SHOWN + 1)
    if bad:
        return Check("audit", "감사 로그", False,
                     f"사슬이 이어지지 않는 줄: {_some(bad)}번 (고쳤거나, 지웠거나, 끼워 넣은 흔적)")
    if n == 0:
        return Check("audit", "감사 로그", True, "기록이 아직 없어요")
    return Check("audit", "감사 로그", True, f"{n:,}줄의 번호와 해시 사슬이 이어짐 (마지막 줄 {n:,}번, 해시 {last[:12]})")


# ---------- stored rows against a replay of the events ----------

def check_journal(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    stored: dict[int, tuple] = {}
    for eid, date, kind, src, acct, dr, cr in conn.execute(
            "select e.entry_id, e.date, e.kind, e.source_id, l.account_code, l.debit, l.credit"
            " from journal_entries e join journal_lines l using (entry_id) where e.account_id = %s"
            " order by e.entry_id, l.line_id", (account_id,)):
        stored.setdefault(eid, ((date, kind, src), []))[1].append((acct, dr, cr))
    db = [(eid, head, sorted(lines)) for eid, (head, lines) in stored.items()]
    mem = [((e.date, e.kind, e.source_id), sorted((ln.account.code, ln.debit, ln.credit) for ln in e.lines))
           for e in ledger.journal]
    for i, m in enumerate(mem):
        if i >= len(db) or db[i][1:] != m:
            where = f"분개번호 {db[i][0]}" if i < len(db) else "저장된 분개가 모자라요"
            date, kind, src = m[0]
            return Check("journal", "분개 재생", False,
                         f"{i + 1}번째 분개({date} {kind} {src})가 재생 결과와 달라요: {where}")
    if len(db) > len(mem):
        return Check("journal", "분개 재생", False,
                     f"이벤트에 없는 분개가 {len(db) - len(mem):,}건 저장돼 있어요 (분개번호 {db[len(mem)][0]}부터)")
    return Check("journal", "분개 재생", True, f"저장된 분개 {len(db):,}건 = 이벤트 재생 결과")


def check_accounts(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    stored = dict(conn.execute("select code, balance from account_balances_v where account_id = %s", (account_id,)))
    diff = [f"{a.code} 저장 {int(stored.get(a.code, 0)):,} / 재생 {ledger.account_balance(a):,}"
            for a in Account if stored.get(a.code, 0) != ledger.account_balance(a)]
    if diff:
        return Check("accounts", "계정 잔액", False, f"다른 계정: {_some(diff)}")
    return Check("accounts", "계정 잔액", True, f"계정 {len(Account)}개 일치")


def _row_diff(stored: list[tuple], expected: list[tuple], label) -> list[str]:
    """Rows as multisets: what is stored but not expected, and expected but not stored."""
    extra, missing = Counter(stored) - Counter(expected), Counter(expected) - Counter(stored)
    return [f"저장에만 {label(r)}" for r in extra] + [f"재생에만 {label(r)}" for r in missing]


def check_positions(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    stored = conn.execute("select trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                          " from position_ledger where account_id = %s", (account_id,)).fetchall()
    expected = [r for r in (position_row(ledger, ev) for ev in ledger.events) if r is not None]
    diff = _row_diff(stored, expected, lambda r: f"{r[2]}({r[1]} {r[3]:+,}주 {r[4]:+,}원)")
    totals = {s: (int(q), int(c)) for s, q, c in conn.execute(
        "select symbol, qty, book_cost from positions_v where account_id = %s", (account_id,))}
    mem = {s: (p.qty, p.book_cost) for s, p in ledger.positions().items()}
    diff += [f"{s} 저장 {totals.get(s)} / 재생 {mem.get(s)}" for s in sorted(set(totals) | set(mem))
             if totals.get(s) != mem.get(s)]
    if diff:
        return Check("positions", "보유", False, f"다른 행: {_some(diff)}")
    return Check("positions", "보유", True, f"보유 원장 {len(stored):,}행, {len(mem)}종목의 수량·장부금액 일치")


def check_cash(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    stored = conn.execute("select date, reason, amount, ref_id from cash_ledger where account_id = %s",
                          (account_id,)).fetchall()
    expected = [r for e in ledger.journal for r in cash_rows(e)]
    diff = _row_diff(stored, expected, lambda r: f"{r[0]} {r[1]} {r[3]} {r[2]:+,}원")
    balance = int(conn.execute("select coalesce(sum(amount), 0) from cash_ledger where account_id = %s",
                               (account_id,)).fetchone()[0])
    view = conn.execute("select balance from cash_balance_v where account_id = %s", (account_id,)).fetchone()
    if view is None or int(view[0]) != balance:
        diff.append(f"예수금 뷰 {view and int(view[0])} / 현금 원장 합계 {balance:,}")
    if balance != ledger.cash_balance():
        diff.append(f"예수금 저장 {balance:,} / 재생 {ledger.cash_balance():,}")
    if diff:
        return Check("cash", "현금", False, f"다른 행: {_some(diff)}")
    return Check("cash", "현금", True, f"현금 원장 {len(stored):,}행, 예수금 {balance:,}원 일치")


def check_closes(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    """R-77: every stored close is a close event of the replay, at the NAV the replay closed at."""
    stored = conn.execute(
        "select c.date, c.net_assets, c.units, c.nav_per_1000, e.event_type, (e.payload ->> 'date')::date"
        " from day_closes c join ledger_events e on e.seq = c.event_seq where c.account_id = %s",
        (account_id,)).fetchall()
    diff = [f"{d} 마감 행이 가리키는 이벤트가 그날의 마감 이벤트가 아니에요 ({t} {on})"
            for d, *_, t, on in stored if (t, on) != ("DayClosedEvent", d)]
    expected = [close_row(ledger, ev) for ev in ledger.events if isinstance(ev, DayClosedEvent)]
    diff += _row_diff([r[:4] for r in stored], expected, lambda r: f"{r[0]} 순자산 {int(r[1]):,} 기준가 {r[3]}")
    # R-79: the ABOR view (rows up to the last close) holds what a replay up to that close holds
    abor = ledger.abor()
    view = {s: (int(q), int(c)) for s, q, c in conn.execute(
        "select symbol, qty, book_cost from abor_positions_v where account_id = %s", (account_id,))}
    mem = {s: (p.qty, p.book_cost) for s, p in abor.positions().items()} if abor else {}
    diff += [f"마감 보유 {s} 저장 {view.get(s)} / 재생 {mem.get(s)}" for s in sorted(set(view) | set(mem))
             if view.get(s) != mem.get(s)]
    if diff:
        return Check("closes", "일 마감", False, f"다른 행: {_some(diff)}")
    if not stored:
        return Check("closes", "일 마감", True, "마감한 날이 아직 없어요")
    return Check("closes", "일 마감", True,
                 f"마감 {len(stored):,}일({ledger.closed_through}까지)의 확정 기준가 일치, 그 뒤 이벤트 "
                 f"{len(ledger.unclosed_events()):,}건")


def projection_checks(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Check]:
    """Everything derived from one account's events, against `ledger` (a replay of those events)."""
    checks = [check_journal(conn, ledger, account_id), check_accounts(conn, ledger, account_id),
              check_positions(conn, ledger, account_id), check_cash(conn, ledger, account_id),
              check_closes(conn, ledger, account_id)]
    return [replace(c, account=account_id) for c in checks]


def integrity_report(conn: psycopg.Connection, calendar, fees, instruments) -> list[Check]:
    """All checks, in the order a person reads them. Reads only; replays each account's events itself."""
    top = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with conn.transaction():
        if top:   # one snapshot, so a write in another session cannot split the picture
            conn.execute("set transaction isolation level repeatable read read only")
        stream = check_event_stream(conn)
        checks = [stream, check_guards(conn), check_balanced(conn), check_audit(conn)]
        if not stream.ok:
            return checks + [Check("replay", "이벤트 재생", False, "순번에 빈 곳이 있어 재생하지 않았어요")]
        for (account_id,) in conn.execute("select account_id from trading_accounts order by account_id").fetchall():
            try:
                events = [from_payload(t, p) for t, p in conn.execute(
                    "select event_type, payload from ledger_events where account_id = %s order by seq",
                    (account_id,))]
                ledger = Ledger.replay(events, calendar, fees, instruments)
            except Exception as e:   # a stored event the rules reject is itself the finding
                checks.append(Check("replay", "이벤트 재생", False, f"{type(e).__name__}: {e}", account_id))
                continue
            checks += projection_checks(conn, ledger, account_id)
        return checks
