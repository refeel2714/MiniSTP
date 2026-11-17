"""Spec 04 S1 trade list (docs/spec/04-sample-trades.md)."""
from ministp.ledger import Side

TRADES = [
    # id, trade date, symbol, side, qty, price
    ("T1", "11-02", "A", Side.BUY, 100, 50_000),
    ("T2", "11-03", "A", Side.BUY, 50, 53_000),
    ("T3", "11-04", "A", Side.SELL, 60, 55_000),
    ("T4", "11-05", "E", Side.BUY, 200, 10_050),
    ("T5", "11-06", "A", Side.SELL, 90, 49_000),
    ("T6", "11-09", "E", Side.SELL, 100, 10_200),
]

BUSINESS_DAYS = ["11-02", "11-03", "11-04", "11-05", "11-06", "11-09", "11-10", "11-11"]
INITIAL_DEPOSIT = 20_000_000


def run_day(ledger, day, d):
    """Settle what is due, then book that day's executions (spec R-13)."""
    ledger.settle_due(d(day))
    results = {}
    for tid, td, sym, side, qty, px in TRADES:
        if td == day:
            results[tid] = ledger.execute(tid, d(td), sym, side, qty, px)
    return results
