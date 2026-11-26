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
            # one row per column of each foreign key; a composite key (다계좌: 이벤트 순번 + 계좌) has several
            "select a.attname, rc.relname, ra.attname, u.ord = 1,"
            "       (select string_agg(x.attname, ', ' order by w.ord) from unnest(k.conkey) with ordinality w(num, ord)"
            "          join pg_attribute x on x.attrelid = k.conrelid and x.attnum = w.num)"
            " from pg_constraint k join pg_class c on c.oid = k.conrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " join pg_class rc on rc.oid = k.confrelid"
            " cross join lateral unnest(k.conkey, k.confkey) with ordinality u(num, refnum, ord)"
            " join pg_attribute a on a.attrelid = k.conrelid and a.attnum = u.num"
            " join pg_attribute ra on ra.attrelid = k.confrelid and ra.attnum = u.refnum"
            " where n.nspname = %s and c.relname = %s and k.contype = 'f' order by k.conkey[1], k.conname, u.ord",
            (schema, name)).fetchall()
        out[name] = {"kind": "view" if kind == "v" else "table", "comment": comment,
                     "columns": [{"name": c, "type": t, "not_null": nn, "comment": cm,
                                  "pk": c in pk, "unique": c in uniq} for c, t, nn, cm in cols],
                     "fks": [{"column": a, "ref_table": rt, "ref_column": rc, "first": first, "label": label}
                             for a, rt, rc, first, label in fks]}
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
        for f in (f for f in info["fks"] if f["first"]):    # one line per foreign key, not per column
            lines.append(f'    {f["ref_table"]} ||--o{{ {t} : "{f["label"]}"')
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
    ("고객과 계좌", "고객 한 명이 위탁계좌 여러 개를 가져요. 아래 모든 기록은 계좌 하나에 속해요. 계좌번호와 인증키는 DB에 없어요.",
     ["customers", "trading_accounts", "account_status_history"]),
    ("원장", "모든 기록의 원천. 이벤트가 쌓이면 분개·보유·예수금이 함께 남아요. 마감한 날로는 더 들어가지 않아요.",
     ["ledger_events", "journal_entries", "journal_lines", "position_ledger", "cash_ledger", "day_closes"]),
    ("기준정보", "계정과목과 종목 마스터.", ["accounts_chart", "instruments"]),
    ("주문", "주문 의도를 먼저 저장하고, 증권사 접수 결과를 따로 남겨요.", ["oms_orders", "oms_order_acks"]),
    ("증권사 응답과 대사", "증권사 응답 원본과 그 실행에서 나온 대사 결과.", ["broker_snapshots", "recon_results"]),
    ("공통코드", "코드와 코드명. 다른 테이블의 코드 컬럼은 여기를 FK로 묶지 않고, 점검(code check)이 어긋남을 찾아요.",
     ["code_groups", "common_codes"]),
]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------- relationship diagram (inline SVG, no script) ----------

# Bands of the diagram, each a list of four columns of tables (left to right = data flow).
LAYOUT = [
    ("고객과 계좌", [["customers"], ["trading_accounts"], ["account_status_history"], []]),
    ("원장 · 기준정보", [["ledger_events"], ["journal_entries", "position_ledger", "cash_ledger", "day_closes"],
                     ["journal_lines"], ["accounts_chart"]]),
    ("주문", [["instruments"], ["oms_orders"], ["oms_order_acks"], []]),
    ("증권사 응답과 대사", [["broker_snapshots"], ["recon_results"], [], []]),
    ("공통코드", [["code_groups"], ["common_codes"], [], []]),
]
BOX_W, COL_GAP, MARGIN = 232, 84, 28
HEAD_H, ROW_H, MORE_H, BOX_GAP, BAND_TOP, BAND_PAD = 54, 40, 28, 28, 44, 22
NCOLS = 4
SVG_W = MARGIN * 2 + NCOLS * BOX_W + (NCOLS - 1) * COL_GAP


def _short(s: str, n: int) -> str:
    s = (s or "").split(" (")[0]
    return s if len(s) <= n else s[: n - 1] + "…"


def _key_cols(info: dict) -> list[dict]:
    fk_cols = {f["column"] for f in info["fks"]}
    return [c for c in info["columns"] if c["pk"] or c["unique"] or c["name"] in fk_cols]


