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
from ..ledger import DayClosedEvent, Ledger, OpeningPositionEvent
from .postgres import DEFAULT_ACCOUNT, cash_rows, close_row, from_payload, position_row

# Tables no one may UPDATE, DELETE or TRUNCATE (R-04). Same list as the loop in schema.sql;
# tests/unit/test_integrity_tables.py keeps the two in step.
APPEND_ONLY_TABLES = ("ledger_events", "journal_entries", "journal_lines", "position_ledger", "cash_ledger",
                      "instruments", "oms_orders", "oms_order_acks", "broker_snapshots", "recon_results",
                      "companies", "trading_accounts", "account_status_history", "day_closes", "audit_log",
                      "corporate_actions", "master_loads", "instrument_master",
                      "app_users", "user_rights", "order_requests", "order_request_lines", "order_approvals",
                      "order_sends", "erp_exports", "erp_export_docs", "erp_export_voids",
                      "close_checks", "close_check_steps", "company_erp_codes")
BALANCE_TRIGGERS = ("journal_entries_balanced", "journal_lines_balanced")
# R-78 in the database: no entry, cash row or close dated inside a closed day (ERD v2.2).
CLOSED_DAY_TRIGGERS = ("journal_entries_closed_day", "cash_ledger_closed_day", "day_closes_closed_day")
AUDIT_CHAIN_TRIGGER = "audit_log_chain"   # numbers, stamps and links each audit row (ERD v2.3)
APPROVAL_FLOW_TRIGGER = "order_approvals_flow"   # 결재 순서와 셀프 승인 표시 (ERD v2.9, R-100)
APPROVAL_TRIGGERS = (APPROVAL_FLOW_TRIGGER, "order_request_lines_fixed", "order_sends_flow")
# 같은 전표 두 번 막기 (v2.11), 단계 기준 보호 (v2.12), 사용 중지 계정 막기 (v2.13).
LATER_TRIGGERS = ("erp_export_docs_once", "close_steps_guard", "journal_lines_account_in_use")
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
    want += BALANCE_TRIGGERS + CLOSED_DAY_TRIGGERS + (AUDIT_CHAIN_TRIGGER,) + APPROVAL_TRIGGERS + LATER_TRIGGERS
    off = [w for w in want if w not in live]
    if off:
        return Check("guards", "수정·삭제 방지", False, f"꺼져 있거나 없는 트리거: {_some(off)}")
    return Check("guards", "수정·삭제 방지", True,
                 f"{len(APPEND_ONLY_TABLES)}개 테이블의 수정·삭제·비우기 금지, 분개 대차 검사, 마감일 잠금, 감사 로그 사슬, 결재 순서, ERP 중복 차단, 사용 중지 계정 막기가 켜져 있음")


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


def check_rights(conn: psycopg.Connection) -> Check:
    """R-59: every dividend in the books comes from the register, for the amount the register says."""
    actions, booked, bad = conn.execute(
        "select (select count(*) from corporate_actions_v), count(*),"
        " array_remove(array_agg(case"
        "   when a.action_id is null then d.account_id || ' CA-' || d.action_id || ' 권리 원장에 없음'"
        "   when a.symbol <> d.symbol then d.account_id || ' CA-' || d.action_id || ' 종목이 다름'"
        "   when d.qty is not null and d.accrued_gross <> trunc(d.qty * a.amount_per_share)"
        "     then d.account_id || ' CA-' || d.action_id || ' 인식 금액이 수량 × 주당 배당금과 다름'"
        "   when d.status = 'PAID' and a.canceled_at is not null"
        "     then d.account_id || ' CA-' || d.action_id || ' 취소된 권리인데 입금으로 기록됨'"
        " end order by d.action_id, d.account_id), null)"
        " from dividends_v d left join corporate_actions_v a using (action_id)").fetchone()
    if bad:
        return Check("rights", "권리", False, f"권리 원장과 다른 배당: {_some(bad)}")
    if not actions:
        return Check("rights", "권리", True, "등록된 권리가 아직 없어요")
    return Check("rights", "권리", True, f"권리 {actions:,}건, 계좌별 배당 {booked:,}건이 권리 원장과 맞음")


