"""Example: moving-average crossover, long only. For demonstrating the pipeline, not for profit."""
from dataclasses import dataclass

from ..ledger import Side
from ..strategy import Bar, StrategyContext


@dataclass
class MovingAverageCross:
    symbol: str
    short: int = 5
    long: int = 20
    budget: int = 1_000_000  # 진입 시 사용할 금액
    name: str = "ma_cross"

    def on_bar(self, ctx: StrategyContext, bars: dict[str, Bar]) -> None:
        if self.symbol not in bars:
            return
        closes = ctx.closes(self.symbol, self.long + 1)
        if len(closes) < self.long + 1:
            return
        def ma(xs, n):
            return sum(xs[-n:]) / n
        prev_s, prev_l = ma(closes[:-1], self.short), ma(closes[:-1], self.long)
        cur_s, cur_l = ma(closes, self.short), ma(closes, self.long)
        held = ctx.position_qty(self.symbol)
        if prev_s <= prev_l and cur_s > cur_l and held == 0:
            qty = self.budget // closes[-1]
            if qty > 0:
                ctx.submit(self.symbol, Side.BUY, qty)
        elif prev_s >= prev_l and cur_s < cur_l and held > 0:
            ctx.submit(self.symbol, Side.SELL, held)
