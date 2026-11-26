"""Read transactions by id. Every one only reads; the DB session is read-only too."""
import datetime as dt
from dataclasses import dataclass, field
from functools import cached_property

import psycopg

from .. import queries as q
from ..calendar import BusinessCalendar
from ..fees import FeeSchedule
from ..ops import GATE_DAYS, Books, clean_streak, load_books, load_instruments, stored_open_orders
from ..queries import Dataset
from ..store import DEFAULT_ACCOUNT


class TxError(ValueError):
    pass


@dataclass
class TxContext:
    conn: psycopg.Connection
    calendar: BusinessCalendar
    fees: FeeSchedule
    today: dt.date
    account_id: str = DEFAULT_ACCOUNT   # the account the screens are looking at (계좌 선택, 화면 공통)

    @cached_property
    def books(self) -> Books:
        return load_books(self.conn, self.calendar, self.fees, self.account_id)


@dataclass(frozen=True)
class TxResult:
    datasets: dict[str, Dataset]
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tx:
    tx_id: str
    title: str
    params: dict[str, str]  # name -> date | text | int | bool
    run: object             # (TxContext, params) -> TxResult


def _date(v):
    return dt.date.fromisoformat(v) if isinstance(v, str) else v


COERCE = {"date": _date, "text": str, "int": int, "bool": lambda v: v in (True, "true", "1", "on", 1)}


def _holdings(c: TxContext, p) -> TxResult:
    """보유는 오늘 `run`이 받은 현재가로 평가해요 (R-68). `run`이 꺼져 있으면 가장 최근 종가로."""
    from ..quotes import latest_quotes
    quotes, now = latest_quotes(c.conn, c.today), dt.datetime.now()
    base = q.holdings(c.conn, p.get("day") or c.today, c.account_id)
    view = q.holdings_view(base, c.books.ledger, quotes, now)
    symbols = [r[view.columns.index("symbol")] for r in view.rows]
    return TxResult({"main": view, "mix": q.holdings_mix(view)},
                    {"basis": q.valuation_basis(c.books.ledger, symbols, quotes, now)})


def _cash(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.cash_summary(c.books.ledger, stored_open_orders(c.books, c.today))})


def _capacities(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.capacity_by_symbol(c.books, c.today)})


def _order_check(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드를 넣어 주세요")
    side = (p.get("side") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        raise TxError(f"매매구분은 BUY 또는 SELL이에요: {side}")
    try:
        ds = q.order_check(c.books, c.today, p["symbol"], side, p.get("qty") or 0, p.get("limit_price"))
    except KeyError as e:
        raise TxError(e.args[0]) from e
    r = ds.records()[0]
    return TxResult({"main": ds}, {"ok": r["ok"], "reason_text": r["reason_text"]})


def _blotter(c: TxContext, p) -> TxResult:
    orders = q.order_blotter(c.conn, p.get("day") or c.today, c.account_id)
    return TxResult({"main": q.blotter_view(orders, c.books.ledger, c.today)})


def _order(c: TxContext, p) -> TxResult:
    if not p.get("client_id"):
        raise TxError("내부 주문번호가 필요해요")
    try:
        d = q.order_detail(c.conn, c.books.ledger, p["client_id"], c.today, c.account_id)
    except KeyError as e:
        raise TxError(e.args[0]) from e
    r = q.order_request(c.conn, c.books.ledger, p["client_id"], c.today, c.account_id)
    head = r.request.records()[0] if r.request.rows else {}
    extra = (head.get("request_no"), head.get("order_type"), head.get("status"), head.get("amount"), head.get("fill_rate"))
    order = Dataset((*d.order.columns, "request_no", "order_type", "request_status", "request_amount", "fill_rate"),
                    tuple((*row, *extra) for row in d.order.rows))
    return TxResult({"order": order, "fills": d.fills, "lines": r.lines})


def _recon(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.recon_history(c.conn, p.get("date_from"), p.get("date_to"),
                                             p.get("kind") or "DAILY_RECON", bool(p.get("only_breaks")),
                                             c.account_id)})


MAX_CALENDAR_DAYS = 800   # 달력 보기는 두 해 남짓까지


def _streak(c: TxContext) -> int:
    """무오류 연속 영업일 of the account in view, as of the last day that was closed (the number every screen shows)."""
    last = max((r.date for r in c.books.ledger.nav_history()), default=None)
    return clean_streak(c.conn, c.calendar, last, c.account_id) if last else 0


def _recon_days(c: TxContext, p) -> TxResult:
    f, t = p.get("date_from"), p.get("date_to")
    if f and (t or c.today) < f:
        raise TxError("시작일이 종료일보다 늦어요")
    if f and ((t or c.today) - f).days > MAX_CALENDAR_DAYS:
        raise TxError("달력은 두 해까지만 그려요. 기간을 줄여 주세요")
    ds = q.recon_calendar(c.conn, c.calendar, c.today, f, t, p.get("kind") or "DAILY_RECON", c.account_id)
    ran = [r for r in ds.records() if r["items"] is not None]   # newest first
    # The day to open with: the latest one that still has a break to judge, else the latest run.
    opens = next((r for r in ran if r["open_breaks"]), ran[0] if ran else None)
    streak = _streak(c)
    meta = {"clean_streak": streak, "gate_left": max(0, GATE_DAYS - streak)}
    if opens:
        meta["selected"] = opens["date"].isoformat()
    return TxResult({"main": ds}, meta)


def _integrity(c: TxContext, p) -> TxResult:
    ds = q.integrity_checks(c.conn, c.calendar, c.fees, load_instruments(c.conn), c.account_id)
    breaks = [r["key"] for r in ds.records() if r["result"] != q.RESULT_TEXT[True]]
    meta = {"all_ok": not breaks, "breaks": len(breaks)}
    if breaks:
        meta["selected"] = breaks[0]   # open on what is wrong
    return TxResult({"main": ds}, meta)


def _integrity_rows(c: TxContext, p) -> TxResult:
    if not p.get("title"):
        raise TxError("점검 항목이 필요해요")
    try:
        check, rows = q.integrity_compared(c.conn, p["title"], c.calendar, c.fees, load_instruments(c.conn),
                                           c.account_id)
    except KeyError as e:
        raise TxError(e.args[0]) from e
    return TxResult({"check": check, "rows": rows})


def _nav(c: TxContext, p) -> TxResult:
    ds = q.nav_series(c.books.ledger, p.get("date_from"), p.get("date_to"))
    return TxResult({"main": ds}, {"clean_streak": _streak(c), "gate_days": GATE_DAYS})


def _nav_changes(c: TxContext, p) -> TxResult:
    ds = q.nav_change_days(c.books.ledger, p.get("date_from"), p.get("date_to"))
    off = [r["date"] for r in ds.records() if r["unexplained"]]
    meta = {"all_explained": not off, "unexplained_days": len(off)}
    if off:
        meta["selected"] = off[0].isoformat()   # the latest day with a won no entry explains
    return TxResult({"main": ds}, meta)


def _nav_bridge(c: TxContext, p) -> TxResult:
    if not p.get("date"):
        raise TxError("기준일이 필요해요")
    try:
        return TxResult({"main": q.nav_change_bridge(c.books.ledger, p["date"])})
    except KeyError as e:
        raise TxError(e.args[0]) from e


PERIOD_DAYS = {"30일": 30, "60일": 60, "90일": 90, "1년": 365}


def _span(c: TxContext, p) -> tuple[dt.date | None, dt.date | None]:
    """조회 기간: 시작일을 넣으면 그대로, 비워 두면 '기간'(30일·60일·90일·1년)만큼 종료일(없으면 오늘)에서 거슬러 가요.
    '전체'는 원장을 시작한 날부터예요."""
    days = PERIOD_DAYS.get(p.get("period") or "")
    if p.get("date_from") or not days:
        return p.get("date_from"), p.get("date_to")
    to = p.get("date_to") or c.today
    return to - dt.timedelta(days=days - 1), to


def _price_symbols(c: TxContext, p) -> TxResult:
    from ..pricecheck import BAD, WARN, PriceRules
    ds = q.price_trend_symbols(c.books.ledger, *_span(c, p))
    rules = PriceRules()
    meta = {"warn_change": q._pct(rules.warn_change), "limit_change": q._pct(rules.limit_change),
            "warn_exec_gap": q._pct(rules.warn_exec_gap)}
    rows = ds.records()
    opens = next((r for r in rows if r["level"] == BAD), None) or next((r for r in rows if r["level"] == WARN), None)
    if opens:
        meta["selected"] = opens["symbol"]      # a symbol with a close to look at in the period
    return TxResult({"main": ds}, meta)


def _price_rows(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    ds = q.price_trend_rows(c.books.ledger, p["symbol"], p.get("date_from"), p.get("date_to"))
    closes = [(r["close"], r["date"]) for r in ds.records()]
    meta = {}
    if closes:
        (hi, hi_day), (lo, lo_day) = max(closes), min(closes)
        meta = {"high": f"{hi:,} ({hi_day:%m-%d})", "low": f"{lo:,} ({lo_day:%m-%d})"}
    return TxResult({"main": ds}, meta)


def _returns(c: TxContext, p) -> TxResult:
    ds = q.return_months(c.books.ledger, *_span(c, p))
    periods = q.return_periods(c.books.ledger)
    meta = {k: f"{v:+.2f}%" for k, v in periods.items() if k != "last_date" and v is not None}
    if periods:
        meta["last_date"] = periods["last_date"].isoformat()
    return TxResult({"main": ds}, meta)


def _return_days(c: TxContext, p) -> TxResult:
    if not p.get("month"):
        raise TxError("월이 필요해요")
    try:
        return TxResult({"main": q.return_month_days(c.books.ledger, p["month"], p.get("date_from"),
                                                     p.get("date_to"))})
    except ValueError as e:
        raise TxError(e.args[0]) from e


def _close_days(c: TxContext, p) -> TxResult:
    from ..closeboard import DAY_BAD
    ds = q.close_days(c.conn, c.calendar, c.fees, c.today, p.get("date_from"), p.get("date_to"), c.account_id)
    return TxResult({"main": ds}, {"attention_days": sum(r["state"] == DAY_BAD for r in ds.records())})


def _close_steps(c: TxContext, p) -> TxResult:
    if not p.get("date"):
        raise TxError("영업일이 필요해요")
    try:
        return TxResult({"main": q.close_steps(c.conn, c.calendar, c.fees, c.today, p["date"], c.account_id)})
    except KeyError as e:
        raise TxError(e.args[0]) from e


def _events(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.ledger_events(c.conn, p.get("date_from"), p.get("date_to"),
                                             p.get("event_type"), p.get("symbol"), p.get("seq"), c.account_id)})


def _trace(c: TxContext, p) -> TxResult:
    if p.get("seq") is None:
        raise TxError("seq가 필요해요")
    try:
        t = q.event_trace(c.conn, p["seq"], c.account_id)
    except KeyError as e:
        raise TxError(str(e)) from e
    return TxResult({"event": t.event, "positions": t.positions, "cash": t.cash})   # its journal: STP3200R02


def _journal_events(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.journal_events(c.conn, p.get("date_from"), p.get("date_to"),
                                              p.get("event_type"), p.get("symbol"), p.get("seq"), c.account_id)})