def render_svg(catalog: dict) -> str:
    tables = {k: v for k, v in catalog.items() if v["kind"] == "table"}
    placed = {t for _, cols in LAYOUT for col in cols for t in col}
    extra = [t for t in tables if t not in placed]
    layout = [(name, [[t for t in col if t in tables] for col in cols]) for name, cols in LAYOUT]
    for i in range(0, len(extra), NCOLS):
        chunk = extra[i:i + NCOLS]
        layout.append(("기타", [[t] for t in chunk] + [[] for _ in range(NCOLS - len(chunk))]))

    box, rows = {}, {}  # table -> (x, y, h); (table, column) -> y center
    out, y = [], MARGIN
    for band, cols in layout:
        if not any(cols):
            continue
        heights = []
        for ci, col in enumerate(cols):
            yy = y + BAND_TOP
            for t in col:
                keys = _key_cols(tables[t])
                more = len(tables[t]["columns"]) - len(keys)
                h = HEAD_H + ROW_H * len(keys) + (MORE_H if more else 0)
                box[t] = (MARGIN + ci * (BOX_W + COL_GAP), yy, h)
                for i, c in enumerate(keys):
                    rows[(t, c["name"])] = yy + HEAD_H + ROW_H * i + ROW_H / 2
                yy += h + BOX_GAP
            heights.append(yy - BOX_GAP - y)
        band_h = max(heights) + BAND_PAD
        out.append(f'<rect class="band" x="8" y="{y - 12}" width="{SVG_W - 16}" height="{band_h + 12}" rx="10"/>'
                   f'<text class="band-t" x="{MARGIN}" y="{y + 14}">{_esc(band)}</text>')
        y += band_h + 24
    height = y

    edges = []
    for t, info in tables.items():
        for f in info["fks"]:
            p, c = f["ref_table"], t
            if not f["first"] or p not in box or c not in box:
                continue
            py, cy = rows.get((p, f["ref_column"]), box[p][1] + HEAD_H / 2), rows[(c, f["column"])]
            (px, _, _), (cx, _, _) = box[p], box[c]
            if p == c:  # self reference: loop on the right side
                r = px + BOX_W
                d = f"M{r},{py} h14 a8,8 0 0 1 8,8 V{cy - 8} a8,8 0 0 1 -8,8 h-14"
            elif cx > px:
                a, b = px + BOX_W, cx
                m = (a + b) / 2
                d = f"M{a},{py} C{m},{py} {m},{cy} {b},{cy}"
            elif cx < px:
                a, b = px, cx + BOX_W
                m = (a + b) / 2
                d = f"M{a},{py} C{m},{py} {m},{cy} {b},{cy}"
            else:  # same column, different box: loop on the left side
                d = f"M{px},{py} h-14 a8,8 0 0 0 -8,8 V{cy - 8} a8,8 0 0 0 8,8 h14"
            label = f'{p}.{f["ref_column"]} → {c}.{f["column"]}'
            edges.append(f'<path class="edge" d="{d}" marker-start="url(#one)" marker-end="url(#many)">'
                         f"<title>{_esc(label)}</title></path>")

    boxes = []
    fk_of = {t: {f["column"]: f for f in info["fks"]} for t, info in tables.items()}
    for t, (x, by, h) in box.items():
        info = tables[t]
        keys = _key_cols(info)
        more = len(info["columns"]) - len(keys)
        g = [f'<g class="node"><a href="#t-{t}"><title>{_esc(t)} {_esc(info["comment"])}</title>',
             f'<rect class="box" x="{x}" y="{by}" width="{BOX_W}" height="{h}" rx="8"/>',
             f'<path class="head" d="M{x},{by + 8} a8,8 0 0 1 8,-8 h{BOX_W - 16} a8,8 0 0 1 8,8 '
             f'v{HEAD_H - 8} h-{BOX_W} z"/>',
             f'<text class="tn" x="{x + 14}" y="{by + 22}">{_esc(t)}</text>',
             f'<text class="tk" x="{x + 14}" y="{by + 42}">{_esc(_short(info["comment"], 16))}</text>']
        for i, c in enumerate(keys):
            ry = by + HEAD_H + ROW_H * i
            kind = "pk" if c["pk"] else ("fk" if c["name"] in fk_of[t] else "uk")
            tag = {"pk": "PK", "fk": "FK", "uk": "UK"}[kind]
            if i:
                g.append(f'<line class="sep" x1="{x + 12}" x2="{x + BOX_W - 12}" y1="{ry}" y2="{ry}"/>')
            g.append(f'<rect class="chip {kind}" x="{x + 12}" y="{ry + 12}" width="26" height="16" rx="3"/>'
                     f'<text class="chip-t {kind}" x="{x + 25}" y="{ry + 24}">{tag}</text>'
                     f'<text class="cn" x="{x + 46}" y="{ry + 18}">{_esc(c["name"])}</text>'
                     f'<text class="ck" x="{x + 46}" y="{ry + 33}">{_esc(_short(c["comment"], 15))}</text>')
        if more:
            g.append(f'<text class="more" x="{x + 14}" y="{by + h - 10}">외 {more}개 컬럼</text>')
        g.append("</a></g>")
        boxes.append("".join(g))

    defs = ('<defs>'
            '<marker id="one" viewBox="0 0 12 16" refX="1" refY="8" markerWidth="12" markerHeight="16" '
            'markerUnits="userSpaceOnUse" orient="auto">'
            '<path class="mk" d="M5,2 V14 M9,2 V14"/></marker>'
            '<marker id="many" viewBox="0 0 16 16" refX="15" refY="8" markerWidth="16" markerHeight="16" '
            'markerUnits="userSpaceOnUse" orient="auto">'
            '<path class="mk" d="M1,8 H15 M15,1 L3,8 L15,15 M1,2 V14"/></marker></defs>')
    return (f'<svg class="erd" viewBox="0 0 {SVG_W} {height}" width="{SVG_W}" height="{height}" '
            f'role="img" aria-label="MiniSTP 테이블 관계도">{defs}{"".join(out)}'
            f'{"".join(edges)}{"".join(boxes)}</svg>')


