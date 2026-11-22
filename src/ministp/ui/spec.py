"""화면정의서 generator: the screen spec is written from the definition, never by hand.

Each screen's document lists its conditions, grids, transactions and, for every
column, the DB column and the Korean name taken from the schema COMMENT. The
index adds 영향도 (which screens show a DB column), so a schema change shows
at once which screens it touches.
"""
from .definitions import Screen, column_comment, resolve, short, split_sides, usages
from .transactions import TRANSACTIONS

LOAD_TEXT = {"search": "조회 버튼", "open": "화면 열 때 + 조회 버튼", "master": "목록(마스터)에서 행 선택",
             "input": "조회조건을 바꿀 때마다 (입력 즉시 재계산)"}
FORMAT_TEXT = {"text": "문자", "code": "코드", "number": "수량", "money": "금액", "rate": "비율·단가",
               "date": "날짜", "datetime": "일시", "json": "JSON"}
COLOR_TEXT = {None: "", "sign": "부호(+빨강/-파랑)", "side": "매수 빨강/매도 파랑", "status": "상태", "verdict": "판단(가능 초록/불가 빨강)"}


def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _names(grids) -> str:
    return ", ".join(f"`{g.id}` {g.title}" for g in grids)


def screen_markdown(screen: Screen, catalog: dict) -> str:
    r = resolve(screen, catalog)
    out = [f"# {screen.id} {screen.title}", "", f"> 이 문서는 `src/ministp/ui/screens/{screen.file}`에서 생성돼요."
           " 직접 고치지 말고 화면 정의를 고친 뒤 `python scripts/gen_screen_docs.py`를 실행하세요.", "",
           "## 목적", "", screen.purpose, ""]
    if screen.layout == "split":
        left, right, below = split_sides(screen)
        out += ["## 화면 구성", "", "좌우 구성이에요. 왼쪽 목록에서 행을 고르면 오른쪽이 그 행의 상세로 바뀌어요.", "",
                f"- 왼쪽 (너비 {screen.left}%): {_names(left)}", f"- 오른쪽: {_names(right)}"]
        if below:
            out.append(f"- 아래 (고른 행과 무관): {_names(below)}")
        out.append("")
    if screen.conditions:
        out += ["## 조회조건", "", "| 조건 | 한글명 | 형식 | DB 컬럼 | 필수 | 기본값 | 선택값 |",
                "|---|---|---|---|---|---|---|"]
        for c, cd in zip(screen.conditions, r["conditions"]):
            out.append(f"| `{c.name}` | {_cell(cd['label'])} | {c.type} | {_cell(c.source or '-')} "
                       f"| {'예' if c.required else ''} | {_cell(c.default)} | {_cell(', '.join(c.options))} |")
        out.append("")
    out += ["## 트랜잭션", "", "| 트랜잭션 | 이름 | 조회조건 | 쓰는 그리드 |", "|---|---|---|---|"]
    seen: dict[str, list[str]] = {}
    for g in screen.grids:
        seen.setdefault(g.tx, []).append(g.id)
    for tx_id, gids in seen.items():
        tx = TRANSACTIONS[tx_id]
        params = ", ".join(f"`{k}` ({v})" for k, v in tx.params.items()) or "없음"
        out.append(f"| `{tx_id}` | {tx.title} | {params} | {', '.join(gids)} |")
    out.append("")
    for g, gd in zip(screen.grids, r["grids"]):
        out += [f"## 그리드 `{g.id}` {g.title}", ""]
        facts = [f"- 트랜잭션: `{g.tx}` → 데이터셋 `{g.dataset}`", f"- 불러오는 때: {LOAD_TEXT[g.load]}"]
        if g.master:
            link = ", ".join(f"`{p}` ← `{g.master}.{c}`" for p, c in g.link.items())
            facts.append(f"- 마스터: `{g.master}` ({link})")
        if g.weight != 1:
            facts.append(f"- 높이 비중: {g.weight} (좌우 구성에서 같은 쪽 그리드와 나눠 쓰는 높이)")
        if g.view != "grid":
            facts.append("- 표시: 요약 카드")
        if g.totals:
            facts.append(f"- 합계: {', '.join(f'`{t}`' for t in g.totals)}")
        if g.meta:
            facts.append(f"- 상태 표시: {', '.join(f'{v} (`{k}`)' for k, v in g.meta.items())}")
        if g.drill:
            params = ", ".join(f"`{k}` ← `{v}`" for k, v in g.drill.get("params", {}).items())
            if g.drill.get("account"):
                params = ", ".join(x for x in (params, f"계좌 ← `{g.drill['account']}`") if x)
            facts.append(f"- 행 더블클릭·Enter: {g.drill['screen']}로 이동 ({params})")
        if g.live:
            facts.append(f"- 실시간: DB가 바뀌면 마지막 조회조건으로 다시 조회, 바뀐 칸 강조 (행 식별 열 `{g.key or g.columns[0].name}`)")
        if g.chart:
            facts.append(f"- 차트: `{g.chart['x']}` 대비 `{g.chart['y']}` 선 그래프")
        out += facts + ["", "| 열 | 한글명 | DB 컬럼 | DB 주석 | 형식 | 정렬 | 색 |", "|---|---|---|---|---|---|---|"]
        for c, cd in zip(g.columns, gd["columns"]):
            comment = column_comment(catalog, c.source) if c.source else "(화면 계산·집계 값)"
            out.append(f"| `{c.name}` | {_cell(cd['label'])} | {_cell(c.source or '-')} | {_cell(comment)} "
                       f"| {FORMAT_TEXT[c.format]} | {'오른쪽' if c.align == 'right' else '왼쪽'} "
                       f"| {COLOR_TEXT[c.color]} |")
        out.append("")
    return "\n".join(out)


def index_markdown(screens: dict[str, Screen], catalog: dict) -> str:
    out = ["# STP-UI 화면 목록과 영향도", "",
           "> `python scripts/gen_screen_docs.py`로 생성돼요. 화면 정의(YAML)와 DB 주석에서 만들어지니 직접 고치지 마세요.",
           "", "## 화면", "", "| 화면번호 | 화면명 | 그리드 | 트랜잭션 |", "|---|---|---|---|"]
    for s in screens.values():
        txs = ", ".join(dict.fromkeys(f"`{g.tx}`" for g in s.grids))
        out.append(f"| [{s.id}]({s.id}.md) | {s.title} | {len(s.grids)} | {txs} |")
    out += ["", "## 영향도: DB 컬럼 → 화면", "",
            "스키마를 바꾸기 전에 이 표에서 그 컬럼을 쓰는 화면을 확인해요.", "",
            "| DB 컬럼 | 한글명 | 쓰는 화면·그리드 |", "|---|---|---|"]
    for col, where in usages(screens).items():
        out.append(f"| `{col}` | {_cell(short(column_comment(catalog, col)))} | {', '.join(where)} |")
    return "\n".join(out) + "\n"


def all_docs(screens: dict[str, Screen], catalog: dict) -> dict[str, str]:
    """File name -> content for docs/screens/."""
    docs = {f"{s.id}.md": screen_markdown(s, catalog) + "\n" for s in screens.values()}
    docs["README.md"] = index_markdown(screens, catalog)
    return docs
