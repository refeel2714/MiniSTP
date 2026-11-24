"""종목 마스터 이력 (ERD v2.7, R-94~95): the market master in the database, one row per change.

`master fetch` keeps the KIS files on disk and `master check` reads them. This
module puts what they say into two append-only tables, so that the question
"what did the market say about this code on that day?" has an answer that does
not depend on files someone may have deleted:

* `master_loads`: one row per day loaded, with the hashes of the two files it came from;
* `instrument_master`: a row for a code only on the day one of its attributes changes
  (name, market, group, lot, par value, listed shares, state flags). A code that
  leaves the file gets a row with `listed = false`; one that comes back gets a new row.

Prices (기준가, 시가총액, 전일 거래량) and the day-to-day flags (락구분, 증거금) are left
out: they change every day and belong to price data, not to what the code is.

A day is loaded once, and days are loaded in order: a history that took a later day
first could not say which state came first. Nothing here touches the books.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import psycopg

from . import audit
from .instruments import Market
from .master import KeptDay, MasterRow
from .ops import OpsError

# What a history row holds besides the code, its day and its load. Same order as the columns.
ATTRS = ("listed", "isin", "name", "market", "sec_group", "lot", "par_value", "listed_on", "listed_shares_k",
         "preferred", "spac", "halted", "liquidation", "managed", "warning")
MATCH_STATUSES = ("MATCH", "RENAMED", "DIFFERENT", "DELISTED", "MISSING")   # instrument_master_match_v.status


def attrs(r: MasterRow) -> tuple:
    return (True, r.isin, r.name, r.market.value, r.group, r.lot, r.par_value, r.listed_on, r.listed_shares,
            r.preferred, r.spac, r.halted, r.liquidation, r.managed, r.warning)


@dataclass(frozen=True)
class Load:
    load_id: int
    base_date: dt.date
    rows_total: int
    rows_new: int
    rows_changed: int
    rows_delisted: int
    already: bool = False   # these exact files were loaded before; nothing was written

    @property
    def ref(self) -> str:
        return f"ML-{self.load_id}"


def has_tables(conn: psycopg.Connection) -> bool:
    return conn.execute("select to_regclass('master_loads') is not null").fetchone()[0]


def last_load(conn: psycopg.Connection) -> Load | None:
    row = conn.execute("select load_id, base_date, rows_total, rows_new, rows_changed, rows_delisted"
                       " from master_loads order by base_date desc limit 1").fetchone()
    return Load(*row) if row else None


def current(conn: psycopg.Connection) -> dict[str, tuple]:
    """Each code's latest row, as the attribute tuple `attrs()` makes."""
    cols = ", ".join(ATTRS)
    return {r[0]: tuple(r[1:]) for r in conn.execute(f"select symbol, {cols} from instrument_master_current_v")}


def changes(now: dict[str, tuple], rows: dict[str, MasterRow]) -> tuple[list, list, list]:
    """(new, changed, delisted) as (symbol, attribute tuple) pairs, in code order."""
    new, changed, delisted = [], [], []
    for sym in sorted(rows):
        a = attrs(rows[sym])
        before = now.get(sym)
        if before is None:
            new.append((sym, a))
        elif before != a:
            changed.append((sym, a))   # includes a code that comes back after leaving the file
    for sym in sorted(now.keys() - rows.keys()):
        if now[sym][0]:
            delisted.append((sym, (False,) + now[sym][1:]))
    return new, changed, delisted


