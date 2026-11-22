"""Schema migrations: versioned SQL files applied in order, each proven not to touch existing rows.

`schema.sql` stays the full schema a new database is built from (`db-init`).
A migration is the step that takes an *existing* database from one ERD version
to the next. The ledger tables are append-only and the operating database holds
the reconciliation record, so a migration has to show more than "it ran":

* each migration runs in one transaction, and a failure leaves nothing behind;
* before and after it, every table gets a fingerprint (row count and a hash of
  every row over the columns it had before). A table whose existing rows changed
  fails the migration unless the file declares it (`-- changes-data: table`);
* `dry_run` does all of that and then rolls back, to rehearse on a copy first;
* an applied migration is recorded with its checksum; editing an applied file
  is refused.

File name: `NNNN_slug.sql`, numbered from 0001 without gaps. Header comments:

    -- title: 다계좌: 고객·위탁계좌, 원장에 계좌ID
    -- erd: 2.0
    -- changes-data: some_table, other_table     (optional, rarely needed)

A database without the `schema_migrations` table is at version 0 (ERD v1.1,
the schema before migrations existed). The table is created by the first
migration that is applied, so looking at the status never changes a database.
"""
import hashlib
import re
from dataclasses import dataclass, field
from importlib.resources import files

import psycopg
from psycopg import sql

TABLE = "schema_migrations"
FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
HEADER_RE = re.compile(r"^--\s*(title|erd|changes-data)\s*:\s*(.*?)\s*$", re.M)

CREATE_TABLE = f"""
create table if not exists {TABLE} (
    version     integer primary key check (version > 0),
    name        text not null,
    erd_version text,
    checksum    text not null,
    applied_at  timestamptz not null default now()
);
comment on table {TABLE} is '스키마 마이그레이션 적용 기록';
comment on column {TABLE}.version is '마이그레이션 번호';
comment on column {TABLE}.name is '마이그레이션 이름';
comment on column {TABLE}.erd_version is '적용 후 ERD 버전';
comment on column {TABLE}.checksum is '적용한 파일의 해시';
comment on column {TABLE}.applied_at is '적용 시각';
"""


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    title: str = ""
    erd_version: str | None = None
    changes_data: frozenset[str] = frozenset()

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()[:16]

    @property
    def label(self) -> str:
        return f"{self.version:04d} {self.title or self.name}"


def parse(file_name: str, text: str) -> Migration:
    m = FILE_RE.match(file_name)
    if not m:
        raise MigrationError(f"마이그레이션 파일 이름은 NNNN_이름.sql 형식이에요: {file_name}")
    head = dict(HEADER_RE.findall(text))
    changes = frozenset(t.strip() for t in head.get("changes-data", "").split(",") if t.strip())
    return Migration(int(m.group(1)), m.group(2), text, head.get("title", ""), head.get("erd") or None, changes)


def discover(directory=None) -> list[Migration]:
    """Shipped migrations in order. Numbers must run 1, 2, 3 … with no gap or repeat."""
    base = directory or files("ministp.store").joinpath("migrations")
    found = []
    if base.is_dir():
        for f in sorted(base.iterdir(), key=lambda p: p.name):
            if f.name.endswith(".sql"):
                found.append(parse(f.name, f.read_text(encoding="utf-8")))
    for expected, mig in enumerate(found, start=1):
        if mig.version != expected:
            raise MigrationError(f"마이그레이션 번호가 이어지지 않아요: {expected:04d} 자리에 {mig.version:04d}_{mig.name}")
    return found


# ---------- state ----------

