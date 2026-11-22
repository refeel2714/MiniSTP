"""Commission and transaction taxes (spec R-20~22). Rates live in data, not code paths."""
from dataclasses import dataclass, replace
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


DIVIDEND_INCOME_TAX, DIVIDEND_LOCAL_TAX = Decimal("0.14"), Decimal("0.014")


def dividend_withholding(gross: int) -> int:
    """R-23: 배당소득세 14% + 지방소득세 1.4%, each truncated to the won on its own."""
    return truncate_won(gross * DIVIDEND_INCOME_TAX) + truncate_won(gross * DIVIDEND_LOCAL_TAX)


@dataclass(frozen=True)
class FeeSchedule:
    commission_rate: Decimal
    # (market, asset_type) -> (증권거래세율, 농어촌특별세율), sell side only
    sell_tax_rates: dict[tuple[Market, AssetType], tuple[Decimal, Decimal]]
    # 수수료 절사 단위(원). 1 = 원 미만 절사, 10 = 10원 미만 절사 (R-20)
    commission_unit: int = 1

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

    @classmethod
    def kis_paper(cls) -> "FeeSchedule":
        """KIS 모의계좌 실측 (2026-11-18): 수수료 10원 미만 절사. 요율 0.015%는 아직 가정값."""
        return replace(cls.spec_v1(), commission_unit=10)

    def costs(self, instrument: Instrument, is_sell: bool, gross: int) -> TradeCosts:
        commission = truncate_won(gross * self.commission_rate)
        commission -= commission % self.commission_unit
        if not is_sell:
            return TradeCosts(commission, 0, 0)
        tx_rate, rural_rate = self.sell_tax_rates[(instrument.market, instrument.asset_type)]
        # 각각 따로 계산, 각각 절사 (R-21)
        return TradeCosts(commission, truncate_won(gross * tx_rate), truncate_won(gross * rural_rate))
