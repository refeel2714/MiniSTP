"""바스켓 주문 시뮬레이션 (spec D9 더할 것 6, R-97): 목표 편입비와 총 주문금액으로 종목별 수량을 계산해요.

계산만 하고 DB에 쓰지 않아요. 주문은 내지 않아요(결재와 전송은 게이트 뒤).

- 목표 금액 = 총 주문금액 × 편입비, 수량 = 목표 금액 ÷ 주문가 (1주 단위 내림).
- 주문가: 현재가를 호가단위에 맞춘 값(매수는 올림, 매도는 내림), +1호가, 또는 시장가(현재가로 어림).
- 검사는 주문 전 검증(R-36~38)과 같은 기준: 주문가능금액(여유분 포함), 매도가능수량, 호가단위.
  여기에 주문 후 종목 비중 한도(투자 지침이 생기기 전까지 예시값)를 더해요.
- 화면을 처음 열 때 목표 편입비는 현재 편입비(보유 종목의 평가금액 비중, 예수금 제외)예요.
"""
import csv
import datetime as dt
import io
import pathlib
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

from .fees import FeeSchedule
from .instruments import Instrument
from .ledger import Side
from .risk import RiskLimits
from .ticks import round_to_tick, tick_size

HUNDRED = Decimal(100)
CENT = Decimal("0.01")
DEFAULT_WEIGHT_LIMIT = Decimal(30)   # 종목당 비중 한도(%), 투자 지침(D9 더할 것 2) 전까지의 예시값


class BasketError(ValueError):
    pass


class PriceBasis(StrEnum):
    CURRENT = "current"   # 현재가 지정가
    TICK1 = "tick1"       # 현재가 +1호가 (매도는 -1호가)
    MARKET = "market"     # 시장가: 현재가로 어림


BASIS_TEXT = {PriceBasis.CURRENT: "현재가 지정가", PriceBasis.TICK1: "현재가 +1호가", PriceBasis.MARKET: "시장가"}

# 행 검사 결과. BLOCK이면 이 바스켓은 결재로 넘길 수 없어요. WARN은 보여 주기만 해요.
ISSUE_TEXT = {
    "NOT_REGISTERED": "등록되지 않은 종목이에요",
    "NO_PRICE": "현재가가 없어요: 가격을 넣어 주세요",
    "ZERO_QTY": "편입비가 작아 1주도 살 수 없어요",
    "SHORT_SELL": "매도가능수량보다 많아요 (R-36)",
    "WEIGHT_LIMIT": "주문 후 비중이 종목당 한도를 넘어요",
    "INSUFFICIENT_CASH": "필요 금액이 주문가능금액보다 커요 (R-37)",
}
WARN = {"ZERO_QTY"}


@dataclass(frozen=True)
class Portfolio:
    """시뮬레이션에 쓰는 계좌의 지금 모습. DB에서 읽거나(`load_portfolio`) 시험에서 직접 만들어요."""
    instruments: dict[str, Instrument]
    prices: dict[str, int]          # 종목별 현재가 (장중 현재가, 없으면 최근 종가)
    held_qty: dict[str, int]        # 보유수량
    sellable_qty: dict[str, int]    # 매도가능수량 (보유 − 미체결 매도)
    available_cash: int             # 주문가능금액 (R-37)
    projected_cash: int             # 결제 후 예수금
    price_basis: str = ""           # 화면 머리에 적는 "계산 기준" (예: 현재가 14:32)

    def held_value(self, symbol: str) -> int:
        price = self.prices.get(symbol)
        return self.held_qty.get(symbol, 0) * price if price else 0

    @property
    def market_value(self) -> int:
        return sum(self.held_value(s) for s in self.held_qty)

    @property
    def total_assets(self) -> int:
        """주문 후 비중의 분모: 보유 평가금액 + 결제 후 예수금."""
        return self.market_value + self.projected_cash


@dataclass(frozen=True)
class Target:
    symbol: str
    weight: Decimal                 # 목표 편입비(%)
    price: int | None = None        # 현재가 대신 쓸 가격 (보유하지 않은 종목처럼 현재가가 없을 때)