def _journal(c: TxContext, p) -> TxResult:
    if p.get("seq") is None:
        raise TxError("seq가 필요해요")
    try:
        journal, balanced = q.event_journal(c.conn, p["seq"], c.account_id)
    except KeyError as e:
        raise TxError(str(e)) from e
    return TxResult({"journal": journal}, {"balanced": balanced})


def _trial(c: TxContext, p) -> TxResult:
    ds, meta = q.trial_balance(c.conn, p.get("day") or c.today, c.account_id)
    return TxResult({"main": ds}, meta)


def _journal_templates(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.journal_templates()})


def _journal_template_lines(c: TxContext, p) -> TxResult:
    if not p.get("code"):
        raise TxError("분개 모형 코드가 필요해요")
    try:
        lines, meta = q.journal_template_lines(p["code"])
    except KeyError as e:
        raise TxError(e.args[0]) from e
    return TxResult({"main": lines}, meta)


def _instruments(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.instrument_master(c.books)})


def _market(c: TxContext, p) -> TxResult:
    ds, meta = q.market_instruments(c.conn, p.get("date"), p.get("market"), p.get("sec_group"), p.get("state"),
                                    p.get("keyword"), p.get("registered_only", False))
    return TxResult({"main": ds}, meta)


def _market_one(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    try:
        return TxResult({"main": q.market_instrument(c.conn, p["symbol"], p.get("date"))})
    except KeyError as e:
        raise TxError(e.args[0]) from e


def _market_form(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    try:
        return TxResult({"main": q.market_instrument_form(c.conn, p["symbol"], p.get("date"))})
    except KeyError as e:
        raise TxError(e.args[0]) from e


def _market_history(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    return TxResult({"main": q.market_instrument_history(c.conn, p["symbol"], p.get("date"))})


def _master_loads(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.master_loads(c.conn, p.get("date_from"), p.get("date_to"), p.get("load_id"))})


def _master_changes(c: TxContext, p) -> TxResult:
    if p.get("load_id") is None:
        raise TxError("적재 번호가 필요해요")
    try:
        return TxResult({"main": q.master_load_changes(c.conn, p["load_id"], p.get("change"))})
    except KeyError as e:
        raise TxError(e.args[0]) from e


def _instrument_rules(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    sym = p["symbol"]
    inst = None if sym in c.books.instruments else q.master_instrument(c.conn, sym, p.get("date"))
    if inst is None and sym not in c.books.instruments:
        if c.conn.execute("select 1 from instrument_master where symbol = %s", (sym,)).fetchone():
            return TxResult({"main": q.Dataset(("kind", "item", "value", "note"), ())})   # not a 주권 or an ETF
        raise TxError(f"등록되지 않은 종목이에요: {sym}")
    return TxResult({"main": q.instrument_rules(c.books, sym, c.today, inst)})


def _account(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.account_summary(c.books, c.calendar, c.today)})


def _ladder(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.cash_ladder(c.books, c.calendar, c.today)})


def _settlements(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.pending_settlements(c.books)})


def _fees(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.fee_rules(c.fees)})


def _settlement_rules(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.settlement_rules(c.books.ledger, c.today)})


def _ticks(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.tick_rules()})


def _holidays(c: TxContext, p) -> TxResult:
    ds = q.holiday_calendar(c.calendar, c.today)
    years = c.calendar.loaded_years
    until = c.calendar.verified_until or (dt.date(years[-1], 12, 31) if years else None)
    return TxResult({"main": ds}, {"years": ", ".join(str(y) for y in years),
                                   "until": until.isoformat() if until else ""})


def _basket_portfolio(c: TxContext):
    from ..basket import load_portfolio
    return load_portfolio(c.books, c.conn, c.today)


