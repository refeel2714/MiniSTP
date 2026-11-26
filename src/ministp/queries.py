"""조회 서비스 계층: the read transactions the STP-UI screens will call.

Each service reads the database only (never the broker, never a write) and
returns a `Dataset`: column names exactly as in the DB, so the screen platform
can take each column's Korean name from the schema COMMENTs. Services are
registered under a transaction id (`TRANSACTIONS`), one id per read the screens
need (화면 기획서: 조회 트랜잭션 하나 = 조회 서비스 하나).
"""
import datetime as dt
import json
from dataclasses import dataclass

import psycopg

from .journal_templates import KIND_NAMES as JOURNAL_KIND_NAMES  # 전표 유형 이름: 분개 모형과 같은 표
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
                  event_type: str | None = None, symbol: str | None = None, seq: int | None = None) -> Dataset:
    """이벤트 원장 목록, newest first. Dates are business dates, not recording times.
    `seq` narrows the list to one event: how another screen opens this one on the event it had picked."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               e.recorded_at, e.payload
          from ledger_events e
         where (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
           and (%(seq)s::bigint is null or e.seq = %(seq)s)
         order by e.seq desc""", {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol, "seq": seq})


def journal_events(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                   event_type: str | None = None, symbol: str | None = None, seq: int | None = None) -> Dataset:
    """The events that wrote a journal, newest first, each with how much it wrote: 전표 수, 분개 줄 수, 차변 합계.
    An event that books nothing (a NAV strike) is not listed: this is the list a journal is picked from."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               count(distinct j.entry_id)::int as entries, count(l.line_id)::int as lines,
               sum(l.debit)::bigint as amount
          from ledger_events e
          join journal_entries j on j.event_seq = e.seq
          join journal_lines l using (entry_id)
         where (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
           and (%(seq)s::bigint is null or e.seq = %(seq)s)
         group by e.seq
         order by e.seq desc""", {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol, "seq": seq})


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
        select j.entry_id, j.date, j.kind, coalesce(%s::jsonb ->> j.kind, j.kind) as kind_name,
               j.source_id, j.reverses_entry_id,
               l.line_id, l.account_code, a.name as account_name, l.debit, l.credit
          from journal_entries j join journal_lines l using (entry_id)
          join accounts_chart a on a.code = l.account_code
         where j.event_seq = %s order by j.entry_id, l.line_id""", (json.dumps(JOURNAL_KIND_NAMES), seq))


def event_journal(conn: psycopg.Connection, seq: int) -> tuple[Dataset, bool]:
    """이벤트 하나가 만든 분개 전부 (전표와 줄), and whether every 전표 balances."""
    if not conn.execute("select 1 from ledger_events where seq = %s", (seq,)).fetchone():
        raise KeyError(f"no ledger event {seq}")
    journal = _journal_of(conn, seq)
    return journal, _balanced(journal)


def event_trace(conn: psycopg.Connection, seq: int) -> EventTrace:
    """이벤트 하나 → 분개 → 보유·예수금 변동 (원장 추적)."""
    event = _q(conn, f"select e.seq, e.event_type, {_EVENT_DATE} as date, e.recorded_at, e.payload"
                     " from ledger_events e where e.seq = %s", (seq,))
    if not event.rows:
        raise KeyError(f"no ledger event {seq}")
    journal = _journal_of(conn, seq)
    positions = _q(conn, "select id, trade_date, symbol, exec_id, qty_delta, cost_delta, realized_pnl"
                         " from position_ledger where event_seq = %s order by id", (seq,))
    cash = _q(conn, "select id, date, reason, amount, ref_id from cash_ledger where event_seq = %s order by id",
              (seq,))
    return EventTrace(event, journal, positions, cash)


ACCOUNT_CLASSES = {"1": "자산", "2": "부채", "3": "자본", "4": "수익", "5": "비용"}


def trial_balance(conn: psycopg.Connection, day: dt.date,
                  account_id: str = DEFAULT_ACCOUNT) -> tuple[Dataset, dict]:
    """합계잔액시산표 모양: 차변(당일잔액·당일증감·전일잔액) | 계정 | 대변(전일잔액·당일증감·당일잔액).

    잔액은 계정의 정상 쪽(자산·비용은 차변, 부채·자본·수익은 대변)에 적고, 반대로 쌓인 잔액(평가조정 같은 차감 계정)은
    그 쪽에 음수로 적어요. 당일증감은 그날의 차변 합계와 대변 합계를 양쪽에 그대로 적어요. 계정 분류(코드 첫 자리)마다
    합계 줄이 먼저 오고, 맨 아래 총합계 줄의 차변과 대변이 같아야 해요. 계좌 하나(기본 계좌)의 장부만 더해요."""
    rows = conn.execute("""
        select a.code, a.name, a.normal_side,
               coalesce(sum(l.debit - l.credit) filter (where e.date < %(d)s), 0) as prev,
               coalesce(sum(l.debit) filter (where e.date = %(d)s), 0) as dr,
               coalesce(sum(l.credit) filter (where e.date = %(d)s), 0) as cr
          from accounts_chart a
          left join journal_lines l on l.account_code = a.code
          left join journal_entries e on e.entry_id = l.entry_id and e.date <= %(d)s and e.account_id = %(a)s
         group by a.code, a.name, a.normal_side order by a.code""", {"d": day, "a": account_id}).fetchall()

    def line(code, name, side, prev, dr, cr, level):
        now = prev + dr - cr
        d_now, d_prev = (now, prev) if side == "D" else (None, None)
        c_prev, c_now = (-prev, -now) if side == "C" else (None, None)
        return (d_now, dr or None, d_prev, code, name, c_prev, cr or None, c_now, level)

    out, totals = [], {"D": [0, 0, 0, 0], "C": [0, 0, 0, 0]}
    for cls, cls_name in ACCOUNT_CLASSES.items():
        mine = [r for r in rows if r[0].startswith(cls) and (r[3] or r[4] or r[5])]
        if not mine:
            continue
        side = mine[0][2]
        prev, dr, cr = (sum(r[i] for r in mine) for i in (3, 4, 5))
        out.append(line(cls + "000", f"{cls_name} 합계", side, prev, dr, cr, "class"))
        out.extend(line(c, n, sd, pv, d, cr_, "account") for c, n, sd, pv, d, cr_ in mine)
        t = totals[side]
        for i, v in enumerate((prev + dr - cr, dr, prev, cr)):
            t[i] += v
    d, c = totals["D"], totals["C"]
    out.append((d[0], (d[1] + c[1]) or None, d[2], "", "총합계", -c[2], (d[3] + c[3]) or None, -c[0], "total"))
    debit_total, credit_total = d[0], -c[0]
    return (Dataset(("d_now", "d_change", "d_prev", "code", "name", "c_prev", "c_change", "c_now", "level"),
                    tuple(out)),
            {"day": day.isoformat(), "balanced": "예" if debit_total == credit_total else "아니요"})


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
                  kind: str = "DAILY_RECON", only_breaks: bool = False) -> Dataset:
    """대사 결과 with each break's latest owner judgement next to it (RESOLVED rows are not listed alone).

    Within a run the rows that need someone come first: a break nobody has judged, then judged breaks, then the
    matches, so a break of one holding out of a hundred is not lost in the middle. `key_name` names the
    instrument when the key is a symbol."""
    return _q(conn, """
        select r.id, r.date, s.kind, s.taken_at, r.item,
               coalesce(%(items)s::jsonb ->> r.item, r.item) as item_name,
               case when i.symbol is not null then '종목' when r.key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then '일자'
                    when coalesce(r.key, '') = '' then '계좌 전체' else '기타' end as key_kind,
               r.key, i.name as key_name,
               r.internal_value, r.broker_value, r.diff,
               r.status, case when r.status = 'MATCH' then '일치' when j.id is null then '불일치'
                              else '불일치(판단함)' end as status_name,
               r.cause_code, %(causes)s::jsonb ->> r.cause_code as cause_name,
               j.id as resolved_by, j.cause_code as resolved_cause, %(causes)s::jsonb ->> j.cause_code as resolved_cause_name,
               j.note as resolved_note,
               j.created_at as resolved_at
          from recon_results r
          join broker_snapshots s on s.snapshot_id = r.run_id
          left join instruments i on i.symbol = r.key
          left join lateral (select x.id, x.cause_code, x.note, x.created_at from recon_results x
                              where x.supersedes_id = r.id order by x.id desc limit 1) j on true
         where r.status <> 'RESOLVED'
           and s.kind = %(kind)s
           and (%(f)s::date is null or r.date >= %(f)s)
           and (%(t)s::date is null or r.date <= %(t)s)
           and (not %(ob)s or r.status = 'BREAK')
         order by r.date desc, r.run_id desc, (r.status = 'BREAK' and j.id is null) desc, r.status = 'BREAK' desc, r.id""",
              {"kind": kind, "f": date_from, "t": date_to, "ob": only_breaks,
               "items": json.dumps(RECON_ITEM_NAMES), "causes": json.dumps(RECON_CAUSE_NAMES)})


# 대사 항목과 원인의 한글 이름 (화면에 영어 코드 대신)
RECON_ITEM_NAMES = {"CASH_D0": "예수금 D+0", "CASH_PROJECTED": "예수금 D+2", "EXEC_QTY": "체결 수량",
                    "EXEC_COUNT": "체결 건수", "POSITION_QTY": "보유 수량", "BOOK_COST": "장부금액"}
RECON_CAUSE_NAMES = {"MISSING_EXEC": "체결 누락", "FEE_DIFF": "수수료 차이", "TAX_DIFF": "세금 차이",
                     "ROUNDING": "끝수 차이", "CORP_ACTION": "권리 처리", "TIMING": "시점 차이", "UNKNOWN": "원인 모름"}


def _open_day(calendar, day: dt.date) -> bool | None:
    """Whether the exchange is open that day; None when the holiday calendar does not cover it yet."""
    try:
        return calendar.is_business_day(day)
    except LookupError:
        return None


def recon_calendar(conn: psycopg.Connection, calendar, today: dt.date, date_from: dt.date | None = None,
                   date_to: dt.date | None = None, kind: str = "DAILY_RECON") -> Dataset:
    """날짜별 대사 요약, newest first: one row a day, judged by that day's latest run (ops.recon_days,
    the same reading the gate count uses).

    일치 = no break · 불일치(판단함) = breaks, every one judged (the gate still counts the day) ·
    불일치 = a break nobody has judged. A weekday without a run is 휴장 on an exchange holiday,
    대사 전 if it is today, 대사 없음 if it has passed (일 마감 대사 only: a missed day ends the
    clean streak; an intraday recon is not owed every day). Days before the first run and after
    today have no row.
    """
    from .ops import recon_days
    date_to = min(date_to or today, today)
    if date_from is None:   # this month and the one before: enough to see the 20-day gate
        date_from = (date_to.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    days = recon_days(conn, kind, date_from, date_to)
    first = conn.execute("select min(r.date) from recon_results r join broker_snapshots b"
                         " on b.snapshot_id = r.run_id where b.kind = %s", (kind,)).fetchone()[0]
    daily = kind == "DAILY_RECON"
    rows, d = [], date_to
    while d >= date_from:
        day = days.get(d)
        if day is not None:
            state = "일치" if not day.breaks else "불일치(판단함)" if day.clean else "불일치"
            rows.append((d, state, kind, day.items, day.breaks, day.open_breaks))
        elif d.weekday() < 5:
            if _open_day(calendar, d) is False:
                rows.append((d, "휴장", kind, None, None, None))
            elif daily and first is not None and d >= first:
                rows.append((d, "대사 전" if d == today else "대사 없음", kind, None, None, None))
        d -= dt.timedelta(days=1)
    return Dataset(("date", "state", "kind", "items", "breaks", "open_breaks"), tuple(rows))


# ---------- STP-1200 주문·체결 블로터 ----------

def order_blotter(conn: psycopg.Connection, day: dt.date) -> Dataset:
    """그날 주문: 전송(주문 의도 기록) → 접수 → 반영된 체결, 체결통보 반영 지연."""
    return _q(conn, """
        select o.client_id, o.trade_date, o.created_at, o.symbol, i.name as instrument_name, i.asset_type,
               o.side, o.qty, o.limit_price, a.outcome, a.order_no, a.detail,
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


