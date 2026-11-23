"""STP-2210 기준가 내역: net assets counted from holdings, explained by the journal."""
import pytest

from ministp import queries as q
from ministp.ledger import Side

from conftest import d


def day(ledger, date):
    return next(c for c in q.nav_changes(ledger) if c.date == d(date))


def test_every_won_of_a_day_has_an_entry_behind_it(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.revalue(d("11-02"), {"A": 10_500})
    ledger.nav(d("11-02"), {"A": 10_500})
    ledger.execute("X2", d("11-03"), "A", Side.SELL, 4, 11_000)
    ledger.revalue(d("11-03"), {"A": 11_000})
    ledger.nav(d("11-03"), {"A": 11_000})

    first, second = day(ledger, "11-02"), day(ledger, "11-03")
    assert (first.prev_net_assets, first.unexplained, second.unexplained) == (0, 0, 0)
    assert second.prev_net_assets == first.net_assets
    moved = {name: amount for _, _, name, amount in first.moves}
    assert moved["투자원금"] == 1_000_000 and moved["평가이익"] == 5_000 and moved["지급수수료"] < 0
    kinds = {name: kind for kind, _, name, _ in second.moves}
    assert kinds["매매이익"] == "수익" and kinds["세금과공과"] == "비용"
    assert second.change == second.explained == sum(m[3] for m in second.moves)
    # the second day's 평가이익 is the change in the unrealized gain, not its level: 6 x 1,000 - 5,000
    assert dict((m[2], m[3]) for m in second.moves)["평가이익"] == 1_000


def test_a_nav_struck_at_prices_the_books_never_took_shows_as_unexplained(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.nav(d("11-02"), {"A": 10_700})          # no revaluation booked
    c = day(ledger, "11-02")
    assert c.unexplained == 10 * 700 and c.change == c.explained + 7_000
    days = q.nav_change_days(ledger).records()
    assert [(r["unexplained"], r["result"]) for r in days] == [(7_000, "불일치")]


def test_a_day_struck_twice_counts_once_at_its_last_nav(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.nav(d("11-02"), {})
    ledger.execute("X1", d("11-02"), "A", Side.BUY, 10, 10_000)
    ledger.revalue(d("11-02"), {"A": 10_000})
    ledger.nav(d("11-02"), {"A": 10_000})
    [c] = q.nav_changes(ledger)
    assert c.prev_net_assets == 0 and c.unexplained == 0 and c.struck == 2      # the history says it was restruck
    assert {m[2] for m in c.moves} == {"투자원금", "지급수수료"}


def test_the_bridge_reads_from_yesterday_to_today(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.nav(d("11-02"), {})
    ledger.execute("X1", d("11-03"), "A", Side.BUY, 10, 10_000)
    ledger.revalue(d("11-03"), {"A": 9_000})
    ledger.nav(d("11-03"), {"A": 9_000})
    rows = q.nav_change_bridge(ledger, d("11-03")).records()
    assert [r["kind"] for r in rows] == ["전일 순자산", "비용", "비용", "설명되지 않은 차이", "당일 순자산"]
    assert rows[0]["running"] == 1_000_000 and rows[0]["amount"] is None
    assert [r["name"] for r in rows[1:3]] == ["평가손실", "지급수수료"] and rows[1]["amount"] == -10_000
    assert rows[-2]["amount"] == 0 and rows[-2]["running"] == rows[-1]["running"] == day(ledger, "11-03").net_assets
    with pytest.raises(KeyError, match="기준가가 없어요"):
        q.nav_change_bridge(ledger, d("11-04"))


def test_days_are_listed_newest_first_and_cut_to_the_period(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    for date in ("11-02", "11-03", "11-04"):
        ledger.nav(d(date), {})
    assert [r["date"] for r in q.nav_change_days(ledger).records()] == [d("11-04"), d("11-03"), d("11-02")]
    assert [r["date"] for r in q.nav_change_days(ledger, d("11-03"), d("11-03")).records()] == [d("11-03")]


def test_the_history_carries_the_nav_record_of_each_day(ledger):
    ledger.deposit(d("11-02"), 1_000_000)
    ledger.nav(d("11-02"), {})
    ledger.execute("X1", d("11-03"), "A", Side.BUY, 10, 10_000)
    ledger.revalue(d("11-03"), {"A": 11_000})
    ledger.nav(d("11-03"), {"A": 11_000})
    second, first = q.nav_change_days(ledger).records()
    by_day = {r.date: r for r in ledger.nav_history()}
    assert (first["nav_per_1000"], first["nav_change"], first["struck"]) == (by_day[d("11-02")].nav_per_1000, None, 1)
    assert (second["nav_per_1000"], second["units"]) == (by_day[d("11-03")].nav_per_1000, by_day[d("11-03")].units)
    assert second["nav_change"] == second["nav_per_1000"] - first["nav_per_1000"] and second["nav_change"] > 0
    assert second["net_assets"] - first["net_assets"] == second["change"] == second["explained"]