def _basket_current(c: TxContext, p) -> TxResult:
    """바스켓의 첫 모습: 보유 종목을 현재 편입비 그대로 (R-97)."""
    from ..basket import current_weights
    pf = _basket_portfolio(c)
    rows = tuple((s, pf.instruments[s].name if s in pf.instruments else "",
                  pf.instruments[s].asset_type.value if s in pf.instruments else "", w, w, pf.prices.get(s))
                 for s, w in current_weights(pf).items())
    return TxResult({"main": Dataset(("symbol", "instrument_name", "asset_type", "current_weight", "target_weight",
                                      "price"), rows)}, {"basis": pf.price_basis or "가격 없음"})


BASKET_SIDES = {"BUY": "BUY", "SELL": "SELL"}
BASKET_BASES = {"현재가 지정가": "current", "현재가 +1호가": "tick1", "시장가": "market"}


def parse_basket(text: str):
    """화면이 보내는 바스켓 `종목:배분 비율,종목:배분 비율`."""
    from decimal import Decimal, InvalidOperation

    from ..basket import Target
    out = []
    for part in filter(None, (x.strip() for x in text.split(","))):
        sym, _, w = part.partition(":")
        try:
            out.append(Target(sym.strip(), Decimal(w.strip() or "0")))
        except InvalidOperation as e:
            raise TxError(f"배분 비율이 숫자가 아니에요: {sym} {w}") from e
    return out


def _basket_plan(c: TxContext, p) -> TxResult:
    """종목별 주문안과 요약 (R-97). 계산만 하고 DB에는 쓰지 않아요."""
    from decimal import Decimal

    from ..basket import BasketError, PriceBasis, default_targets, issue_text, simulate
    from ..ledger import Side
    pf = _basket_portfolio(c)
    targets = parse_basket(p["basket"]) if p.get("basket") else default_targets(pf)
    side = (p.get("side") or "BUY").upper()
    if side not in BASKET_SIDES:
        raise TxError(f"매매구분은 BUY 또는 SELL이에요: {side}")
    basis = BASKET_BASES.get(p.get("basis") or "현재가 지정가")
    if basis is None:
        raise TxError(f"주문가 기준은 {', '.join(BASKET_BASES)} 가운데 하나예요")
    try:
        r = simulate(pf, targets, p.get("amount") or 0, Side(side), PriceBasis(basis), c.fees,
                     Decimal(p["limit"]) if p.get("limit") is not None else Decimal(30))
    except BasketError as e:
        raise TxError(str(e)) from e
    lines = tuple((ln.symbol, ln.name, ln.asset_type, ln.order_price, f"호가 {ln.tick:,}원" if ln.tick else "",
                   "지정가" if ln.limit_price else ("시장가" if ln.order_price else ""), ln.target_amount, ln.qty,
                   ln.amount, ln.actual_weight, ln.weight_gap, ln.commission + ln.taxes, ln.post_weight,
                   not ln.blocked, issue_text(ln.issues),
                   f"{ln.symbol}:{side}:{ln.qty}:{ln.limit_price or ''}:BK" if ln.qty > 0 else None)
             for ln in r.lines)
    summary = ((r.total_amount, r.allocated, r.leftover, r.commission + r.taxes, r.required_cash, r.available_cash,
                r.ok, issue_text(r.issues) if r.issues else ("결재로 넘길 수 있어요" if r.ok else "검사에 걸린 종목이 있어요")),)
    return TxResult({"main": Dataset(("symbol", "instrument_name", "asset_type", "order_price", "tick_text",
                                      "order_kind", "target_amount", "qty", "amount", "actual_weight", "weight_gap",
                                      "costs", "post_weight", "ok", "check_text", "line"), lines),
                     "summary": Dataset(("total_amount", "allocated", "leftover", "costs", "required_cash",
                                         "available_cash", "ok", "verdict"), summary)},
                    {"basis": pf.price_basis or "가격 없음", "ok": r.ok, "limit": f"{r.weight_limit}%"})


def _risk(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.risk_rules()})


def _accounts(c: TxContext, p) -> TxResult:
    """Every account, whichever one the screens are looking at. 계좌번호 is not in the database: it is masked
    from this PC's environment variable as the rows are answered (`accounts.masked_account_no`)."""
    from ..accounts import masked_account_no
    ds = q.account_list(c.conn)
    ref = ds.columns.index("credential_ref")
    rows = tuple(r + (masked_account_no(r[ref]) or "이 PC에 없음",) for r in ds.rows)
    return TxResult({"main": Dataset(ds.columns + ("masked_no",), rows)}, {"selected": c.account_id})


def _account_status(c: TxContext, p) -> TxResult:
    if not p.get("account_id"):
        raise TxError("계좌 ID가 필요해요")
    return TxResult({"main": q.account_status_history(c.conn, p["account_id"])})


def _companies(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.company_list(c.conn)})


def _company_positions(c: TxContext, p) -> TxResult:
    """One company's accounts added up per symbol (all companies when none is named)."""
    return TxResult({"main": q.company_positions(c.conn, p.get("company_id") or None)})


def _company_accounts(c: TxContext, p) -> TxResult:
    if not p.get("company_id"):
        raise TxError("법인 ID가 필요해요")
    return TxResult({"main": q.company_accounts(c.conn, p["company_id"])})


def _company_erp_codes(c: TxContext, p) -> TxResult:
    if not p.get("company_id"):
        raise TxError("법인 ID가 필요해요")
    return TxResult({"main": q.company_erp_codes(c.conn, p["company_id"])})


def _code_groups(c: TxContext, p) -> TxResult:
    """Every code group, and whether the code table still agrees with the schema and the program."""
    from .. import codes
    return TxResult({"main": q.code_group_list(c.conn)}, {"consistent": not codes.check(c.conn)})


def _codes(c: TxContext, p) -> TxResult:
    if not p.get("group_id"):
        raise TxError("코드 그룹이 필요해요")
    try:
        return TxResult({"main": q.code_list(c.conn, p["group_id"])})
    except KeyError:
        raise TxError(f"없는 코드 그룹이에요: {p['group_id']}") from None


# ---------- 주문 결재 (STP-1210 주식 주문, STP-5100 주문 결재, STP-5200 결재 권한; R-98~101) ----------
# 화면은 읽기만 해요. 줄마다 단건(이 줄만)과 묶음(체크한 줄을 보낼 순서대로 쌓아서) 두 결과를 보여 주고,
# 처리 버튼이 잠기는 이유(권한, 묶음 검증)도 같은 계산에서 나와요. 서버는 버튼을 누를 때 같은 검사를 다시 해요.

CHECK_SHORT = {   # 칸에 들어가는 짧은 말. 긴 설명은 묶음 검증 그리드와 요청 거부 문장에 있어요
    "KILL_SWITCH": "킬 스위치", "ACCOUNT_NOT_OPEN": "계좌 상태", "NOT_BUSINESS_DAY": "휴장일",
    "BAD_QTY": "수량", "BAD_TICK": "호가단위", "MAX_ORDER_VALUE": "1회 한도", "SHORT_SELL": "매도가능 초과",
    "INSUFFICIENT_CASH": "예수금 부족", "NO_PRICE": "가격 없음", "NOT_REGISTERED": "미등록 종목",
    "WEIGHT_LIMIT": "비중 한도", "GROUP_BLOCKED": "묶음 막힘",
}
NOT_PICKED = "선택 안 함"
DRAFT = "작성 중"


def check_short(issues) -> str:
    return ", ".join(CHECK_SHORT.get(i, i) for i in issues) or "통과"