# 주문 진행 단계: where an order stands on its way to cash. Each step is done · now · stop · wait.
ORDER_STEPS = ("전송", "접수", "체결", "결제")   # 전송: 보내기 직전에 주문을 기록하고 보냄 (oms_orders, 주문 의도)


def order_progress(outcome: str | None, qty: int, filled: int, settled: int, day_over: bool,
                   overdue: bool) -> tuple[tuple[str, ...], str]:
    """The state of each of ORDER_STEPS and one phrase for the whole order.

    `day_over`: the order's trade date has passed, so whatever was not filled never will be
    (orders are day orders). `overdue`: a fill is past its settlement date and still unsettled.
    """
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
        steps, stage = order_progress(r["outcome"], r["qty"], r["filled_qty"], settled, r["trade_date"] < today, overdue)
        rows.append((*r.values(), settled, list(steps), stage))
    return Dataset((*base.columns, "settled_qty", "progress", "stage"), tuple(rows))


@dataclass(frozen=True)
class OrderDetail:
    order: Dataset   # what was sent and what came back (one row)
    fills: Dataset   # each execution of the order with its settlement


def order_detail(conn: psycopg.Connection, ledger: Ledger, client_id: str, today: dt.date) -> OrderDetail:
    """주문 하나의 접수 내용과 체결별 결제: 체결일 → 결제 예정일 → 결제일."""
    order = _q(conn, """
        select o.client_id, o.trade_date, o.limit_price, a.outcome, a.order_no, a.created_at as acked_at, a.detail,
               n.notices, n.max_booking_ms
          from oms_orders o
          left join oms_order_acks a using (client_id)
          left join lateral (select count(*) as notices, max((s.payload->>'booking_ms')::int) as max_booking_ms
                               from broker_snapshots s
                              where s.kind = 'FILL_NOTICE' and s.date = o.trade_date
                                and s.payload->'notice'->>'order_no' = a.order_no) n on true
         where o.client_id = %s""", (client_id,))
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
    says why it moved (투자원금, 수익, 비용 accounts). The two are worked out separately, so their
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
                if ln.account.code[0] in MOVE_KIND:
                    effect[ln.account] = effect.get(ln.account, 0) + ln.credit - ln.debit
        moves = tuple((MOVE_KIND[a.code[0]], a.code, a.value[1], v)
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
    outside = cb.Outside(orders, recon_days(conn), len(checks), sum(not c.ok for c in checks))
    try:
        ledger = replay(conn, calendar, fees, instruments, account_id)
    except Exception:       # events the rules reject: no day can be read, the one thing to show is that
        return {today: [cb.integrity_step(outside)]}
    return cb.board(ledger, calendar, today, outside)


def close_days(conn: psycopg.Connection, calendar, fees, today: dt.date, date_from: dt.date | None = None,
               date_to: dt.date | None = None, account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """영업일 목록, newest first: how many of the day's steps are finished and whether anything needs a decision."""
    from . import closeboard as cb
    rows = [(day, cb.finished(steps), len(steps), cb.needing(steps), cb.day_state(steps))
            for day, steps in _close_board(conn, calendar, fees, today, account_id).items()
            if (date_from is None or day >= date_from) and (date_to is None or day <= date_to)]
    return Dataset(("date", "done", "steps", "attention", "state"), tuple(rows))


def close_steps(conn: psycopg.Connection, calendar, fees, today: dt.date, date: dt.date,
                account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One day's steps in the order `daily` does them, each with its state, what it found and the screen
    that shows the detail."""
    days = _close_board(conn, calendar, fees, today, account_id)
    if date not in days:
        raise KeyError(f"{date}은 영업일이 아니거나 원장을 시작하기 전이에요")
    return Dataset(("no", "step", "state", "detail", "screen", "date"),
                   tuple((s.no, s.name, s.state, s.detail, s.screen, date) for s in days[date]))


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


ASSET_TYPE_NAMES = {"STOCK": "주식", "ETF": "ETF"}


def holdings_mix(view: Dataset) -> Dataset:
    """자산 유형별 보유: 종목 수, 평가금액, 비중, 평가손익. 평가 가격이 없는 종목은 장부금액으로 셈에 넣지 않고 수만 세요."""
    rows = view.records()
    total = sum(r["market_value"] or 0 for r in rows)
    out = []
    for t in sorted({r["asset_type"] for r in rows}, key=lambda t: (t != "STOCK", t)):
        mine = [r for r in rows if r["asset_type"] == t]
        value = sum(r["market_value"] or 0 for r in mine)
        out.append((ASSET_TYPE_NAMES.get(t, t), len(mine), value, round(value / total, 4) if total else None,
                    sum(r["unrealized_pnl"] or 0 for r in mine)))
    return Dataset(("asset_type_name", "count", "market_value", "weight", "unrealized_pnl"), tuple(out))


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


def instrument_rules(books, symbol: str, today: dt.date) -> Dataset:
    """한 종목에 적용되는 거래 기준: 수수료, 매도 세금, 결제주기, 호가단위. The same objects the ledger and the
    order check calculate with (STP-4300 shows them for every market and asset type).
    KeyError when the symbol is not registered."""
    from .ticks import tick_table
    inst = books.instruments.get(symbol)
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


# ---------- STP-3400 예수금·결제 현황 ----------
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
        " where b.kind = 'DAILY_RECON' group by b.snapshot_id order by b.snapshot_id desc limit 1").fetchone()
    last_nav = max((r.date for r in led.nav_history()), default=None)
    return Dataset(
        ("opened", "cash_d0", "receivables", "payables", "projected_cash", "unsettled", "holdings",
         "market_value", "valued_on", "last_recon_date", "last_recon_at", "open_breaks", "clean_streak"),
        ((opened, led.cash_balance(), sum(s.signed for s in pending if s.signed > 0),
          -sum(s.signed for s in pending if s.signed < 0), led.projected_cash(), len(pending), len(held),
          market_value, close_day, recon[0] if recon else None, recon[1] if recon else None,
          recon[2] if recon else None, clean_streak(books.conn, calendar, last_nav) if last_nav else 0),))


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


# ---------- STP-4500 분개 모형 ----------

def journal_templates() -> Dataset:
    """분개 모형 목록: 원장이 이벤트 유형마다 만드는 전표의 모양 (코드에 있는 규칙, DB를 읽지 않아요)."""
    from .journal_templates import KIND_NAMES, SIDE_NAMES, TEMPLATES

    def shape(t):
        return " / ".join(f"{SIDE_NAMES[ln.side]} {ln.account.value[1] if ln.account else '직전 평가 전표'}"
                          for ln in t.lines)
    return Dataset(("code", "event_type", "kind", "kind_name", "case", "shape", "line_count", "note"),
                   tuple((t.code, t.event_type, t.kind, KIND_NAMES.get(t.kind, t.kind), t.case, shape(t), len(t.lines),
                          t.note) for t in TEMPLATES))


def journal_template_lines(code: str) -> tuple[Dataset, dict]:
    """모형 하나의 줄(차변·대변, 계정, 금액 산식, 업무 규칙 번호)과 그 모형의 참고 한 줄.
    KeyError when there is no such template."""
    from .journal_templates import SIDE_NAMES, TEMPLATES
    t = next((t for t in TEMPLATES if t.code == code), None)
    if t is None:
        raise KeyError(f"없는 분개 모형: {code}")
    return (Dataset(("side_name", "account_code", "account_name", "amount", "rule"),
                    tuple((SIDE_NAMES[ln.side], ln.account.code if ln.account else None,
                           ln.account.value[1] if ln.account else "직전 평가 전표의 계정", ln.amount, ln.rule or None)
                          for ln in t.lines)),
            {"case": t.case, "note": t.note or "없음"})


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
    "STP4500R01": journal_templates,
    "STP4500R02": journal_template_lines,
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
    "STP4100R01": instrument_master,
    "STP4100R02": instrument_rules,
    "STP4300R01": fee_rules,
    "STP4300R02": settlement_rules,
    "STP4300R03": tick_rules,
    "STP4300R04": holiday_calendar,
    "STP4300R05": risk_rules,
}
