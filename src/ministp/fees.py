"""Commission and transaction taxes (spec R-20~22). Rates live in data, not code paths."""
from dataclasses import dataclass
from decimal import Decimal

from .instruments import AssetType, Instrument, Market
from .money import truncate_won


@dataclass(frozen=True)
class TradeCosts:
    commission: int
    tx_tax: int
    rural_tax: int

    @property
    def taxes(self) -> int:
        return self.tx_tax + self.rural_tax


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: Decimal
    # (market, asset_type) -> (증권거래세율, 농어촌특별세율), sell side only
    sell_tax_rates: dict[tuple[Market, AssetType], tuple[Decimal, Decimal]]

    @classmethod
    def spec_v1(cls) -> "FeeSchedule":
        """Rates from spec v1.0 (2026-01-01 세율, 가상 수수료 0.015%)."""
        zero = (Decimal("0"), Decimal("0"))
        return cls(
            commission_rate=Decimal("0.00015"),
            sell_tax_rates={
                (Market.KOSPI, AssetType.STOCK): (Decimal("0.0005"), Decimal("0.0015")),
                (Market.KOSDAQ, AssetType.STOCK): (Decimal("0.0020"), Decimal("0")),
                (Market.KOSPI, AssetType.ETF): zero,
                (Market.KOSDAQ, AssetType.ETF): zero,
            },
        )

    def costs(self, instrument: Instrument, is_sell: bool, gross: int) -> TradeCosts:
        commission = truncate_won(gross * self.commission_rate)
        if not is_sell:
            return TradeCosts(commission, 0, 0)
        tx_rate, rural_rate = self.sell_tax_rates[(instrument.market, instrument.asset_type)]
        # 각각 따로 계산, 각각 절사 (R-21)
        return TradeCosts(commission, truncate_won(gross * tx_rate), truncate_won(gross * rural_rate))
