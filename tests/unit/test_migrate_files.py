"""Migration files: naming, headers and numbering (no database needed)."""
import pytest

from ministp.store import migrate as mg


def test_file_name_and_headers_are_parsed():
    m = mg.parse("0001_accounts.sql", "-- title: 다계좌\n-- erd: 2.0\n-- changes-data: a, b\nselect 1;\n")
    assert (m.version, m.name, m.title, m.erd_version) == (1, "accounts", "다계좌", "2.0")
    assert m.changes_data == {"a", "b"} and m.label == "0001 다계좌"
    assert mg.parse("0002_x.sql", "select 1;").changes_data == frozenset()


def test_checksum_follows_the_file_content():
    a, b = mg.parse("0001_x.sql", "select 1;"), mg.parse("0001_x.sql", "select 2;")
    assert a.checksum != b.checksum and a.checksum == mg.parse("0001_x.sql", "select 1;").checksum


@pytest.mark.parametrize("name", ["1_x.sql", "0001-x.sql", "0001_X.sql", "accounts.sql"])
def test_bad_file_names_are_refused(name):
    with pytest.raises(mg.MigrationError, match="NNNN"):
        mg.parse(name, "select 1;")


def test_numbers_must_run_without_gaps(tmp_path):
    (tmp_path / "0001_a.sql").write_text("select 1;", encoding="utf-8")
    (tmp_path / "0003_c.sql").write_text("select 1;", encoding="utf-8")
    with pytest.raises(mg.MigrationError, match="이어지지 않아요"):
        mg.discover(tmp_path)
    (tmp_path / "0002_b.sql").write_text("select 1;", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not a migration", encoding="utf-8")
    assert [m.version for m in mg.discover(tmp_path)] == [1, 2, 3]


def test_shipped_migrations_end_at_the_schema_version():
    """`schema.sql` is what a new database gets; the last migration must bring an old one to the same ERD version."""
    from ministp.store import erd
    shipped = mg.discover()
    assert [m.version for m in shipped] == list(range(1, len(shipped) + 1)) and shipped
    assert all(m.title and m.erd_version for m in shipped)
    assert shipped[-1].erd_version == erd.schema_version(erd.schema_sql())
    assert shipped[0].name == "multi_account" and not shipped[0].changes_data
