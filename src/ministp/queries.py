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


# ---------- STP-3100 원장·분개 조회 ----------

def ledger_events(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  event_type: str | None = None, symbol: str | None = None,
                  account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's 이벤트 원장 목록, newest first. Dates are business dates, not recording times."""
    return _q(conn, f"""
        select e.seq, e.event_type, {_EVENT_DATE} as date, e.payload->>'symbol' as symbol,
               e.recorded_at, e.payload
          from ledger_events e
         where e.account_id = %(a)s
           and (%(f)s::date is null or {_EVENT_DATE} >= %(f)s)
           and (%(t)s::date is null or {_EVENT_DATE} <= %(t)s)
           and (%(ty)s::text is null or e.event_type = %(ty)s)
           and (%(sym)s::text is null or e.payload->>'symbol' = %(sym)s)
         order by e.seq desc""", {"f": date_from, "t": date_to, "ty": event_type, "sym": symbol, "a": account_id})


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


def event_trace(conn: psycopg.Connection, seq: int, account_id: str = DEFAULT_ACCOUNT) -> EventTrace:
    """이벤트 하나 → 분개 → 보유·예수금 변동 (원장 추적). An event of another account is not found."""
    event = _q(conn, f"select e.seq, e.event_type, {_EVENT_DATE} as date, e.recorded_at, e.payload"
                     " from ledger_events e where e.seq = %s and e.account_id = %s", (seq, account_id))
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


def trial_balance(conn: psycopg.Connection, account_id: str = DEFAULT_ACCOUNT) -> tuple[Dataset, bool]:
    """One account's 시산표 and whether its total debits equal its total credits."""
    ds = _q(conn, "select code, name, normal_side, balance from account_balances_v"
                  " where account_id = %s and balance <> 0 order by code", (account_id,))
    dr, cr = conn.execute(
        "select coalesce(sum(l.debit), 0), coalesce(sum(l.credit), 0)"
        " from journal_entries e join journal_lines l using (entry_id) where e.account_id = %s",
        (account_id,)).fetchone()
    return ds, dr == cr


# ---------- STP-2100 대사 결과 ----------

def recon_history(conn: psycopg.Connection, date_from: dt.date | None = None, date_to: dt.date | None = None,
                  kind: str = "DAILY_RECON", only_breaks: bool = False,
                  account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """One account's 대사 결과 with each break's latest owner judgement next to it (RESOLVED rows are not listed alone)."""
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
           and r.account_id = %(a)s
           and s.kind = %(kind)s
           and (%(f)s::date is null or r.date >= %(f)s)
           and (%(t)s::date is null or r.date <= %(t)s)
           and (not %(ob)s or r.status = 'BREAK')
         order by r.date desc, r.run_id desc, r.id""",
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
    """One account's orders of the day: 의도 → 접수 → 반영된 체결, 체결통보 반영 지연.

    Brokers number orders per account, so fills and notices are matched inside the order's account."""
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


def holdings_view(base: Dataset, ledger: Ledger) -> Dataset:
    """보유에 비중(장부금액 ÷ 장부금액 합계)과 최근 종가 추세를 붙여요. 화면이 막대와 추세선으로 그리는 값이에요."""
    cost = base.columns.index("book_cost")
    symbol = base.columns.index("symbol")
    total = sum(r[cost] for r in base.rows)
    trends = close_trends(ledger)
    rows = tuple((*r, round(float(r[cost]) / float(total), 4) if total else None, trends.get(r[symbol], []))
                 for r in base.rows)
    return Dataset((*base.columns, "weight", "trend"), rows)


def cash_summary(ledger: Ledger, open_orders) -> Dataset:
    """예수금 D+0, 결제 후 예수금, 미체결 매수 예약, 주문가능금액 (R-37과 같은 계산)."""
    from .capacity import pending_buy_value
    reserved, unpriced = pending_buy_value(open_orders, {})
    projected = ledger.projected_cash()
    return Dataset(("cash_d0", "projected_cash", "pending_buy_value", "unpriced_buy_orders", "available_cash"),
                   ((ledger.cash_balance(), projected, reserved, unpriced, projected - reserved),))




# ---------- STP-1300 주문가능 조회 ----------



def last_closes(ledger: Ledger) -> tuple[dt.date | None, dict[str, int]]:
    """The most recent closing prices the ledger knows (NavEvent/RevaluationEvent), until live quotes come."""
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
                     cap.pending_sell_qty, cap.sellable_qty))
    return Dataset(("symbol", "instrument_name", "ref_price", "price_date", "available_cash", "max_buy_qty",
                    "held_qty", "pending_sell_qty", "sellable_qty"), tuple(rows))


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

