from conftest import d
from gate.trades import BUSINESS_DAYS, INITIAL_DEPOSIT, run_day
from ministp.store import from_payload, to_payload


def test_payload_round_trip(ledger):
    ledger.deposit(d("11-02"), INITIAL_DEPOSIT)
    ledger.open_position(d("11-02"), "B", 10, 500_000)
    for day in BUSINESS_DAYS:
        run_day(ledger, day, d)
    ledger.revalue(d("11-11"), {"E": 10_300, "B": 50_000})
    ledger.nav(d("11-11"), {"E": 10_300, "B": 50_000})
    for ev in ledger.events:
        assert from_payload(type(ev).__name__, to_payload(ev)) == ev
