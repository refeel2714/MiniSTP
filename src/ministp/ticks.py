"""KRX price tick sizes (호가가격단위, KOSPI/KOSDAQ unified since 2023-01-25)."""
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .instruments import AssetType

_STOCK_TICKS = [  # (price below, tick)
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500),
]


def tick_table() -> list[tuple[str, int, int | None, int]]:
    """(asset type, price from, price below, tick) for the reference screen."""
    rows, lo = [], 0
    for upper, tick in _STOCK_TICKS:
        rows.append(("STOCK", lo, upper, tick))
        lo = upper
    return rows + [("STOCK", lo, None, 1_000), ("ETF", 0, None, 5)]


def tick_size(price: Decimal | int, asset_type: AssetType) -> int:
    if asset_type is AssetType.ETF:
        return 5
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