@dataclass(frozen=True)
class Line:
    symbol: str
    name: str
    asset_type: str
    current_weight: Decimal         # 현재 편입비(%)
    weight: Decimal                 # 목표 편입비(%)
    current_price: int | None
    order_price: int | None         # 계산에 쓴 단가 (시장가는 현재가)
    limit_price: int | None         # 주문으로 나갈 지정가 (시장가는 없음)
    tick: int | None
    target_amount: int
    qty: int
    amount: int
    actual_weight: Decimal          # 주문금액 ÷ 총 주문금액(%)
    commission: int
    taxes: int
    post_weight: Decimal | None     # 주문 후 비중(%) = (보유 평가금액 ± 주문금액) ÷ 총자산
    issues: tuple[str, ...] = ()

    @property
    def weight_gap(self) -> Decimal:
        return self.actual_weight - self.weight

    @property
    def blocked(self) -> bool:
        return any(i not in WARN for i in self.issues)


@dataclass(frozen=True)
class Result:
    side: Side
    basis: PriceBasis
    total_amount: int
    weight_limit: Decimal
    lines: tuple[Line, ...]
    available_cash: int
    required_cash: int              # 매수: Σ 주문금액 × (1 + 여유분율), 주문 검증(R-37)과 같은 계산
    issues: tuple[str, ...] = field(default=())   # 바스켓 전체 검사 (주문가능금액)

    @property
    def allocated(self) -> int:
        return sum(ln.amount for ln in self.lines)

    @property
    def leftover(self) -> int:
        return self.total_amount - self.allocated

    @property
    def commission(self) -> int:
        return sum(ln.commission for ln in self.lines)

    @property
    def taxes(self) -> int:
        return sum(ln.taxes for ln in self.lines)

    @property
    def ok(self) -> bool:
        return not self.issues and not any(ln.blocked for ln in self.lines)


def _pct(part: int, whole: int) -> Decimal:
    return (Decimal(part) * HUNDRED / whole).quantize(CENT) if whole else Decimal(0)


def current_weights(pf: Portfolio) -> dict[str, Decimal]:
    """보유 종목의 평가금액 비중(%), 소수 둘째 자리, 합계가 정확히 100.00이 되게(큰 나머지 순으로 0.01씩).

    현재가가 없는 종목은 비중을 잴 수 없어서 빼요."""
    values = {s: pf.held_value(s) for s, q in pf.held_qty.items() if q > 0 and pf.prices.get(s)}
    total = sum(values.values())
    if not total:
        return {}
    exact = {s: Decimal(v) * HUNDRED / total for s, v in values.items()}
    floor = {s: w.quantize(CENT, rounding=ROUND_DOWN) for s, w in exact.items()}
    short = int((HUNDRED - sum(floor.values())) / CENT)
    for s in sorted(exact, key=lambda s: (floor[s] - exact[s], -values[s], s))[:short]:
        floor[s] += CENT
    return dict(sorted(floor.items(), key=lambda kv: (-values[kv[0]], kv[0])))


def default_targets(pf: Portfolio) -> list[Target]:
    """화면을 처음 열 때의 바스켓: 보유 종목을 현재 편입비 그대로."""
    return [Target(s, w) for s, w in current_weights(pf).items()]


def _order_price(price: int, inst: Instrument, side: Side, basis: PriceBasis) -> tuple[int, int | None, int]:
    """(계산 단가, 지정가 또는 None, 호가단위)."""
    buy = side is Side.BUY
    p = round_to_tick(price, inst.asset_type, up=buy)
    tick = tick_size(p, inst.asset_type)
    if basis is PriceBasis.MARKET:
        return p, None, tick
    if basis is PriceBasis.TICK1:
        p = p + tick if buy else max(p - tick, tick)
        p = round_to_tick(p, inst.asset_type, up=buy)   # 호가단위 경계를 넘으면 새 단위로
        tick = tick_size(p, inst.asset_type)
    return p, p, tick


