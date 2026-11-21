"""ERD and data dictionary (데이터 사전) generated from the live schema.

The schema is built in a throwaway PostgreSQL schema, read back from the catalog
(columns, keys, Korean COMMENTs) and written as Markdown with a Mermaid diagram.
Generating from the database, not from hand-kept notes, keeps the ERD honest.
"""
import hashlib
import re
from importlib.resources import files

import psycopg

TMP_SCHEMA = "ministp_erd_tmp"
VERSION_RE = re.compile(r"-- ERD version: (\d+\.\d+)")
STAMP_RE = re.compile(r"ERD 버전: v(\d+\.\d+) · 스키마 해시: `([0-9a-f]+)`")


class ErdError(RuntimeError):
    pass


def schema_sql() -> str:
    return files("ministp.store").joinpath("schema.sql").read_text(encoding="utf-8")


def schema_version(sql: str) -> str:
    m = VERSION_RE.search(sql)
    if not m:
        raise ErdError("schema.sql has no '-- ERD version: X.Y' line")
    return m.group(1)


def schema_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]


def read_catalog(conn: psycopg.Connection, schema: str) -> dict:
    rels = conn.execute(
        "select c.relname, c.relkind, obj_description(c.oid, 'pg_class')"
        " from pg_class c join pg_namespace n on n.oid = c.relnamespace"
        " where n.nspname = %s and c.relkind in ('r', 'v') order by c.relkind, c.oid",
        (schema,)).fetchall()
    out = {}
    for name, kind, comment in rels:
        cols = conn.execute(
            "select a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,"
            " col_description(a.attrelid, a.attnum)"
            " from pg_attribute a join pg_class c on c.oid = a.attrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = %s and c.relname = %s and a.attnum > 0 and not a.attisdropped"
            " order by a.attnum", (schema, name)).fetchall()
        pk = {r[0] for r in conn.execute(
            "select a.attname from pg_index i join pg_class c on c.oid = i.indrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)"
            " where n.nspname = %s and c.relname = %s and i.indisprimary", (schema, name))}
        uniq = {r[0] for r in conn.execute(
            "select a.attname from pg_index i join pg_class c on c.oid = i.indrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)"
            " where n.nspname = %s and c.relname = %s and i.indisunique and not i.indisprimary"
            " and array_length(i.indkey, 1) = 1", (schema, name))}
        fks = conn.execute(
            "select a.attname, rc.relname, ra.attname"
            " from pg_constraint k join pg_class c on c.oid = k.conrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " join pg_class rc on rc.oid = k.confrelid"
            " join pg_attribute a on a.attrelid = k.conrelid and a.attnum = k.conkey[1]"
            " join pg_attribute ra on ra.attrelid = k.confrelid and ra.attnum = k.confkey[1]"
            " where n.nspname = %s and c.relname = %s and k.contype = 'f' order by a.attnum",
            (schema, name)).fetchall()
        out[name] = {"kind": "view" if kind == "v" else "table", "comment": comment,
                     "columns": [{"name": c, "type": t, "not_null": nn, "comment": cm,
                                  "pk": c in pk, "unique": c in uniq} for c, t, nn, cm in cols],
                     "fks": [{"column": a, "ref_table": rt, "ref_column": rc} for a, rt, rc in fks]}
    return out


def build_catalog(conn: psycopg.Connection, sql: str) -> dict:
    """Run schema.sql inside a throwaway schema and read it back."""
    with conn.transaction(force_rollback=True):
        conn.execute(f"create schema {TMP_SCHEMA}")
        conn.execute(f"set local search_path to {TMP_SCHEMA}")
        conn.execute(sql)
        return read_catalog(conn, TMP_SCHEMA)


def missing_names(catalog: dict) -> list[str]:
    miss = []
    for t, info in catalog.items():
        if not info["comment"]:
            miss.append(t)
        miss += [f"{t}.{c['name']}" for c in info["columns"] if not c["comment"]]
    return miss


def _mermaid_type(t: str) -> str:
    return {"timestamp with time zone": "timestamptz", "character(1)": "char1"}.get(t, t.replace(" ", "_"))


def _clean(s: str) -> str:
    return (s or "").replace('"', "'")