@dataclass(frozen=True)
class DraftLine:
    """화면에서 쓰는 주문 한 줄 (저장하지 않아요). `key`는 화면이 붙인 줄 번호예요."""
    key: str
    line: object            # approval.Line
    group: str = ""         # 바스켓에서 온 줄: BK (묶음으로 요청돼요)


def parse_lines(text: str | None) -> list[DraftLine]:
    """화면이 보내는 주문 줄 `줄번호:종목:BUY|SELL:수량:지정가[:묶음]`을 쉼표로 이은 것. 지정가가 비면 시장가."""
    from ..approval import Line
    from ..ledger import Side
    out = []
    for part in filter(None, (x.strip() for x in (text or "").split(","))):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) not in (5, 6) or bits[2].upper() not in ("BUY", "SELL") or not bits[1]:
            raise TxError(f"주문 줄 형식이 올바르지 않아요: {part}")
        try:
            qty, price = int(bits[3]), int(bits[4]) if bits[4] else None
        except ValueError:
            raise TxError(f"수량과 가격은 숫자예요: {part}") from None
        if qty <= 0 or (price is not None and price <= 0):
            raise TxError(f"수량과 가격은 0보다 커야 해요: {part}")
        group = bits[5].upper() if len(bits) == 6 else ""
        if group not in ("", "BK"):
            raise TxError(f"묶음은 바스켓(BK)만 있어요: {part}")
        out.append(DraftLine(bits[0], Line(bits[1], Side(bits[2].upper()), qty, price), group))
    if len({d.key for d in out}) != len(out):
        raise TxError("주문 줄 번호가 겹쳐요")
    return out


def picked_keys(text: str | None) -> list[str]:
    return [k for k in dict.fromkeys(x.strip() for x in (text or "").split(",")) if k]


def _who(c: TxContext) -> tuple[str, dict]:
    from .. import approval
    user = approval.actor()
    return user, approval.rights_of(c.conn, user, c.account_id, c.today)


def _write_lock(c: TxContext, user: str, rights: dict) -> str:
    from .. import approval
    if not approval.has_users(c.conn):
        return "결재 사용자가 아직 없어요. STP-5200 결재 권한에서 먼저 등록해 주세요"
    if "ORDER_WRITE" not in rights:
        return f"{user}에게 이 계좌의 주문 작성 권한이 없어요"
    return ""


def _limit_text(right) -> str:
    return "한도 없음" if right.max_order_value is None else f"1건 한도 {right.max_order_value:,}원"


def _limits_and_weight():
    from ..precheck import DEFAULT_WEIGHT_LIMIT
    from ..risk import RiskLimits
    return RiskLimits(), DEFAULT_WEIGHT_LIMIT


def _set_rows(state, result, limits, weight_limit, picked_n: int) -> list[tuple]:
    """선택 묶음 검증: 항목마다 (항목, 내용, 설명, 결과, 통과 여부). 결과는 짧은 말이에요."""
    from ..ledger import Side
    lines = result.lines
    together = [i for ln in lines for i in ln.together]
    rows = []
    cash_bad = "INSUFFICIENT_CASH" in result.issues or "INSUFFICIENT_CASH" in together
    rows.append(("예수금", f"매수 {result.required_cash:,}원 (여유분 포함)",
                 f"주문가능 {result.available_cash:,}원 (잡힌 요청을 뺀 금액)",
                 "부족" if cash_bad else "통과", not cash_bad))
    buys = [ln for ln in lines if ln.item.side is Side.BUY and ln.post_weight is not None]
    top = max(buys, key=lambda ln: ln.post_weight, default=None)
    weight_bad = "WEIGHT_LIMIT" in together
    rows.append(("종목별 비중", f"주문 후 최대 {state.instruments[top.item.symbol].name} {top.post_weight}%"
                 if top and top.item.symbol in state.instruments else "매수 없음",
                 f"한도 종목당 {weight_limit}% (보유 + 미체결·앞 줄 매수)",
                 "초과" if weight_bad else ("통과" if top else "없음"), False if weight_bad else (True if top else None)))
    sells = [ln for ln in lines if ln.item.side is Side.SELL]
    sell_bad = "SHORT_SELL" in together
    rows.append(("매도가능수량", f"매도 {len(sells)}줄" if sells else "매도 없음", "보유 − 미체결 매도 − 앞 줄 매도",
                 "초과" if sell_bad else ("통과" if sells else "없음"), False if sell_bad else (True if sells else None)))
    biggest = max((ln.item.amount for ln in lines), default=0)
    max_bad = "MAX_ORDER_VALUE" in together
    rows.append(("1회 주문 금액", f"최대 {biggest:,}원",
                 "한도 없음" if limits.max_order_value is None else f"건당 {limits.max_order_value:,}원",
                 "초과" if max_bad else "통과", not max_bad))
    form = [i for i in dict.fromkeys([*result.issues, *together])
            if i not in ("INSUFFICIENT_CASH", "WEIGHT_LIMIT", "SHORT_SELL", "MAX_ORDER_VALUE", "GROUP_BLOCKED")]
    rows.append(("주문 형식·계좌", "호가단위, 수량, 가격, 영업일, 계좌 상태, 킬 스위치",
                 check_short(form) if form else "모두 맞아요", check_short(form), not form))
    if not picked_n:
        rows = [(a, b, c, NOT_PICKED, None) for a, b, c, _, _ in rows]
    return rows


SET_COLUMNS = ("item", "value", "detail", "result", "ok")


def _set_result(result, rows, picked_n: int, extra: str = "") -> TxResult:
    from ..approval import describe
    verdict = ("체크한 줄이 없어요" if not picked_n else
               ("묶음 검증 통과" if result.ok else f"묶음 검증 불합격: {describe(result)}") + extra)
    return TxResult({"main": Dataset(SET_COLUMNS, tuple(rows))},
                    {"verdict": verdict, "picked": f"{picked_n}건", "required": result.required_cash,
                     "available": result.available_cash})


def _committed(state, reqs, skip=()) -> list:
    """이미 잡힌 요청의 아직 안 나간 줄 (나간 줄은 미체결 주문으로 잡혀 있어요). `skip`: 빼고 볼 요청 키."""
    from .. import approval
    return [approval._from_line(state, r, ln) for r in reqs if str(r.key) not in skip for ln in r.unsent]


def _issues_of(result, prefix: str) -> tuple:
    """묶음 검증 결과에서 한 요청(줄 키가 `요청키:`로 시작)에 걸린 사유. 계좌 전체 사유도 함께."""
    out = list(result.issues)
    for ln in result.lines:
        if ln.item.key.startswith(prefix):
            out += [i for i in ln.together if i != "GROUP_BLOCKED" or not any(ln.together[:1])]
    return tuple(dict.fromkeys(out))


def _drafts_checked(c: TxContext, p):
    """화면의 주문 줄과 체크한 줄의 묶음 검증. (상태, 줄, 체크한 줄, 줄마다 단건 결과, 묶음 결과, 잡힌 요청)."""
    from .. import approval
    from ..precheck import check_set
    limits, weight = _limits_and_weight()
    drafts = parse_lines(p.get("lines"))
    picked = set(picked_keys(p.get("picked")))
    state = approval.account_state(c.books, c.today)
    live = approval.requests(c.conn, account_id=c.account_id, from_date=c.today, statuses=approval.LIVE)
    committed = _committed(state, live)
    proposed = {d.key: approval._proposed(state, d.line.symbol, d.line.side, d.line.qty, d.line.limit_price,
                                          d.key, d.group) for d in drafts}
    alone = check_set(state, list(proposed.values()), committed, limits, weight)
    chosen = [proposed[d.key] for d in drafts if d.key in picked]
    together = check_set(state, chosen, committed, limits, weight)
    return state, drafts, chosen, {ln.item.key: ln for ln in alone.lines}, together, live


