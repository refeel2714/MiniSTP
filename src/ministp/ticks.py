"""KRX price tick sizes (호가가격단위).

Stocks: KOSPI/KOSDAQ unified since 2023-01-25. ETF·ETN: 1원 below 2,000원 and 5원 from 2,000원 since
2023-12-11 (유가증권시장 업무규정 시행세칙 개정; before that 5원 at every price)."""
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .instruments import AssetType

_STOCK_TICKS = [  # (price below, tick)
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500),
]
_ETF_TICKS = [(2_000, 1)]  # 2,000원 이상은 5원


def tick_table() -> list[tuple[str, int, int | None, int]]:
    """(asset type, price from, price below, tick) for the reference screen."""
    rows, lo = [], 0
    for upper, tick in _STOCK_TICKS:
        rows.append(("STOCK", lo, upper, tick))
        lo = upper
    rows.append(("STOCK", lo, None, 1_000))
    lo = 0
    for upper, tick in _ETF_TICKS:
        rows.append(("ETF", lo, upper, tick))
        lo = upper
    return rows + [("ETF", lo, None, 5)]


def tick_size(price: Decimal | int, asset_type: AssetType) -> int:
    if asset_type is AssetType.ETF:
        return next((tick for upper, tick in _ETF_TICKS if price < upper), 5)
    for upper, tick in _STOCK_TICKS:
        if price < upper:
            return tick
    return 1_000


def round_to_tick(price: Decimal | int, asset_type: AssetType, up: bool) -> int:
    """Round to a valid tick: up for buys (pay more), down for sells (receive less)."""
    p = Decimal(price)
    t = tick_size(p, asset_type)
    n = (p / t).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return int(n * t)