# ---------- STP-4400 계좌 목록 · STP-4500 고객별 보유 (조회 전용) ----------

def account_list(conn: psycopg.Connection, calendar) -> Dataset:
    """Every account with its customer, current status and the size of its books.

    The account number and the keys are not in the database; `credential_ref` is only
    the name of the environment variables the owner's PC keeps them in."""
    from .ops import clean_streak
    ds = _q(conn, """
        select t.account_id, t.alias, t.customer_id, c.alias as customer_alias, t.broker, t.env,
               t.account_type, s.status, s.effective_from as status_from, t.opened_on, t.credential_ref,
               (select count(*) from ledger_events e where e.account_id = t.account_id) as events,
               (select balance from cash_balance_v b where b.account_id = t.account_id) as cash_d0,
               (select count(*) from positions_v p where p.account_id = t.account_id) as holdings,
               (select coalesce(sum(p.book_cost), 0) from positions_v p where p.account_id = t.account_id) as book_cost,
               (select max(b.date) from broker_snapshots b
                 where b.account_id = t.account_id and b.kind = 'DAILY_RECON') as last_recon_date
          from trading_accounts t
          join customers c using (customer_id)
          join account_status_v s using (account_id)
         order by t.customer_id, t.account_id""")
    at = ds.columns.index("last_recon_date")
    rows = tuple(r + (clean_streak(conn, calendar, r[at], r[0]) if r[at] else 0,) for r in ds.rows)
    return Dataset(ds.columns + ("clean_streak",), rows)


def account_status_history(conn: psycopg.Connection, account_id: str) -> Dataset:
    """One account's status rows, newest first (a change is a new row, never an update)."""
    return _q(conn, "select id, status, effective_from, reason, created_at from account_status_history"
                    " where account_id = %s order by effective_from desc, id desc", (account_id,))


def account_positions(conn: psycopg.Connection, account_id: str) -> Dataset:
    """One account's holdings (`positions_v`), whichever account the screens are looking at."""
    return _q(conn, """
        select p.symbol, i.name as instrument_name, p.qty, p.book_cost
          from positions_v p join instruments i using (symbol)
         where p.account_id = %s
         order by p.book_cost desc""", (account_id,))


def customer_positions(conn: psycopg.Connection, customer_id: str | None = None) -> Dataset:
    """고객별 보유 합산: one customer's accounts added up per symbol (`customer_positions_v`).

    With `customer_id` only that customer's rows (the detail under an account row)."""
    return _q(conn, """
        select v.customer_id, c.alias as customer_alias, v.symbol, i.name as instrument_name, v.qty, v.book_cost,
               (select count(distinct p.account_id) from positions_v p join trading_accounts t using (account_id)
                 where t.customer_id = v.customer_id and p.symbol = v.symbol) as accounts
          from customer_positions_v v
          join customers c using (customer_id)
          join instruments i using (symbol)
         where (%(c)s::text is null or v.customer_id = %(c)s)
         order by v.customer_id, v.book_cost desc""", {"c": customer_id})


def customer_list(conn: psycopg.Connection) -> Dataset:
    """고객 목록: every customer with the number of accounts and what they add up to."""
    return _q(conn, """
        select c.customer_id, c.alias, count(t.account_id) as accounts,
               coalesce(sum((select balance from cash_balance_v b where b.account_id = t.account_id)), 0) as cash_d0,
               coalesce(sum((select sum(p.book_cost) from positions_v p where p.account_id = t.account_id)), 0) as book_cost
          from customers c left join trading_accounts t using (customer_id)
         group by c.customer_id, c.alias
         order by c.customer_id""")