def _line_status(r, ln) -> str:
    return {"SENT": "SENT", "FAILED": "SEND_FAILED"}.get(ln.send_outcome or "", r.status)


ORDER_LINE_COLUMNS = ("key", "pickable", "symbol", "instrument_name", "asset_type", "side", "kind", "qty",
                      "limit_price", "amount", "alone", "together", "status", "request_no", "order_type", "line_no",
                      "request_key", "group")


def _order_lines(c: TxContext, p) -> TxResult:
    """STP-1210 주문 작성: 화면에서 쓴 줄(저장 전)과 이 계좌의 오늘 이후 요청의 줄. 체크한 줄의 묶음 결과도 함께."""
    from .. import approval
    state, drafts, chosen, alone, together, _ = _drafts_checked(c, p)
    by_key = {ln.item.key: ln for ln in together.lines}
    rows = []
    for d in drafts:
        a, inst = alone[d.key], state.instruments.get(d.line.symbol)
        t = by_key.get(d.key)
        rows.append((d.key, True, d.line.symbol, inst.name if inst else "", inst.asset_type.value if inst else "",
                     d.line.side.value, "지정가" if d.line.limit_price else "시장가", d.line.qty, d.line.limit_price,
                     a.item.amount if a.item.ref_price else None, check_short(a.alone),
                     check_short(t.together) if t else NOT_PICKED, DRAFT, None,
                     "BASKET" if d.group else "SINGLE", None, None, d.group or None))
    for r in sorted(approval.requests(c.conn, account_id=c.account_id, from_date=c.today),
                    key=lambda r: r.key, reverse=True):
        for ln in r.lines:
            inst = state.instruments.get(ln.symbol)
            price = ln.limit_price or state.prices.get(ln.symbol)
            rows.append((f"{r.key}:{ln.line_no}", False, ln.symbol, inst.name if inst else "",
                         inst.asset_type.value if inst else "", ln.side.value, "지정가" if ln.limit_price else "시장가",
                         ln.qty, ln.limit_price, ln.qty * price if price else None, "", "보낸 요청",
                         _line_status(r, ln), r.request_no, r.order_type, ln.line_no, str(r.key), None))
    user, rights = _who(c)
    lock = _write_lock(c, user, rights)
    if not lock and not chosen:
        lock = "결재 요청할 줄을 체크하세요"
    elif not lock and len({x.group for x in chosen}) > 1:
        lock = "바스켓에서 온 줄은 바스켓 줄끼리 요청해요(요청 한 건이 돼요). 낱개 줄은 따로 체크하세요"
    elif not lock and not together.ok:
        lock = f"묶음 검증에 걸려요: {approval.describe(together)}"
    send = lock
    if not send:
        right = rights.get("ORDER_APPROVE")
        if right is None:
            send = f"{user}에게 주문 승인 권한이 없어요. 결재 요청만 할 수 있어요"
        elif not right.self_approve:
            send = f"{user}의 승인 권한에 셀프 승인 허용이 없어요. 결재 요청만 할 수 있어요"
        elif approval.over_limit(right, chosen):
            send = (f"{'바스켓 합계' if any(x.group for x in chosen) else '주문금액'} "
                    f"{approval.over_limit(right, chosen)[1]:,}원이 1건 승인 한도 {right.max_order_value:,}원을 넘어요. "
                    "결재 요청만 할 수 있어요")
    day = approval.order_day(c.calendar, dt.datetime.combine(c.today, dt.datetime.now().time()))
    return TxResult({"main": Dataset(ORDER_LINE_COLUMNS, tuple(rows))},
                    {"order_date": day.isoformat(), "user": user, "request_lock": lock, "send_lock": send})


def _order_set(c: TxContext, p) -> TxResult:
    """STP-1210 선택 묶음 검증: 체크한 줄 + 이미 잡힌 요청(결재 대기·승인 후 미전송)."""
    from .. import approval
    state, drafts, chosen, alone, together, _ = _drafts_checked(c, p)
    limits, weight = _limits_and_weight()
    rows = _set_rows(state, together, limits, weight, len(chosen))
    user, rights = _who(c)
    right = rights.get("ORDER_APPROVE")
    if right is None:
        who = (f"{user}: 승인 권한 없음", "요청만", None)
    elif not right.self_approve:
        who = (f"{user}: 셀프 승인 허용 없음 ({_limit_text(right)})", "요청만", None)
    else:
        over = approval.over_limit(right, chosen) is not None    # 요청 1건 단위: 바스켓은 합계 (C-13)
        who = (f"{user}: 셀프 승인 허용 ({_limit_text(right)})", "한도 초과, 요청만" if over else "셀프 승인 가능", None)
    basket = any(x.group for x in chosen)
    rows.append(("결재", f"{'바스켓 요청 1건' if basket else f'단건 요청 {len(chosen)}건'}, {sum(x.amount for x in chosen):,}원",
                 who[0], who[1] if chosen else NOT_PICKED, who[2]))
    return _set_result(together, rows, len(chosen))


HISTORY_COLUMNS = ("at", "action", "actor", "self_approved", "line_no", "client_id", "reason")


def _approval_history(c: TxContext, p) -> TxResult:
    """요청 하나의 결재 이력(요청 단위)과 전송 이력(줄 단위), 시각 순. 그때의 묶음 검증 결과는 DB에 남아 있어요."""
    from .. import approval
    if not p.get("request_key"):
        return TxResult({"main": Dataset(HISTORY_COLUMNS, ())})
    k = approval.parse_key(p["request_key"], c.today)
    rows = [(at, approval.ACTIONS.get(a, a), actor, "셀프 승인" if self_ok else None, None, None, reason, 0, i)
            for i, at, a, actor, self_ok, reason in c.conn.execute(
                "select id, at, action, actor, self_approved, reason from order_approvals"
                " where trade_date = %s and request_no = %s", (k.trade_date, k.request_no))]
    rows += [(at, approval.SEND_OUTCOMES.get(o, o), "run", None, line_no, cid, reason, 1, i)
             for i, at, o, line_no, cid, reason in c.conn.execute(
                 "select id, at, outcome, line_no, client_id, reason from order_sends"
                 " where trade_date = %s and request_no = %s", (k.trade_date, k.request_no))]
    rows.sort(key=lambda r: (r[0], r[7], r[8]))
    return TxResult({"main": Dataset(HISTORY_COLUMNS, tuple(r[:7] for r in rows))})


REQUEST_FILTERS = ("PENDING", "APPROVED", "REJECTED", "CANCELED", "SENT", "SEND_FAILED", "EXPIRED")