def transfer_halves(conn: psycopg.Connection) -> tuple[dict, list[str]]:
    """R-65: every transfer has exactly two halves that name each other: one account sent what the
    other received, on the same day, and both accounts belong to one company.
    Returns (transfer id -> its halves, what is wrong with the ones that do not pair)."""
    halves: dict[int, list[tuple]] = {}
    for tid, acct, kind, date, amount, other, company in conn.execute(
            "select m.transfer_id, m.account_id, m.kind, m.date, m.amount, m.counter_account, t.company_id"
            " from cash_movements_v m join trading_accounts t using (account_id)"
            " where m.kind in ('TRANSFER_OUT', 'TRANSFER_IN') order by m.transfer_id, m.kind desc, m.event_seq"):
        halves.setdefault(tid, []).append((acct, kind, date, int(amount), other, company))
    bad = []
    for tid, rows in halves.items():
        if tid is None or [r[1] for r in rows] != ["TRANSFER_OUT", "TRANSFER_IN"]:
            bad.append(f"TR-{tid} 보낸 쪽과 받은 쪽이 하나씩이 아님")
            continue
        out, got = rows
        if (out[0], got[0]) != (got[4], out[4]) or out[0] == got[0]:
            bad.append(f"TR-{tid} 상대 계좌가 서로 맞지 않음")
        elif out[3] != -got[3] or out[2] != got[2]:
            bad.append(f"TR-{tid} 보낸 금액·날짜와 받은 금액·날짜가 다름")
        elif out[5] != got[5]:
            bad.append(f"TR-{tid} 법인이 다른 계좌 사이의 대체")
    return halves, bad


def check_transfers(conn: psycopg.Connection) -> Check:
    halves, bad = transfer_halves(conn)
    if bad:
        return Check("transfers", "대체", False, f"짝이 맞지 않는 대체: {_some(bad)}")
    if not halves:
        return Check("transfers", "대체", True, "계좌 간 대체가 아직 없어요")
    total = sum(rows[1][3] for rows in halves.values())
    return Check("transfers", "대체", True, f"대체 {len(halves):,}건의 보낸 쪽과 받은 쪽이 맞음 (합계 {total:,}원)")


def check_audit(conn: psycopg.Connection) -> Check:
    """R-87: the audit rows are numbered 1..n and each carries the hash of its content and of the row before."""
    n, last, bad = audit.chain_problems(conn, SHOWN + 1)
    if bad:
        return Check("audit", "감사 로그", False,
                     f"사슬이 이어지지 않는 줄: {_some(bad)}번 (고쳤거나, 지웠거나, 끼워 넣은 흔적)")
    if n == 0:
        return Check("audit", "감사 로그", True, "기록이 아직 없어요")
    return Check("audit", "감사 로그", True, f"{n:,}줄의 번호와 해시 사슬이 이어짐 (마지막 줄 {n:,}번, 해시 {last[:12]})")


def check_master(conn: psycopg.Connection) -> Check:
    """R-95: the market master history holds changes only, each row where its load says it is."""
    from .. import master_db
    bad = master_db.problems(conn)
    if bad:
        return Check("master", "종목 마스터", False, f"적재 기록과 맞지 않는 이력: {_some(bad)}")
    last = master_db.last_load(conn)
    if last is None:
        return Check("master", "종목 마스터", True, "적재한 마스터가 아직 없어요")
    n, rows = conn.execute("select count(*), (select count(*) from instrument_master) from master_loads").fetchone()
    return Check("master", "종목 마스터", True,
                 f"적재 {n:,}번, 이력 {rows:,}행이 적재 기록과 맞음 (마지막 {last.ref} {last.base_date}, {last.rows_total:,}종목)")


def check_erp(conn: psycopg.Connection) -> Check:
    """R-104: every export holds the documents it says, and no document is in two exports not voided."""
    from .. import erp
    bad = erp.problems(conn)
    if bad:
        return Check("erp", "ERP 전송 이력", False, f"맞지 않는 전송 이력: {_some(bad)}")
    n, docs, voids = conn.execute("select count(*), (select count(*) from erp_export_docs),"
                                  " (select count(*) from erp_export_voids) from erp_exports").fetchone()
    if n == 0:
        return Check("erp", "ERP 전송 이력", True, "ERP로 내보낸 파일이 아직 없어요")
    return Check("erp", "ERP 전송 이력", True, f"내보내기 {n:,}번(무효 {voids:,}번), 전표 {docs:,}건이 이력과 맞고 두 번 나간 전표 없음")


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


