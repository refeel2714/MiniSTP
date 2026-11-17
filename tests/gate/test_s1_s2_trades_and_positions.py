"""Spec 04 S1 (건별 기대값) and S2 (포지션과 손익)."""
from decimal import Decimal

import pytest

from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day

pytestmark = pytest.mark.gate

# id: gross, commission, tx_tax, rural_tax, net, settle_date
S1 = {
    "T1": (5_000_000, 750, 0, 0, 5_000_750, "11-04"),
    "T2": (2_650_000, 397, 0, 0, 2_650_397, "11-05"),
    "T3": (3_300_000, 495, 1_650, 4_950, 3_292_905, "11-06"),
    "T4": (2_010_000, 301, 0, 0, 2_010_301, "11-09"),
    "T5": (4_410_000, 661, 2_205, 6_615, 4_400_519, "11-10"),
    "T6": (1_020_000, 153, 0, 0, 1_019_847, "11-11"),
}

# after trade: (A qty, A book_cost, A avg | None, E qty, E book_cost, cost_of_sale | None, realized | None)
S2 = {
    "T1": (100, 5_000_000, "50000.00", 0, 0, None, None),
    "T2": (150, 7_650_000, "51000.00", 0, 0, None, None),
    "T3": (90, 4_590_000, "51000.00", 0, 0, 3_060_000, 240_000),
    "T4": (90, 4_590_000, "51000.00", 200, 2_010_000, None, None),
    "T5": (0, 0, None, 200, 2_010_000, 4_590_000, -180_000),
    "T6": (0, 0, None, 100, 1_005_000, 1_005_000, 15_000),
}


@pytest.fixture
def results(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    out = {}
    for day in BUSINESS_DAYS:
        for tid, res in run_day(ledger, day, d).items():
            a, e = ledger.position("A"), ledger.position("E")
            out[tid] = (res, (a.qty, a.book_cost, a.avg_price, e.qty, e.book_cost))
    return out


@pytest.mark.parametrize("tid", list(S1))
def test_s1_execution_amounts(results, tid):
    gross, fee, tx, rural, net, settle = S1[tid]
    res, _ = results[tid]
    assert res.gross == gross
    assert res.commission == fee
    assert res.tx_tax == tx
    assert res.rural_tax == rural
    assert res.net == net
    assert res.settle_date == d(settle)


@pytest.mark.parametrize("tid", list(S2))
def test_s2_positions_and_pnl(results, tid):
    a_qty, a_cost, a_avg, e_qty, e_cost, cos, pnl = S2[tid]
    res, (qa, ca, avga, qe, ce) = results[tid]
    assert (qa, ca, qe, ce) == (a_qty, a_cost, e_qty, e_cost)
    assert avga == (Decimal(a_avg) if a_avg else None)
    assert res.cost_of_sale == cos
    assert res.realized_pnl == pnl


def test_s2_totals(results):
    res = [r for r, _ in results.values()]
    assert sum(r.realized_pnl or 0 for r in res) == 75_000
    assert sum(r.commission for r in res) == 2_757
    assert sum(r.tx_tax + r.rural_tax for r in res) == 15_420
