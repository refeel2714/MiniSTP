"""PostgreSQL event store for the ledger (spec 03).

The in-memory `Ledger` stays the single source of business rules. This store
appends each event and its derived rows in one transaction, and can rebuild a
ledger from the stored events and check it against the stored projections.

ERD v2.0 (다계좌): there is one event table with one sequence for the whole
database, and every event belongs to one account. A store is bound to one
account and replays only that account's events, so a `Ledger` is still the
books of a single account.
"""
import dataclasses
import datetime as dt
from importlib.resources import files

import psycopg

from ..accounting import Account
from ..ledger import (DepositEvent, ExecutionEvent, Ledger, NavEvent, OpeningPositionEvent,
                      RevaluationEvent, SettlementEvent, Side)

EVENT_TYPES = {cls.__name__: cls for cls in
               (DepositEvent, OpeningPositionEvent, ExecutionEvent, SettlementEvent, RevaluationEvent,
                NavEvent)}
DATE_FIELDS = {"date", "trade_date"}
DEFAULT_ACCOUNT = "A0001"    # the account every pre-v2.0 row belongs to
DEFAULT_CUSTOMER = "C0001"


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


def position_row(ledger: Ledger, ev) -> tuple | None:
    """What an event adds to position_ledger: (trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl)."""
    if isinstance(ev, OpeningPositionEvent):
        return (ev.date, ev.symbol, f"OPEN-{ev.symbol}", ev.qty, ev.book_cost, None)
    if isinstance(ev, ExecutionEvent):
        r = ledger.execution_result(ev.exec_id)
        buy = ev.side is Side.BUY
        return (ev.trade_date, ev.symbol, ev.exec_id, ev.qty if buy else -ev.qty,
                r.gross if buy else -r.cost_of_sale, r.realized_pnl)
    return None


def cash_rows(entry) -> list[tuple]:
    """What a journal entry adds to cash_ledger: (date, reason, amount, ref_id) per cash line."""
    return [(entry.date, entry.kind, ln.debit - ln.credit, entry.source_id)
            for ln in entry.lines if ln.account is Account.CASH]


