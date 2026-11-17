"""PostgreSQL event store for the ledger (spec 03).

The in-memory `Ledger` stays the single source of business rules. This store
appends each event and its derived rows in one transaction, and can rebuild a
ledger from the stored events and check it against the stored projections.
"""
import dataclasses
import datetime as dt
from importlib.resources import files

import psycopg

from ..accounting import Account
from ..ledger import (DepositEvent, ExecutionEvent, Ledger, NavEvent, RevaluationEvent,
                      SettlementEvent, Side)

EVENT_TYPES = {cls.__name__: cls for cls in
               (DepositEvent, ExecutionEvent, SettlementEvent, RevaluationEvent, NavEvent)}
DATE_FIELDS = {"date", "trade_date"}


class SchemaError(RuntimeError):
    pass


def to_payload(ev) -> dict:
    out = {}
    for f in dataclasses.fields(ev):
        v = getattr(ev, f.name)
        if isinstance(v, dt.date):
            v = v.isoformat()
        elif isinstance(v, Side):
            v = v.value
        elif f.name == "closes":
            v = [list(x) for x in v]
        out[f.name] = v
    return out


def from_payload(event_type: str, payload: dict):
    cls = EVENT_TYPES[event_type]
    kw = {}
    for k, v in payload.items():
        if k in DATE_FIELDS:
            v = dt.date.fromisoformat(v)
        elif k == "side":
            v = Side(v)
        elif k == "closes":
            v = tuple((s, int(p)) for s, p in v)
        kw[k] = v
    return cls(**kw)


class PostgresLedgerStore:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn
        self._entry_ids: list[int] = []  # index in ledger.journal -> entry_id

    # ----- schema -----

    def init_schema(self) -> None:
        sql = files("ministp.store").joinpath("schema.sql").read_text(encoding="utf-8")
        with self.conn.transaction():
            self.conn.execute(sql)
            with self.conn.cursor() as cur:
                cur.executemany(
                    "insert into accounts_chart (code, name, normal_side) values (%s, %s, %s)"
                    " on conflict (code) do nothing",
                    [(a.code, a.value[1], a.normal_side) for a in Account])

    # ----- write path -----

    def attach(self, ledger: Ledger) -> None:
        """Start persisting `ledger`. Its event stream must equal what is stored."""
        stored = self.conn.execute("select count(*) from ledger_events").fetchone()[0]
        if stored != len(ledger.events):
            raise SchemaError(f"ledger has {len(ledger.events)} events but DB has {stored}; "
                              "load the ledger from the store instead")
        self._entry_ids = [r[0] for r in self.conn.execute(
            "select entry_id from journal_entries order by entry_id")]
        ledger.subscribe(self._on_event)

    def _on_event(self, ledger: Ledger, seq: int, ev, entries) -> None:
        with self.conn.transaction():
            self.conn.execute(
                "insert into ledger_events (seq, event_type, payload) values (%s, %s, %s)",
                (seq, type(ev).__name__, psycopg.types.json.Jsonb(to_payload(ev))))
            journal = ledger.journal
            for e in entries:
                rev_id = None
                if e.reverses is not None:
                    idx = next(i for i, x in enumerate(journal) if x is e.reverses)
                    rev_id = self._entry_ids[idx]
                eid = self.conn.execute(
                    "insert into journal_entries (event_seq, date, kind, source_id, reverses_entry_id)"
                    " values (%s, %s, %s, %s, %s) returning entry_id",
                    (seq, e.date, e.kind, e.source_id, rev_id)).fetchone()[0]
                self._entry_ids.append(eid)
                with self.conn.cursor() as cur:
                    cur.executemany(
                        "insert into journal_lines (entry_id, account_code, debit, credit)"
                        " values (%s, %s, %s, %s)",
                        [(eid, ln.account.code, ln.debit, ln.credit) for ln in e.lines])
                for ln in e.lines:
                    if ln.account is Account.CASH:
                        self.conn.execute(
                            "insert into cash_ledger (event_seq, date, reason, amount, ref_id)"
                            " values (%s, %s, %s, %s, %s)",
                            (seq, e.date, e.kind, ln.debit - ln.credit, e.source_id))
            if isinstance(ev, ExecutionEvent):
                r = ledger.execution_result(ev.exec_id)
                buy = ev.side is Side.BUY
                self.conn.execute(
                    "insert into position_ledger (event_seq, trade_date, symbol, exec_id,"
                    " qty_delta, cost_delta, realized_pnl) values (%s, %s, %s, %s, %s, %s, %s)",
                    (seq, ev.trade_date, ev.symbol, ev.exec_id,
                     ev.qty if buy else -ev.qty, r.gross if buy else -r.cost_of_sale,
                     r.realized_pnl))

    # ----- read path -----

    def load_events(self) -> list:
        rows = self.conn.execute(
            "select seq, event_type, payload from ledger_events order by seq").fetchall()
        for expected, (seq, _, _) in enumerate(rows, start=1):
            if seq != expected:
                raise SchemaError(f"event stream gap: expected seq {expected}, found {seq}")
        return [from_payload(t, p) for _, t, p in rows]

    def load(self, calendar, fees, instruments, settlement_cycle=None) -> Ledger:
        """Rebuild the ledger by replaying stored events, then keep persisting it."""
        kw = {} if settlement_cycle is None else {"settlement_cycle": settlement_cycle}
        ledger = Ledger.replay(self.load_events(), calendar, fees, instruments, **kw)
        self.attach(ledger)
        return ledger

    def verify(self, ledger: Ledger) -> list[str]:
        """Compare stored projections with the ledger. Empty list = consistent."""
        problems = []
        rows = self.conn.execute(
            "select e.entry_id, e.date, e.kind, e.source_id, l.account_code, l.debit, l.credit"
            " from journal_entries e join journal_lines l using (entry_id)"
            " order by e.entry_id, l.line_id").fetchall()
        stored: dict[int, tuple] = {}
        for eid, date, kind, src, acct, dr, cr in rows:
            head, lines = stored.get(eid, ((date, kind, src), []))
            lines.append((acct, dr, cr))
            stored[eid] = (head, lines)
        db_journal = [(h, sorted(ls)) for h, ls in stored.values()]
        mem_journal = [((e.date, e.kind, e.source_id),
                        sorted((ln.account.code, ln.debit, ln.credit) for ln in e.lines))
                       for e in ledger.journal]
        if db_journal != mem_journal:
            problems.append("journal differs")
        balances = dict(self.conn.execute("select code, balance from account_balances_v"))
        for a in Account:
            if balances.get(a.code, 0) != ledger.account_balance(a):
                problems.append(f"account {a.code} differs")
        db_pos = {s: (q, c) for s, q, c in self.conn.execute(
            "select symbol, qty, book_cost from positions_v")}
        mem_pos = {s: (p.qty, p.book_cost) for s, p in ledger.positions().items()}
        if db_pos != mem_pos:
            problems.append("positions differ")
        if self.conn.execute("select balance from cash_balance_v").fetchone()[0] != ledger.cash_balance():
            problems.append("cash differs")
        return problems