def _approvals_checked(c: TxContext, p):
    """STP-5100: (상태, 보이는 요청, 체크한 요청, 요청마다 혼자 본 사유, 체크한 결재 대기 요청의 묶음 결과)."""
    from .. import approval
    from ..precheck import check_set
    limits, weight = _limits_and_weight()
    status = p.get("status")
    if status and status not in REQUEST_FILTERS:
        raise TxError(f"상태는 {', '.join(REQUEST_FILTERS)} 가운데 하나예요")
    reqs = approval.requests(c.conn, account_id=c.account_id, trade_date=p.get("trade_date"),
                             from_date=None if p.get("trade_date") else c.today,
                             statuses=(status,) if status else None)
    picked = set(picked_keys(p.get("picked")))
    state = approval.account_state(c.books, c.today)
    approved = approval.requests(c.conn, account_id=c.account_id, from_date=c.today, statuses=("APPROVED",))
    committed = _committed(state, approved, skip=picked)
    alone = {}
    for r in reqs:
        if r.status == "PENDING":
            res = check_set(state, [approval._from_line(state, r, ln) for ln in r.lines], committed, limits, weight)
            alone[str(r.key)] = _issues_of(res, f"{r.key}:")
    keys = approval.parse_keys(sorted(picked), c.today) if picked else []
    chosen_reqs = [r for r in approval.requests(c.conn, account_id=c.account_id, keys=keys)
                   if r.status in approval.LIVE] if keys else []
    chosen = [approval._from_line(state, r, ln) for r in chosen_reqs if r.status == "PENDING" for ln in r.lines]
    together = check_set(state, chosen, committed, limits, weight)
    return state, reqs, chosen_reqs, alone, together


def _request_amount(state, r) -> int | None:
    prices = [(ln.limit_price or state.prices.get(ln.symbol)) for ln in r.lines]
    return None if None in prices else sum(ln.qty * pr for ln, pr in zip(r.lines, prices))


def _approval_requests(c: TxContext, p) -> TxResult:
    """STP-5100 결재할 요청: 요청 한 건이 한 행(바스켓도 한 행). 체크한 요청을 승인하면 어떻게 되는지도."""
    from .. import approval
    state, reqs, chosen_reqs, alone, together = _approvals_checked(c, p)
    picked_pending = {str(r.key) for r in chosen_reqs if r.status == "PENDING"}
    rows = []
    for r in reqs:
        k = str(r.key)
        first = r.lines[0]
        inst = state.instruments.get(first.symbol)
        name = (inst.name if inst else first.symbol) + (f" 외 {len(r.lines) - 1}종목" if len(r.lines) > 1 else "")
        sides = {ln.side.value for ln in r.lines}
        rows.append((k, r.status in approval.LIVE, r.trade_date, r.request_no, r.order_type, r.created_by,
                     first.symbol, name, inst.asset_type.value if inst else "", sides.pop() if len(sides) == 1 else None,
                     len(r.lines), _request_amount(state, r),
                     check_short(alone[k]) if k in alone else "",
                     check_short(_issues_of(together, f"{k}:")) if k in picked_pending else
                     (NOT_PICKED if r.status == "PENDING" else ""), r.status, r.approved_by, r.last_reason))
    user, rights = _who(c)
    right = rights.get("ORDER_APPROVE")
    pending = [r for r in chosen_reqs if r.status == "PENDING"]
    lock = "" if right else f"{user}에게 이 계좌의 주문 승인 권한이 없어요"
    reject = lock
    if not lock:
        if not pending:
            lock = reject = "결재 대기 요청을 체크하세요"
        elif len(pending) < len(chosen_reqs):
            lock = reject = "결재 대기가 아닌 요청이 섞여 있어요 (승인·반려는 결재 대기만)"
        elif any(r.created_by == user for r in pending) and not right.self_approve:
            lock = f"본인이 쓴 요청이 있어요. {user}의 권한에 셀프 승인 허용이 없어 다른 승인자가 승인해야 해요"
        elif approval.over_limit(right, [x.item for x in together.lines]):
            key, amount = approval.over_limit(right, [x.item for x in together.lines])
            lock = f"요청 {key.split(':')[0].rsplit('-', 1)[-1]}의 금액 {amount:,}원이 {_limit_text(right)}을 넘어요"
        elif not together.ok:
            lock = f"묶음 검증에 걸려요: {approval.describe(together)}"
    cancel = "" if chosen_reqs else "취소할 요청을 체크하세요"
    if not cancel and any(r.created_by != user for r in chosen_reqs):
        cancel = f"작성자만 요청을 취소할 수 있어요 ({user}가 쓰지 않은 요청이 있어요)"
    elif not cancel and any(len(r.unsent) < len(r.lines) for r in chosen_reqs):
        cancel = "이미 나간 줄이 있는 요청은 거둘 수 없어요 (나간 주문은 주문 취소로)"
    return TxResult({"main": Dataset(("request_key", "pickable", "trade_date", "request_no", "order_type", "created_by",
                                      "symbol", "instrument_name", "asset_type", "side", "line_count", "amount",
                                      "alone", "together", "status", "approved_by", "last_reason"), tuple(rows))},
                    {"user": user, "approve_lock": lock, "reject_lock": reject, "cancel_lock": cancel})


def _answer(ln) -> str:
    """증권사 답 (R-103): 접수, 거부, 결과 미확인(보냈는데 답이 없음), 아직 안 보냈으면 빈칸."""
    if ln.awaiting_ack:
        return "결과 미확인"
    return {"ACCEPTED": "접수", "REJECTED": "거부"}.get(ln.ack_outcome or "", "")


def _request_lines(c: TxContext, p) -> TxResult:
    """STP-5100 요청의 줄: 고른 요청의 종목 줄과 줄마다 전송 결과."""
    from .. import approval
    cols = ("line_no", "symbol", "instrument_name", "asset_type", "side", "kind", "qty", "limit_price", "amount",
            "send_outcome", "client_id", "send_reason", "broker_answer", "order_no")
    if not p.get("request_key"):
        return TxResult({"main": Dataset(cols, ())})
    k = approval.parse_key(p["request_key"], c.today)
    found = approval.requests(c.conn, account_id=c.account_id, keys=[k])
    state = approval.account_state(c.books, c.today) if found else None
    rows = []
    for r in found:
        for ln in r.lines:
            inst = state.instruments.get(ln.symbol)
            price = ln.limit_price or state.prices.get(ln.symbol)
            rows.append((ln.line_no, ln.symbol, inst.name if inst else "", inst.asset_type.value if inst else "",
                         ln.side.value, "지정가" if ln.limit_price else "시장가", ln.qty, ln.limit_price,
                         ln.qty * price if price else None, ln.send_outcome, ln.client_id,
                         ln.send_reason or (ln.ack_detail if ln.ack_outcome == "REJECTED" else None),
                         _answer(ln), ln.order_no))
    return TxResult({"main": Dataset(cols, tuple(rows))})


def _approval_set(c: TxContext, p) -> TxResult:
    """STP-5100 선택 묶음 검증: 체크한 결재 대기 요청 + 이미 승인했지만 아직 안 나간 요청. 내 승인 한도도."""
    from .. import approval
    state, reqs, chosen_reqs, alone, together = _approvals_checked(c, p)
    limits, weight = _limits_and_weight()
    n = len([r for r in chosen_reqs if r.status == "PENDING"])
    rows = _set_rows(state, together, limits, weight, n)
    user, rights = _who(c)
    right = rights.get("ORDER_APPROVE")
    biggest = max(approval.request_amounts([ln.item for ln in together.lines]).values(), default=0)
    if right is None:
        rows.append(("내 승인 한도", f"{user}: 승인 권한 없음", "", "권한 없음" if n else NOT_PICKED, False if n else None))
    else:
        over = right.max_order_value is not None and biggest > right.max_order_value
        mine = sum(1 for r in chosen_reqs if r.created_by == user)
        rows.append(("내 승인 한도", f"요청 1건 최대 {biggest:,}원",
                     f"{user}: {_limit_text(right)}, 셀프 승인 {'허용' if right.self_approve else '안 됨'}"
                     + (f" (본인 요청 {mine}건)" if mine else ""),
                     NOT_PICKED if not n else ("초과" if over else ("셀프 승인 안 됨" if mine and not right.self_approve
                                                                     else "한도 안")),
                     None if not n else not over and not (mine and not right.self_approve)))
    return _set_result(together, rows, n)