def customer_accounts(conn: psycopg.Connection, customer_id: str) -> Dataset:
    """One customer's accounts with their status, cash and holdings at book cost."""
    return _q(conn, """
        select t.account_id, t.alias, s.status,
               (select balance from cash_balance_v b where b.account_id = t.account_id) as cash_d0,
               (select coalesce(sum(p.book_cost), 0) from positions_v p where p.account_id = t.account_id) as book_cost
          from trading_accounts t join account_status_v s using (account_id)
         where t.customer_id = %s
         order by t.account_id""", (customer_id,))


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
            select a.code, a.name, '정상잔액 ' || coalesce(n.name, a.normal_side) as description, '사용' as in_use
              from accounts_chart a
              left join common_codes n on n.group_id = 'NORMAL_SIDE' and n.code = a.normal_side
             order by a.code""")
    if conn.execute("select 1 from code_groups where group_id = %s", (group_id,)).fetchone() is None:
        raise KeyError(group_id)
    return _q(conn, """
        select code, name, description, case when in_use then '사용' else '사용 안 함' end as in_use
          from common_codes where group_id = %s
         order by sort_order, code""", (group_id,))


# ---------- STP-4100·4200·4300 기준정보 (조회 전용) ----------
# Reference data as the system really uses it: instruments from the DB, fee rates,
# settlement rules, tick sizes and the holiday calendar from the objects the
# ledger is built with. Nothing here is a second copy that could drift.

WEEKDAYS = "월화수목금토일"


def _pct(rate) -> str:
    """0.00015 -> '0.015%' (rates are shown exactly, not rounded to two places)."""
    from decimal import Decimal
    return f"{(Decimal(rate) * 100).normalize():f}%"


def instrument_master(books) -> Dataset:
    """종목정보: 등록 종목, 최근 종가와 그 가격의 호가단위, 매도 세율, 보유."""
    from .ticks import tick_size
    fees = books.ledger.fees
    close_day, closes = last_closes(books.ledger)
    held = books.ledger.positions()
    rows = []
    for sym, inst in sorted(books.instruments.items()):
        price = closes.get(sym)
        tx, rural = fees.sell_tax_rates[(inst.market, inst.asset_type)]
        pos = held.get(sym)
        rows.append((sym, inst.name, inst.market.value, inst.asset_type.value, price,
                     close_day if price is not None else None,
                     tick_size(price, inst.asset_type) if price is not None else None,
                     _pct(tx + rural), pos.qty if pos else 0, pos.book_cost if pos else 0))
    return Dataset(("symbol", "instrument_name", "market", "asset_type", "last_close", "close_date", "tick",
                    "sell_tax", "qty", "book_cost"), tuple(rows))


def instrument_events(conn: psycopg.Connection, symbol: str, account_id: str = DEFAULT_ACCOUNT) -> Dataset:
    """한 종목의 원장 이벤트 (개시 보유, 체결), newest first. 결제 이벤트는 체결ID로만 이어져 여기엔 없어요."""
    return ledger_events(conn, symbol=symbol, account_id=account_id)


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
        name = books.instruments[sym].name if sym in books.instruments else None
        rows.append((s.exec_id, s.trade_date, s.settle_date, sym, name, s.side.value, e.qty if e else None,
                     s.signed))
    return Dataset(("exec_id", "trade_date", "settle_date", "symbol", "instrument_name", "side", "qty", "amount"),
                   tuple(rows))


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
    "STP3100R03": trial_balance,
    "STP2100R01": recon_history,
    "STP1200R01": order_blotter,
    "STP2200R01": nav_series,
    "STP4100R01": instrument_master,
    "STP4100R02": instrument_events,
    "STP4200R01": account_summary,
    "STP4200R02": cash_ladder,
    "STP4200R03": pending_settlements,
    "STP4300R01": fee_rules,
    "STP4300R02": settlement_rules,
    "STP4300R03": tick_rules,
    "STP4300R04": holiday_calendar,
    "STP4300R05": risk_rules,
}
