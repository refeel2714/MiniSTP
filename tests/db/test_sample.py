"""Sample database: simulated days through the real operation code, never the real DB."""
from types import SimpleNamespace

import pytest

from db.test_ops import CAL, FEES, books, conn  # noqa: F401
from ministp.ledger import NavEvent
from ministp import audit
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


# ---------- 다계좌 샘플: 고객 2명, 계좌 3개 ----------

def _events(conn, account_id):
    return conn.execute("select event_type, payload from ledger_events where account_id = %s order by seq",
                        (account_id,)).fetchall()


def test_extra_accounts_do_not_change_the_default_accounts_history(conn):
    one = build_sample(conn, CAL, FEES, days=12, seed=3)
    alone = _events(conn, "A0001")
    three = build_sample(conn, CAL, FEES, days=12, seed=3, accounts=3)
    assert _events(conn, "A0001") == alone
    assert (one.orders, one.fills, one.breaks_resolved, one.streak) == (
        three.orders, three.fills, three.breaks_resolved, three.streak)
    assert one.others == [] and [a.account_id for a in three.others] == ["A0002", "A0003"]
    with pytest.raises(SampleError, match="1~3개"):
        build_sample(conn, CAL, FEES, days=3, accounts=4)


def test_three_account_sample_is_two_customers_trading_one_market(conn):
    from ministp.ops import load_instruments
    from ministp.store.integrity import integrity_report
    s = build_sample(conn, CAL, FEES, days=12, seed=3, accounts=3)
    second, third = s.others

    assert conn.execute("select account_id, customer_id, credential_ref, opened_on from trading_accounts"
                        " order by 1").fetchall() == [
        ("A0001", "C0001", "KIS", s.first_day), ("A0002", "C0001", "KIS_2", second.opened_on),
        ("A0003", "C0002", "KIS_3", s.first_day)]
    assert second.opened_on > s.first_day                         # opened part-way through
    assert conn.execute("select exec_id, qty_delta from position_ledger where account_id = 'A0003'"
                        " order by id limit 1").fetchone() == ("OPEN-005930", 100)    # held before the books opened

    n, last = conn.execute("select count(*), max(seq) from ledger_events").fetchone()
    assert n == last                                              # one sequence, no gap
    checks = integrity_report(conn, CAL, FEES, load_instruments(conn))
    assert all(c.ok for c in checks) and {c.account for c in checks} == {"", "A0001", "A0002", "A0003"}

    # each account closes every day it is open, and a judged break still counts as clean (R-74)
    days_open = dict(conn.execute("select account_id, count(distinct date) from broker_snapshots"
                                  " where kind = 'DAILY_RECON' group by 1"))
    assert days_open["A0001"] == days_open["A0003"] == 12 and days_open["A0002"] == second.streak < 12
    assert (s.streak, third.streak, second.breaks_resolved) == (12, 12, 2)
    assert second.orders and third.orders and second.fills >= second.orders


def test_the_sample_shows_a_suspension_that_refuses_orders_but_not_the_close(conn):
    s = build_sample(conn, CAL, FEES, days=12, seed=3, accounts=3)
    third = s.others[1]
    history = conn.execute("select status, effective_from from account_status_history where account_id = 'A0003'"
                           " order by id").fetchall()
    assert [st for st, _ in history] == ["OPEN", "SUSPENDED", "OPEN"] and third.status == "OPEN"
    suspended_from, reopened = history[1][1], history[2][1]
    assert third.refused >= 1 and any("ACCOUNT_NOT_OPEN" in n for n in third.notes)
    # R-85: the refused orders are in no order table; the audit log is where they are kept, as the generator's rows
    refused = audit.rows(conn, limit=1000, account_id=third.account_id, action="ORDER", outcome="REFUSED")
    assert len(refused) == third.refused and {r.detail["reason"] for r in refused} == {"ACCOUNT_NOT_OPEN"}
    assert {(r.channel, r.actor) for r in audit.rows(conn, limit=5000)} == {("SYSTEM", "sample")}
    assert audit.chain_problems(conn)[2] == []
    assert conn.execute("select count(*) from oms_orders where account_id = 'A0003' and trade_date >= %s"
                        " and trade_date < %s", (suspended_from, reopened)).fetchone()[0] == 0
    assert conn.execute("select count(distinct date) from broker_snapshots where account_id = 'A0003'"
                        " and kind = 'DAILY_RECON' and date >= %s and date < %s",
                        (suspended_from, reopened)).fetchone()[0] == 2        # still closed and reconciled


