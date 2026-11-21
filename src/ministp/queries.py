"""조회 서비스 계층: the read transactions the STP-UI screens will call.

Each service reads the database only (never the broker, never a write) and
returns a `Dataset`: column names exactly as in the DB, so the screen platform
can take each column's Korean name from the schema COMMENTs. Services are
registered under a transaction id (`TRANSACTIONS`), one id per read the screens
need (화면 기획서: 조회 트랜잭션 하나 = 조회 서비스 하나).
"""
import datetime as dt
from dataclasses import dataclass

import psycopg

from .ledger import Ledger


@dataclass(frozen=True)
class Dataset:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]

    def records(self) -> list[dict]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def __len__(self) -> int:
        return len(self.rows)


def _dataset(cur: psycopg.Cursor) -> Dataset:
    return Dataset(tuple(c.name for c in cur.description), tuple(cur.fetchall()))


def _q(conn: psycopg.Connection, sql: str, params=()) -> Dataset:
    return _dataset(conn.execute(sql, params))


# Business date of an event: the date fields the ledger writes into the payload.
_EVENT_DATE = "coalesce(e.payload->>'trade_date', e.payload->>'date')::date"


# ---------- STP-3100 원장·분개 조회 ----------

def ledger_events(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  event_type: str | None = None, symbol: str | None = None) -> Dataset:
    """이벤트 원장 목록, newest first. Dates are business dates, not recording times."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               e.recorded_at, e.payload
          from ledger_events e
         where (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
         order by e.seq desc""", {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol})


@dataclass(frozen=True)
class EventTrace:
    event: Dataset       # the event itself (one row)
    journal: Dataset     # its entries and lines, with account names
    positions: Dataset   # position_ledger rows it wrote
    cash: Dataset        # cash_ledger rows it wrote

    @property
    def balanced(self) -> bool:
        """Every entry's debits equal its credits (the DB also enforces this)."""
        sums: dict[int, int] = {}
        for r in self.journal.records():
            sums[r["entry_id"]] = sums.get(r["entry_id"], 0) + r["debit"] - r["credit"]
        return all(v == 0 for v in sums.values())


def event_trace(conn: psycopg.Connection, seq: int) -> EventTrace:
    """이벤트 하나 → 분개 → 보유·예수금 변동 (원장 추적)."""
    event = _q(conn, f"select e.seq, e.event_type, {_EVENT_DATE} as date, e.recorded_at, e.payload"
                     " from ledger_events e where e.seq = %s", (seq,))
    if not event.rows:
        raise KeyError(f"no ledger event {seq}")
    journal = _q(conn, """
        select j.entry_id, j.date, j.kind, j.source_id, j.reverses_entry_id,
               l.line_id, l.account_code, a.name as account_name, l.debit, l.credit
          from journal_entries j join journal_lines l using (entry_id)
          join accounts_chart a on a.code = l.account_code
         where j.event_seq = %s order by j.entry_id, l.line_id""", (seq,))
    positions = _q(conn, "select id, trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                         " from position_ledger where event_seq = %s order by id", (seq,))
    cash = _q(conn, "select id, date, reason, amount, ref_id from cash_ledger where event_seq = %s order by id",
              (seq,))
    return EventTrace(event, journal, positions, cash)


def trial_balance(conn: psycopg.Connection) -> tuple[Dataset, bool]:
    """시산표 and whether total debits equal total credits over the whole journal."""
    ds = _q(conn, "select code, name, normal_side, balance from account_balances_v"
                  " where balance <> 0 order by code")
    dr, cr = conn.execute("select coalesce(sum(debit), 0), coalesce(sum(credit), 0) from journal_lines").fetchone()
    return ds, dr == cr


# ---------- STP-2100 대사 결과 ----------

def recon_history(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  kind: str = "DAILY_RECON", only_breaks: bool = False) -> Dataset:
    """대사 결과 with each break's latest owner judgement next to it (RESOLVED rows are not listed alone)."""
    return _q(conn, """
        select r.id, r.date, s.kind, s.taken_at, r.item, r.key, r.internal_value, r.broker_value, r.diff,
               r.status, r.cause_code,
               j.id as resolved_by, j.cause_code as resolved_cause, j.note as resolved_note,
               j.created_at as resolved_at
          from recon_results r
          join broker_snapshots s on s.snapshot_id = r.run_id
          left join lateral (select x.id, x.cause_code, x.note, x.created_at from recon_results x
                              where x.supersedes_id = r.id order by x.id desc limit 1) j on true
         where r.status <> 'RESOLVED'
           and s.kind = %(kind)s
           and (%(f)s::date is null or r.date >= %(f)s)
           and (%(t)s::date is null or r.date <= %(t)s)
           and (not %(ob)s or r.status = 'BREAK')
         order by r.date desc, r.run_id desc, r.id""",
              {"kind": kind, "f": date_from, "t": date_to, "ob": only_breaks})


# ---------- STP-1200 주문·체결 블로터 ----------

def order_blotter(conn: psycopg.Connection, day: dt.date) -> Dataset:
    """그날 주문: 의도 → 접수 → 반영된 체결, 체결통보 반영 지연."""
    return _q(conn, """
        select o.client_id, o.created_at, o.symbol, i.name as instrument_name, o.side, o.qty, o.limit_price,
               a.outcome, a.order_no, a.detail,
               coalesce(f.filled_qty, 0) as filled_qty, f.filled_amount,
               n.notices, n.max_booking_ms
          from oms_orders o
          join instruments i on i.symbol = o.symbol
          left join oms_order_acks a using (client_id)
          left join lateral (select sum((e.payload->>'qty')::bigint) as filled_qty,
                                    sum((e.payload->>'qty')::bigint * (e.payload->>'price')::bigint) as filled_amount
                               from ledger_events e
                              where e.event_type = 'ExecutionEvent' and a.order_no is not null
                                and e.payload->>'exec_id' like to_char(o.trade_date, 'YYYYMMDD') || '-' || a.order_no || '-%%') f on true
          left join lateral (select count(*) as notices, max((s.payload->>'booking_ms')::int) as max_booking_ms
                               from broker_snapshots s
                              where s.kind = 'FILL_NOTICE' and s.date = o.trade_date
                                and s.payload->'notice'->>'order_no' = a.order_no) n on true
         where o.trade_date = %s
         order by o.created_at desc""", (day,))


# ---------- STP-2200 기준가 ----------

def nav_series(ledger: Ledger, date_from: dt.date | None = None, date_to: dt.date | None = None) -> Dataset:
    """기준가 추이. NAV is derived, so it comes from the replayed ledger, not a table."""
    rows = tuple((r.date, r.cash, r.receivables, r.payables, r.market_value, r.net_assets, r.units,
                  r.nav_per_1000)
                 for r in ledger.nav_history()
                 if (date_from is None or r.date >= date_from) and (date_to is None or r.date <= date_to))
    return Dataset(("date", "cash", "receivables", "payables", "market_value", "net_assets", "units",
                    "nav_per_1000"), rows)


# Transaction id -> service. Ids follow the screen numbers (화면번호 + R + 일련번호).
TRANSACTIONS = {
    "STP3100R01": ledger_events,
    "STP3100R02": event_trace,
    "STP3100R03": trial_balance,
    "STP2100R01": recon_history,
    "STP1200R01": order_blotter,
    "STP2200R01": nav_series,
}
