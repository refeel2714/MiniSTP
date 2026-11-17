"""Strategy interface shared by backtest and paper/live trading.

A strategy only sees bars up to "now" and can only *request* orders through
the context. Risk checks, the OMS and the ledger sit behind the context, so the
same strategy code runs unchanged in backtest and against a broker.
"""
import datetime as dt
from dataclasses import dataclass
from typing import Protocol

from .ledger import Side


@dataclass(frozen=True)
class Bar:
    date: dt.date
    symbol: str
    open: int
    high: int
    low: int
    close: int
    volume: int = 0


class StrategyContext(Protocol):
    today: dt.date

    def position_qty(self, symbol: str) -> int: ...
    def buying_power(self) -> int: ...
    def closes(self, symbol: str, n: int) -> list[int]: ...
    def submit(self, symbol: str, side: Side, qty: int) -> str | None: ...


class Strategy(Protocol):
    name: str

    def on_bar(self, ctx: StrategyContext, bars: dict[str, Bar]) -> None: ...
