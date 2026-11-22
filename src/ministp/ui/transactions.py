"""Read transactions by id. Every one only reads; the DB session is read-only too."""
import datetime as dt
from dataclasses import dataclass, field
from functools import cached_property

import psycopg

from .. import queries as q
from ..calendar import BusinessCalendar
from ..fees import FeeSchedule
from ..ops import Books, clean_streak, load_books, stored_open_orders
from ..queries import Dataset


class TxError(ValueError):
    pass


@dataclass
class TxContext:
    conn: psycopg.Connection
    calendar: BusinessCalendar
    fees: FeeSchedule
    today: dt.date

    @cached_property
    def books(self) -> Books:
        return load_books(self.conn, self.calendar, self.fees)


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
    return TxResult({"main": q.holdings(c.conn, p.get("day") or c.today)})


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
    return TxResult({"main": q.order_blotter(c.conn, p.get("day") or c.today)})


def _recon(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.recon_history(c.conn, p.get("date_from"), p.get("date_to"),
                                             p.get("kind") or "DAILY_RECON", bool(p.get("only_breaks")))})


def _nav(c: TxContext, p) -> TxResult:
    ds = q.nav_series(c.books.ledger, p.get("date_from"), p.get("date_to"))
    last = max((r.date for r in c.books.ledger.nav_history()), default=None)
    return TxResult({"main": ds}, {"clean_streak": clean_streak(c.conn, c.calendar, last) if last else 0,
                                   "gate_days": 20})


def _events(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.ledger_events(c.conn, p.get("date_from"), p.get("date_to"),
                                             p.get("event_type"), p.get("symbol"))})


def _trace(c: TxContext, p) -> TxResult:
    if p.get("seq") is None:
        raise TxError("seq가 필요해요")
    try:
        t = q.event_trace(c.conn, p["seq"])
    except KeyError as e:
        raise TxError(str(e)) from e
    return TxResult({"event": t.event, "journal": t.journal, "positions": t.positions, "cash": t.cash},
                    {"balanced": t.balanced})


def _trial(c: TxContext, p) -> TxResult:
    ds, balanced = q.trial_balance(c.conn)
    return TxResult({"main": ds}, {"balanced": balanced})


def _instruments(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.instrument_master(c.books)})


def _instrument_events(c: TxContext, p) -> TxResult:
    if not p.get("symbol"):
        raise TxError("종목코드가 필요해요")
    return TxResult({"main": q.instrument_events(c.conn, p["symbol"])})


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
    return TxResult({"main": ds}, {"years": ", ".join(str(y) for y in years),
                                   "until": f"{years[-1]}-12-31" if years else ""})


def _risk(c: TxContext, p) -> TxResult:
    return TxResult({"main": q.risk_rules()})


TRANSACTIONS: dict[str, Tx] = {t.tx_id: t for t in [
    Tx("STP1100R01", "종목별 보유", {"day": "date"}, _holdings),
    Tx("STP1100R02", "예수금·주문가능금액", {}, _cash),
    Tx("STP1300R01", "종목별 주문가능", {}, _capacities),
    Tx("STP1300R02", "주문가능 판단", {"symbol": "text", "side": "text", "qty": "int", "limit_price": "int"},
       _order_check),
    Tx("STP1200R01", "주문·체결 블로터", {"day": "date"}, _blotter),
    Tx("STP2100R01", "대사 결과", {"date_from": "date", "date_to": "date", "kind": "text", "only_breaks": "bool"},
       _recon),
    Tx("STP2200R01", "기준가 추이", {"date_from": "date", "date_to": "date"}, _nav),
    Tx("STP3100R01", "원장 이벤트 목록", {"date_from": "date", "date_to": "date", "event_type": "text",
                                       "symbol": "text"}, _events),
    Tx("STP3100R02", "이벤트 추적", {"seq": "int"}, _trace),
    Tx("STP3100R03", "시산표", {}, _trial),
    Tx("STP4100R01", "종목정보", {}, _instruments),
    Tx("STP4100R02", "종목 원장 이벤트", {"symbol": "text"}, _instrument_events),
    Tx("STP4200R01", "계좌 요약", {}, _account),
    Tx("STP4200R02", "예수금 D+n", {}, _ladder),
    Tx("STP4200R03", "결제 예정", {}, _settlements),
    Tx("STP4300R01", "수수료·세금", {}, _fees),
    Tx("STP4300R02", "결제주기 규칙", {}, _settlement_rules),
    Tx("STP4300R03", "호가단위", {}, _ticks),
    Tx("STP4300R04", "휴장일", {}, _holidays),
    Tx("STP4300R05", "주문 검증 기준", {}, _risk),
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


def output_problems(screens, ctx: TxContext) -> list[str]:
    """Run every grid's transaction and check that the dataset it names has every column the grid shows.

    The static validator checks definitions against the schema; this checks them
    against what the services really return. Master grids are run with the first
    row of their master grid, so a database with some data is needed.
    """
    errs, cache = [], {}
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
            for c in g.link.values():
                if c not in (rows.get(g.master) or [{}])[0]:
                    errs.append(f"{s.id} {g.id}: 마스터 {g.master}에 연결 열 {c}가 없어요")
            for k in g.meta:
                if k not in res.meta:
                    errs.append(f"{s.id} {g.id}: {g.tx}가 주지 않는 상태값 {k}")
            rows[g.id] = ds.records()
    return errs