def _has_table(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("select to_regclass(%s) is not null", (f"{_schema(conn)}.{name}",)).fetchone()[0]


def _schema(conn: psycopg.Connection) -> str:
    return conn.execute("select current_schema()").fetchone()[0]


def applied(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    """version -> (name, checksum). Empty for a database that has never been migrated."""
    if not _has_table(conn, TABLE):
        return {}
    return {v: (n, c) for v, n, c in conn.execute(f"select version, name, checksum from {TABLE}")}


def pending(conn: psycopg.Connection, migrations: list[Migration]) -> list[Migration]:
    """Migrations not applied yet. Refuses a database whose history does not match the files."""
    done = applied(conn)
    known = {m.version: m for m in migrations}
    for version, (name, checksum) in sorted(done.items()):
        mig = known.get(version)
        if mig is None:
            raise MigrationError(f"DB에는 {version:04d}_{name}이 적용돼 있는데 이 버전의 코드에는 없어요. "
                                 "코드를 최신으로 받아 주세요.")
        if mig.checksum != checksum:
            raise MigrationError(f"{version:04d}_{name}은 이미 적용됐는데 파일 내용이 달라졌어요. "
                                 "적용된 마이그레이션은 고치지 않고 새 번호로 추가해야 해요.")
    last = max(done, default=0)
    if sorted(done) != list(range(1, last + 1)):
        raise MigrationError(f"적용 기록에 빈 번호가 있어요: {sorted(done)}")
    return [m for m in migrations if m.version > last]


# ---------- fingerprint ----------

@dataclass(frozen=True)
class TablePrint:
    columns: tuple[str, ...]
    rows: int
    digest: str


def _tables(conn: psycopg.Connection) -> dict[str, tuple[str, ...]]:
    """Base tables of the current schema with their columns, the migration record excluded."""
    rows = conn.execute(
        "select c.relname, a.attname from pg_class c"
        " join pg_namespace n on n.oid = c.relnamespace"
        " join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped"
        " where n.nspname = current_schema() and c.relkind = 'r' and c.relname <> %s"
        " order by c.relname, a.attnum", (TABLE,)).fetchall()
    out: dict[str, list[str]] = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return {t: tuple(cs) for t, cs in out.items()}


def _print_of(conn: psycopg.Connection, table: str, columns: tuple[str, ...]) -> TablePrint:
    """Row count and an order-independent hash of every row over `columns`."""
    row = sql.SQL("md5(row({})::text)").format(sql.SQL(", ").join(sql.Identifier(c) for c in columns))
    q = sql.SQL("select count(*), md5(coalesce(string_agg({row}, '' order by {row}), '')) from {t}").format(
        row=row, t=sql.Identifier(table))
    n, digest = conn.execute(q).fetchone()
    return TablePrint(columns, n, digest)


def fingerprint(conn: psycopg.Connection, like: dict[str, TablePrint] | None = None) -> dict[str, TablePrint]:
    """Fingerprint of every table. With `like`, the same tables over the same columns as an earlier
    fingerprint, so a column a migration added does not count as a change to the rows that were there."""
    now = _tables(conn)
    if like is None:
        return {t: _print_of(conn, t, cols) for t, cols in now.items()}
    out = {}
    for t, before in like.items():
        if t in now and set(before.columns) <= set(now[t]):
            out[t] = _print_of(conn, t, before.columns)
    return out


# ---------- apply ----------

@dataclass(frozen=True)
class TableReport:
    table: str
    rows_before: int | None     # None: the migration created the table
    rows_after: int | None      # None: the migration dropped it (or dropped one of its columns)
    unchanged: bool             # the rows that existed are identical over their old columns
    declared: bool              # listed under `changes-data`


@dataclass(frozen=True)
class Report:
    migration: Migration
    applied: bool               # False: rehearsed and rolled back
    tables: list[TableReport] = field(default_factory=list)

    @property
    def new_tables(self) -> list[str]:
        return [t.table for t in self.tables if t.rows_before is None]

    @property
    def kept(self) -> list[TableReport]:
        return [t for t in self.tables if t.rows_before is not None and t.unchanged]


def _compare(mig: Migration, before: dict[str, TablePrint], after: dict[str, TablePrint],
             tables_after: dict[str, tuple[str, ...]]) -> list[TableReport]:
    out, broken = [], []
    for t, b in before.items():
        a = after.get(t)
        same = a is not None and (a.rows, a.digest) == (b.rows, b.digest)
        declared = t in mig.changes_data
        out.append(TableReport(t, b.rows, a.rows if a else None, same, declared))
        if not same and not declared:
            if a is None:
                broken.append(f"{t}: 테이블이나 기존 컬럼이 없어졌어요")
            elif a.rows != b.rows:
                broken.append(f"{t}: 행 수가 {b.rows:,} → {a.rows:,}로 바뀌었어요")
            else:
                broken.append(f"{t}: 기존 행의 내용이 바뀌었어요")
    for t in tables_after:
        if t not in before:
            out.append(TableReport(t, None, None, True, False))
    unknown = mig.changes_data - set(before)
    if unknown:
        broken.append(f"changes-data에 적힌 테이블이 없어요: {', '.join(sorted(unknown))}")
    if broken:
        raise MigrationError(f"{mig.label}: 기존 데이터가 바뀌어 되돌렸어요.\n  " + "\n  ".join(broken)
                             + "\n  의도한 변경이면 파일 머리에 '-- changes-data: 테이블'로 적어 주세요.")
    return out


def apply_one(conn: psycopg.Connection, mig: Migration) -> Report:
    """Run one migration in a transaction; any error or changed data rolls all of it back."""
    try:
        with conn.transaction():
            before = fingerprint(conn)
            try:
                conn.execute(mig.sql)
                conn.execute("set constraints all immediate")   # deferred checks now, not at some later commit
            except psycopg.Error as e:
                raise MigrationError(f"{mig.label}: SQL 오류로 되돌렸어요: "
                                     f"{e.diag.message_primary or type(e).__name__}") from e
            rows = _compare(mig, before, fingerprint(conn, like=before), _tables(conn))
            conn.execute(CREATE_TABLE)
            conn.execute(f"insert into {TABLE} (version, name, erd_version, checksum) values (%s, %s, %s, %s)",
                         (mig.version, mig.name, mig.erd_version, mig.checksum))
            return Report(mig, True, rows)
    except psycopg.Error as e:
        raise MigrationError(f"{mig.label}: 제약 위반으로 되돌렸어요: "
                             f"{e.diag.message_primary or type(e).__name__}") from e


def migrate(conn: psycopg.Connection, migrations: list[Migration] | None = None, *,
            dry_run: bool = False) -> list[Report]:
    """Apply every pending migration in order.

    `dry_run` rehearses: everything is applied and checked inside one outer
    transaction that is then rolled back, so the database is left exactly as it was."""
    todo = pending(conn, discover() if migrations is None else migrations)
    if not dry_run:
        return [apply_one(conn, m) for m in todo]
    reports = []
    with conn.transaction():
        reports = [Report(r.migration, False, r.tables) for r in (apply_one(conn, m) for m in todo)]
        raise psycopg.Rollback()
    return reports
