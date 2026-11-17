"""Spec 04 S3 예수금 (결제일 기준)."""
import pytest

from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day

pytestmark = pytest.mark.gate

# day: (예수금 D+0 장 마감 후, D+2 예상 예수금)
S3 = {
    "11-02": (20_000_000, 14_999_250),
    "11-03": (20_000_000, 12_348_853),
    "11-04": (14_999_250, 15_641_758),
    "11-05": (12_348_853, 13_631_457),
    "11-06": (15_641_758, 18_031_976),
    "11-09": (13_631_457, 19_051_823),
    "11-10": (18_031_976, 19_051_823),
    "11-11": (19_051_823, 19_051_823),
}


def test_s3_cash_timeline(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
        assert (ledger.cash_balance(), ledger.expected_cash_d2()) == S3[day], day


def test_s3_cross_check(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
    e = ledger.position("E")
    assert ledger.cash_balance() == 20_000_000 + 75_000 - 2_757 - 15_420 - e.book_cost
