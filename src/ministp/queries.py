"""조회 서비스 계층: the read transactions the STP-UI screens will call.

Each service reads the database only (never the broker, never a write) and
returns a `Dataset`: column names exactly as in the DB, so the screen platform
can take each column's Korean name from the schema COMMENTs. Services are
registered under a transaction id (`TRANSACTIONS`), one id per read the screens
need (화면 기획서: 조회 트랜잭션 하나 = 조회 서비스 하나).
"""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import psycopg

from .ledger import Ledger
from .risk import REASON_TEXT
from .store import DEFAULT_ACCOUNT


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


# ---------- STP-3100 원장 조회 · STP-3200 분개 조회 · STP-3300 시산표 ----------

def ledger_events(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  event_type: str | None = None, symbol: str | None = None, seq: int | None = None,
                  account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's 이벤트 원장 목록, newest first. Dates are business dates, not recording times.
    `seq` narrows the list to one event: how another screen opens this one on the event it had picked."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               e.recorded_at, e.payload
          from ledger_events e
         where e.account_id = %(a)s
           and (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
           and (%(seq)s::bigint is null or e.seq = %(seq)s)
         order by e.seq desc""",
              {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol, "seq": seq, "a": account_id})


def journal_events(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                   event_type: str | None = None, symbol: str | None = None, seq: int | None = None,
                   account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's events that wrote a journal, newest first, each with how much it wrote: 전표 수, 분개 줄 수,
    차변 합계. An event that books nothing (a NAV strike) is not listed: this is the list a journal is picked from."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               count(distinct j.entry_id)::int as entries, count(l.line_id)::int as lines,
               sum(l.debit)::bigint as amount
          from ledger_events e
          join journal_entries j on j.event_seq = e.seq
          join journal_lines l using (entry_id)
         where e.account_id = %(a)s
           and (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
           and (%(seq)s::bigint is null or e.seq = %(seq)s)
         group by e.seq
         order by e.seq desc""",
              {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol, "seq": seq, "a": account_id})


@dataclass(frozen=True)
class EventTrace:
    event: Dataset       # the event itself (one row)
    journal: Dataset     # its entries and lines, with account names
    positions: Dataset   # position_ledger rows it wrote
    cash: Dataset        # cash_ledger rows it wrote

    @property
    def balanced(self) -> bool:
        return _balanced(self.journal)


def _balanced(journal: Dataset) -> bool:
    """Every entry's debits equal its credits (the DB also enforces this)."""
    sums: dict[int, int] = {}
    for r in journal.records():
        sums[r["entry_id"]] = sums.get(r["entry_id"], 0) + r["debit"] - r["credit"]
    return all(v == 0 for v in sums.values())


def _journal_of(conn: psycopg.Connection, seq: int) -> Dataset:
    return _q(conn, """
        select j.entry_id, j.date, j.kind, j.source_id, j.reverses_entry_id,
               l.line_id, l.account_code, a.name as account_name, l.debit, l.credit
          from journal_entries j join journal_lines l using (entry_id)
          join accounts_chart a on a.code = l.account_code
         where j.event_seq = %s order by j.entry_id, l.line_id""", (seq,))


def event_journal(conn: psycopg.Connection, seq: int, account_id: str = DEFAULT_ACCOUNT) -> tuple[Dataset, bool]:
    """이벤트 하나가 만든 분개 전부 (전표와 줄), and whether every 전표 balances.
    An event of another account is not found."""
    if not conn.execute("select 1 from ledger_events where seq = %s and account_id = %s",
                        (seq, account_id)).fetchone():
        raise KeyError(f"no ledger event {seq}")
    journal = _journal_of(conn, seq)
    return journal, _balanced(journal)


def event_trace(conn: psycopg.Connection, seq: int, account_id: str = DEFAULT_ACCOUNT) -> EventTrace:
    """이벤트 하나 → 분개 → 보유·예수금 변동 (원장 추적). An event of another account is not found."""
    event = _q(conn, f"select e.seq, e.event_type, {_EVENT_DATE} as date, e.recorded_at, e.payload"
                     " from ledger_events e where e.seq = %s and e.account_id = %s", (seq, account_id))
    if not event.rows:
        raise KeyError(f"no ledger event {seq}")
    journal = _journal_of(conn, seq)
    positions = _q(conn, "select id, trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                         " from position_ledger where event_seq = %s order by id", (seq,))
    cash = _q(conn, "select id, date, reason, amount, ref_id from cash_ledger where event_seq = %s order by id",
              (seq,))
    return EventTrace(event, journal, positions, cash)


def trial_balance(conn: psycopg.Connection, account_id: str = DEFAULT_ACCOUNT) -> tuple[Dataset, bool]:
    """One account's 시산표 and whether its total debits equal its total credits."""
    ds = _q(conn, "select code, name, normal_side, balance from account_balances_v"
                  " where account_id = %s and balance <> 0 order by code", (account_id,))
    dr, cr = conn.execute(
        "select coalesce(sum(l.debit), 0), coalesce(sum(l.credit), 0)"
        " from journal_entries e join journal_lines l using (entry_id) where e.account_id = %s",
        (account_id,)).fetchone()
    return ds, dr == cr


# ---------- STP-2110 원장 내부 대사 ----------

RESULT_TEXT = {True: "일치", False: "불일치"}


def integrity_checks(conn: psycopg.Connection, calendar, fees, instruments,
                     account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """The `verify` report as rows: one check a row, in the order a person reads them. The checks of the
    whole database (event stream, guards, balance) and those of one account's books (replay against what
    is stored); the other accounts' checks are not listed, `verify` prints them all.
    It replays the events itself and does not go through `load_books`, which refuses books that
    fail these very checks: this is the list to look at when every other screen will not open."""
    from .store.integrity import integrity_report
    checks = [c for c in integrity_report(conn, calendar, fees, instruments) if c.account in ("", account_id)]
    return Dataset(("key", "title", "result", "detail"),
                   tuple((c.key, c.title, RESULT_TEXT[c.ok], c.detail) for c in checks))


def integrity_compared(conn: psycopg.Connection, title: str, calendar, fees,
                       instruments, account_id: str = DEFAULT_ACCOUNT) -> tuple[Dataset, Dataset]:
    """The check named `title` as (its verdict in a sentence, the numbers behind it side by side): what
    is stored, what it has to equal and the difference, as the broker reconciliation shows 내부 값 and
    증권사 값. Both are read in one snapshot. KeyError when there is no such check."""
    from .store.integrity_view import compared
    top = conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    with conn.transaction():
        if top:
            conn.execute("set transaction isolation level repeatable read read only")
        checks = integrity_checks(conn, calendar, fees, instruments, account_id)
        check = next((r for r in checks.rows if r[1] == title), None)
        if check is None:
            raise KeyError(f"없는 점검 항목: {title}")
        rows = compared(conn, check[0], calendar, fees, instruments, account_id)
    return (Dataset(checks.columns, (check,)),
            Dataset(("item", "stored", "expected", "basis", "diff", "result"),
                    tuple((r.item, r.stored, r.expected, r.basis, r.diff, RESULT_TEXT[r.ok]) for r in rows)))


# ---------- STP-2100 대사 결과 ----------

def recon_history(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  kind: str = "DAILY_RECON", only_breaks: bool = False,
                  account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's 대사 결과 with each break's latest owner judgement next to it (RESOLVED rows are not listed alone).

    Within a run the rows that need someone come first: a break nobody has judged, then judged breaks, then the
    matches, so a break of one holding out of a hundred is not lost in the middle. `key_name` names the
    instrument when the key is a symbol."""
    return _q(conn, """
        select r.id, r.date, s.kind, s.taken_at, r.item, r.key, i.name as key_name,
               r.internal_value, r.broker_value, r.diff,
               r.status, r.cause_code,
               j.id as resolved_by, j.cause_code as resolved_cause, j.note as resolved_note,
               j.created_at as resolved_at
          from recon_results r
          join broker_snapshots s on s.snapshot_id = r.run_id
          left join instruments i on i.symbol = r.key
          left join lateral (select x.id, x.cause_code, x.note, x.created_at from recon_results x
                              where x.supersedes_id = r.id order by x.id desc limit 1) j on true
         where r.status <> 'RESOLVED'
           and r.account_id = %(a)s
           and s.kind = %(kind)s
           and (%(f)s::date is null or r.date >= %(f)s)
           and (%(t)s::date is null or r.date <= %(t)s)
           and (not %(ob)s or r.status = 'BREAK')
         order by r.date desc, r.run_id desc, (r.status = 'BREAK' and j.id is null) desc, r.status = 'BREAK' desc, r.id""",
              {"kind": kind, "f": date_from, "t": date_to, "ob": only_breaks, "a": account_id})


def _open_day(calendar, day: dt.date) -> bool | None:
    """Whether the exchange is open that day; None when the holiday calendar does not cover it yet."""
    try:
        return calendar.is_business_day(day)
    except LookupError:
        return None


def recon_calendar(conn: psycopg.Connection, calendar, today: dt.date, date_from: dt.date | None = None,
                   date_to: dt.date | None = None, kind: str = "DAILY_RECON",
                   account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """한 계좌의 날짜별 대사 요약, newest first: one row a day, judged by that day's latest run (ops.recon_days,
    the same reading the gate count uses).

    일치 = no break · 판단 완료 = breaks, every one judged (the gate still counts the day) ·
    불일치 = a break nobody has judged. A weekday without a run is 휴장 on an exchange holiday,
    대사 전 if it is today, 대사 없음 if it has passed (일 마감 대사 only: a missed day ends the
    clean streak; an intraday recon is not owed every day). Days before the first run and after
    today have no row.
    """
    from .ops import recon_days
    date_to = min(date_to or today, today)
    if date_from is None:   # this month and the one before: enough to see the 20-day gate
        date_from = (date_to.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    days = recon_days(conn, kind, date_from, date_to, account_id)
    first = conn.execute("select min(r.date) from recon_results r join broker_snapshots b"
                         " on b.snapshot_id = r.run_id where b.kind = %s and r.account_id = %s",
                         (kind, account_id)).fetchone()[0]
    daily = kind == "DAILY_RECON"
    rows, d = [], date_to
    while d >= date_from:
        day = days.get(d)
        if day is not None:
            state = "일치" if not day.breaks else "판단 완료" if day.clean else "불일치"
            rows.append((d, state, kind, day.items, day.breaks, day.open_breaks))
        elif d.weekday() < 5:
            if _open_day(calendar, d) is False:
                rows.append((d, "휴장", kind, None, None, None))
            elif daily and first is not None and d >= first:
                rows.append((d, "대사 전" if d == today else "대사 없음", kind, None, None, None))
        d -= dt.timedelta(days=1)
    return Dataset(("date", "state", "kind", "items", "breaks", "open_breaks"), tuple(rows))


# ---------- STP-1200 주문·체결 블로터 ----------

def order_blotter(conn: psycopg.Connection, day: dt.date, account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's orders of the day: 의도 → 접수 → 반영된 체결, 체결통보 반영 지연, and the request it was sent
    for (R-101; orders from before ERD v2.9 or from `place_order` have none).

    Brokers number orders per account, so fills and notices are matched inside the order's account."""
    return _q(conn, """
        select o.client_id, o.trade_date, o.created_at, o.symbol, i.name as instrument_name, i.asset_type,
               o.side, o.qty, o.limit_price, a.outcome, a.order_no, a.detail,
               coalesce(f.filled_qty, 0) as filled_qty, f.filled_amount,
               n.notices, n.max_booking_ms, s.request_no, s.line_no, r.order_type,
               exists (select 1 from broker_snapshots c
                        where c.kind = 'CANCEL_REQUEST' and c.date = o.trade_date and c.account_id = o.account_id
                          and c.payload->>'client_id' = o.client_id) as cancel_requested
          from oms_orders o
          join instruments i on i.symbol = o.symbol
          left join oms_order_acks a using (client_id)
          left join order_sends s on s.client_id = o.client_id
          left join order_requests r on r.trade_date = s.trade_date and r.request_no = s.request_no
          left join lateral (select sum((e.payload->>'qty')::bigint) as filled_qty,
                                    sum((e.payload->>'qty')::bigint * (e.payload->>'price')::bigint) as filled_amount
                               from ledger_events e
                              where e.event_type = 'ExecutionEvent' and a.order_no is not null
                                and e.account_id = o.account_id
                                and e.payload->>'exec_id' like to_char(o.trade_date, 'YYYYMMDD') || '-' || a.order_no || '-%%') f on true
          left join lateral (select count(*) as notices, max((s.payload->>'booking_ms')::int) as max_booking_ms
                               from broker_snapshots s
                              where s.kind = 'FILL_NOTICE' and s.date = o.trade_date
                                and s.account_id = o.account_id
                                and s.payload->'notice'->>'order_no' = a.order_no) n on true
         where o.trade_date = %s and o.account_id = %s
         order by o.created_at desc""", (day, account_id))


# 주문 진행 단계: where an order stands on its way to cash. Each step is done · now · stop · wait.
ORDER_STEPS = ("의도", "접수", "체결", "결제")


def order_progress(outcome: str | None, qty: int, filled: int, settled: int, day_over: bool,
                   overdue: bool, canceled: bool = False) -> tuple[tuple[str, ...], str]:
    """The state of each of ORDER_STEPS and one phrase for the whole order.

    `day_over`: the order's trade date has passed, so whatever was not filled never will be
    (orders are day orders). `overdue`: a fill is past its settlement date and still unsettled.
    `canceled`: the broker took a cancel of the rest (R-45, also the basket stop of R-103), so the rest will not fill
    either: nothing filled reads 취소 요청, a part filled goes on to settlement like a day that is over.
    """
    if canceled and outcome == "ACCEPTED" and filled == 0:
        return ("done", "done", "stop", "wait"), "취소 요청"
    day_over = day_over or canceled
    if outcome is None:        # intent stored, no answer recorded: the broker's order list has to be checked
        return ("done", "now", "wait", "wait"), "접수 기록 없음"
    if outcome != "ACCEPTED":
        return ("done", "stop", "wait", "wait"), "접수 거부"
    if filled == 0:
        return (("done", "done", "stop", "wait"), "미체결 종료") if day_over else (("done", "done", "now", "wait"), "체결 대기")
    if filled < qty and not day_over:
        return ("done", "done", "now", "wait"), "부분 체결"
    if settled >= filled:
        return ("done", "done", "done", "done"), "결제 완료"
    return ("done", "done", "done", "stop" if overdue else "now"), "결제 지연" if overdue else "결제 대기"


def _order_key(trade_date: dt.date, order_no: str) -> str:
    """What every execution id of one broker order starts with (`YYYYMMDD-주문번호-순번`)."""
    return f"{trade_date:%Y%m%d}-{order_no}"


def _settlement_facts(ledger: Ledger):
    """Executions by order, the day each one settled, and the ones still waiting, all from the replayed ledger."""
    from .ledger import ExecutionEvent, SettlementEvent
    fills: dict[str, list] = {}
    settled_on: dict[str, dt.date] = {}
    for e in ledger.events:
        if isinstance(e, ExecutionEvent):
            fills.setdefault(e.exec_id.rsplit("-", 1)[0], []).append(e)
        elif isinstance(e, SettlementEvent):
            settled_on[e.exec_id] = e.date
    pending = {x.exec_id: x.settle_date for x in ledger.pending_settlements()}
    return fills, settled_on, pending


def blotter_view(base: Dataset, ledger: Ledger, today: dt.date) -> Dataset:
    """The blotter with each order's progress: settled quantity, the four steps and one phrase (진행 상태).

    Settlement is read from the replayed ledger, the same one the cash screens use.
    """
    fills, settled_on, pending = _settlement_facts(ledger)
    rows = []
    for r in base.records():
        mine = fills.get(_order_key(r["trade_date"], r["order_no"]), []) if r["order_no"] else []
        settled = sum(e.qty for e in mine if e.exec_id in settled_on)
        overdue = any(pending.get(e.exec_id, today) < today for e in mine)
        steps, stage = order_progress(r["outcome"], r["qty"], r["filled_qty"], settled, r["trade_date"] < today, overdue,
                                      bool(r.get("cancel_requested")))
        rows.append((*r.values(), settled, list(steps), stage, request_label(r)))
    return Dataset((*base.columns, "settled_qty", "progress", "stage", "request"), tuple(rows))


def request_label(r: dict) -> str | None:
    """요청 번호를 화면에 쓰는 모양: 단건은 3, 바스켓의 줄은 3-2 (R-99 요청 번호는 주문일마다 1부터)."""
    if r.get("request_no") is None:
        return None
    return f"{r['request_no']}-{r['line_no']}" if r.get("order_type") == "BASKET" else str(r["request_no"])


@dataclass(frozen=True)
class OrderRequest:
    request: Dataset   # the request the order was sent for (one row, none if it came from no request)
    lines: Dataset     # every line of that request: what went, what the broker said, how much filled


def order_request(conn: psycopg.Connection, ledger: Ledger, client_id: str, today: dt.date,
                  account_id: str = DEFAULT_ACCOUNT) -> OrderRequest:
    """STP-1200 오른쪽 아래: 고른 주문이 나간 요청과 그 요청의 모든 줄 (C-21).

    바스켓은 줄마다 주문이 따로 나가요. 체결률은 요청 금액(수량 × 지정가) 가운데 체결된 금액이에요.
    나가지 않은 줄(바스켓 중단, 보내기 직전 검증)과 결과를 모르는 줄도 함께 보여요."""
    head = ("trade_date", "request_no", "order_type", "status", "line_count", "amount", "filled_amount", "fill_rate",
            "accepted_lines", "rejected_lines", "unknown_lines", "created_by", "approved_by", "last_reason")
    cols = ("line_no", "symbol", "instrument_name", "asset_type", "side", "qty", "limit_price", "amount", "send_outcome",
            "broker_answer", "order_no", "filled_qty", "filled_amount", "stage", "client_id", "reason")
    key = conn.execute("select s.trade_date, s.request_no from order_sends s join order_requests r"
                       " using (trade_date, request_no) where s.client_id = %s and r.account_id = %s",
                       (client_id, account_id)).fetchone()
    if key is None:
        return OrderRequest(Dataset(head, ()), Dataset(cols, ()))
    day, no = key
    blotter = {r["client_id"]: r for r in blotter_view(order_blotter(conn, day, account_id), ledger, today).records()}
    lines = []
    for ln in _q(conn, """
            select l.line_no, l.symbol, i.name as instrument_name, i.asset_type, l.side, l.qty, l.limit_price, l.amount,
                   l.send_outcome, l.ack_outcome, l.awaiting_ack, l.order_no, l.client_id,
                   coalesce(l.send_reason, case when l.ack_outcome = 'REJECTED' then l.ack_detail end) as reason
              from order_request_lines_v l join instruments i on i.symbol = l.symbol
             where l.trade_date = %s and l.request_no = %s order by l.line_no""", (day, no)).records():
        b = blotter.get(ln["client_id"]) or {}
        answer = "결과 미확인" if ln["awaiting_ack"] else {"ACCEPTED": "접수", "REJECTED": "거부"}.get(ln["ack_outcome"] or "", "")
        stage = b.get("stage") or ("전송 실패" if ln["send_outcome"] == "FAILED" else "보내기 전")
        lines.append((ln["line_no"], ln["symbol"], ln["instrument_name"], ln["asset_type"], ln["side"], ln["qty"], ln["limit_price"],
                      ln["amount"], ln["send_outcome"], answer, ln["order_no"], b.get("filled_qty", 0),
                      b.get("filled_amount") or 0, stage, ln["client_id"], ln["reason"]))
    q = _q(conn, """select trade_date, request_no, order_type, status, line_count, amount, accepted_lines,
                           rejected_lines, unknown_lines, created_by, approved_by, last_reason
                      from order_requests_v where trade_date = %s and request_no = %s""", (day, no)).records()[0]
    filled = sum(x[12] for x in lines)
    rate = round(Decimal(filled) * 100 / Decimal(q["amount"]), 2) if q["amount"] else None
    row = (q["trade_date"], q["request_no"], q["order_type"], q["status"], q["line_count"], q["amount"], filled, rate,
           q["accepted_lines"], q["rejected_lines"], q["unknown_lines"], q["created_by"], q["approved_by"],
           q["last_reason"])
    return OrderRequest(Dataset(head, (row,)), Dataset(cols, tuple(lines)))


@dataclass(frozen=True)
class OrderDetail:
    order: Dataset   # what was sent and what came back (one row)
    fills: Dataset   # each execution of the order with its settlement


def order_detail(conn: psycopg.Connection, ledger: Ledger, client_id: str, today: dt.date,
                 account_id: str = DEFAULT_ACCOUNT) -> OrderDetail:
    """주문 하나의 접수 내용과 체결별 결제: 체결일 → 결제 예정일 → 결제일.

    `ledger` is that account's ledger; an order of another account is not found here."""
    order = _q(conn, """
        select o.client_id, o.trade_date, o.limit_price, a.outcome, a.order_no, a.created_at as acked_at, a.detail,
               n.notices, n.max_booking_ms
          from oms_orders o
          left join oms_order_acks a using (client_id)
          left join lateral (select count(*) as notices, max((s.payload->>'booking_ms')::int) as max_booking_ms
                               from broker_snapshots s
                              where s.kind = 'FILL_NOTICE' and s.date = o.trade_date
                                and s.account_id = o.account_id
                                and s.payload->'notice'->>'order_no' = a.order_no) n on true
         where o.client_id = %s and o.account_id = %s""", (client_id, account_id))
    if not order.rows:
        raise KeyError(f"없는 주문이에요: {client_id}")
    o = order.records()[0]
    fills, settled_on, pending = _settlement_facts(ledger)
    rows = []
    for e in fills.get(_order_key(o["trade_date"], o["order_no"]), []) if o["order_no"] else []:
        res = ledger.execution_result(e.exec_id)
        done = settled_on.get(e.exec_id)
        state = "결제 완료" if done else "결제 지연" if pending.get(e.exec_id, today) < today else "결제 대기"
        rows.append((e.exec_id, e.trade_date, e.qty, e.price, res.gross, res.commission + res.tx_tax + res.rural_tax,
                     res.net, res.settle_date, done, state))
    return OrderDetail(order, Dataset(("exec_id", "trade_date", "qty", "price", "gross", "fees", "net", "settle_date",
                                       "settled_on", "settle_state"), tuple(rows)))


# ---------- STP-2200 기준가 ----------

def nav_series(ledger: Ledger, date_from: dt.date | None = None, date_to: dt.date | None = None) -> Dataset:
    """기준가 추이. NAV is derived, so it comes from the replayed ledger, not a table."""
    rows = tuple((r.date, r.cash, r.receivables, r.payables, r.market_value, r.net_assets, r.units,
                  r.nav_per_1000)
                 for r in ledger.nav_history()
                 if (date_from is None or r.date >= date_from) and (date_to is None or r.date <= date_to))
    return Dataset(("date", "cash", "receivables", "payables", "market_value", "net_assets", "units",
                    "nav_per_1000"), rows)


# ---------- STP-2210 기준가 이력 (날짜별 기준가와 변동 상세) ----------

# What an account's movement does to net assets: equity put in or taken out, income, expense.
# Assets and liabilities are what net assets is made of, so they explain nothing here.
MOVE_KIND = {"3": "입출금", "4": "수익", "5": "비용"}
# 계정과목표 v2: tax withheld is 선납세금 (1040), an asset of the company but not of the brokerage account, so
# it leaves net assets (R-60) the way an expense did in v1, and explains that part of the change.
PREPAID_MOVE = "선납세금"


def _move_kind(account) -> str | None:
    from .accounting import Account
    return PREPAID_MOVE if account is Account.PREPAID_TAX else MOVE_KIND.get(account.code[0])


@dataclass(frozen=True)
class NavChange:
    date: dt.date
    prev_net_assets: int
    net_assets: int
    moves: tuple[tuple[str, str, str, int], ...]   # (구분, 계정코드, 계정명, 순자산에 준 영향)
    units: int = 0
    nav_per_1000: object = None                    # Decimal, as the NAV screen shows it
    prev_nav_per_1000: object = None               # None on the first day
    struck: int = 1                                # how many times the day's NAV was recorded

    @property
    def nav_change(self):
        return None if self.prev_nav_per_1000 is None else self.nav_per_1000 - self.prev_nav_per_1000

    @property
    def change(self) -> int:
        return self.net_assets - self.prev_net_assets

    @property
    def explained(self) -> int:
        return sum(m[3] for m in self.moves)

    @property
    def unexplained(self) -> int:
        return self.change - self.explained


def nav_changes(ledger: Ledger) -> list[NavChange]:
    """Each NAV day's change in net assets against what the journal booked since the day before.

    Net assets is counted from what is held (예수금 + 미수 - 미지급 + 보유수량 × 종가, R-60); the journal
    says why it moved (본사 자금 대체, 수익, 비용 accounts, and 선납세금 that left the account). The two are worked out separately, so their
    difference is a check: it is 0 when every won of the change has an entry behind it.
    A day struck more than once counts once, at its last NAV."""
    days: dict[dt.date, tuple] = {}
    for row, entries in ledger.nav_steps():
        _, before, struck = days.get(row.date, (None, [], 0))
        days[row.date] = (row, before + list(entries), struck + 1)
    out, prev, prev_nav = [], 0, None
    for date, (row, entries, struck) in days.items():
        net = row.net_assets
        effect: dict = {}
        for e in entries:
            for ln in e.lines:
                if _move_kind(ln.account):
                    effect[ln.account] = effect.get(ln.account, 0) + ln.credit - ln.debit
        moves = tuple((_move_kind(a), a.code, a.value[1], v)
                      for a, v in sorted(effect.items(), key=lambda kv: kv[0].code) if v)
        out.append(NavChange(date, prev, net, moves, row.units, row.nav_per_1000, prev_nav, struck))
        prev, prev_nav = net, row.nav_per_1000
    return out


def nav_change_days(ledger: Ledger, date_from: dt.date | None = None, date_to: dt.date | None = None) -> Dataset:
    """기준가 이력, newest first: the day's NAV record (기준가, 전일 대비, 순자산, 좌수, 산출 횟수) and whether
    the journal explains the change in net assets (분개로 설명된 변동, 설명되지 않은 차이)."""
    rows = tuple((c.date, c.nav_per_1000, c.nav_change, c.net_assets, c.units, c.struck, c.change, c.explained,
                  c.unexplained, RESULT_TEXT[c.unexplained == 0])
                 for c in reversed(nav_changes(ledger))
                 if (date_from is None or c.date >= date_from) and (date_to is None or c.date <= date_to))
    return Dataset(("date", "nav_per_1000", "nav_change", "net_assets", "units", "struck", "change", "explained",
                    "unexplained", "result"), rows)


def nav_change_bridge(ledger: Ledger, date: dt.date) -> Dataset:
    """One day's change as a bridge read top to bottom: 전일 순자산, each account that moved it,
    what no entry explains, 당일 순자산. `running` is net assets so far. It reaches the day's net assets by
    construction, so the check is the 설명되지 않은 차이 row being 0.
    KeyError when no NAV was struck that day."""
    c = next((c for c in nav_changes(ledger) if c.date == date), None)
    if c is None:
        raise KeyError(f"{date} 기준가가 없어요")
    rows, running = [("전일 순자산", None, None, None, c.prev_net_assets)], c.prev_net_assets
    for kind, code, name, amount in c.moves:
        running += amount
        rows.append((kind, code, name, amount, running))
    running += c.unexplained
    rows.append(("설명되지 않은 차이", None, None, c.unexplained, running))
    rows.append(("당일 순자산", None, None, None, c.net_assets))
    return Dataset(("kind", "code", "name", "amount", "running"), tuple(rows))


# ---------- STP-2300 시세 추이 ----------

def _rate_pct(rate) -> float | None:
    """0.0123 -> 1.23 (a percentage with two places, for a column that is coloured by its sign)."""
    return None if rate is None else float(round(rate * 100, 2)) + 0.0   # + 0.0: -0.001% shows as 0.00, not -0.00


def _in_period(checks, date_from: dt.date | None, date_to: dt.date | None) -> list:
    return [c for c in checks if (date_from is None or c.date >= date_from) and (date_to is None or c.date <= date_to)]


def price_trend_symbols(ledger: Ledger, date_from: dt.date | None = None, date_to: dt.date | None = None) -> Dataset:
    """종목별 기간 시세: one row for every symbol the books took a close for in the period.

    기간 등락 is the last close against the close before the period (the first close of the period when
    there is none before it), so a one-day period reads as that day against the day before. `trend` is the
    period's closes, oldest first. `level` is the worst result of the period's checks on the closes themselves
    (pricecheck: tick grid, valuation and NAV at the same close, the price limit): whether a close can be a
    close, not why the price moved. The period is carried on each row for the detail to use. The worst level
    comes first (확인 필요, then 주의, then 정상), by symbol within a level, so a warning is not lost among a hundred."""
    from decimal import Decimal

    from .pricecheck import _RANK, price_checks, worst
    by_symbol: dict[str, list] = {}
    for c in _in_period(price_checks(ledger), date_from, date_to):
        by_symbol.setdefault(c.symbol, []).append(c)
    rows = []
    for symbol, mine in sorted(by_symbol.items()):
        inst = ledger.instruments.get(symbol)
        base = mine[0].prev_close or mine[0].close
        closes = [c.close for c in mine]
        rows.append((symbol, inst.name if inst else None, inst.asset_type.value if inst else None, len(mine), base,
                     closes[-1], mine[-1].date, _rate_pct(Decimal(closes[-1]) / base - 1) if base > 0 else None,
                     max(closes), min(closes), closes, worst(c.level for c in mine), date_from, date_to))
    rows.sort(key=lambda r: -_RANK[r[11]])   # stable: by symbol within a level
    return Dataset(("symbol", "instrument_name", "asset_type", "days", "base_close", "last_close", "last_date",
                    "change", "high", "low", "trend", "level", "date_from", "date_to"), tuple(rows))


def price_trend_rows(ledger: Ledger, symbol: str, date_from: dt.date | None = None,
                     date_to: dt.date | None = None) -> Dataset:
    """One symbol's closes over the period, newest first: each against the close before it and the day's
    executions, with what the checks on the close said."""
    from .pricecheck import price_checks
    mine = [c for c in _in_period(price_checks(ledger), date_from, date_to) if c.symbol == symbol]
    return Dataset(("date", "close", "prev_close", "diff", "change", "exec_gap", "level", "reasons"),
                   tuple((c.date, c.close, c.prev_close, c.close - c.prev_close if c.prev_close else None,
                          _rate_pct(c.change), _rate_pct(c.exec_gap), c.level, " · ".join(c.reasons))
                         for c in reversed(mine)))


# ---------- STP-2400 마감 점검 ----------

def _close_board(conn: psycopg.Connection, calendar, fees, today: dt.date,
                 account_id: str = DEFAULT_ACCOUNT) -> dict:
    """Every business day's steps for one account (closeboard.board). The books are replayed here and not
    taken from `load_books`, which refuses books that fail verification: the board is where that failure
    has to show. 원장 내부 대사 counts the checks of the whole database and of this account."""
    from . import closeboard as cb
    from .ops import load_instruments, recon_days
    from .store.integrity import integrity_report
    from .store.integrity_view import replay
    instruments = load_instruments(conn)
    checks = [c for c in integrity_report(conn, calendar, fees, instruments) if c.account in ("", account_id)]
    orders: dict[dt.date, list] = {}
    for day, qty, outcome, order_no in conn.execute(
            "select o.trade_date, o.qty, a.outcome, a.order_no from oms_orders o"
            " left join oms_order_acks a using (client_id) where o.account_id = %s order by o.created_at",
            (account_id,)):
        orders.setdefault(day, []).append((qty, outcome, order_no))
    steps = cb.load_steps(conn)
    outside = cb.Outside(orders, recon_days(conn, account_id=account_id), len(checks),
                         sum(not c.ok for c in checks), _dividend_days(conn, account_id), _erp_days(conn, account_id))
    try:
        ledger = replay(conn, calendar, fees, instruments, account_id)
    except Exception:       # events the rules reject: no day can be read, the one thing to show is that
        return {today: [cb.integrity_step(outside, steps)]}
    kinds = {instruments[s].asset_type.value for s in _symbols_had(ledger) if s in instruments}
    from dataclasses import replace
    return cb.board(ledger, calendar, today, replace(outside, asset_types=frozenset(kinds)), steps)


def _dividend_days(conn: psycopg.Connection, account_id: str) -> list:
    """(accrued_on, pay_date, paid_on, status) of the account's dividends, for the 배당 step."""
    if not conn.execute("select to_regclass('dividends_v') is not null").fetchone()[0]:
        return []
    return conn.execute("select d.accrued_on, a.pay_date, d.paid_on, d.status from dividends_v d"
                        " join corporate_actions_v a using (action_id) where d.account_id = %s",
                        (account_id,)).fetchall()


def _erp_days(conn: psycopg.Connection, account_id: str) -> frozenset:
    """Days of the account that an ERP export not voided holds (R-104), for the ERP 전송 step."""
    if not conn.execute("select to_regclass('erp_export_docs') is not null").fetchone()[0]:
        return frozenset()
    return frozenset(d for (d,) in conn.execute(
        "select distinct d.doc_date from erp_export_docs d where d.account_id = %s"
        " and not exists (select 1 from erp_export_voids v where v.export_id = d.export_id)", (account_id,)))


def _symbols_had(ledger: Ledger) -> set[str]:
    """Every stock the account has traded or held."""
    from .ledger import ExecutionEvent, OpeningPositionEvent
    return {e.symbol for e in ledger.events if isinstance(e, (ExecutionEvent, OpeningPositionEvent))}


def close_days(conn: psycopg.Connection, calendar, fees, today: dt.date, date_from: dt.date | None = None,
               date_to: dt.date | None = None, account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """영업일 목록, newest first: how many of the day's steps are finished and whether anything needs a decision."""
    from . import closeboard as cb
    rows = [(day, cb.finished(steps), cb.required(steps), cb.needing(steps), cb.day_state(steps), cb.optional_left(steps))
            for day, steps in _close_board(conn, calendar, fees, today, account_id).items()
            if (date_from is None or day >= date_from) and (date_to is None or day <= date_to)]
    return Dataset(("date", "done", "steps", "attention", "state", "optional"), tuple(rows))


def close_day_steps(conn: psycopg.Connection, calendar, fees, today: dt.date, date: dt.date,
                    account_id: str = DEFAULT_ACCOUNT) -> list:
    """One day's closeboard.Step list (what `close-check` keeps). KeyError for a day not on the board."""
    days = _close_board(conn, calendar, fees, today, account_id)
    if date not in days:
        raise KeyError(f"{date}은 영업일이 아니거나 원장을 시작하기 전이에요")
    return days[date]


def close_steps(conn: psycopg.Connection, calendar, fees, today: dt.date, date: dt.date,
                account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One day's steps in the order `daily` does them, each with its state, what it found and the screen
    that shows the detail."""
    days = _close_board(conn, calendar, fees, today, account_id)
    if date not in days:
        raise KeyError(f"{date}은 영업일이 아니거나 원장을 시작하기 전이에요")
    return Dataset(("no", "step", "kind", "state", "detail", "screen", "date"),
                   tuple((s.no, s.name, "필수" if s.required else "선택", s.state, s.detail, s.screen, date)
                         for s in days[date]))


# ---------- STP-6100 수익률 ----------
# The book's return is the return of its NAV per 1000 units (R-67). Money put in or taken out buys or
# redeems units at the NAV before it (R-61), so it moves net assets but not the NAV: the NAV's change is
# the return on what was invested, the time-weighted return, with no cash-flow arithmetic of its own.
# 손익 is the same day in won: the change in net assets less what was put in or taken out.

@dataclass(frozen=True)
class ReturnDay:
    date: dt.date
    base_nav: object      # Decimal: the NAV before this day (1,000.00 before the first)
    nav: object           # Decimal: the day's NAV
    net_assets: int
    flows: int            # 입출금 booked since the NAV before
    profit: int           # change in net assets less flows

    @property
    def day_return(self):
        return self.nav / self.base_nav - 1


def return_days(ledger: Ledger) -> list[ReturnDay]:
    """Every NAV day (a day struck twice counts once, at its last NAV) with what it returned."""
    from .ledger import INITIAL_NAV
    out = []
    for c in nav_changes(ledger):
        flows = sum(m[3] for m in c.moves if m[0] == "입출금")
        base = INITIAL_NAV if c.prev_nav_per_1000 is None else c.prev_nav_per_1000
        out.append(ReturnDay(c.date, base, c.nav_per_1000, c.net_assets, flows, c.change - flows))
    return out


def _period_days(days: list[ReturnDay], date_from: dt.date | None, date_to: dt.date | None) -> list[ReturnDay]:
    return [d for d in days if (date_from is None or d.date >= date_from) and (date_to is None or d.date <= date_to)]


def _return(base, end) -> float | None:
    return _rate_pct(end / base - 1) if base else None


def _month_back(day: dt.date) -> dt.date:
    """The same day a month earlier (the month's last day when it is shorter): 03-31 -> 02-28."""
    import calendar as cal
    y, m = (day.year, day.month - 1) if day.month > 1 else (day.year - 1, 12)
    return dt.date(y, m, min(day.day, cal.monthrange(y, m)[1]))


def return_periods(ledger: Ledger) -> dict:
    """기간 수익률 to the last NAV: 설정 이후, 연초 이후, 최근 1개월. Each is the last NAV against the NAV
    just before the period starts (1,000.00 when the books start inside it). Percentages with two places;
    {} before the first NAV. The return of a period asked for is the newest month's 누적 (return_months)."""
    days = return_days(ledger)
    if not days:
        return {}
    last = days[-1]

    def after(start: dt.date) -> float | None:      # from the NAV on or before `start` to the last NAV
        inside = [d for d in days if d.date > start]
        return _return(inside[0].base_nav, last.nav) if inside else 0.0

    return {"last_date": last.date, "since_start": _return(days[0].base_nav, last.nav),
            "year_to_date": after(dt.date(last.date.year - 1, 12, 31)), "one_month": after(_month_back(last.date))}


def return_months(ledger: Ledger, date_from: dt.date | None = None, date_to: dt.date | None = None) -> Dataset:
    """월별 수익률, newest first. A month's return is its last NAV against the NAV before the month (the
    month-end before it); 누적 runs from the start of the period (from the first NAV when none is asked).
    손익 and 입출금 are the month's won. `trend` is the month's NAVs, oldest first."""
    days = _period_days(return_days(ledger), date_from, date_to)
    months: dict[str, list[ReturnDay]] = {}
    for d in days:
        months.setdefault(f"{d.date:%Y-%m}", []).append(d)
    start = days[0].base_nav if days else None
    rows = tuple((month, ds[0].date, ds[-1].date, len(ds), ds[0].base_nav, ds[-1].nav,
                  _return(ds[0].base_nav, ds[-1].nav), _return(start, ds[-1].nav), sum(d.flows for d in ds),
                  sum(d.profit for d in ds), ds[-1].net_assets, [float(d.nav) for d in ds], date_from, date_to)
                 for month, ds in reversed(months.items()))
    return Dataset(("month", "first_date", "last_date", "days", "base_nav", "end_nav", "month_return",
                    "cum_return", "flows", "profit", "net_assets", "trend", "date_from", "date_to"), rows)


def return_month_days(ledger: Ledger, month: str, date_from: dt.date | None = None,
                      date_to: dt.date | None = None) -> Dataset:
    """One month's NAV days, newest first: the day's return, the month's return so far, and the won.
    ValueError when `month` is not YYYY-MM."""
    try:
        first = dt.datetime.strptime(month, "%Y-%m").date()
    except (TypeError, ValueError):
        raise ValueError(f"월은 YYYY-MM으로 적어 주세요: {month}") from None
    days = [d for d in _period_days(return_days(ledger), date_from, date_to)
            if (d.date.year, d.date.month) == (first.year, first.month)]
    base = days[0].base_nav if days else None
    return Dataset(("date", "nav_per_1000", "day_return", "month_to_date", "net_assets", "flows", "profit"),
                   tuple((d.date, d.nav, _rate_pct(d.day_return), _return(base, d.nav), d.net_assets, d.flows,
                          d.profit) for d in reversed(days)))


# ---------- STP-1100 잔고 (IBOR) ----------

def holdings(conn: psycopg.Connection, day: dt.date | None = None, account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's 종목별 보유: 수량, 장부금액, 오늘 체결 수량, 마지막 변동 시각."""
    return _q(conn, """
        select p.symbol, i.name as instrument_name, i.asset_type, p.qty, p.book_cost,
               coalesce(t.today_qty, 0) as today_qty, t.last_change
          from positions_v p
          join instruments i on i.symbol = p.symbol
          left join lateral (select sum(x.qty_delta) filter (where x.trade_date = %(d)s) as today_qty,
                                    max(e.recorded_at) as last_change
                               from position_ledger x join ledger_events e on e.seq = x.event_seq
                              where x.symbol = p.symbol and x.account_id = p.account_id) t on true
         where p.account_id = %(a)s and p.qty <> 0
         order by p.book_cost desc""", {"d": day or dt.date.today(), "a": account_id})


def close_trends(ledger: Ledger, days: int = 20) -> dict[str, list[int]]:
    """종목별 최근 종가, 오래된 날부터 최대 days개. 기준가·재평가 이벤트가 남긴 값만 써요 (하루에 마지막 값)."""
    from .ledger import NavEvent, RevaluationEvent
    by_date: dict[dt.date, dict[str, int]] = {}
    for ev in ledger.events:
        if isinstance(ev, (NavEvent, RevaluationEvent)):
            by_date.setdefault(ev.date, {}).update(dict(ev.closes))
    out: dict[str, list[int]] = {}
    for day in sorted(by_date)[-days:]:
        for symbol, price in by_date[day].items():
            out.setdefault(symbol, []).append(price)
    return out


def holdings_view(base: Dataset, ledger: Ledger, quotes=None, now: dt.datetime | None = None) -> Dataset:
    """보유에 평가 가격, 평가금액, 평가손익, 비중을 붙여요 (IBOR, R-68).

    평가 가격은 원장이 아는 가장 최근 종가와 오늘 `run`이 받은 현재가(`quotes`) 가운데 더 늦은 것이에요.
    가격이 아직 없는 종목은 추측하지 않고 비워 둬요. 비중은 평가금액 ÷ 평가금액 합계예요."""
    from .quotes import marks
    cost = base.columns.index("book_cost")
    symbol = base.columns.index("symbol")
    qty = base.columns.index("qty")
    prices = marks(*last_closes(ledger), quotes or {}, now or dt.datetime.now())

    def valued(r) -> tuple:
        m = prices.get(r[symbol])
        if m is None:
            return None, None, None, None, None
        value = r[qty] * m.price
        pnl = value - r[cost]
        return m.price, _mark_time(m), value, pnl, f"{float(pnl) / float(r[cost]) * 100 + 0.0:+.2f}%" if r[cost] else None

    extra = [valued(r) for r in base.rows]
    total = sum(x[2] for x in extra if x[2] is not None)
    rows = tuple((*r, *x, round(x[2] / total, 4) if total and x[2] is not None else None)
                 for r, x in zip(base.rows, extra))
    return Dataset((*base.columns, "price", "price_at", "market_value", "unrealized_pnl", "pnl_rate", "weight"),
                   rows)


def _mark_time(m) -> str:
    """가격 칸 아래에 적는 말: 현재가는 받은 시각, 종가는 그 날짜."""
    if m.source == "현재가":
        return f"{m.as_of:%H:%M}" + (" 갱신 멈춤" if m.stale else "")
    return f"종가 {m.as_of:%m-%d}"


def valuation_basis(ledger: Ledger, symbols, quotes=None, now: dt.datetime | None = None) -> str:
    """그리드 머리의 "평가 기준": 보유 종목에 쓴 가격 가운데 가장 오래된 쪽 (섞이면 칸 아래 시각으로 구분)."""
    from .quotes import marks
    prices = marks(*last_closes(ledger), quotes or {}, now or dt.datetime.now())
    used = [prices[s] for s in symbols if s in prices]
    if not used:
        return "가격 없음"
    closes = [m for m in used if m.source == "종가"]
    if closes:
        return f"종가 {min(m.as_of for m in closes):%m-%d}"
    oldest = min(m.as_of for m in used)
    return f"현재가 {oldest:%H:%M}" + (" · 갱신 멈춤" if any(m.stale for m in used) else "")


def cash_summary(ledger: Ledger, open_orders) -> Dataset:
    """예수금 D+0, 결제 후 예수금, 미체결 매수 예약, 주문가능금액 (R-37과 같은 계산)."""
    from .capacity import pending_buy_value
    reserved, unpriced = pending_buy_value(open_orders, {})
    projected = ledger.projected_cash()
    return Dataset(("cash_d0", "projected_cash", "pending_buy_value", "unpriced_buy_orders", "available_cash"),
                   ((ledger.cash_balance(), projected, reserved, unpriced, projected - reserved),))




# ---------- STP-1300 주문가능 조회 ----------



def last_closes(ledger: Ledger) -> tuple[dt.date | None, dict[str, int]]:
    """The most recent closing prices the ledger knows (NavEvent/RevaluationEvent). Live quotes: quotes.marks."""
    from .ledger import NavEvent, RevaluationEvent
    for ev in reversed(ledger.events):
        if isinstance(ev, (NavEvent, RevaluationEvent)):
            return ev.date, dict(ev.closes)
    return None, {}


def capacity_by_symbol(books, day: dt.date) -> Dataset:
    """종목별 주문가능: 최근 종가 기준 최대 매수수량과 매도가능수량 (R-37과 같은 계산)."""
    from .ops import order_capacity
    close_day, closes = last_closes(books.ledger)
    rows = []
    for sym, inst in sorted(books.instruments.items()):
        price = closes.get(sym)
        if price is None:
            continue
        cap = order_capacity(books, day, sym, price, closes)
        rows.append((sym, inst.name, price, close_day, cap.available_cash, cap.max_buy_qty, cap.held_qty,
                     cap.pending_sell_qty, cap.sellable_qty, inst.asset_type.value))
    return Dataset(("symbol", "instrument_name", "ref_price", "price_date", "available_cash", "max_buy_qty",
                    "held_qty", "pending_sell_qty", "sellable_qty", "asset_type"), tuple(rows))


def order_check(books, day: dt.date, symbol: str, side: str, qty: int, limit_price: int | None,
                halted: bool = False) -> Dataset:
    """주문 입력 하나에 대한 주문가능 판단: 화면은 입력할 때마다 이걸 부르고, 주문 검증(session.submit)과 같은 check를 써요."""
    from .capacity import check
    from .ledger import Side
    from .ops import order_capacity
    from .risk import RiskLimits
    if symbol not in books.instruments:
        raise KeyError(f"등록되지 않은 종목이에요: {symbol}")
    inst = books.instruments[symbol]
    _, closes = last_closes(books.ledger)
    ref = limit_price or closes.get(symbol)
    s = Side(side)
    columns = ("symbol", "instrument_name", "side", "qty", "ref_price", "ref_source", "order_value",
               "required_cash", "cash_d0", "projected_cash", "pending_buy_value", "available_cash",
               "max_buy_qty", "held_qty", "pending_sell_qty", "sellable_qty", "ok", "reason", "reason_text")
    if ref is None:
        return Dataset(columns, ((symbol, inst.name, side, qty, None, None, None, None, None, None, None, None,
                                  None, None, None, None, False, "NO_PRICE", REASON_TEXT["NO_PRICE"]),))
    cap = order_capacity(books, day, symbol, ref, closes)
    reason = check(cap, s, qty, halted=halted, limit_price=limit_price, asset_type=inst.asset_type,
                   business_day=books.ledger.calendar.is_business_day(day),
                   account_open=books.status == "OPEN").reason
    value = qty * ref
    required = int(value * (1 + RiskLimits().cash_buffer_rate)) if s is Side.BUY else 0
    return Dataset(columns, ((symbol, inst.name, side, qty, ref, "지정가" if limit_price else "최근 종가", value,
                              required, cap.cash_d0, cap.projected_cash, cap.pending_buy_value, cap.available_cash,
                              cap.max_buy_qty, cap.held_qty, cap.pending_sell_qty, cap.sellable_qty,
                              reason == "", reason, REASON_TEXT.get(reason, reason)),))

# ---------- STP-4400 계좌 목록 · STP-1400 법인별 보유 (조회 전용) ----------

def account_list(conn: psycopg.Connection) -> Dataset:
    """Every account as it is registered: its company, current status, broker, environment and opening day.

    Reference data only: what an account holds is on the account's own screens (STP-3400, STP-1100, STP-1400).
    The account number and the keys are not in the database; `credential_ref` is only
    the name of the environment variables the owner's PC keeps them in."""
    return _q(conn, """
        select t.account_id, t.alias, t.company_id, c.alias as company_alias, t.broker, t.env,
               t.account_type, s.status, s.effective_from as status_from, t.opened_on, t.credential_ref
          from trading_accounts t
          join companies c using (company_id)
          join account_status_v s using (account_id)
         order by t.company_id, t.account_id""")


def account_status_history(conn: psycopg.Connection, account_id: str) -> Dataset:
    """One account's status rows, newest first (a change is a new row, never an update)."""
    return _q(conn, "select id, status, effective_from, reason, created_at from account_status_history"
                    " where account_id = %s order by effective_from desc, id desc", (account_id,))


def company_positions(conn: psycopg.Connection, company_id: str | None = None) -> Dataset:
    """법인별 보유 합산: one company's accounts added up per symbol (`company_positions_v`).

    With `company_id` only that company's rows (the detail under a company row)."""
    return _q(conn, """
        select v.company_id, c.alias as company_alias, v.symbol, i.name as instrument_name, v.qty, v.book_cost,
               (select count(distinct p.account_id) from positions_v p join trading_accounts t using (account_id)
                 where t.company_id = v.company_id and p.symbol = v.symbol) as accounts,
               i.asset_type
          from company_positions_v v
          join companies c using (company_id)
          join instruments i using (symbol)
         where (%(c)s::text is null or v.company_id = %(c)s)
         order by v.company_id, v.book_cost desc""", {"c": company_id})


def company_list(conn: psycopg.Connection) -> Dataset:
    """법인 목록: every company with its ERP company code today, the number of accounts and what they add up to."""
    return _q(conn, """
        select c.company_id, c.alias,
               (select e.erp_company_code from company_erp_codes e
                 where e.company_id = c.company_id and e.effective_from <= current_date
                 order by e.effective_from desc, e.id desc limit 1) as erp_company_code,
               count(t.account_id) as accounts,
               coalesce(sum((select balance from cash_balance_v b where b.account_id = t.account_id)), 0) as cash_d0,
               coalesce(sum((select sum(p.book_cost) from positions_v p where p.account_id = t.account_id)), 0) as book_cost
          from companies c left join trading_accounts t using (company_id)
         group by c.company_id, c.alias
         order by c.company_id""")


def company_accounts(conn: psycopg.Connection, company_id: str) -> Dataset:
    """One company's accounts with their status, cash and holdings at book cost."""
    return _q(conn, """
        select t.account_id, t.alias, s.status,
               (select balance from cash_balance_v b where b.account_id = t.account_id) as cash_d0,
               (select coalesce(sum(p.book_cost), 0) from positions_v p where p.account_id = t.account_id) as book_cost
          from trading_accounts t join account_status_v s using (account_id)
         where t.company_id = %s
         order by t.account_id""", (company_id,))


def company_erp_codes(conn: psycopg.Connection, company_id: str) -> Dataset:
    """One company's ERP company codes, newest first (a change is a new row, never an update)."""
    return _q(conn, "select id, erp_company_code, effective_from, reason, created_at from company_erp_codes"
                    " where company_id = %s order by effective_from desc, id desc", (company_id,))


# ---------- STP-4600 코드 조회 (조회 전용) ----------
# Every coded value in one place: 계정과목 from its own table (the ledger references it),
# everything else from the common code tables.

CHART_GROUP = "accounts_chart"


def code_group_list(conn: psycopg.Connection) -> Dataset:
    """코드 그룹: 계정과목(전용 테이블)과 공통코드 그룹."""
    return _q(conn, """
        select %s as group_id, '계정과목' as name, '전용 테이블' as store, count(*) as codes,
               'journal_lines.account_code' as used_by, 0 as rank
          from accounts_chart
        union all
        select g.group_id, g.name, case when g.fixed then '공통코드' else '공통코드 (운영자 관리)' end,
               count(c.code), g.used_by, 1
          from code_groups g left join common_codes c using (group_id)
         group by g.group_id
         order by rank, group_id""", (CHART_GROUP,))


def code_list(conn: psycopg.Connection, group_id: str) -> Dataset:
    """One group's codes and names. 계정과목 comes from `accounts_chart` with its normal side named."""
    if group_id == CHART_GROUP:
        return _q(conn, """
            select a.code, a.name, '정상잔액 ' || coalesce(n.name, a.normal_side) as description,
                   case when a.in_use then '사용' else '사용 중지' end as in_use
              from accounts_chart a
              left join common_codes n on n.group_id = 'NORMAL_SIDE' and n.code = a.normal_side
             order by a.code""")
    if conn.execute("select 1 from code_groups where group_id = %s", (group_id,)).fetchone() is None:
        raise KeyError(group_id)
    return _q(conn, """
        select code, name, description, case when in_use then '사용' else '사용 안 함' end as in_use
          from common_codes where group_id = %s
         order by sort_order, code""", (group_id,))


# ---------- STP-4100 종목정보 · STP-4300 거래 기준 (기준정보, 조회 전용) ----------
# Reference data as the system really uses it: instruments from the DB, fee rates,
# settlement rules, tick sizes and the holiday calendar from the objects the
# ledger is built with. Nothing here is a second copy that could drift.
# Reference data is what holds whichever account is looked at: no holdings, no balances and
# no closes the books happened to take. Those are the account's (STP-1100, STP-2300, STP-3400).

WEEKDAYS = "월화수목금토일"


def _pct(rate) -> str:
    """0.00015 -> '0.015%' (rates are shown exactly, not rounded to two places)."""
    from decimal import Decimal
    return f"{(Decimal(rate) * 100).normalize():f}%"


def instrument_master(books) -> Dataset:
    """종목정보: 등록 종목과 그 종목을 팔 때의 세율. 계좌와 무관한 값만 있어요."""
    fees = books.ledger.fees
    rows = []
    for sym, inst in sorted(books.instruments.items()):
        tx, rural = fees.sell_tax_rates[(inst.market, inst.asset_type)]
        rows.append((sym, inst.name, inst.market.value, inst.asset_type.value, _pct(tx + rural)))
    return Dataset(("symbol", "instrument_name", "market", "asset_type", "sell_tax"), tuple(rows))


def instrument_rules(books, symbol: str, today: dt.date, inst=None) -> Dataset:
    """한 종목에 적용되는 거래 기준: 수수료, 매도 세금, 결제주기, 호가단위. The same objects the ledger and the
    order check calculate with (STP-4300 shows them for every market and asset type).
    `inst` is an unregistered code taken from the market master; else the registered one.
    KeyError when the symbol is not registered."""
    from .ticks import tick_table
    inst = inst or books.instruments.get(symbol)
    if inst is None:
        raise KeyError(f"등록되지 않은 종목이에요: {symbol}")
    fees, rules = books.ledger.fees, books.ledger.settlement_cycle.rules
    tx, rural = fees.sell_tax_rates[(inst.market, inst.asset_type)]
    cycle = max((r for r in rules if r[0] <= today), key=lambda r: r[0], default=None)
    rows = [("수수료", "위탁수수료율", _pct(fees.commission_rate), "매수·매도 모두, 체결금액 기준"),
            ("수수료", "수수료 절사 단위", f"{fees.commission_unit:,}원", "이 단위 미만은 버려요"),
            ("세금", "증권거래세율", _pct(tx), "매도할 때만"),
            ("세금", "농어촌특별세율", _pct(rural), "매도할 때만"),
            ("결제", "결제주기", f"T+{cycle[1]}" if cycle else None, "오늘 체결하면 적용되는 규칙")]
    for asset, lo, hi, tick in tick_table():
        if asset == inst.asset_type.value:
            band = "모든 가격" if not lo and hi is None else f"{lo:,}원 이상" if hi is None else f"{hi:,}원 미만"
            rows.append(("호가단위", band, f"{tick:,}원", ""))
    return Dataset(("kind", "item", "value", "note"), tuple(rows))


# ---------- STP-3400 계좌정보 ----------
# The account's own state (balances, what is still to settle, the last recon): it is the books' view of one
# account, so it sits with the ledger screens and not with reference data.

def account_summary(books, calendar, today: dt.date) -> Dataset:
    """계좌 요약 (원장 기준). 계좌번호와 키는 DB에 없고 화면에도 나오지 않아요."""
    from .ops import clean_streak
    led = books.ledger
    pending = led.pending_settlements()
    close_day, closes = last_closes(led)
    held = led.positions()
    market_value = sum(p.qty * closes[s] for s, p in held.items() if s in closes) if closes else None
    first = led.events[0] if led.events else None
    opened = getattr(first, "date", None) or getattr(first, "trade_date", None)
    recon = books.conn.execute(
        "select b.date, b.taken_at, count(*) filter (where r.status = 'BREAK' and not exists"
        " (select 1 from recon_results x where x.supersedes_id = r.id))"
        " from broker_snapshots b join recon_results r on r.run_id = b.snapshot_id"
        " where b.kind = 'DAILY_RECON' and b.account_id = %s"
        " group by b.snapshot_id order by b.snapshot_id desc limit 1", (books.account_id,)).fetchone()
    last_nav = max((r.date for r in led.nav_history()), default=None)
    alias = books.conn.execute("select alias from trading_accounts where account_id = %s",
                               (books.account_id,)).fetchone()[0]
    return Dataset(
        ("account_id", "alias", "status", "opened", "cash_d0", "receivables", "payables", "projected_cash", "unsettled", "holdings",
         "market_value", "valued_on", "last_recon_date", "last_recon_at", "open_breaks", "clean_streak"),
        ((books.account_id, alias, books.status, opened, led.cash_balance(),
          sum(s.signed for s in pending if s.signed > 0),
          -sum(s.signed for s in pending if s.signed < 0), led.projected_cash(), len(pending), len(held),
          market_value, close_day, recon[0] if recon else None, recon[1] if recon else None,
          recon[2] if recon else None, clean_streak(books.conn, calendar, last_nav, books.account_id) if last_nav else 0),))


def cash_ladder(books, calendar, today: dt.date) -> Dataset:
    """예수금 D+n: 오늘 예수금에서 영업일마다 들어오고 나가는 결제대금을 차례로 더한 금액.

    증권사 HTS의 D+1·D+2 예수금과 같은 개념이라 D+2까지는 결제가 없어도 행을 보여줘요.
    결제일이 이미 지났는데 아직 결제되지 않은 건은 D+0에 넣어요.
    """
    buckets: dict[int, list[int]] = {}
    for s in books.ledger.pending_settlements():
        n = calendar.business_days_between(today, s.settle_date) if s.settle_date > today else 0
        io = buckets.setdefault(n, [0, 0])
        io[0 if s.signed > 0 else 1] += abs(s.signed)
    cash, rows = books.ledger.cash_balance(), []
    for n in range(max([2, *buckets]) + 1):
        cash_in, cash_out = buckets.get(n, (0, 0))
        cash += cash_in - cash_out
        rows.append((f"D+{n}", calendar.add_business_days(today, n), cash_in, cash_out, cash))
    return Dataset(("day_label", "date", "cash_in", "cash_out", "cash_after"), tuple(rows))


def pending_settlements(books) -> Dataset:
    """결제 예정 체결: 체결일, 결제일, 종목, 매매구분, 수량, 결제금액 (+받을 돈 / −낼 돈)."""
    from .ledger import ExecutionEvent
    execs = {e.exec_id: e for e in books.ledger.events if isinstance(e, ExecutionEvent)}
    rows = []
    for s in books.ledger.pending_settlements():
        e = execs.get(s.exec_id)
        sym = e.symbol if e else None
        inst = books.instruments.get(sym)
        rows.append((s.exec_id, s.trade_date, s.settle_date, sym, inst.name if inst else None, s.side.value,
                     e.qty if e else None, s.signed, inst.asset_type.value if inst else None))
    return Dataset(("exec_id", "trade_date", "settle_date", "symbol", "instrument_name", "side", "qty", "amount",
                    "asset_type"), tuple(rows))


def fee_rules(fees) -> Dataset:
    """수수료·세금: 시장·자산유형별 매도 세율과 위탁수수료율, 절사 단위 (R-20~22)."""
    rows = []
    for (market, asset), (tx, rural) in sorted(fees.sell_tax_rates.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        rows.append((market.value, asset.value, _pct(fees.commission_rate), fees.commission_unit, _pct(tx),
                     _pct(rural), _pct(tx + rural)))
    return Dataset(("market", "asset_type", "commission_rate", "commission_unit", "tx_tax_rate", "rural_tax_rate",
                    "sell_tax"), tuple(rows))


def settlement_rules(ledger: Ledger, today: dt.date) -> Dataset:
    """결제주기 규칙: 적용시작일별 T+n. 체결일에 적용 중인 규칙이 결제일을 정해요 (R-11)."""
    rules = ledger.settlement_cycle.rules
    in_force = max((d for d, _ in rules if d <= today), default=None)
    return Dataset(("effective_from", "cycle", "status"),
                   tuple((None if d == dt.date.min else d, f"T+{n}",
                          "적용 중" if d == in_force else ("예정" if d > today else "지난 규칙"))
                         for d, n in rules))


def tick_rules() -> Dataset:
    """호가가격단위 (KOSPI·KOSDAQ 공통, 2023-01-25부터)."""
    from .ticks import tick_table
    return Dataset(("asset_type", "price_from", "price_below", "tick"), tuple(tick_table()))


def holiday_calendar(calendar, today: dt.date) -> Dataset:
    """휴장일: 등록된 평일 휴장일 (주말은 따로 등록하지 않아요). 등록된 연도 밖의 날짜는 계산을 멈춰요 (R-10)."""
    return Dataset(("date", "weekday", "status"),
                   tuple((d, WEEKDAYS[d.weekday()], "지남" if d < today else "예정") for d in calendar.holidays))


def risk_rules() -> Dataset:
    """주문 검증 기준 (R-36·37): 주문가능 판단과 주문 검증이 같이 쓰는 값."""
    from .risk import RiskLimits
    r = RiskLimits()
    return Dataset(("max_order_value", "cash_buffer_rate", "short_sell"),
                   ((r.max_order_value, _pct(r.cash_buffer_rate), "금지 (매도가능수량까지만)"),))


# Transaction id -> service. Ids follow the screen numbers (화면번호 + R + 일련번호).
TRANSACTIONS = {
    "STP1100R01": holdings,
    "STP1100R02": cash_summary,
    "STP1300R01": capacity_by_symbol,
    "STP1300R02": order_check,
    "STP3100R01": ledger_events,
    "STP3100R02": event_trace,
    "STP3200R01": journal_events,
    "STP3200R02": event_journal,
    "STP3300R01": trial_balance,
    "STP3400R01": account_summary,
    "STP3400R02": cash_ladder,
    "STP3400R03": pending_settlements,
    "STP2100R01": recon_history,
    "STP2110R01": integrity_checks,
    "STP2110R02": integrity_compared,
    "STP1200R01": order_blotter,
    "STP2200R01": nav_series,
    "STP2210R01": nav_change_days,
    "STP2210R02": nav_change_bridge,
    "STP2300R01": price_trend_symbols,
    "STP2300R02": price_trend_rows,
    "STP2400R01": close_days,
    "STP2400R02": close_steps,
    "STP6100R01": return_months,
    "STP6100R02": return_month_days,
    "STP4100R04": instrument_rules,
    "STP4100R05": instrument_master,
    "STP4300R01": fee_rules,
    "STP4300R02": settlement_rules,
    "STP4300R03": tick_rules,
    "STP4300R04": holiday_calendar,
    "STP4300R05": risk_rules,
}


# ---------- STP-4100 종목정보(시장 전 종목) · STP-4101 마스터 적재 이력 (ERD v2.7, 조회 전용) ----------
# One list for every code: the market master as of a day, and the registered codes the books trade.
# The tables stay apart (instruments is the books' fixed reference, the master moves with every load);
# the screen shows them side by side and says where they differ (R-95).

MASTER_LIMIT = 500   # rows a market list sends at once; the conditions narrow the rest
WARNINGS = {"01": "투자주의", "02": "투자경고", "03": "투자위험"}
MASTER_STATES = ("거래정지", "정리매매", "관리종목", "시장경고", "상장폐지")
# How a history row reads, item by item: (item, its column(s), how the value is written)
_MASTER_ITEMS = (
    ("상장 여부", "listed", lambda v: "상장" if v else "상장폐지 (파일에서 빠짐)"),
    ("종목명", "name", str),
    ("표준코드", "isin", str),
    ("시장", "market", lambda v: {"KOSPI": "유가증권시장", "KOSDAQ": "코스닥"}.get(v, v)),
    ("증권그룹 구분", "sec_group", None),   # named from 공통코드 SECURITY_GROUP
    ("매매수량단위", "lot", lambda v: f"{v:,}주"),
    ("액면가", "par_value", lambda v: f"{v:,}원"),
    ("상장일", "listed_on", lambda v: v.isoformat() if v else "-"),
    ("상장주수", "listed_shares_k", lambda v: f"{v:,}천 주"),
    ("우선주 구분", "preferred", lambda v: "보통주" if v == "0" else f"우선주 ({v})"),
    ("스팩", "spac", lambda v: "예" if v else "아니오"),
    ("거래정지", "halted", lambda v: "예" if v else "아니오"),
    ("정리매매", "liquidation", lambda v: "예" if v else "아니오"),
    ("관리종목", "managed", lambda v: "예" if v else "아니오"),
    ("시장경고", "warning", lambda v: f"{WARNINGS[v]} ({v})" if v in WARNINGS else f"없음 ({v})"),
)

_MASTER_STATE_SQL = """case when not m.listed then '상장폐지' when m.halted then '거래정지'
            when m.liquidation then '정리매매' when m.managed then '관리종목'
            when m.warning = '03' then '투자위험' when m.warning = '02' then '투자경고'
            when m.warning = '01' then '투자주의' end"""


def _as_of(conn: psycopg.Connection, day: dt.date | None) -> dt.date | None:
    """The base date the market screens show: the day asked for, else the latest load."""
    if day is not None:
        return day
    return conn.execute("select max(base_date) from master_loads").fetchone()[0]


GROUP_OF = {"STOCK": "ST", "ETF": "EF"}   # a registered asset type and the 증권그룹 the master gives it
_MATCH_SQL = """case when n.symbol is null then 'MISSING'
            when not n.listed then 'DELISTED'
            when n.market <> i.market or n.sec_group <> case i.asset_type when 'STOCK' then 'ST' when 'ETF' then 'EF' end
                 then 'DIFFERENT'
            when replace(n.name, ' ', '') <> replace(i.name, ' ', '') then 'RENAMED'
            else 'MATCH' end"""


def market_instruments(conn: psycopg.Connection, day: dt.date | None = None, market: str | None = None,
                       sec_group: str | None = None, state: str | None = None, keyword: str | None = None,
                       registered_only: bool = False) -> tuple[Dataset, dict]:
    """STP-4100: every code of the market as the history says it was on `day` (default: the latest load), and
    every registered code, with its 대조 결과 against that day (R-95). A registered code the master does not
    hold is listed from instruments (MISSING). Codes that had left the file are shown when registered, or when
    `state` asks for 상장폐지."""
    day = _as_of(conn, day)
    params = {"day": day}
    filters = []
    if market:
        filters.append("market = %(market)s"); params["market"] = market
    if sec_group:
        filters.append("sec_group = %(group)s"); params["group"] = sec_group
    if state == "시장경고":
        filters.append("state in ('투자주의', '투자경고', '투자위험')")
    elif state:
        filters.append("state = %(state)s"); params["state"] = state
    else:
        filters.append("(listed or registered is not null)")
    if keyword:
        filters.append("(symbol ilike %(kw)s or replace(name, ' ', '') ilike %(kw_name)s)")
        params["kw"], params["kw_name"] = keyword.strip() + "%", "%" + keyword.replace(" ", "") + "%"
    if registered_only:
        filters.append("registered is not null")
    sql = f"""
        with now as (select distinct on (m.symbol) m.*, {_MASTER_STATE_SQL} as state
                       from instrument_master m where m.valid_from <= %(day)s
                      order by m.symbol, m.valid_from desc),
             shown as (select coalesce(n.symbol, i.symbol) as symbol, coalesce(n.name, i.name) as name,
                              coalesce(n.market, i.market) as market,
                              coalesce(n.sec_group, case i.asset_type when 'STOCK' then 'ST' when 'ETF' then 'EF' end)
                                  as sec_group,
                              n.state, coalesce(n.listed, true) as listed, n.valid_from,
                              case when i.symbol is not null then '등록' end as registered,
                              case when i.symbol is not null then {_MATCH_SQL} end as master_match,
                              case when i.symbol is not null then 'STP-1300' end as opens
                         from now n full join instruments i using (symbol))
        select symbol, name, market, sec_group, state, registered, master_match, opens, valid_from,
               %(day)s::date as as_of
          from shown
         where {' and '.join(filters)}
         order by registered is null, market, symbol"""
    total = conn.execute(f"select count(*) from ({sql}) x", params).fetchone()[0]
    ds = _q(conn, sql + f" limit {MASTER_LIMIT}", params)
    shown = f"{total:,}건 중 {len(ds):,}건" if total > len(ds) else f"{total:,}건"
    return ds, {"base_date": day.isoformat() if day else "적재 전", "shown": shown}


def master_instrument(conn: psycopg.Connection, symbol: str, day: dt.date | None = None):
    """The code as the market master has it on `day`, as an Instrument the fee and tick rules take; None when the
    master does not hold it or it is not a 주권 or an ETF (the only kinds the books trade)."""
    from .instruments import AssetType, Instrument, Market
    row = conn.execute("select name, market, sec_group from instrument_master where symbol = %s and valid_from <= %s"
                       " order by valid_from desc limit 1", (symbol, _as_of(conn, day))).fetchone()
    asset = {v: k for k, v in GROUP_OF.items()}.get(row[2]) if row else None
    return Instrument(symbol, row[0], Market(row[1]), AssetType(asset)) if asset else None


def _master_rows(conn: psycopg.Connection, symbol: str, day: dt.date | None) -> list[dict]:
    cols = ", ".join(c for _, c, _ in _MASTER_ITEMS)
    cur = conn.execute(f"select valid_from, load_id, {cols} from instrument_master"
                       " where symbol = %s and (%s::date is null or valid_from <= %s) order by valid_from",
                       (symbol, day, day))
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def _item_text(group_names: dict, col: str, show, v) -> str:
    if show is None:   # a code: written with its 공통코드 name
        return f"{group_names.get(v, v)} ({v})"
    return show(v)


def market_instrument(conn: psycopg.Connection, symbol: str, day: dt.date | None = None) -> Dataset:
    """STP-4100: one code's attributes on `day`, each with the day it took that value. KeyError if unknown."""
    from .codes import names
    rows = _master_rows(conn, symbol, _as_of(conn, day))
    if not rows:
        if conn.execute("select 1 from instruments where symbol = %s", (symbol,)).fetchone():
            return Dataset(("item", "value", "since"), ())   # registered, not (yet) in the master: MISSING
        raise KeyError(f"그날 마스터에 없는 종목이에요: {symbol}")
    groups = names(conn, "SECURITY_GROUP")
    out = []
    for item, col, show in _MASTER_ITEMS:
        since = rows[0]["valid_from"]
        for prev, row in zip(rows, rows[1:]):
            if row[col] != prev[col]:
                since = row["valid_from"]
        out.append((item, _item_text(groups, col, show, rows[-1][col]), since))
    return Dataset(("item", "value", "since"), tuple(out))


# 종목정보 양식(STP-4100): 주요 항목을 먼저, 상태 표시 다섯 개(상장 여부·거래정지·정리매매·관리종목·시장경고)는 한 줄로
FORM_FIRST = ("종목명", "시장", "증권그룹 구분", "거래 상태", "상장일", "표준코드", "매매수량단위", "액면가", "상장주수",
              "우선주 구분", "스팩")
_STATE_ITEMS = ("상장 여부", "거래정지", "정리매매", "관리종목", "시장경고")


def market_instrument_form(conn: psycopg.Connection, symbol: str, day: dt.date | None = None) -> Dataset:
    """STP-4100: the attributes of `market_instrument` as a two-column form, two attributes a row, the main ones
    first. Each value has the day it took that value under it; `load_id` is the load that last changed the code
    (the row opens that load in STP-4101). KeyError if unknown."""
    attrs = {r[0]: (r[1], r[2]) for r in market_instrument(conn, symbol, day).rows}
    rows = _master_rows(conn, symbol, _as_of(conn, day))
    if not attrs:
        return Dataset(("item_l", "value_l", "since_l", "item_r", "value_r", "since_r", "load_id"), ())
    flags = [i for i in _STATE_ITEMS[1:] if attrs[i][0] not in ("아니오",) and not attrs[i][0].startswith("없음")]
    listed = attrs["상장 여부"][0] == "상장"
    state = ", ".join(([] if listed else ["상장폐지"]) + [attrs[i][0].split(" (")[0] if i == "시장경고" else i
                                                          for i in flags]) or "정상"
    attrs["거래 상태"] = (state, max(attrs[i][1] for i in _STATE_ITEMS))
    items = [(i, *attrs[i]) for i in FORM_FIRST]
    last_load = rows[-1]["load_id"]
    pairs = [items[k:k + 2] for k in range(0, len(items), 2)]
    return Dataset(("item_l", "value_l", "since_l", "item_r", "value_r", "since_r", "load_id"),
                   tuple((*a, *(b[0] if b else (None, None, None)), last_load) for a, *b in pairs))


def market_instrument_history(conn: psycopg.Connection, symbol: str, day: dt.date | None = None) -> Dataset:
    """STP-4100: what changed in one code and when, newest first. The first row of a code is its first load."""
    from .codes import names
    rows = _master_rows(conn, symbol, _as_of(conn, day))
    groups = names(conn, "SECURITY_GROUP")
    out = [(rows[0]["valid_from"], "(첫 적재)", None, "전 항목", rows[0]["load_id"])] if rows else []
    for prev, row in zip(rows, rows[1:]):
        for item, col, show in _MASTER_ITEMS:
            if row[col] != prev[col]:
                out.append((row["valid_from"], item, _item_text(groups, col, show, prev[col]),
                            _item_text(groups, col, show, row[col]), row["load_id"]))
    out.sort(key=lambda r: r[0], reverse=True)
    return Dataset(("valid_from", "item", "before", "after", "load_id"), tuple(out))


def master_loads(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                 load_id: int | None = None) -> Dataset:
    """STP-4101: one row per `master load`, newest first."""
    return _q(conn, """
        select load_id, base_date, rows_total, rows_new, rows_changed, rows_delisted,
               left(kospi_sha256, 6) || '…/' || left(kosdaq_sha256, 6) || '…' as files, fetched_at
          from master_loads
         where (%(f)s::date is null or base_date >= %(f)s) and (%(t)s::date is null or base_date <= %(t)s)
           and (%(id)s::bigint is null or load_id = %(id)s)
         order by base_date desc""", {"f": date_from, "t": date_to, "id": load_id})


def master_load_changes(conn: psycopg.Connection, load_id: int, kind: str | None = None) -> Dataset:
    """STP-4101: the codes one load wrote, item by item: 신규 (first row), 변경, 빠짐 (listed false)."""
    from .codes import names
    if conn.execute("select 1 from master_loads where load_id = %s", (load_id,)).fetchone() is None:
        raise KeyError(f"없는 적재 번호예요: ML-{load_id}")
    groups = names(conn, "SECURITY_GROUP")
    cols = ", ".join(c for _, c, _ in _MASTER_ITEMS)
    cur = conn.execute(f"""
        select m.symbol, m.valid_from, {', '.join('m.' + c for _, c, _ in _MASTER_ITEMS)},
               p.master_id is not null as had_before, {', '.join('p.' + c + ' as p_' + c for _, c, _ in _MASTER_ITEMS)}
          from instrument_master m
          left join lateral (select master_id, {cols} from instrument_master x
                              where x.symbol = m.symbol and x.valid_from < m.valid_from
                              order by x.valid_from desc limit 1) p on true
         where m.load_id = %s order by m.symbol""", (load_id,))
    names_ = [c.name for c in cur.description]
    out = []
    for r in (dict(zip(names_, t)) for t in cur.fetchall()):
        if not r["had_before"]:
            change = "신규"
            items = [("(상장)", None, f"{r['valid_from']} 처음 보임")]
        elif not r["listed"]:
            change = "빠짐"
            items = [("상장 여부", "상장", "상장폐지 (파일에서 빠짐)")]
        else:
            change = "변경"
            items = [(item, _item_text(groups, col, show, r["p_" + col]), _item_text(groups, col, show, r[col]))
                     for item, col, show in _MASTER_ITEMS if r[col] != r["p_" + col]]
        if kind and kind != change:
            continue
        out += [(change, r["symbol"], r["name"], r["sec_group"], item, before, after) for item, before, after in items]
    return Dataset(("change", "symbol", "name", "sec_group", "item", "before", "after"), tuple(out))
