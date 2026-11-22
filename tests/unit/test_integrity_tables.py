"""The integrity report must guard the same tables the schema makes append-only."""
import re
from importlib.resources import files

from ministp.store.integrity import APPEND_ONLY_TABLES


def test_guarded_tables_are_the_schemas_append_only_tables():
    sql = files("ministp.store").joinpath("schema.sql").read_text(encoding="utf-8")
    listed = re.search(r"foreach t in array array\[(.*?)\]", sql, re.S).group(1)
    assert tuple(re.findall(r"'(\w+)'", listed)) == APPEND_ONLY_TABLES
