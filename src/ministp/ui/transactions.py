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
    return TxResult({"main": view}, {"basis": q.valuation_basis(c.books.ledger, symbols, quotes, now)})


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
    return TxResult({"order": d.order, "fills": d.fills})


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


def _price_symbols(c: TxContext, p) -> TxResult:
    from ..pricecheck import BAD, WARN, PriceRules
    ds = q.price_trend_symbols(c.books.ledger, p.get("date_from"), p.get("date_to"))
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
    ds = q.return_months(c.books.ledger, p.get("date_from"), p.get("date_to"))
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
    ds, balanced = q.trial_balance(c.conn, c.account_id)
    return TxResult({"main": ds}, {"balanced": balanced})


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
                   not ln.blocked, issue_text(ln.issues)) for ln in r.lines)
    summary = ((r.total_amount, r.allocated, r.leftover, r.commission + r.taxes, r.required_cash, r.available_cash,
                r.ok, issue_text(r.issues) if r.issues else ("결재로 넘길 수 있어요" if r.ok else "검사에 걸린 종목이 있어요")),)
    return TxResult({"main": Dataset(("symbol", "instrument_name", "asset_type", "order_price", "tick_text",
                                      "order_kind", "target_amount", "qty", "amount", "actual_weight", "weight_gap",
                                      "costs", "post_weight", "ok", "check_text"), lines),
                     "summary": Dataset(("total_amount", "allocated", "leftover", "costs", "required_cash",
                                         "available_cash", "ok", "verdict"), summary)},
                    {"basis": pf.price_basis or "가격 없음", "ok": r.ok, "limit": f"{r.weight_limit}%"})


def _risk(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.risk_rules()})


def _accounts(c: TxContext, p) -> TxResult:
    """Every account, whichever one the screens are looking at."""
    return TxResult({"main": q.account_list(c.conn)}, {"selected": c.account_id})


def _account_status(c: TxContext, p) -> TxResult:
    if not p.get("account_id"):
        raise TxError("계좌 ID가 필요해요")
    return TxResult({"main": q.account_status_history(c.conn, p["account_id"])})


def _customers(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.customer_list(c.conn)})


def _customer_positions(c: TxContext, p) -> TxResult:
    """One customer's accounts added up per symbol (all customers when none is named)."""
    return TxResult({"main": q.customer_positions(c.conn, p.get("customer_id") or None)})


def _customer_accounts(c: TxContext, p) -> TxResult:
    if not p.get("customer_id"):
        raise TxError("고객 ID가 필요해요")
    return TxResult({"main": q.customer_accounts(c.conn, p["customer_id"])})


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


TRANSACTIONS: dict[str, Tx] = {t.tx_id: t for t in [
    Tx("STP1100R01", "종목별 보유", {"day": "date"}, _holdings),
    Tx("STP1100R02", "예수금·주문가능금액", {}, _cash),
    Tx("STP1300R01", "종목별 주문가능", {}, _capacities),
    Tx("STP1300R02", "주문가능 판단", {"symbol": "text", "side": "text", "qty": "int", "limit_price": "int"},
       _order_check),
    Tx("STP1500R01", "바스켓 (현재 편입비)", {}, _basket_current),
    Tx("STP1500R02", "종목별 주문안", {"amount": "int", "side": "text", "basis": "text", "limit": "int",
                                   "basket": "text"}, _basket_plan),
    Tx("STP1200R01", "주문·체결 블로터", {"day": "date"}, _blotter),
    Tx("STP1200R02", "주문의 접수와 체결별 결제", {"client_id": "text"}, _order),
    Tx("STP2100R01", "대사 결과", {"date_from": "date", "date_to": "date", "kind": "text", "only_breaks": "bool"},
       _recon),
    Tx("STP2100R02", "날짜별 대사 요약", {"date_from": "date", "date_to": "date", "kind": "text"}, _recon_days),
    Tx("STP2110R01", "원장 점검 항목", {}, _integrity),
    Tx("STP2110R02", "점검 항목의 판정과 값 비교", {"title": "text"}, _integrity_rows),
    Tx("STP2200R01", "기준가 추이", {"date_from": "date", "date_to": "date"}, _nav),
    Tx("STP2210R01", "기준가 이력", {"date_from": "date", "date_to": "date"}, _nav_changes),
    Tx("STP2210R02", "기준일의 변동 상세", {"date": "date"}, _nav_bridge),
    Tx("STP2300R01", "종목별 기간 시세", {"date_from": "date", "date_to": "date"}, _price_symbols),
    Tx("STP2300R02", "종목의 기간별 종가", {"symbol": "text", "date_from": "date", "date_to": "date"}, _price_rows),
    Tx("STP2400R01", "영업일별 마감 점검 요약", {"date_from": "date", "date_to": "date"}, _close_days),
    Tx("STP2400R02", "영업일의 작업 단계별 상태", {"date": "date"}, _close_steps),
    Tx("STP6100R01", "월별 수익률", {"date_from": "date", "date_to": "date"}, _returns),
    Tx("STP6100R02", "월의 일별 수익률", {"month": "text", "date_from": "date", "date_to": "date"}, _return_days),
    Tx("STP3100R01", "원장 이벤트 목록", {"date_from": "date", "date_to": "date", "event_type": "text",
                                       "symbol": "text", "seq": "int"}, _events),
    Tx("STP3100R02", "이벤트의 보유·예수금 변동", {"seq": "int"}, _trace),
    Tx("STP3200R01", "분개를 만든 이벤트 목록", {"date_from": "date", "date_to": "date", "event_type": "text",
                                            "symbol": "text", "seq": "int"}, _journal_events),
    Tx("STP3200R02", "이벤트의 분개", {"seq": "int"}, _journal),
    Tx("STP3300R01", "시산표", {}, _trial),
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
    Tx("STP1400R01", "고객 목록", {}, _customers),
    Tx("STP1400R02", "고객 보유 합산", {"customer_id": "text"}, _customer_positions),
    Tx("STP1400R03", "고객의 계좌", {"customer_id": "text"}, _customer_accounts),
    Tx("STP4600R01", "코드 그룹 목록", {}, _code_groups),
    Tx("STP4600R02", "그룹의 코드", {"group_id": "text"}, _codes),
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
        for g in sorted(s.grids, key=lambda g: g.load == "input"):  # input grids last: they borrow values
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
