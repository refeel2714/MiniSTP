"""Money helpers. Amounts are int won; intermediates are Decimal (spec R-01)."""
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def truncate_won(x: Decimal | int) -> int:
    """원 미만 절사 (toward zero)."""
    return int(Decimal(x).to_integral_value(rounding=ROUND_DOWN))


def round2(x: Decimal) -> Decimal:
    """소수 셋째 자리에서 반올림 (R-30, R-62)."""
    return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
