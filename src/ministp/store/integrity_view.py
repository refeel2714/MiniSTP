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
from ..ledger import DayClosedEvent, Ledger, OpeningPositionEvent
from .integrity import transfer_halves
from .integrity import APPEND_ONLY_TABLES, AUDIT_CHAIN_TRIGGER, BALANCE_TRIGGERS, CLOSED_DAY_TRIGGERS
from .postgres import DEFAULT_ACCOUNT, cash_rows, close_row, from_payload, position_row

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


def replay(conn: psycopg.Connection, calendar, fees, instruments, account_id: str = DEFAULT_ACCOUNT) -> Ledger:
    """One account's books as its events alone give them. Raises what `Ledger.replay` raises on an event
    the rules reject."""
    events = [from_payload(t, p) for t, p in conn.execute(
        "select event_type, payload from ledger_events where account_id = %s order by seq", (account_id,))]
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
                            "전표와 분개 줄의 대차 검사 트리거"),
                   Compared("마감일 잠금", sum(t in live for t in CLOSED_DAY_TRIGGERS), len(CLOSED_DAY_TRIGGERS),
                            "마감한 날에 행을 더하지 못하게 하는 트리거"),
                   Compared("감사 로그 사슬", int(AUDIT_CHAIN_TRIGGER in live), 1,
                            "감사 로그 줄에 번호와 해시를 매기는 트리거")]


def compare_balanced(conn: psycopg.Connection) -> list[Compared]:
    bad, dr, cr = conn.execute(
        "select count(*) filter (where dr <> cr or lines = 0), coalesce(sum(dr), 0), coalesce(sum(cr), 0) from ("
        "  select e.entry_id, coalesce(sum(l.debit), 0) dr, coalesce(sum(l.credit), 0) cr, count(l.line_id) lines"
        "  from journal_entries e left join journal_lines l using (entry_id) group by e.entry_id) x").fetchone()
    return [Compared("차변 합계", int(dr), int(cr), "대변 합계"),
            Compared("대차가 맞지 않는 분개 수", bad, 0, "하나도 없어야 함")]


def compare_rights(conn: psycopg.Connection) -> list[Compared]:
    """R-59: every dividend in the books comes from the register, for the amount the register says."""
    orphans, symbols, gross, want, paid = conn.execute(
        "select count(*) filter (where a.action_id is null), count(*) filter (where a.symbol <> d.symbol),"
        " coalesce(sum(d.accrued_gross) filter (where d.qty is not null and a.action_id is not null), 0),"
        " coalesce(sum(trunc(d.qty * a.amount_per_share)) filter (where d.qty is not null"
        "   and a.action_id is not null), 0),"
        " count(*) filter (where d.status = 'PAID' and a.canceled_at is not null)"
        " from dividends_v d left join corporate_actions_v a using (action_id)").fetchone()
    none = "하나도 없어야 함"
    return [Compared("권리 원장에 없는 배당 수", orphans, 0, none),
            Compared("권리와 종목이 다른 배당 수", symbols, 0, none),
            Compared("배당 인식 금액 합계", int(gross), int(want), "기준일 보유수량 × 주당 배당금의 합계"),
            Compared("취소된 권리인데 입금으로 기록된 배당 수", paid, 0, none)]


def compare_transfers(conn: psycopg.Connection) -> list[Compared]:
    """R-65: what the sending accounts sent against what the receiving accounts got."""
    halves, bad = transfer_halves(conn)
    sides = [r for rows in halves.values() for r in rows]
    out, got = [r for r in sides if r[1] == "TRANSFER_OUT"], [r for r in sides if r[1] == "TRANSFER_IN"]
    return [Compared("보낸 쪽 건수", len(out), len(got), "받은 쪽 건수"),
            Compared("보낸 금액 합계", -sum(r[3] for r in out), sum(r[3] for r in got), "받은 금액 합계"),
            Compared("짝이 맞지 않는 대체 수", len(bad), 0, "하나도 없어야 함")]


def compare_audit(conn: psycopg.Connection) -> list[Compared]:
    """R-87: the audit rows are numbered 1..n and each carries the hash of its content and of the row before."""
    from .. import audit
    n, _, bad = audit.chain_problems(conn, 1_000_000)
    hi = conn.execute("select coalesce(max(id), 0) from audit_log").fetchone()[0]
    return [Compared("감사 로그 줄 수", n, hi, "마지막 번호 (빈 번호가 없으면 줄 수와 같음)"),
            Compared("해시 사슬이 이어지지 않는 줄 수", len(bad), 0, "하나도 없어야 함")]


