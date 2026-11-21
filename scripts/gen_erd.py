"""Regenerate docs/erd/ERD.md from schema.sql (run after any schema change).

    MINISTP_DATABASE_URL=postgresql://... python scripts/gen_erd.py

Builds the schema in a throwaway schema inside a transaction that is rolled
back, so it is safe to point at your own MiniSTP database.
"""
import os
import sys
from pathlib import Path

import psycopg

from ministp.store.erd import (ErdError, build_catalog, generate, history_rows, render_html,
                               schema_hash, schema_sql, schema_version)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "erd" / "ERD.md"
HISTORY = ROOT / "docs" / "erd" / "HISTORY.md"
HTML = ROOT / "docs" / "erd" / "erd.html"


def main() -> int:
    url = os.environ.get("MINISTP_DATABASE_URL") or os.environ.get("MINISTP_TEST_DATABASE_URL")
    if not url:
        print("MINISTP_DATABASE_URL을 설정해 주세요.")
        return 2
    previous = OUT.read_text(encoding="utf-8") if OUT.exists() else None
    with psycopg.connect(url) as conn:
        try:
            history = HISTORY.read_text(encoding="utf-8")
            text = generate(conn, history, previous)
            sql = schema_sql()
            html = render_html(build_catalog(conn, sql), schema_version(sql), schema_hash(sql),
                               history_rows(history))
        except ErdError as e:
            print(f"오류: {e}")
            return 1
    OUT.write_text(text, encoding="utf-8")
    HTML.write_text(html, encoding="utf-8")
    print(f"생성: {OUT.relative_to(ROOT)}, {HTML.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