def render_html(catalog: dict, version: str, digest: str, history_rows: list[tuple[str, str, str]]) -> str:
    diagram = render_svg(catalog)
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
  --band: #f1f4f2; --box-line: #b9c3be; --edge: #2b5d9a;
  --sans: "IBM Plex Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", system-ui, sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #121615; --surface: #1a201e; --fg: #e4e9e6; --muted: #9aa6a1; --line: #2c3532;
  --accent: #5cc1a3; --accent-soft: #1d332c; --pk: #e7b65a; --pk-soft: #33291a; --fk: #8db4e8; --fk-soft: #1d2836;
  --band: #161b1a; --box-line: #44504b; --edge: #8db4e8; color-scheme: dark }} }}
:root[data-theme="dark"] {{
  --bg: #121615; --surface: #1a201e; --fg: #e4e9e6; --muted: #9aa6a1; --line: #2c3532;
  --accent: #5cc1a3; --accent-soft: #1d332c; --pk: #e7b65a; --pk-soft: #33291a; --fk: #8db4e8; --fk-soft: #1d2836;
  --band: #161b1a; --box-line: #44504b; --edge: #8db4e8; color-scheme: dark }}
* {{ box-sizing: border-box }}
body {{ background: var(--bg); color: var(--fg); font: 15px/1.6 var(--sans); }}
.wrap {{ max-width: 1240px; margin: 0 auto; padding-block: 40px 64px;
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
.intro {{ max-width: 72ch; color: var(--muted); margin: 16px 0 0 }}
.diagram {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 8px; overflow-x: auto }}
.erd {{ display: block; width: 100%; min-width: 1000px; height: auto; font-family: var(--sans) }}
.erd .band {{ fill: var(--band) }}
.erd .band-t {{ fill: var(--accent); font-size: 15px; font-weight: 600 }}
.erd .box {{ fill: var(--surface); stroke: var(--box-line); stroke-width: 1.5 }}
.erd .head {{ fill: var(--accent-soft) }}
.erd .node:hover .box, .erd .node a:focus-visible .box {{ stroke: var(--accent); stroke-width: 2.5 }}
.erd a:focus {{ outline: none }}
.erd .tn {{ font: 600 15px var(--mono); fill: var(--fg) }}
.erd .tk {{ font-size: 13px; fill: var(--muted) }}
.erd .cn {{ font: 500 13.5px var(--mono); fill: var(--fg) }}
.erd .ck {{ font-size: 12px; fill: var(--muted) }}
.erd .more {{ font-size: 12px; fill: var(--muted) }}
.erd .sep {{ stroke: var(--line) }}
.erd .chip.pk {{ fill: var(--pk-soft) }} .erd .chip.fk {{ fill: var(--fk-soft) }} .erd .chip.uk {{ fill: var(--accent-soft) }}
.erd .chip-t {{ font: 600 10.5px var(--mono); text-anchor: middle }}
.erd .chip-t.pk {{ fill: var(--pk) }} .erd .chip-t.fk {{ fill: var(--fk) }} .erd .chip-t.uk {{ fill: var(--accent) }}
.erd .edge {{ fill: none; stroke: var(--edge); stroke-width: 2 }}
.erd .edge:hover {{ stroke-width: 3.5 }}
.erd .mk {{ fill: none; stroke: var(--edge); stroke-width: 2 }}
.legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; margin: 0 0 10px }}
.legend .k {{ margin-left: 6px }} .legend .k:first-child {{ margin-left: 0 }}
.rel {{ font-family: var(--mono); color: var(--edge); font-weight: 600; margin-left: 6px }}
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
  <p class="legend"><span class="k pk">PK</span> 기본키 <span class="k fk">FK</span> 외래키 <span class="k uk">UK</span> 유일키 <span class="rel">‖──&lt;</span> 1 : N (한 행이 여러 행에 연결) · 키 컬럼만 그렸고, 상자를 누르면 전체 컬럼으로 이동해요.</p>
  <div class="diagram">{diagram}</div>

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
