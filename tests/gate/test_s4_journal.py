"""Spec 04 S4 분개 and spec 03 불변 조건."""
import pytest

from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day
from ministp.accounting import Account

pytestmark = pytest.mark.gate


def lines_of(ledger, source_id, kind):
    [entry] = [e for e in ledger.journal if e.source_id == source_id and e.kind == kind]
    return {(ln.account, ln.debit, ln.credit) for ln in entry.lines}


@pytest.fixture
def booked(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
    return ledger


def test_t1_execution_entry(booked):
    assert lines_of(booked, "T1", "EXECUTION") == {
        (Account.FVPL_STOCK, 5_000_000, 0),
        (Account.COMMISSION, 750, 0),
        (Account.BUY_PAYABLE, 0, 5_000_750),
    }


def test_t1_settlement_entry(booked):
    assert lines_of(booked, "T1", "SETTLEMENT") == {
        (Account.BUY_PAYABLE, 5_000_750, 0),
        (Account.CASH, 0, 5_000_750),
    }


def test_t3_execution_entry(booked):
    assert lines_of(booked, "T3", "EXECUTION") == {
        (Account.SELL_RECEIVABLE, 3_292_905, 0),
        (Account.COMMISSION, 495, 0),
        (Account.TAXES, 6_600, 0),
        (Account.FVPL_STOCK, 0, 3_060_000),
        (Account.TRADING_GAIN, 0, 240_000),
    }


def test_t5_execution_entry(booked):
    assert lines_of(booked, "T5", "EXECUTION") == {
        (Account.SELL_RECEIVABLE, 4_400_519, 0),
        (Account.COMMISSION, 661, 0),
        (Account.TAXES, 8_820, 0),
        (Account.TRADING_LOSS, 180_000, 0),
        (Account.FVPL_STOCK, 0, 4_590_000),
    }


def test_every_entry_balances(booked):
    for e in booked.journal:
        assert sum(ln.debit for ln in e.lines) == sum(ln.credit for ln in e.lines)


def test_invariants_hold_every_day(ledger):
    """Spec 03 불변 조건 1~3, checked at every end of day."""
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
        assert ledger.account_balance(Account.CASH) == ledger.cash_balance()
        assert ledger.account_balance(Account.FVPL_STOCK) == sum(
            p.book_cost for p in ledger.positions().values()
        )
        assert (
            ledger.account_balance(Account.SELL_RECEIVABLE)
            - ledger.account_balance(Account.BUY_PAYABLE)
            == ledger.pending_settlement_net()
        )