def render(catalog: dict, version: str, digest: str, history: str) -> str:
    tables = {k: v for k, v in catalog.items() if v["kind"] == "table"}
    views = {k: v for k, v in catalog.items() if v["kind"] == "view"}
    lines = ["# MiniSTP ERD", "",
             f"ERD 버전: v{version} · 스키마 해시: `{digest}`", "",
             "이 문서는 `scripts/gen_erd.py`가 `src/ministp/store/schema.sql`로 만든 DB에서 읽어 생성해요. "
             "직접 고치지 말고 스키마의 한글 주석(COMMENT)을 고친 뒤 다시 생성하세요. "
             "모든 원장 테이블은 append-only(수정·삭제 불가)예요.", "",
             "## 관계도", "", "```mermaid", "erDiagram"]
    for t, info in tables.items():
        lines.append(f"    {t} {{")
        for c in info["columns"]:
            keys = [k for k, on in (("PK", c["pk"]), ("FK", any(f["column"] == c["name"] for f in info["fks"])),
                                    ("UK", c["unique"])) if on]
            key = f" {','.join(keys)}" if keys else ""
            lines.append(f'        {_mermaid_type(c["type"])} {c["name"]}{key} "{_clean(c["comment"])}"')
        lines.append("    }")
    for t, info in tables.items():
        for f in info["fks"]:
            lines.append(f'    {f["ref_table"]} ||--o{{ {t} : "{f["column"]}"')
    lines += ["```", "", "## 데이터 사전", ""]
    for t, info in list(tables.items()) + list(views.items()):
        label = "뷰" if info["kind"] == "view" else "테이블"
        lines += [f"### {t} — {info['comment']} ({label})", "",
                  "| 컬럼 | 한글명 | 타입 | 필수 | 키 |", "|---|---|---|---|---|"]
        for c in info["columns"]:
            fk = next((f for f in info["fks"] if f["column"] == c["name"]), None)
            key = ", ".join(x for x in ["PK" if c["pk"] else "", "UK" if c["unique"] else "",
                                        f"FK → {fk['ref_table']}.{fk['ref_column']}" if fk else ""] if x)
            req = "Y" if c["not_null"] else ""
            lines.append(f"| `{c['name']}` | {c['comment']} | {c['type']} | {req} | {key} |")
        lines.append("")
    lines += ["## 변경 이력", "", history.strip(), ""]
    return "\n".join(lines)


def generate(conn: psycopg.Connection, history: str, previous: str | None = None) -> str:
    """Returns the ERD markdown. Refuses if the schema changed but its version did not."""
    sql = schema_sql()
    version, digest = schema_version(sql), schema_hash(sql)
    if previous:
        m = STAMP_RE.search(previous)
        if m and m.group(1) == version and m.group(2) != digest:
            raise ErdError(f"schema.sql이 바뀌었는데 ERD 버전이 v{version} 그대로예요. "
                           "'-- ERD version' 줄과 docs/erd/HISTORY.md를 올려 주세요.")
    if f"v{version}" not in history:
        raise ErdError(f"docs/erd/HISTORY.md에 v{version} 항목이 없어요.")
    catalog = build_catalog(conn, sql)
    miss = missing_names(catalog)
    if miss:
        raise ErdError(f"한글 명칭(COMMENT)이 없는 항목: {', '.join(miss)}")
    return render(catalog, version, digest, history)


# ---------- shareable HTML page ----------

