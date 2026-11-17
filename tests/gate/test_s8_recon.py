"""Spec 04 S8 대사 불일치 (R-70~72)."""
import pytest

from conftest import d
from ministp.recon import Cause, ReconItem, Status, reconcile

pytestmark = pytest.mark.gate


def test_s8_breaks_get_cause_candidates():
    items = [
        ReconItem("POSITION_QTY", "A", internal=90, broker=90),
        ReconItem("BOOK_COST", "A", internal=4_590_000, broker=4_590_000),
        ReconItem("CASH_D0", "", internal=14_999_250, broker=14_999_250),
        ReconItem("CASH_PROJECTED", "", internal=15_641_758, broker=15_641_759),
        ReconItem("EXEC_COUNT", "", internal=1, broker=2),
    ]
    results = reconcile(d("11-04"), items)
    got = [(r.item, r.status, r.cause, r.diff) for r in results]
    assert got == [
        ("POSITION_QTY", Status.MATCH, None, 0),
        ("BOOK_COST", Status.MATCH, None, 0),
        ("CASH_D0", Status.MATCH, None, 0),
        ("CASH_PROJECTED", Status.BREAK, Cause.ROUNDING, 1),
        ("EXEC_COUNT", Status.BREAK, Cause.MISSING_EXEC, 1),
    ]
