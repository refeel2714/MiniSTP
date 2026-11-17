from dataclasses import dataclass
from enum import StrEnum


class Market(StrEnum):
    KOSPI = "KOSPI"
    KOSDAQ = "KOSDAQ"


class AssetType(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: Market
    asset_type: AssetType
