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


# ---------- STP-1100 잔고 (IBOR) ----------

def holdings(conn: psycopg.Connection, day: dt.date | None = None) -> Dataset:
    """종목별 보유: 수량, 장부금액, 오늘 체결 수량, 마지막 변동 시각."""
    return _q(conn, """
        select p.symbol, i.name as instrument_name, i.asset_type, p.qty, p.book_cost,
               coalesce(t.today_qty, 0) as today_qty, t.last_change
          from positions_v p
          join instruments i on i.symbol = p.symbol
          left join lateral (select sum(x.qty_delta) filter (where x.trade_date = %(d)s) as today_qty,
                                    max(e.recorded_at) as last_change
                               from position_ledger x join ledger_events e on e.seq = x.event_seq
                              where x.symbol = p.symbol) t on true
         where p.qty <> 0
         order by p.book_cost desc""", {"d": day or dt.date.today()})


def cash_summary(ledger: Ledger, open_orders) -> Dataset:
    """예수금 D+0, 결제 후 예수금, 미체결 매수 예약, 주문가능금액 (R-37과 같은 계산)."""
    from .capacity import pending_buy_value
    reserved, unpriced = pending_buy_value(open_orders, {})
    projected = ledger.projected_cash()
    return Dataset(("cash_d0", "projected_cash", "pending_buy_value", "unpriced_buy_orders", "available_cash"),
                   ((ledger.cash_balance(), projected, reserved, unpriced, projected - reserved),))




# ---------- STP-1300 주문가능 조회 ----------

REASON_TEXT = {
    "": "주문 가능",
    "KILL_SWITCH": "킬 스위치가 켜져 있어요",
    "BAD_QTY": "수량을 1주 이상 넣어 주세요",
    "MAX_ORDER_VALUE": "1회 주문 금액 한도를 넘어요",
    "SHORT_SELL": "매도가능수량보다 많아요 (공매도 금지, R-36)",
    "INSUFFICIENT_CASH": "주문가능금액이 모자라요",
    "BAD_TICK": "호가단위에 맞지 않는 가격이에요",
    "NO_PRICE": "기준 단가가 없어요: 지정가를 넣어 주세요",
}


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
    from .ticks import tick_size
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
    decision = check(cap, s, qty, halted=halted)
    reason = decision.reason
    if decision.ok and limit_price and limit_price % tick_size(limit_price, inst.asset_type):
        reason = "BAD_TICK"
    value = qty * ref
    required = int(value * (1 + RiskLimits().cash_buffer_rate)) if s is Side.BUY else 0
    return Dataset(columns, ((symbol, inst.name, side, qty, ref, "지정가" if limit_price else "최근 종가", value,
                              required, cap.cash_d0, cap.projected_cash, cap.pending_buy_value, cap.available_cash,
                              cap.max_buy_qty, cap.held_qty, cap.pending_sell_qty, cap.sellable_qty,
                              reason == "", reason, REASON_TEXT.get(reason, reason)),))

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


def instrument_events(conn: psycopg.Connection, symbol: str) -> Dataset:
    """한 종목의 원장 이벤트 (개시 보유, 체결), newest first. 결제 이벤트는 체결ID로만 이어져 여기엔 없어요."""
    return ledger_events(conn, symbol=symbol)


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