def validate(targets: list[Target], total_amount: int) -> None:
    if total_amount <= 0:
        raise BasketError("총 주문금액을 1원 이상 넣어 주세요")
    if not targets:
        raise BasketError("바스켓에 종목이 없어요")
    seen = set()
    for t in targets:
        if t.symbol in seen:
            raise BasketError(f"같은 종목이 두 번 있어요: {t.symbol}")
        seen.add(t.symbol)
        if t.weight < 0:
            raise BasketError(f"편입비는 0% 이상이에요: {t.symbol} {t.weight}%")
        if t.price is not None and t.price <= 0:
            raise BasketError(f"가격은 1원 이상이에요: {t.symbol}")
    total = sum(t.weight for t in targets)
    if total.quantize(CENT) != HUNDRED:
        raise BasketError(f"목표 편입비 합계가 100%가 아니에요: {total.quantize(CENT)}%")


def simulate(pf: Portfolio, targets: list[Target], total_amount: int, side: Side = Side.BUY,
             basis: PriceBasis = PriceBasis.CURRENT, fees: FeeSchedule | None = None,
             weight_limit: Decimal = DEFAULT_WEIGHT_LIMIT, limits: RiskLimits = RiskLimits()) -> Result:
    validate(targets, total_amount)
    fees = fees or FeeSchedule.spec_v1()
    now_w = current_weights(pf)
    assets = pf.total_assets
    lines = []
    for t in targets:
        inst = pf.instruments.get(t.symbol)
        target_amount = int((Decimal(total_amount) * t.weight / HUNDRED).to_integral_value(rounding=ROUND_DOWN))
        cur = t.price or pf.prices.get(t.symbol)
        base = dict(symbol=t.symbol, current_weight=now_w.get(t.symbol, Decimal(0)), weight=t.weight,
                    current_price=cur, target_amount=target_amount)
        if inst is None or cur is None:
            lines.append(Line(**base, name=inst.name if inst else "", asset_type=inst.asset_type.value if inst else "",
                              order_price=None, limit_price=None, tick=None, qty=0, amount=0,
                              actual_weight=Decimal(0), commission=0, taxes=0, post_weight=None,
                              issues=("NOT_REGISTERED" if inst is None else "NO_PRICE",)))
            continue
        price, limit, tick = _order_price(cur, inst, side, basis)
        qty = target_amount // price
        amount = qty * price
        costs = fees.costs(inst, side is Side.SELL, amount)
        held = pf.held_value(t.symbol)
        post = held + amount if side is Side.BUY else held - amount
        issues = []
        if qty == 0 and t.weight > 0:
            issues.append("ZERO_QTY")
        if side is Side.SELL and qty > pf.sellable_qty.get(t.symbol, 0):
            issues.append("SHORT_SELL")
        post_weight = _pct(post, assets) if assets else None
        if post_weight is not None and post_weight > weight_limit:
            issues.append("WEIGHT_LIMIT")
        lines.append(Line(**base, name=inst.name, asset_type=inst.asset_type.value, order_price=price,
                          limit_price=limit, tick=tick, qty=qty, amount=amount,
                          actual_weight=_pct(amount, total_amount), commission=costs.commission,
                          taxes=costs.taxes, post_weight=post_weight, issues=tuple(issues)))
    required = (sum(int(ln.amount * (1 + limits.cash_buffer_rate)) for ln in lines)
                if side is Side.BUY else 0)
    issues = ("INSUFFICIENT_CASH",) if side is Side.BUY and required > pf.available_cash else ()
    return Result(side, basis, total_amount, weight_limit, tuple(lines), pf.available_cash, required, issues)


# ---------- 바스켓 파일 (CSV) ----------

_SYMBOL = ("종목코드", "symbol")
_WEIGHT = ("편입비", "목표 편입비", "weight")
_PRICE = ("가격", "price")


def _pick(row: dict, names) -> str:
    for n in names:
        if n in row and row[n] not in (None, ""):
            return row[n].strip()
    return ""