def _app_users(c: TxContext, p) -> TxResult:
    """STP-5200 사용자: 등록한 사용자와 오늘 적용되는 권한 요약 (모든 계좌 기준, 계좌별 권한은 오른쪽 이력에)."""
    from .. import approval
    user, _ = _who(c)
    rows = []
    for uid, name, by, at in c.conn.execute(
            "select user_id, name, created_by, created_at from app_users order by user_id").fetchall():
        rights = c.conn.execute(
            "select right_code, account_id, max_order_value, self_approve from user_rights_v"
            " where user_id = %s order by right_code, account_id nulls first", (uid,)).fetchall()
        said = []
        for code, acct, limit, self_ok in rights:
            extra = ""
            if code == "ORDER_APPROVE":
                extra = f" {'한도 없음' if limit is None else f'{limit:,}원'}{', 셀프' if self_ok else ''}"
            said.append(f"{approval.RIGHTS[code]}{extra}{f' ({acct})' if acct else ''}")
        rows.append((uid, name, " · ".join(said) or "권한 없음", by, at, uid == user))
    return TxResult({"main": Dataset(("user_id", "name", "rights", "created_by", "created_at", "me"), tuple(rows))},
                    {"user": user, "admin_lock": _admin_lock(c, user), "selected": user})


def _admin_lock(c: TxContext, user: str) -> str:
    from .. import approval
    if not approval.has_users(c.conn):
        return ""      # 첫 사용자는 누구나 등록하고, 자기에게 권한 관리를 줘요
    admins = c.conn.execute("select count(*) from user_rights_v where right_code = 'RIGHTS_ADMIN'").fetchone()[0]
    if not admins:
        return ""
    if "RIGHTS_ADMIN" not in approval.rights_of(c.conn, user, None, c.today):
        return f"{user}에게 권한 관리 권한이 없어요"
    return ""


def _user_rights(c: TxContext, p) -> TxResult:
    """STP-5200 권한 이력: 고치지 않고 쌓인 부여·회수 행. 지금 적용되는 행과 예정인 행을 표시해요."""
    if not p.get("user_id"):
        raise TxError("사용자 ID가 필요해요")
    user, _ = _who(c)
    rows = c.conn.execute("""
        select r.right_row_id, r.right_code, r.account_id, r.granted, r.max_order_value, r.self_approve,
               r.effective_from, r.granted_by, r.reason, r.created_at,
               case when v.right_row_id is not null then '적용 중'
                    when r.effective_from > %s then '예정' else '지난 행' end
          from user_rights r left join user_rights_v v using (right_row_id)
         where r.user_id = %s order by r.right_row_id desc""", (c.today, p["user_id"])).fetchall()
    rows = tuple((rid, code, acct or "모든 계좌", "부여" if granted else "회수", limit, "허용" if self_ok else "", since, by,
                  reason, at,
                  state) for rid, code, acct, granted, limit, self_ok, since, by, reason, at, state in rows)
    return TxResult({"main": Dataset(("right_row_id", "right_code", "account_id", "granted", "max_order_value",
                                      "self_approve", "effective_from", "granted_by", "reason", "created_at",
                                      "state"), rows)},
                    {"admin_lock": _admin_lock(c, user)})


TRANSACTIONS: dict[str, Tx] = {t.tx_id: t for t in [
    Tx("STP1100R01", "종목별 보유", {"day": "date"}, _holdings),
    Tx("STP1100R02", "예수금·주문가능금액", {}, _cash),
    Tx("STP1300R01", "종목별 주문가능", {}, _capacities),
    Tx("STP1300R02", "주문가능 판단", {"symbol": "text", "side": "text", "qty": "int", "limit_price": "int"},
       _order_check),
    Tx("STP1310R01", "바스켓 (현재 편입비)", {}, _basket_current),
    Tx("STP1310R02", "종목별 주문안", {"amount": "int", "side": "text", "basis": "text", "limit": "int",
                                   "basket": "text"}, _basket_plan),
    Tx("STP1200R01", "주문·체결 블로터", {"day": "date"}, _blotter),
    Tx("STP1200R02", "주문의 접수와 체결별 결제, 나간 요청과 그 줄", {"client_id": "text"}, _order),
    Tx("STP2100R01", "대사 결과", {"date_from": "date", "date_to": "date", "kind": "text", "only_breaks": "bool"},
       _recon),
    Tx("STP2100R02", "날짜별 대사 요약", {"date_from": "date", "date_to": "date", "kind": "text"}, _recon_days),
    Tx("STP2110R01", "원장 점검 항목", {}, _integrity),
    Tx("STP2110R02", "점검 항목의 판정과 값 비교", {"title": "text"}, _integrity_rows),
    Tx("STP2200R01", "순자산 추이", {"date_from": "date", "date_to": "date"}, _nav),
    Tx("STP2210R01", "순자산 이력", {"date_from": "date", "date_to": "date"}, _nav_changes),
    Tx("STP2210R02", "기준일의 변동 상세", {"date": "date"}, _nav_bridge),
    Tx("STP2300R01", "종목별 기간 시세", {"date_from": "date", "date_to": "date", "period": "text"}, _price_symbols),
    Tx("STP2300R02", "종목의 기간별 종가", {"symbol": "text", "date_from": "date", "date_to": "date"}, _price_rows),
    Tx("STP2400R01", "영업일별 마감 점검 요약", {"date_from": "date", "date_to": "date"}, _close_days),
    Tx("STP2400R02", "영업일의 작업 단계별 상태", {"date": "date"}, _close_steps),
    Tx("STP6100R01", "월별 수익률", {"date_from": "date", "date_to": "date", "period": "text"}, _returns),
    Tx("STP6100R02", "월의 일별 수익률", {"month": "text", "date_from": "date", "date_to": "date"}, _return_days),
    Tx("STP3100R01", "원장 이벤트 목록", {"date_from": "date", "date_to": "date", "event_type": "text",
                                       "symbol": "text", "seq": "int"}, _events),
    Tx("STP3100R02", "이벤트의 보유·예수금 변동", {"seq": "int"}, _trace),
    Tx("STP3200R01", "분개를 만든 이벤트 목록", {"date_from": "date", "date_to": "date", "event_type": "text",
                                            "symbol": "text", "seq": "int"}, _journal_events),
    Tx("STP3200R02", "이벤트의 분개", {"seq": "int"}, _journal),
    Tx("STP3300R01", "시산표", {"day": "date"}, _trial),
    Tx("STP4500R01", "분개 모형", {}, _journal_templates),
    Tx("STP4500R02", "분개 모형의 줄", {"code": "text"}, _journal_template_lines),
    Tx("STP3400R01", "계좌 요약", {}, _account),
    Tx("STP3400R02", "예수금 D+n", {}, _ladder),
    Tx("STP3400R03", "결제 예정", {}, _settlements),
    Tx("STP4100R01", "종목 목록 (시장 전 종목과 등록 종목)", {"date": "date", "market": "text", "sec_group": "text",
                                                   "state": "text", "keyword": "text", "registered_only": "bool"},
       _market),
    Tx("STP4100R02", "기준일의 종목 속성", {"symbol": "text", "date": "date"}, _market_one),
    Tx("STP4100R03", "종목 속성 변경 이력", {"symbol": "text", "date": "date"}, _market_history),
    Tx("STP4100R04", "종목에 적용되는 거래 기준", {"symbol": "text", "date": "date"}, _instrument_rules),
    Tx("STP4100R05", "등록 종목 (종목 찾기)", {}, _instruments),
    Tx("STP4100R06", "기준일의 종목 속성 (두 줄 양식)", {"symbol": "text", "date": "date"}, _market_form),
    Tx("STP4101R01", "마스터 적재 기록", {"date_from": "date", "date_to": "date", "load_id": "int"}, _master_loads),
    Tx("STP4101R02", "적재에서 바뀐 종목", {"load_id": "int", "change": "text"}, _master_changes),
    Tx("STP4300R01", "수수료·세금", {}, _fees),
    Tx("STP4300R02", "결제주기 규칙", {}, _settlement_rules),
    Tx("STP4300R03", "호가단위", {}, _ticks),
    Tx("STP4300R04", "휴장일", {}, _holidays),
    Tx("STP4300R05", "주문 검증 기준", {}, _risk),
    Tx("STP4400R01", "계좌 목록", {}, _accounts),
    Tx("STP4400R02", "계좌 상태 이력", {"account_id": "text"}, _account_status),
    Tx("STP1400R01", "법인 목록", {}, _companies),
    Tx("STP1400R02", "법인 보유 합산", {"company_id": "text"}, _company_positions),
    Tx("STP1400R03", "법인의 계좌", {"company_id": "text"}, _company_accounts),
    Tx("STP1400R04", "법인 ERP 회사코드 이력", {"company_id": "text"}, _company_erp_codes),
    Tx("STP4600R01", "코드 그룹 목록", {}, _code_groups),
    Tx("STP4600R02", "그룹의 코드", {"group_id": "text"}, _codes),
    Tx("STP1210R01", "주문 작성과 보낸 요청", {"lines": "text", "picked": "text"}, _order_lines),
    Tx("STP1210R02", "선택 묶음 검증", {"lines": "text", "picked": "text"}, _order_set),
    Tx("STP1210R03", "결재·전송 이력", {"request_key": "text"}, _approval_history),
    Tx("STP5100R01", "결재할 요청", {"status": "text", "trade_date": "date", "picked": "text"}, _approval_requests),
    Tx("STP5100R02", "선택 묶음 검증 (승인 기준)", {"status": "text", "trade_date": "date", "picked": "text"},
       _approval_set),
    Tx("STP5100R03", "결재·전송 이력", {"request_key": "text"}, _approval_history),
    Tx("STP5100R04", "요청의 줄", {"request_key": "text"}, _request_lines),
    Tx("STP5200R01", "사용자", {}, _app_users),
    Tx("STP5200R02", "권한 이력", {"user_id": "text"}, _user_rights),
]}


