"""Read services the screens will call: a real day's records, read back through them."""
from db.test_ops import CAL, D, books, broker, conn  # noqa: F401
from ministp.accounting import Account
from ministp.ledger import Side
from ministp.ops import daily_close, open_books, place_order, resolve_break
from ministp.queries import (TRANSACTIONS, event_trace, ledger_events, nav_series, order_blotter,
                             recon_history, trial_balance)
from ministp.recon import Cause

DAY = D("2026-11-20")
STOCK = Account.FVPL_STOCK.code


def _a_day(conn, broker):
    """Open, buy 2 (filled at 80,000), close the day with a 5-won cash break, resolve it."""
    broker.set_day(DAY)
    open_books(books(conn), broker, DAY)
    broker.orders[DAY] = {}
    place_order(books(conn), broker, DAY, "069500", Side.BUY, 2, limit_price=80_000)
    broker.fill("0000000001", 80_000)
    broker.cash_skew = -5
    daily_close(books(conn), broker, DAY)
    for (i,) in conn.execute("select id from recon_results where status = 'BREAK' order by id"):
        resolve_break(conn, i, Cause.FEE_DIFF, "모의계좌 수수료")


def test_trace_from_an_execution_to_its_journal(conn, broker):
    _a_day(conn, broker)
    execs = ledger_events(conn, DAY, DAY, event_type="ExecutionEvent").records()
    assert [(e["symbol"], e["date"]) for e in execs] == [("069500", DAY)]
    t = event_trace(conn, execs[0]["seq"])
    assert t.balanced
    lines = {(r["account_code"], r["debit"], r["credit"]) for r in t.journal.records()}
    assert (STOCK, 160_000, 0) in lines and all(r["account_name"] for r in t.journal.records())
    assert [(r["exec_id"], r["qty_delta"], r["cost_delta"]) for r in t.positions.records()] == [
        ("20261120-0000000001-1", 2, 160_000)]
    assert len(t.cash) == 0          # cash moves on settlement day, not trade day


def test_events_newest_first_and_filtered(conn, broker):
    _a_day(conn, broker)
    all_ = ledger_events(conn)
    seqs = [r["seq"] for r in all_.records()]
    assert seqs == sorted(seqs, reverse=True)
    assert all(r["symbol"] == "069500" for r in ledger_events(conn, symbol="069500").records())
    assert len(ledger_events(conn, D("2026-11-23"))) == 0
    assert "payload" in all_.columns and "seq" in all_.columns


def test_trial_balance_is_balanced(conn, broker):
    _a_day(conn, broker)
    ds, balanced = trial_balance(conn)
    assert balanced and len(ds) > 0
    assert set(ds.columns) == {"code", "name", "normal_side", "balance"}


def test_recon_history_shows_judgement_next_to_the_break(conn, broker):
    _a_day(conn, broker)
    rows = recon_history(conn, DAY, DAY).records()
    assert rows and all(r["status"] != "RESOLVED" for r in rows)
    breaks = [r for r in rows if r["status"] == "BREAK"]
    assert {r["item"] for r in breaks} == {"CASH_D0", "CASH_PROJECTED"}
    assert all(r["resolved_cause"] == "FEE_DIFF" and r["resolved_note"] == "모의계좌 수수료" for r in breaks)
    assert [r["item"] for r in recon_history(conn, only_breaks=True).records()] == [r["item"] for r in breaks]
    assert len(recon_history(conn, kind="INTRADAY_RECON")) == 0


def test_blotter_shows_intent_ack_and_fill(conn, broker):
    _a_day(conn, broker)
    (o,) = order_blotter(conn, DAY).records()
    assert (o["client_id"], o["instrument_name"], o["side"], o["qty"], o["limit_price"]) == (
        "S20261120-0001", "KODEX 200", "BUY", 2, 80_000)
    assert (o["outcome"], o["order_no"], o["filled_qty"], o["filled_amount"]) == (
        "ACCEPTED", "0000000001", 2, 160_000)
    assert o["notices"] == 0 and o["max_booking_ms"] is None
    assert len(order_blotter(conn, D("2026-11-23"))) == 0


def test_nav_series_keeps_each_day_as_it_was(conn, broker):
    _a_day(conn, broker)
    broker.set_day(D("2026-11-23"))
    broker.prices["069500"] = 81_000
    daily_close(books(conn), broker, D("2026-11-23"))
    led = books(conn).ledger
    rows = nav_series(led).records()
    assert [r["date"] for r in rows] == [DAY, D("2026-11-23")]
    assert rows[0]["market_value"] == 3 * 80_540 and rows[1]["market_value"] == 3 * 81_000
    assert nav_series(led, date_from=D("2026-11-21")).rows[0][0] == D("2026-11-23")


def test_every_transaction_is_registered_once():
    assert len(set(TRANSACTIONS.values())) == len(TRANSACTIONS)
    assert all(k[:7] in {"STP1100", "STP1200", "STP2100", "STP2200", "STP3100"} for k in TRANSACTIONS)