GROUPS = [
    ("원장", "모든 기록의 원천. 이벤트가 쌓이면 분개·보유·예수금이 함께 남아요.",
     ["ledger_events", "journal_entries", "journal_lines", "position_ledger", "cash_ledger"]),
    ("기준정보", "계정과목과 종목 마스터.", ["accounts_chart", "instruments"]),
    ("주문", "주문 의도를 먼저 저장하고, 증권사 접수 결과를 따로 남겨요.", ["oms_orders", "oms_order_acks"]),
    ("증권사 응답과 대사", "증권사 응답 원본과 그 실행에서 나온 대사 결과.", ["broker_snapshots", "recon_results"]),
]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_html(catalog: dict, version: str, digest: str, history_rows: list[tuple[str, str, str]]) -> str:
    md = render(catalog, version, digest, "")
    mermaid = md.split("```mermaid\n", 1)[1].split("```", 1)[0]
    views = [k for k, v in catalog.items() if v["kind"] == "view"]
    groups = GROUPS + [("조회 뷰", "원장에서 계산해 보여주는 읽기 전용 화면.", views)]
    known = {t for _, _, ts in groups for t in ts}
    extra = [t for t in catalog if t not in known]
    if extra:
        groups.append(("기타", "", extra))

    def table_block(t: str) -> str:
        info = catalog[t]
        rows = []
        for c in info["columns"]:
            fk = next((f for f in info["fks"] if f["column"] == c["name"]), None)
            keys = []
            if c["pk"]:
                keys.append('<span class="k pk">PK</span>')
            if c["unique"]:
                keys.append('<span class="k uk">UK</span>')
            if fk:
                keys.append(f'<span class="k fk">FK</span> <a href="#t-{fk["ref_table"]}">'
                            f'{fk["ref_table"]}.{fk["ref_column"]}</a>')
            rows.append(f"<tr><td><code>{_esc(c['name'])}</code></td><td>{_esc(c['comment'])}</td>"
                        f"<td class='ty'>{_esc(c['type'])}</td><td class='nn'>{'●' if c['not_null'] else ''}</td>"
                        f"<td>{' '.join(keys)}</td></tr>")
        kind = "뷰" if info["kind"] == "view" else f"{len(info['columns'])}개 컬럼"
        return (f'<section class="tbl" id="t-{t}"><header><h4><code>{t}</code> {_esc(info["comment"])}</h4>'
                f'<span class="meta">{kind}</span></header><div class="scroll"><table>'
                "<thead><tr><th>컬럼</th><th>한글명</th><th>타입</th><th>필수</th><th>키</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div></section>")

    group_html = "".join(
        f'<section class="group"><h3>{g}</h3><p class="lede">{_esc(d)}</p>{"".join(table_block(t) for t in ts)}</section>'
        for g, d, ts in groups if ts)
    toc = "".join(f'<a href="#t-{t}"><code>{t}</code><span>{_esc(catalog[t]["comment"].split(" (")[0])}</span></a>'
                  for _, _, ts in groups for t in ts)
    hist = "".join(f"<tr><td>{_esc(v)}</td><td>{_esc(d)}</td><td>{_esc(x)}</td></tr>" for v, d, x in history_rows)
    n_tables = sum(1 for v in catalog.values() if v["kind"] == "table")
    n_cols = sum(len(v["columns"]) for v in catalog.values() if v["kind"] == "table")
    return f"""<title>MiniSTP ERD</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+KR:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one reading column; the diagram and wide tables scroll inside their own boxes. */
:root {{
  --bg: #f6f7f5; --surface: #ffffff; --fg: #1c2321; --muted: #5d6763; --line: #dde2df;
  --accent: #1f6f5c; --accent-soft: #e3f0eb; --pk: #8a5a00; --pk-soft: #fbf0d9; --fk: #2b5d9a; --fk-soft: #e4ecf7;
  --sans: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #121615; --surface: #1a201e; --fg: #e4e9e6; --muted: #9aa6a1; --line: #2c3532;
  --accent: #5cc1a3; --accent-soft: #1d332c; --pk: #e7b65a; --pk-soft: #33291a; --fk: #8db4e8; --fk-soft: #1d2836; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #121615; --surface: #1a201e; --fg: #e4e9e6; --muted: #9aa6a1; --line: #2c3532;
  --accent: #5cc1a3; --accent-soft: #1d332c; --pk: #e7b65a; --pk-soft: #33291a; --fk: #8db4e8; --fk-soft: #1d2836; color-scheme: dark }}
* {{ box-sizing: border-box }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.6 var(--sans); }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding-block: 40px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }}
h1 {{ font-size: 28px; font-weight: 600; margin: 0; letter-spacing: -0.01em; text-wrap: balance }}
h2 {{ font-size: 19px; font-weight: 600; margin: 48px 0 12px }}
h3 {{ font-size: 16px; font-weight: 600; margin: 32px 0 2px; color: var(--accent) }}
h4 {{ font-size: 15px; font-weight: 600; margin: 0 }}
code {{ font-family: var(--mono); font-size: 0.92em }}
a {{ color: var(--fk) }}
.stamp {{ display: flex; flex-wrap: wrap; gap: 8px 16px; margin: 12px 0 0; color: var(--muted); font-size: 13px }}
.stamp b {{ color: var(--fg); font-weight: 600 }}
.badge {{ background: var(--accent-soft); color: var(--accent); padding: 2px 10px; border-radius: 99px; font-weight: 600 }}
.intro {{ max-width: 68ch; color: var(--muted); margin: 16px 0 0 }}
.diagram {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 16px; overflow-x: auto }}
.diagram pre {{ margin: 0; min-width: 900px }}
.toc {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 6px }}
.toc a {{ display: flex; flex-direction: column; padding: 8px 12px; border: 1px solid var(--line); border-radius: 6px;
  text-decoration: none; color: var(--fg); background: var(--surface) }}
.toc a:hover, .toc a:focus-visible {{ border-color: var(--accent); outline: none }}
.toc span {{ font-size: 12px; color: var(--muted) }}
.lede {{ margin: 0 0 12px; color: var(--muted); font-size: 14px }}
.tbl {{ margin: 0 0 16px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; scroll-margin-top: 16px }}
.tbl header {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline; gap: 4px 12px;
  padding: 12px 16px; border-bottom: 1px solid var(--line) }}
.meta {{ color: var(--muted); font-size: 12px }}
.scroll {{ overflow-x: auto }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px }}
th, td {{ text-align: left; padding: 7px 16px; border-bottom: 1px solid var(--line); vertical-align: top }}
tbody tr:last-child td {{ border-bottom: 0 }}
th {{ font-size: 12px; font-weight: 500; color: var(--muted); letter-spacing: 0.02em }}
td.ty {{ font-family: var(--mono); font-size: 12.5px; color: var(--muted); white-space: nowrap }}
td.nn {{ color: var(--accent); text-align: center; width: 48px }}
.k {{ font-family: var(--mono); font-size: 11px; font-weight: 500; padding: 1px 6px; border-radius: 4px }}
.pk {{ background: var(--pk-soft); color: var(--pk) }}
.uk {{ background: var(--accent-soft); color: var(--accent) }}
.fk {{ background: var(--fk-soft); color: var(--fk) }}
.hist td:first-child {{ font-family: var(--mono); white-space: nowrap; font-weight: 500 }}
.hist td:nth-child(2) {{ white-space: nowrap; color: var(--muted); font-variant-numeric: tabular-nums }}
footer {{ margin-top: 40px; color: var(--muted); font-size: 13px }}
</style>
<div class="wrap">
  <h1>MiniSTP ERD</h1>
  <div class="stamp"><span class="badge">v{version}</span><span>테이블 <b>{n_tables}</b>개 · 컬럼 <b>{n_cols}</b>개 · 뷰 <b>{len(views)}</b>개</span><span>스키마 해시 <code>{digest}</code></span></div>
  <p class="intro">백오피스 연계 매매 시스템의 PostgreSQL 스키마예요. DB에서 직접 읽어 만들었고, 한글 명칭은 DB 주석(COMMENT)이라 pgAdmin에서도 똑같이 보여요. 원장 테이블은 모두 수정·삭제가 막혀 있고(append-only), 정정은 새 행으로 남겨요.</p>

  <h2>관계도</h2>
  <div class="diagram"><pre class="mermaid">
{mermaid}</pre></div>

  <h2>테이블 목록</h2>
  <nav class="toc" aria-label="테이블 목록">{toc}</nav>

  <h2>데이터 사전</h2>
  {group_html}

  <h2>변경 이력</h2>
  <div class="tbl"><div class="scroll"><table class="hist"><thead><tr><th>버전</th><th>일자</th><th>변경 내용</th></tr></thead><tbody>{hist}</tbody></table></div></div>
  <footer>원본: github.com/refeel2714/MiniSTP · docs/erd/ERD.md</footer>
</div>
"""


def history_rows(history: str) -> list[tuple[str, str, str]]:
    rows = []
    for line in history.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0].startswith("v"):
            rows.append(tuple(cells))
    return list(reversed(rows))