def run_tx(tx_id: str, ctx: TxContext, raw: dict) -> TxResult:
    tx = TRANSACTIONS.get(tx_id)
    if tx is None:
        raise TxError(f"없는 트랜잭션: {tx_id}")
    unknown = set(raw) - set(tx.params)
    if unknown:
        raise TxError(f"{tx_id}에 없는 조회조건: {', '.join(sorted(unknown))}")
    params = {}
    for name, kind in tx.params.items():
        v = raw.get(name)
        if v in (None, ""):
            continue
        try:
            params[name] = COERCE[kind](v)
        except (TypeError, ValueError) as e:
            raise TxError(f"{name} 값이 올바르지 않아요: {v}") from e
    return tx.run(ctx, params)


def _example_params(screen, grid, rows: dict[str, list[dict]]) -> dict:
    """Values for required conditions: the default, else the same-named column of a loaded grid, else 1."""
    out = {}
    tx = TRANSACTIONS[grid.tx]
    for c in screen.conditions:
        if not c.required or c.name not in tx.params:
            continue
        seen = next((r[0][c.name] for r in rows.values() if r and c.name in r[0]), None)
        out[c.name] = c.default or seen or (1 if tx.params[c.name] == "int" else None)
    return out


def output_problems(screens, ctx: TxContext, lookups=None) -> list[str]:
    """Run every grid's transaction and check that the dataset it names has every column the grid shows.

    The static validator checks definitions against the schema; this checks them
    against what the services really return. Master grids are run with the first
    row of their master grid, so a database with some data is needed.
    """
    errs, cache = [], {}
    from .definitions import STEP_STATES
    for s in screens.values():
        rows: dict[str, list[dict]] = {}
        # input grids after the rest (they borrow values), details last (a list of requests can be an input grid)
        for g in sorted(s.grids, key=lambda g: {"input": 1, "master": 2}.get(g.load, 0)):
            params = {}
            if g.load == "input":
                params = _example_params(s, g, rows)
            if g.load == "master":
                first = (rows.get(g.master) or [None])[0]
                if first is None:
                    errs.append(f"{s.id} {g.id}: 마스터 {g.master}에 행이 없어 확인하지 못했어요")
                    continue
                params = {p: first[c] for p, c in g.link.items()}
            key = (g.tx, tuple(sorted(params.items())))
            if key not in cache:
                cache[key] = run_tx(g.tx, ctx, params)
            res = cache[key]
            ds = res.datasets.get(g.dataset)
            if ds is None:
                errs.append(f"{s.id} {g.id}: {g.tx}에 데이터셋 {g.dataset}이 없어요")
                continue
            missing = [c.name for c in g.columns if c.name not in ds.columns]
            if missing:
                errs.append(f"{s.id} {g.id}: {g.tx}.{g.dataset}에 없는 열 {', '.join(missing)}")
            for col in g.columns:   # a 추세선 draws a run of numbers, nothing else
                if col.display == "spark" and col.name in ds.columns:
                    i = ds.columns.index(col.name)
                    if any(not isinstance(r[i], (list, tuple)) for r in ds.rows):
                        errs.append(f"{s.id} {g.id}.{col.name}: 추세선 열의 값이 숫자열이 아니에요")
                if col.display == "steps" and col.name in ds.columns:   # 진행 단계: one known state a step
                    i = ds.columns.index(col.name)
                    if any(not isinstance(r[i], (list, tuple)) or len(r[i]) != len(col.steps)
                           or not set(r[i]) <= STEP_STATES for r in ds.rows):
                        errs.append(f"{s.id} {g.id}.{col.name}: 진행 단계 열의 값이 단계 수만큼의 상태 목록이 아니에요")
            for c in g.link.values():
                if c not in (rows.get(g.master) or [{}])[0]:
                    errs.append(f"{s.id} {g.id}: 마스터 {g.master}에 연결 열 {c}가 없어요")
            for k in g.meta:
                if k not in res.meta:
                    errs.append(f"{s.id} {g.id}: {g.tx}가 주지 않는 상태값 {k}")
            rows[g.id] = ds.records()
    used = [(x.lookup, x.lookup_params) for s in screens.values()
            for x in [*s.conditions, *(f for g in s.grids for b in g.actions for f in b.fields)] if x.lookup]
    if used and lookups is None:
        from .definitions import load_lookups
        lookups = load_lookups()
    for name, params in used:   # 찾기 팝업: the list really has the columns the popup shows
        lk = lookups.get(name)
        if lk is None:
            errs.append(f"찾기 {name}: 정의가 없어 확인하지 못했어요")
            continue
        key = (lk.tx, tuple(sorted(params.items())))
        if key not in cache:
            cache[key] = run_tx(lk.tx, ctx, dict(params))
        ds = cache[key].datasets.get("main")
        missing = [c.name for c in lk.columns if ds is None or c.name not in ds.columns]
        if missing:
            errs.append(f"찾기 {name}: {lk.tx}에 없는 열 {', '.join(missing)}")
    return errs
