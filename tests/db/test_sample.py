"""Sample database: simulated days through the real operation code, never the real DB."""
from types import SimpleNamespace

import pytest

from db.test_ops import CAL, FEES, books, conn  # noqa: F401
from ministp.ledger import NavEvent
from ministp.sample import SampleError, build_sample, guard_sample_db


def test_refuses_the_operating_database():
    with pytest.raises(SampleError, match="sample"):
        guard_sample_db(SimpleNamespace(info=SimpleNamespace(dbname="ministp")))


def test_sample_days_are_clean_and_shaped_like_real_operation(conn):
    s = build_sample(conn, CAL, FEES, days=15, seed=3)
    assert s.days == 15 and s.orders >= 10 and s.canceled == 1
    assert s.breaks_resolved == 2               # CASH_D0 and CASH_PROJECTED on the skew day
    assert s.streak == 15                       # resolved breaks count as clean (R-74)

    b = books(conn)
    assert b.store.verify(b.ledger) == []       # DB rows match a replay of the events
    assert sum(isinstance(e, NavEvent) for e in b.ledger.events) == 15
    kinds = dict(conn.execute("select kind, count(*) from broker_snapshots group by kind").fetchall())
    assert kinds["OPENING_BALANCE"] == 1 and kinds["DAILY_RECON"] == 15
    outcomes = dict(conn.execute("select outcome, count(*) from oms_order_acks group by outcome").fetchall())
    assert outcomes["ACCEPTED"] == s.orders


def test_same_seed_same_sample(conn):
    a = build_sample(conn, CAL, FEES, days=6, seed=11)
    first = conn.execute("select event_type, payload from ledger_events order by seq").fetchall()
    b = build_sample(conn, CAL, FEES, days=6, seed=11)
    assert (a.orders, a.fills) == (b.orders, b.fills)
    assert conn.execute("select event_type, payload from ledger_events order by seq").fetchall() == first