def check_dividends(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    """R-57: the dividend view says what a replay says, and 미수배당금 is exactly the accruals still open."""
    stored = conn.execute(
        "select action_id, symbol, coalesce(qty, 0), accrued_gross, accrued_on, paid_gross, tax, paid_on, canceled_on,"
        " status from dividends_v where account_id = %s", (account_id,)).fetchall()
    mem = ledger.dividends()
    expected = [(d.action_id, d.symbol, d.qty, d.accrued, d.accrued_on, d.paid_gross, d.tax, d.paid_on,
                 d.canceled_on, d.status) for d in mem.values()]
    diff = _row_diff(stored, expected, lambda r: f"CA-{r[0]} {r[1]} {r[9]} {r[3]:,}원")
    open_accruals = sum(d.receivable for d in mem.values())
    balance = ledger.account_balance(Account.DIVIDEND_RECEIVABLE)
    if balance != open_accruals:
        diff.append(f"미수배당금 {balance:,} / 입금 전 인식 합계 {open_accruals:,}")
    if diff:
        return Check("dividends", "배당", False, f"다른 행: {_some(diff)}")
    if not mem:
        return Check("dividends", "배당", True, "배당 기록이 아직 없어요")
    paid = sum(d.status == "PAID" for d in mem.values())
    return Check("dividends", "배당", True, f"배당 {len(mem):,}건(입금 {paid:,}건) 일치, 미수배당금 {balance:,}원")


def check_movements(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    """R-56: the cash movement view says what a replay says, and 투자원금 is exactly what was put in
    (deposits, transfers in, the opening holdings) less what was taken out."""
    stored = conn.execute("select date, kind, amount, transfer_id, counter_account from cash_movements_v"
                          " where account_id = %s", (account_id,)).fetchall()
    mem = ledger.cash_movements()
    expected = [(m.date, m.kind, m.amount, m.transfer_id, m.counter_account) for m in mem]
    diff = _row_diff(stored, expected, lambda r: f"{r[0]} {r[1]} {r[2]:+,}원")
    opening = sum(e.book_cost for e in ledger.events if isinstance(e, OpeningPositionEvent))
    capital, put_in = ledger.account_balance(Account.CONTRIBUTED_CAPITAL), opening + sum(m.amount for m in mem)
    if capital != put_in:
        diff.append(f"투자원금 {capital:,} / 입출금과 시작 보유의 합계 {put_in:,}")
    if diff:
        return Check("movements", "입출금", False, f"다른 행: {_some(diff)}")
    if not mem:
        return Check("movements", "입출금", True, "입출금 기록이 아직 없어요")
    return Check("movements", "입출금", True,
                 f"입출금·대체 {len(mem):,}건 일치, 투자원금 {capital:,}원, 좌수 {ledger.units:,}")


def check_adjustments(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> Check:
    """R-66: the adjustment view says what a replay says, and a break an adjustment names is a break of
    this account."""
    stored = conn.execute(
        "select adjustment_id, date, reason, amount, tax, cash_amount, memo, recon_result_id, canceled_on,"
        " cancel_reason from adjustments_v where account_id = %s", (account_id,)).fetchall()
    mem = ledger.adjustments()
    expected = [(a.adjustment_id, a.date, a.reason, a.amount, a.tax, a.cash, a.memo, a.recon_result_id,
                 a.canceled_on, a.cancel_memo) for a in mem.values()]
    diff = _row_diff(stored, expected, lambda r: f"ADJ-{r[0]} {r[2]} {r[3]:+,}원")
    named = sorted(a.recon_result_id for a in mem.values() if a.recon_result_id is not None)
    if named:
        breaks = {r[0] for r in conn.execute(
            "select id from recon_results where id = any(%s) and account_id = %s and status <> 'MATCH'",
            (named, account_id))}
        diff += [f"불일치 행 {i}이 이 계좌의 불일치가 아님" for i in named if i not in breaks]
    if diff:
        return Check("adjustments", "조정", False, f"다른 행: {_some(diff)}")
    if not mem:
        return Check("adjustments", "조정", True, "수동 조정이 아직 없어요")
    live = [a for a in mem.values() if not a.canceled_on]
    return Check("adjustments", "조정", True,
                 f"수동 조정 {len(mem):,}건(취소 {len(mem) - len(live):,}건) 일치, 예수금 반영 합계 "
                 f"{sum(a.cash for a in live):+,}원")


def projection_checks(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Check]:
    """Everything derived from one account's events, against `ledger` (a replay of those events)."""
    checks = [check_journal(conn, ledger, account_id), check_accounts(conn, ledger, account_id),
              check_positions(conn, ledger, account_id), check_cash(conn, ledger, account_id),
              check_closes(conn, ledger, account_id), check_dividends(conn, ledger, account_id),
              check_movements(conn, ledger, account_id), check_adjustments(conn, ledger, account_id)]
    return [replace(c, account=account_id) for c in checks]


def integrity_report(conn: psycopg.Connection, calendar, fees, instruments) -> list[Check]:
    """All checks, in the order a person reads them. Reads only; replays each account's events itself."""
    top = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with conn.transaction():
        if top:   # one snapshot, so a write in another session cannot split the picture
            conn.execute("set transaction isolation level repeatable read read only")
        stream = check_event_stream(conn)
        checks = [stream, check_guards(conn), check_balanced(conn), check_audit(conn), check_rights(conn),
                  check_transfers(conn), check_master(conn), check_erp(conn)]
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