class PostgresLedgerStore:
    def __init__(self, conn: psycopg.Connection, account_id: str = DEFAULT_ACCOUNT):
        self.conn = conn
        self.account_id = account_id
        self._entry_ids: list[int] = []  # index in ledger.journal -> entry_id

    # ----- schema -----

    def init_schema(self, opened_on=None) -> None:
        """Build a new database at the current ERD version (`db-init`).

        `opened_on` is the opening date of the default account (today unless given;
        the sample database starts in the past).

        A database that already holds the ledger is never rebuilt from `schema.sql`:
        it moves forward only through migrations (`db-migrate`), which prove the
        existing rows unchanged. On such a database this does nothing, or refuses
        when migrations are still pending."""
        from . import migrate as mg
        migrations = mg.discover()
        if mg._has_table(self.conn, "ledger_events"):
            todo = mg.pending(self.conn, migrations)
            if todo:
                raise SchemaError(
                    "이미 운영 중인 DB라서 db-init으로는 스키마를 바꾸지 않아요. "
                    f"남은 마이그레이션 {len(todo)}개를 db-migrate로 적용해 주세요 "
                    "(먼저 --dry-run으로 리허설).")
            return
        sql = files("ministp.store").joinpath("schema.sql").read_text(encoding="utf-8")
        with self.conn.transaction():
            self.conn.execute(sql)
            with self.conn.cursor() as cur:
                cur.executemany(
                    "insert into accounts_chart (code, name, normal_side) values (%s, %s, %s)"
                    " on conflict (code) do nothing",
                    [(a.code, a.value[1], a.normal_side) for a in Account])
                cur.executemany(
                    f"insert into {mg.TABLE} (version, name, erd_version, checksum) values (%s, %s, %s, %s)",
                    [(m.version, m.name, m.erd_version, m.checksum) for m in migrations])
            from ..codes import seed as seed_codes   # 공통코드, as migration 0002 leaves them
            seed_codes(self.conn)
            # The default customer and account, as the 0001 migration leaves them on an existing database.
            self.conn.execute("insert into customers (customer_id, alias) values (%s, '기본 고객')",
                              (DEFAULT_CUSTOMER,))
            self.conn.execute(
                "insert into trading_accounts (account_id, customer_id, alias, broker, env, account_type,"
                " credential_ref, opened_on) values (%s, %s, '기본 계좌', 'KIS', 'PAPER', 'GENERAL', 'KIS',"
                " coalesce(%s, current_date))", (DEFAULT_ACCOUNT, DEFAULT_CUSTOMER, opened_on))
            self.conn.execute(
                "insert into account_status_history (account_id, status, effective_from, reason)"
                " values (%s, 'OPEN', coalesce(%s, current_date), '기본 계좌')", (DEFAULT_ACCOUNT, opened_on))

    # ----- write path -----

    def attach(self, ledger: Ledger) -> None:
        """Start persisting `ledger`. Its event stream must equal what is stored."""
        stored = self.conn.execute("select count(*) from ledger_events where account_id = %s",
                                   (self.account_id,)).fetchone()[0]
        if stored != len(ledger.events):
            raise SchemaError(f"ledger has {len(ledger.events)} events but DB has {stored} for account "
                              f"{self.account_id}; load the ledger from the store instead")
        self._entry_ids = [r[0] for r in self.conn.execute(
            "select entry_id from journal_entries where account_id = %s order by entry_id", (self.account_id,))]
        ledger.subscribe(self._on_event)

    def _on_event(self, ledger: Ledger, _nth: int, ev, entries) -> None:
        """`_nth` counts this account's events; the stored sequence number is database-wide."""
        acct = self.account_id
        with self.conn.transaction():
            # Fail closed: if this account's stored events are not exactly the ones this ledger has
            # applied, something else wrote to the account and nothing more may be added on top.
            stored = self.conn.execute("select count(*) from ledger_events where account_id = %s",
                                       (acct,)).fetchone()[0]
            if stored != _nth - 1:
                raise SchemaError(f"account {acct} has {stored} stored events but this ledger is at {_nth - 1}")
            # One sequence for all accounts. The single-writer lock keeps two sessions from
            # taking the same number; if one ever did, the primary key refuses it and the ledger halts.
            seq = self.conn.execute(
                "insert into ledger_events (seq, event_type, payload, account_id)"
                " select coalesce(max(seq), 0) + 1, %s, %s, %s from ledger_events returning seq",
                (type(ev).__name__, psycopg.types.json.Jsonb(to_payload(ev)), acct)).fetchone()[0]
            journal = ledger.journal
            for e in entries:
                rev_id = None
                if e.reverses is not None:
                    idx = next(i for i, x in enumerate(journal) if x is e.reverses)
                    rev_id = self._entry_ids[idx]
                eid = self.conn.execute(
                    "insert into journal_entries (event_seq, date, kind, source_id, reverses_entry_id, account_id)"
                    " values (%s, %s, %s, %s, %s, %s) returning entry_id",
                    (seq, e.date, e.kind, e.source_id, rev_id, acct)).fetchone()[0]
                self._entry_ids.append(eid)
                with self.conn.cursor() as cur:
                    cur.executemany(
                        "insert into journal_lines (entry_id, account_code, debit, credit)"
                        " values (%s, %s, %s, %s)",
                        [(eid, ln.account.code, ln.debit, ln.credit) for ln in e.lines])
                with self.conn.cursor() as cur:
                    cur.executemany(
                        "insert into cash_ledger (event_seq, date, reason, amount, ref_id, account_id)"
                        " values (%s, %s, %s, %s, %s, %s)", [(seq, *row, acct) for row in cash_rows(e)])
            row = position_row(ledger, ev)
            if row is not None:
                self.conn.execute(
                    "insert into position_ledger (event_seq, trade_date, symbol, exec_id,"
                    " qty_delta, cost_delta, realized_pnl, account_id) values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (seq, *row, acct))

    # ----- read path -----

    def load_events(self) -> list:
        """This account's events in order. The sequence itself is database-wide and must have no gap."""
        n, last = self.conn.execute("select count(*), coalesce(max(seq), 0) from ledger_events").fetchone()
        if n != last:
            raise SchemaError(f"event stream gap: {n} events but the last seq is {last}")
        return [from_payload(t, p) for t, p in self.conn.execute(
            "select event_type, payload from ledger_events where account_id = %s order by seq",
            (self.account_id,))]

    def load(self, calendar, fees, instruments, settlement_cycle=None) -> Ledger:
        """Rebuild the ledger by replaying stored events, then keep persisting it."""
        kw = {} if settlement_cycle is None else {"settlement_cycle": settlement_cycle}
        ledger = Ledger.replay(self.load_events(), calendar, fees, instruments, **kw)
        self.attach(ledger)
        return ledger

    def verify(self, ledger: Ledger) -> list[str]:
        """Compare stored projections with the ledger. Empty list = consistent."""
        from .integrity import projection_checks
        return [f"{c.title}: {c.detail}" for c in projection_checks(self.conn, ledger, self.account_id)
                if not c.ok]