def read_targets(text: str) -> list[Target]:
    """바스켓 CSV: 열 `종목코드, 편입비[, 가격]` (영문 symbol, weight, price도 받아요). 편입비는 % 숫자."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not any(n in rows[0] for n in _SYMBOL) or not any(n in rows[0] for n in _WEIGHT):
        raise BasketError("바스켓 파일에는 종목코드, 편입비 열이 있어야 해요")
    out = []
    for i, r in enumerate(rows, start=2):
        sym = _pick(r, _SYMBOL)
        if not sym:
            continue
        w, p = _pick(r, _WEIGHT).rstrip("%"), _pick(r, _PRICE).replace(",", "")
        try:
            out.append(Target(sym.zfill(6), Decimal(w), int(p) if p else None))
        except (ArithmeticError, ValueError) as e:
            raise BasketError(f"{i}번째 줄의 편입비나 가격이 숫자가 아니에요: {w} {p}") from e
    return out


HEADER = ["종목코드", "종목명", "현재 편입비", "목표 편입비", "현재가", "주문가", "주문구분", "목표 금액", "수량", "주문금액",
          "실제 편입비", "편입비 차이", "예상 수수료", "예상 세금", "주문 후 비중", "검사"]


def write_csv(result: Result, path: str | pathlib.Path) -> pathlib.Path:
    """결과표를 그대로 품의 첨부로 쓰는 CSV (엑셀에서 열리게 UTF-8 BOM)."""
    p = pathlib.Path(path)
    if p.exists():
        raise BasketError(f"{p}이 이미 있어요. 다른 이름으로 저장해 주세요")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for ln in result.lines:
            w.writerow([ln.symbol, ln.name, ln.current_weight, ln.weight, ln.current_price or "",
                        ln.order_price or "", "지정가" if ln.limit_price else "시장가", ln.target_amount, ln.qty,
                        ln.amount, ln.actual_weight, ln.weight_gap, ln.commission, ln.taxes,
                        "" if ln.post_weight is None else ln.post_weight, issue_text(ln.issues)])
        w.writerow(["합계", "", "", sum(ln.weight for ln in result.lines), "", "", "", result.total_amount, "",
                    result.allocated, "", "", result.commission, result.taxes, "", issue_text(result.issues)])
    return p


def issue_text(issues) -> str:
    return ", ".join(ISSUE_TEXT.get(i, i) for i in issues) or "통과"


def default_out(today: dt.date) -> pathlib.Path:
    return pathlib.Path("basket") / f"바스켓_{today:%Y%m%d}.csv"


# ---------- DB에서 계좌 모습 읽기 (읽기 전용) ----------

def load_portfolio(books, conn, today: dt.date, now: dt.datetime | None = None) -> Portfolio:
    """보유, 현재가(R-68: 오늘 `run`이 받은 현재가, 없으면 최근 종가), 주문가능금액(R-37)."""
    from .capacity import pending_buy_value
    from .ops import stored_open_orders
    from .queries import last_closes, valuation_basis
    from .quotes import latest_quotes, marks
    now = now or dt.datetime.now()
    quotes = latest_quotes(conn, today)
    prices = {s: m.price for s, m in marks(*last_closes(books.ledger), quotes, now).items()}
    open_orders = stored_open_orders(books, today)
    reserved, _ = pending_buy_value(open_orders, prices)
    held = {s: p.qty for s, p in books.ledger.positions().items() if p.qty > 0}
    selling: dict[str, int] = {}
    for o in open_orders:
        if o.side is Side.SELL:
            selling[o.symbol] = selling.get(o.symbol, 0) + o.remaining_qty
    projected = books.ledger.projected_cash()
    return Portfolio(instruments=dict(books.instruments), prices=prices, held_qty=held,
                     sellable_qty={s: max(q - selling.get(s, 0), 0) for s, q in held.items()},
                     available_cash=projected - reserved, projected_cash=projected,
                     price_basis=valuation_basis(books.ledger, list(held), quotes, now))
