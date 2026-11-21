"""Regenerate docs/screens/ (화면정의서 + 영향도) from the screen definitions and the DB comments.

    MINISTP_DATABASE_URL=postgresql://... python scripts/gen_screen_docs.py

Run after changing a screen definition or a schema COMMENT. Like gen_erd.py it
builds the schema in a throwaway schema that is rolled back.
"""
import os
import sys
from pathlib import Path

import psycopg

from ministp.store.erd import build_catalog, schema_sql
from ministp.ui.definitions import load_screens, validate
from ministp.ui.spec import all_docs

OUT = Path(__file__).resolve().parents[1] / "docs" / "screens"


def main() -> int:
    url = os.environ.get("MINISTP_DATABASE_URL") or os.environ.get("MINISTP_TEST_DATABASE_URL")
    if not url:
        print("MINISTP_DATABASE_URL을 설정해 주세요.")
        return 2
    with psycopg.connect(url) as conn:
        catalog = build_catalog(conn, schema_sql())
    screens = load_screens()
    problems = validate(screens, catalog)
    if problems:
        print("화면 정의 오류:\n  " + "\n  ".join(problems))
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in all_docs(screens, catalog).items():
        (OUT / name).write_text(text, encoding="utf-8")
    print(f"생성: docs/screens/ ({len(screens)}개 화면)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
