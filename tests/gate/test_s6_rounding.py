"""Spec 04 S6 원 미만 매도원가 (R-32)."""
from decimal import Decimal

import pytest

from conftest import d
from ministp.ledger import Side

pytestmark = pytest.mark.gate


def test_s6_cost_of_sale_truncated_and_absorbed(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.execute("B1", d("11-02"), "B", Side.BUY, 3, 10_001)
    ledger.execute("B2", d("11-02"), "B", Side.BUY, 3, 10_000)
    p = ledger.position("B")
    assert (p.qty, p.book_cost, p.avg_price) == (6, 60_003, Decimal("10000.50"))

    res = ledger.execute("B3", d("11-03"), "B", Side.SELL, 5, 10_100)
    assert res.cost_of_sale == 50_002
    assert res.realized_pnl == 498
    p = ledger.position("B")
    assert (p.qty, p.book_cost) == (1, 10_001)