def test_screen_queries_stay_inside_one_account(conn):
    """Brokers number orders per account: the same order number on the same day must not mix fills."""
    from ministp import queries as q
    build_sample(conn, CAL, FEES, days=12, seed=3, accounts=3)
    mine = {r[0] for r in conn.execute("select seq from ledger_events where account_id = 'A0001'")}
    assert {r["seq"] for r in q.ledger_events(conn).records()} == mine
    assert {r["seq"] for r in q.ledger_events(conn, account_id="A0003").records()}.isdisjoint(mine)
    other = conn.execute("select min(seq) from ledger_events where account_id = 'A0002'").fetchone()[0]
    with pytest.raises(KeyError):
        q.event_trace(conn, other)                                 # another account's event is not found
    assert q.event_trace(conn, other, "A0002").balanced
    assert len(q.recon_history(conn).rows) == conn.execute(
        "select count(*) from recon_results where account_id = 'A0001' and status <> 'RESOLVED'").fetchone()[0]

    shared = conn.execute(     # days on which two accounts got the same order number from their brokers
        "select o.trade_date from oms_orders o join oms_order_acks a using (client_id)"
        " group by o.trade_date, a.order_no having count(distinct o.account_id) > 1").fetchall()
    assert shared
    for (day,) in shared:
        for acct in ("A0001", "A0002", "A0003"):
            rows = q.order_blotter(conn, day, acct).records()
            assert all(r["client_id"].startswith("S" if acct == "A0001" else f"{acct}-S") for r in rows)
            booked = conn.execute("select coalesce(sum(abs(qty_delta)), 0) from position_ledger"
                                  " where account_id = %s and trade_date = %s and exec_id not like 'OPEN-%%'",
                                  (acct, day)).fetchone()[0]
            assert sum(r["filled_qty"] for r in rows) == booked and all(r["filled_qty"] <= r["qty"] for r in rows)


def test_sample_db_command_builds_three_accounts_by_default(conn, capsys):
    from db.test_ops import URL
    from ministp import __main__ as cli
    env = {"MINISTP_DATABASE_URL": URL, "MINISTP_SAMPLE_DATABASE_URL": URL}
    assert cli.run(["sample-db", "--days", "6"], environ=env) == 0
    out = capsys.readouterr().out
    assert "계좌 A0002 두 번째 계좌" in out and "계좌 A0003 연습 계좌" in out
    assert conn.execute("select count(*) from trading_accounts").fetchone()[0] == 3
    assert cli.run(["sample-db", "--days", "3", "--accounts", "1"], environ=env) == 0
    assert "계좌 A0002" not in capsys.readouterr().out


def test_every_screen_still_answers_on_the_three_account_sample(conn):
    from ministp.ui.definitions import load_screens
    from ministp.ui.transactions import TxContext, output_problems
    s = build_sample(conn, CAL, FEES, days=8, seed=5, accounts=3)
    assert output_problems(load_screens(), TxContext(conn, CAL, FEES, s.last_day)) == []


def test_the_sample_has_money_coming_in_and_going_out(conn):
    s = build_sample(conn, CAL, FEES, days=15, seed=3)
    assert conn.execute("select kind, amount from cash_movements_v order by event_seq").fetchall() == [
        ("DEPOSIT", 100_000_000), ("DEPOSIT", 10_000_000), ("WITHDRAWAL", -3_000_000)]
    assert s.streak == 15 and s.breaks_resolved == 2      # recorded the day it happened: the recon never saw a gap
    navs = [row.nav_per_1000 for row in books(conn).ledger.nav_history()]
    assert all(abs(b / a - 1) < 0.03 for a, b in zip(navs, navs[1:]))   # R-61: money in or out does not move the NAV
    said = [(r.action, r.actor, r.detail["memo"]) for r in audit.rows(conn, limit=500)
            if r.action.startswith(("CASH", "ADJUSTMENT"))]
    assert said[::-1] == [("CASH_DEPOSIT", "sample", "샘플: 추가 입금"), ("ADJUSTMENT_ADD", "sample", "샘플: 예탁금 이용료"),
                          ("CASH_WITHDRAW", "sample", "샘플: 일부 출금")]
    assert conn.execute("select reason, amount, tax, cash_amount from adjustments_v").fetchall() == [
        ("INTEREST", 12_340, 1_890, 10_450)]                # R-66: income the ledger could not have computed