def compare_master(conn: psycopg.Connection) -> list[Compared]:
    """R-95: the rows the loads say they wrote against the rows the history holds."""
    from .. import master_db
    written, held = conn.execute(
        "select coalesce(sum(rows_new + rows_changed + rows_delisted), 0), (select count(*) from instrument_master)"
        " from master_loads").fetchone()
    return [Compared("종목 마스터 이력 행 수", held, int(written), "적재 기록의 신규·변경·빠짐 합계"),
            Compared("적재 기록과 맞지 않는 이력 수", len(master_db.problems(conn)), 0, "하나도 없어야 함")]


# ---------- stored rows against a replay of the events ----------

def compare_journal(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    stored: dict[int, tuple] = {}
    for eid, date, kind, src, acct, dr, cr in conn.execute(
            "select e.entry_id, e.date, e.kind, e.source_id, l.account_code, l.debit, l.credit"
            " from journal_entries e join journal_lines l using (entry_id) where e.account_id = %s"
            " order by e.entry_id, l.line_id", (account_id,)):
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


def compare_accounts(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    stored = dict(conn.execute("select code, balance from account_balances_v where account_id = %s", (account_id,)))
    return [Compared(f"{a.code} {a.value[1]}", int(stored.get(a.code, 0)), ledger.account_balance(a), REPLAY)
            for a in Account]


def compare_positions(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    stored = conn.execute("select trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                          " from position_ledger where account_id = %s", (account_id,)).fetchall()
    expected = [r for r in (position_row(ledger, ev) for ev in ledger.events) if r is not None]
    totals = {s: (int(q), int(c)) for s, q, c in conn.execute(
        "select symbol, qty, book_cost from positions_v where account_id = %s", (account_id,))}
    mem = {s: (p.qty, p.book_cost) for s, p in ledger.positions().items()}
    rows = [Compared("보유 원장 행 수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 보유 원장 행 수", _unlike(stored, expected), 0, "하나도 없어야 함")]
    for s in sorted(set(totals) | set(mem)):
        (q, c), (mq, mc) = totals.get(s, (None, None)), mem.get(s, (None, None))
        rows += [Compared(f"{s} 보유수량", q, mq, REPLAY), Compared(f"{s} 장부금액", c, mc, REPLAY)]
    return rows


def compare_cash(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    stored = conn.execute("select date, reason, amount, ref_id from cash_ledger where account_id = %s",
                          (account_id,)).fetchall()
    expected = [r for e in ledger.journal for r in cash_rows(e)]
    balance = int(conn.execute("select coalesce(sum(amount), 0) from cash_ledger where account_id = %s",
                               (account_id,)).fetchone()[0])
    return [Compared("예수금", balance, ledger.cash_balance(), REPLAY),
            Compared("현금 원장 행 수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 현금 원장 행 수", _unlike(stored, expected), 0, "하나도 없어야 함")]


def compare_closes(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    """R-77, R-79: the stored closes against the close events of the replay, and the ABOR view against
    a replay up to the last close."""
    stored = conn.execute(
        "select c.date, c.net_assets, c.units, c.nav_per_1000, e.event_type, (e.payload ->> 'date')::date"
        " from day_closes c join ledger_events e on e.seq = c.event_seq where c.account_id = %s",
        (account_id,)).fetchall()
    expected = [close_row(ledger, ev) for ev in ledger.events if isinstance(ev, DayClosedEvent)]
    astray = sum((t, on) != ("DayClosedEvent", d) for d, *_, t, on in stored)
    rows = [Compared("마감 행 수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 마감 행 수", _unlike([r[:4] for r in stored], expected), 0, "하나도 없어야 함"),
            Compared("그날의 마감 이벤트를 가리키지 않는 마감 행 수", astray, 0, "하나도 없어야 함")]
    abor = ledger.abor()
    view = {s: (int(q), int(c)) for s, q, c in conn.execute(
        "select symbol, qty, book_cost from abor_positions_v where account_id = %s", (account_id,))}
    mem = {s: (p.qty, p.book_cost) for s, p in abor.positions().items()} if abor else {}
    basis = "마지막 마감까지만 재생한 값"
    for s in sorted(set(view) | set(mem)):
        (q, c), (mq, mc) = view.get(s, (None, None)), mem.get(s, (None, None))
        rows += [Compared(f"마감 보유 {s} 수량", q, mq, basis), Compared(f"마감 보유 {s} 장부금액", c, mc, basis)]
    return rows


def compare_dividends(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    """R-57: the dividend view against a replay, and 미수배당금 against the accruals still open."""
    stored = conn.execute(
        "select action_id, symbol, coalesce(qty, 0), accrued_gross, accrued_on, paid_gross, tax, paid_on, canceled_on,"
        " status from dividends_v where account_id = %s", (account_id,)).fetchall()
    mem = ledger.dividends()
    expected = [(d.action_id, d.symbol, d.qty, d.accrued, d.accrued_on, d.paid_gross, d.tax, d.paid_on,
                 d.canceled_on, d.status) for d in mem.values()]
    return [Compared("배당 건수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 배당 행 수", _unlike(stored, expected), 0, "하나도 없어야 함"),
            Compared("배당 인식 금액 합계", int(sum(r[3] for r in stored)), sum(d.accrued for d in mem.values()), REPLAY),
            Compared("미수배당금", ledger.account_balance(Account.DIVIDEND_RECEIVABLE),
                     sum(d.receivable for d in mem.values()), "입금 전 인식 금액의 합계")]


def compare_movements(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    """R-56: the cash movement view against a replay, and 투자원금 against what was put in less what was taken out."""
    stored = conn.execute("select date, kind, amount, transfer_id, counter_account from cash_movements_v"
                          " where account_id = %s", (account_id,)).fetchall()
    mem = ledger.cash_movements()
    expected = [(m.date, m.kind, m.amount, m.transfer_id, m.counter_account) for m in mem]
    opening = sum(e.book_cost for e in ledger.events if isinstance(e, OpeningPositionEvent))
    return [Compared("입출금·대체 건수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 입출금 행 수", _unlike(stored, expected), 0, "하나도 없어야 함"),
            Compared("입출금·대체 금액 합계", int(sum(r[2] for r in stored)), sum(m.amount for m in mem), REPLAY),
            Compared("투자원금", ledger.account_balance(Account.CONTRIBUTED_CAPITAL),
                     opening + sum(m.amount for m in mem), "입출금·대체와 시작 보유의 합계")]


def compare_adjustments(conn: psycopg.Connection, ledger: Ledger, account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    """R-66: the adjustment view against a replay, and every break an adjustment names is a break of this account."""
    stored = conn.execute(
        "select adjustment_id, date, reason, amount, tax, cash_amount, memo, recon_result_id, canceled_on,"
        " cancel_reason from adjustments_v where account_id = %s", (account_id,)).fetchall()
    mem = ledger.adjustments()
    expected = [(a.adjustment_id, a.date, a.reason, a.amount, a.tax, a.cash, a.memo, a.recon_result_id,
                 a.canceled_on, a.cancel_memo) for a in mem.values()]
    named = sorted(a.recon_result_id for a in mem.values() if a.recon_result_id is not None)
    breaks = {r[0] for r in conn.execute(
        "select id from recon_results where id = any(%s) and account_id = %s and status <> 'MATCH'",
        (named, account_id))} if named else set()
    return [Compared("수동 조정 건수", len(stored), len(expected), REPLAY),
            Compared("한쪽에만 있는 조정 행 수", _unlike(stored, expected), 0, "하나도 없어야 함"),
            Compared("예수금 반영 합계(취소 제외)", int(sum(r[5] for r in stored if r[8] is None)),
                     sum(a.cash for a in mem.values() if not a.canceled_on), REPLAY),
            Compared("이 계좌의 불일치가 아닌 대사 행을 가리키는 조정 수", sum(i not in breaks for i in named), 0,
                     "하나도 없어야 함")]


PLAIN = {"events": compare_events, "guards": compare_guards, "balanced": compare_balanced, "rights": compare_rights,
         "transfers": compare_transfers, "master": compare_master,
         "audit": compare_audit}
REPLAYED = {"journal": compare_journal, "accounts": compare_accounts, "positions": compare_positions,
            "cash": compare_cash, "closes": compare_closes, "dividends": compare_dividends, "movements": compare_movements,
            "adjustments": compare_adjustments}


def compared(conn: psycopg.Connection, key: str, calendar, fees, instruments,
             account_id: str = DEFAULT_ACCOUNT) -> list[Compared]:
    """The numbers behind check `key`: the whole database for the checks that need no replay, one account's
    books for the rest. Empty for a check that has none here (its sentence says it all) and for a replay
    check when the account's events cannot be replayed (the 이벤트 재생 check says why)."""
    top = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with conn.transaction():
        if top:   # one snapshot, as in integrity_report
            conn.execute("set transaction isolation level repeatable read read only")
        if key in PLAIN:
            return PLAIN[key](conn)
        if key not in REPLAYED:
            return []
        try:
            ledger = replay(conn, calendar, fees, instruments, account_id)
        except Exception:
            return []
        return REPLAYED[key](conn, ledger, account_id)
