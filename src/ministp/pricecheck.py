"""시세 점검: are the closing prices the books were valued at believable?

Valuation and NAV take the day's closes as they come from the broker. A wrong
close makes a wrong NAV that still reconciles, because both sides of the
reconciliation are holdings and cash, not prices. These checks look at the
prices themselves, using only what the ledger already recorded (the closes in
RevaluationEvent and NavEvent, the executions of the day). Nothing is fetched
and nothing is written.

The checks say whether a close can be a close. They do not say why a price
moved: that needs market and corporate-event data the ledger does not hold.
STP-2300 shows the closes as a trend over a period with these results beside
them, and the close board (STP-2400) takes its 시세 step from them.

The thresholds are a first proposal (PriceRules), shown on the screen next to
the results so nobody has to guess why a row was flagged.
"""
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from .ledger import ExecutionEvent, Ledger, NavEvent, RevaluationEvent
from .ticks import tick_size

OK, WARN, BAD = "정상", "주의", "확인 필요"
_RANK = {OK: 0, WARN: 1, BAD: 2}


@dataclass(frozen=True)
class PriceRules:
    warn_change: Decimal = Decimal("0.15")     # 전일 종가 대비 이보다 크게 움직이면 주의
    limit_change: Decimal = Decimal("0.30")    # 일반 종목의 가격제한폭: 넘으면 권리 조정이나 종가 오류를 확인
    warn_exec_gap: Decimal = Decimal("0.10")   # 그날 체결단가와 종가가 이보다 크게 벌어지면 주의


@dataclass(frozen=True)
class PriceCheck:
    date: dt.date
    symbol: str
    close: int
    prev_date: dt.date | None      # the day of the close it is compared with (None: first close of the symbol)
    prev_close: int | None
    change: Decimal | None         # close / prev_close - 1
    exec_gap: Decimal | None       # the day's execution price furthest from the close, as price / close - 1
    level: str
    reasons: tuple[str, ...]


def _pct(rate: Decimal) -> str:
    return f"{(rate * 100).normalize():f}%"


def closes_by_day(ledger: Ledger) -> dict[dt.date, tuple[dict[str, int], dict[str, int]]]:
    """date -> (closes the valuation took, closes the NAV took); the last event of each kind wins."""
    days: dict[dt.date, list[dict[str, int]]] = {}
    for ev in ledger.events:
        if isinstance(ev, RevaluationEvent):
            days.setdefault(ev.date, [{}, {}])[0] = dict(ev.closes)
        elif isinstance(ev, NavEvent):
            days.setdefault(ev.date, [{}, {}])[1] = dict(ev.closes)
    return {d: (v[0], v[1]) for d, v in sorted(days.items())}


def price_checks(ledger: Ledger, rules: PriceRules = PriceRules()) -> list[PriceCheck]:
    """One row for every close the books took, oldest day first, symbols in order."""
    fills: dict[tuple[dt.date, str], list[int]] = {}
    for ev in ledger.events:
        if isinstance(ev, ExecutionEvent):
            fills.setdefault((ev.trade_date, ev.symbol), []).append(ev.price)
    out, last = [], {}
    for day, (valued, struck) in closes_by_day(ledger).items():
        for symbol in sorted(set(valued) | set(struck)):
            close = struck.get(symbol, valued.get(symbol))
            reasons: list[tuple[str, str]] = []
            if symbol in valued and symbol in struck and valued[symbol] != struck[symbol]:
                reasons.append((BAD, f"평가에 쓴 종가 {valued[symbol]:,}와 기준가에 쓴 종가 {struck[symbol]:,}가 달라요"))
            inst = ledger.instruments.get(symbol)
            if close <= 0:
                reasons.append((BAD, "종가가 0 이하예요"))
            elif inst is not None and close % tick_size(close, inst.asset_type):
                reasons.append((BAD, f"호가단위({tick_size(close, inst.asset_type):,}원)에 맞지 않는 종가예요"))
            prev_date, prev_close = last.get(symbol, (None, None))
            change = None
            if prev_close and close > 0:
                change = Decimal(close) / prev_close - 1
                if abs(change) > rules.limit_change:
                    reasons.append((BAD, f"전일 종가 대비 가격제한폭({_pct(rules.limit_change)})을 넘게 움직였어요: "
                                         "액면분할 같은 권리 조정이나 종가 오류를 확인해 주세요"))
                elif abs(change) > rules.warn_change:
                    reasons.append((WARN, f"전일 종가 대비 {_pct(rules.warn_change)} 넘게 움직였어요"))
            gap = None
            if close > 0 and (day, symbol) in fills:
                gap = max((Decimal(p) / close - 1 for p in fills[(day, symbol)]), key=abs)
                if abs(gap) > rules.warn_exec_gap:
                    reasons.append((WARN, f"그날 체결단가와 종가가 {_pct(rules.warn_exec_gap)} 넘게 벌어져요"))
            level = max((lv for lv, _ in reasons), key=_RANK.get, default=OK)
            out.append(PriceCheck(day, symbol, close, prev_date, prev_close, change, gap, level,
                                  tuple(text for _, text in reasons)))
            last[symbol] = (day, close)
    return out


def worst(levels) -> str:
    return max(levels, key=_RANK.get, default=OK)