def load(conn: psycopg.Connection, kept: KeptDay, rows: dict[str, MasterRow]) -> Load:
    """R-94: put one fetched day into the history. Only codes whose attributes changed get a row;
    the load row, the history rows and the audit row go in together or not at all."""
    hashes = (kept.sha256[Market.KOSPI], kept.sha256[Market.KOSDAQ])
    same = conn.execute("select load_id, base_date, rows_total, rows_new, rows_changed, rows_delisted from master_loads"
                        " where kospi_sha256 = %s and kosdaq_sha256 = %s", hashes).fetchone()
    if same:
        return Load(*same, already=True)
    last = last_load(conn)
    if last and kept.day <= last.base_date:
        raise OpsError(f"기준일 {kept.day}는 마지막 적재({last.ref}, {last.base_date})보다 늦어야 해요. "
                       "이력은 날짜 순서로만 쌓아요.")
    if not rows:
        raise OpsError(f"{kept.day} 파일에 종목이 하나도 없어요. 적재하지 않았어요.")
    with conn.transaction():
        new, changed, delisted = changes(current(conn), rows)
        load_id = conn.execute(
            "insert into master_loads (base_date, fetched_at, kospi_sha256, kosdaq_sha256, rows_total, rows_new,"
            " rows_changed, rows_delisted) values (%s, %s, %s, %s, %s, %s, %s, %s) returning load_id",
            (kept.day, kept.fetched_at, *hashes, len(rows), len(new), len(changed), len(delisted))).fetchone()[0]
        cols = ", ".join(ATTRS)
        with conn.cursor() as cur:
            cur.executemany(
                f"insert into instrument_master (symbol, valid_from, load_id, {cols})"
                f" values (%s, %s, %s, {', '.join(['%s'] * len(ATTRS))})",
                [(sym, kept.day, load_id, *a) for sym, a in new + changed + delisted])
        done = Load(load_id, kept.day, len(rows), len(new), len(changed), len(delisted))
        audit.record(conn, "MASTER_LOAD", target=done.ref,
                     detail={"base_date": kept.day, "rows_total": done.rows_total, "rows_new": done.rows_new,
                             "rows_changed": done.rows_changed, "rows_delisted": done.rows_delisted,
                             "kospi_sha256": hashes[0][:12], "kosdaq_sha256": hashes[1][:12]})
    return done


def history(conn: psycopg.Connection, symbol: str) -> list[tuple]:
    """Every row of one code, oldest first: (valid_from, load_id, *ATTRS)."""
    cols = ", ".join(ATTRS)
    return conn.execute(f"select valid_from, load_id, {cols} from instrument_master where symbol = %s"
                        " order by valid_from", (symbol,)).fetchall()


def matches(conn: psycopg.Connection) -> list[tuple]:
    """R-95: each registered instrument against the latest master. (symbol, name, master name, status)."""
    return conn.execute("select symbol, name, master_name, status from instrument_master_match_v"
                        " order by symbol").fetchall()


def problems(conn: psycopg.Connection) -> list[str]:
    """R-95: what would make the history untrustworthy. Empty when it is sound.

    * each load's counts are the rows it wrote (new = first row of a code, delisted = listed false);
    * each row starts on the base date of the load that wrote it;
    * no row repeats the one before it (the history holds changes only);
    * the latest load's total is the number of codes listed after it."""
    out = [f"ML-{i} {what}: 기록 {want:,}, 이력 행 {got:,}" for i, what, want, got in conn.execute(
        "with r as (select m.load_id, m.listed,"
        "   lag(m.master_id) over (partition by m.symbol order by m.valid_from) is null as first"
        "   from instrument_master m),"
        " c as (select l.load_id, l.rows_new, l.rows_changed, l.rows_delisted,"
        "   count(*) filter (where r.first and r.listed) as n_new,"
        "   count(*) filter (where not r.first and r.listed) as n_changed,"
        "   count(*) filter (where not r.listed) as n_delisted"
        "   from master_loads l left join r using (load_id) group by l.load_id)"
        " select load_id, what, want, got from c cross join lateral (values"
        "   ('신규', rows_new, n_new), ('변경', rows_changed, n_changed), ('빠짐', rows_delisted, n_delisted))"
        "   v(what, want, got) where want <> got order by load_id, what")]
    out += [f"{s} {d}: 적재 {i}의 기준일 {b}와 다름" for s, d, i, b in conn.execute(
        "select m.symbol, m.valid_from, 'ML-' || m.load_id, l.base_date from instrument_master m"
        " join master_loads l using (load_id) where m.valid_from <> l.base_date order by m.master_id")]
    cols = ", ".join(ATTRS)
    prev = ", ".join(f"lag({c}) over w as p_{c}" for c in ATTRS)
    same = " and ".join(f"{c} is not distinct from p_{c}" for c in ATTRS)
    out += [f"{s} {d}: 앞 행과 같은 속성 (바뀌지 않았는데 새 행)" for s, d in conn.execute(
        f"select symbol, valid_from from (select symbol, valid_from, {cols}, {prev},"
        "   row_number() over w as n from instrument_master window w as (partition by symbol order by valid_from)) x"
        f" where n > 1 and {same} order by symbol, valid_from")]
    last = last_load(conn)
    if last:
        listed = conn.execute("select count(*) from instrument_master_current_v where listed").fetchone()[0]
        if listed != last.rows_total:
            out.append(f"{last.ref} 종목 수: 기록 {last.rows_total:,}, 이력의 상장 종목 {listed:,}")
    return out
