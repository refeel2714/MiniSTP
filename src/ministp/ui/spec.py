"""화면정의서 generator: the screen spec is written from the definition, never by hand.

Each screen's document lists its conditions, grids, transactions and, for every
column, the DB column and the Korean name taken from the schema COMMENT. The
index adds 영향도 (which screens show a DB column), so a schema change shows
at once which screens it touches.
"""
from .actions import ACTIONS
from .definitions import (Screen, column_comment, jump_targets, load_lookups, load_menu, menu_path, menu_tree,
                          parent_id, resolve, short, split_sides, usages)
from .transactions import TRANSACTIONS

LOAD_TEXT = {"search": "조회 버튼", "open": "화면 열 때 + 조회 버튼", "master": "목록(마스터)에서 행 선택",
             "input": "조회조건을 바꿀 때마다 (입력 즉시 재계산)"}
FORMAT_TEXT = {"text": "문자", "code": "코드", "number": "수량", "money": "금액", "rate": "비율·단가",
               "date": "날짜", "datetime": "일시", "time": "시각", "json": "JSON", "series": "숫자열", "steps": "단계 상태"}
DISPLAY_TEXT = {None: "", "badge": "배지(이름 첫 글자, 색은 코드·모양은 자산 유형)", "bar": "셀 안 막대(열의 최댓값 대비)",
                "meter": "비율 막대(%)", "spark": "추세선(오래된 값부터)", "pill": "상태 표시(둥근 표시)",
                "steps": "진행 단계"}
INSIDE_TEXT = {"badge": "배지", "meter": "막대"}
COLOR_TEXT = {None: "", "sign": "부호(+빨강/-파랑)", "side": "매수 빨강/매도 파랑", "status": "상태", "verdict": "판단(가능 초록/불가 빨강)",
              "check": "검사(통과 초록/걸린 사유 빨강)"}


def _cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _names(grids) -> str:
    return ", ".join(f"`{g.id}` {g.title}" for g in grids)


def _lookup_text(x) -> str:
    if not x.lookup:
        return ""
    fixed = ", ".join(f"{k}={v}" for k, v in x.lookup_params.items())
    return f"`{x.lookup}`" + (f" ({fixed})" if fixed else "")


def screen_markdown(screen: Screen, catalog: dict, lookups=None, menu=None) -> str:
    lookups = load_lookups() if lookups is None else lookups
    r = resolve(screen, catalog, lookups)
    path = menu_path(load_menu() if menu is None else menu, screen.id)
    out = [f"# {screen.id} {screen.title}", "", f"> 이 문서는 `src/ministp/ui/screens/{screen.file}`에서 생성돼요."
           " 직접 고치지 말고 화면 정의를 고친 뒤 `python scripts/gen_screen_docs.py`를 실행하세요.", ""]
    if path:
        out += [f"분류: {' > '.join(path)}", ""]
    if parent_id(screen.id):
        out += [f"딸린 화면: [{parent_id(screen.id)}]({parent_id(screen.id)}.md)에서 들어와요. 메뉴트리에는 따로 나오지 않고,"
                " 화면 제목 위의 돌아가기로 들어온 화면으로 돌아가요.", ""]
    out += ["## 목적", "", screen.purpose, ""]
    if screen.layout == "split":
        left, right, below = split_sides(screen)
        intro = ("좌우 구성이에요. 왼쪽에 입력하면 오른쪽이 그 값으로 다시 계산돼요." if any(g.edit for g in left)
                 else "좌우 구성이에요. 왼쪽 목록에서 행을 고르면 오른쪽이 그 행의 상세로 바뀌어요.")
        out += ["## 화면 구성", "", intro, "",
                f"- 왼쪽 (너비 {screen.left}%): {_names(left)}", f"- 오른쪽: {_names(right)}"]
        if below:
            out.append(f"- 아래 (고른 행과 무관): {_names(below)}")
        out.append("")
    if screen.conditions:
        out += ["## 조회조건", "", "| 조건 | 한글명 | 형식 | DB 컬럼 | 필수 | 기본값 | 선택값 | 찾기 팝업 |",
                "|---|---|---|---|---|---|---|---|"]
        for c, cd in zip(screen.conditions, r["conditions"]):
            out.append(f"| `{c.name}` | {_cell(cd['label'])} | {c.type} | {_cell(c.source or '-')} "
                       f"| {'예' if c.required else ''} | {_cell(c.default)} | {_cell(', '.join(c.options))} "
                       f"| {_lookup_text(c)} |")
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
    buttons = [(g, b) for g in screen.grids for b in g.actions]
    if buttons:
        out += ["## 처리 트랜잭션", "",
                "처리 버튼이 부르는 트랜잭션이에요. CLI 명령과 같은 함수를 부르고, 화면 서버를 처리 가능으로 열었을 때만 동작해요.",
                "", "| 트랜잭션 | 이름 | 입력 | 부르는 버튼 |", "|---|---|---|---|"]
        for g, b in buttons:
            a = ACTIONS[b.tx]
            params = ", ".join(f"`{k}` ({v}{'' if a.required(k) else ', 선택'})" for k, v in a.params.items())
            out.append(f"| `{b.tx}` | {a.title} | {params} | {g.id} · {b.label} |")
        out.append("")
    for g, gd in zip(screen.grids, r["grids"]):
        out += [f"## 그리드 `{g.id}` {g.title}", ""]
        facts = [f"- 트랜잭션: `{g.tx}` → 데이터셋 `{g.dataset}`", f"- 불러오는 때: {LOAD_TEXT[g.load]}"]
        if g.master:
            link = ", ".join(f"`{p}` ← `{g.master}.{c}`" for p, c in g.link.items())
            facts.append(f"- 마스터: `{g.master}` ({link})")
        if g.weight != 1:
            facts.append(f"- 높이 비중: {g.weight} (좌우 구성에서 같은 쪽 그리드와 나눠 쓰는 높이)")
        if g.view == "cards":
            facts.append("- 표시: 요약 카드")
        if g.view == "calendar":
            facts.append(f"- 표시: 달력 (하루가 한 칸, 날짜 `{g.calendar['date']}`, 칸 색 `{g.calendar['state']}`)."
                         " 달마다 한 덩어리로 오래된 달부터 그리고, 나머지 열은 칸에 마우스를 올리면 보여요. 칸을 누르면 그날이 골라져요 (←→ 하루, ↑↓ 한 주)")
        if g.map:
            m = g.map
            facts.append(f"- {'표시' if g.view == 'map' else '바꿔 보기'}: 구성 지도 (행 하나가 타일 하나, 이름 `{m['label']}`, 면적 `{m['size']}`"
                         + (f", 색 `{m['tone']}`: 면적 값 대비 +는 빨강, -는 파랑, 값이 없으면 빗금" if m.get("tone") else "")
                         + "). 그리드 머리의 버튼으로 표와 바꿔 보고, 타일을 누르면 그 행이 골라져요")
        if g.view == "taccount":
            t = g.taccount
            facts.append(f"- 표시: T계정 (계정 `{t['account']}`마다 T 하나, 차변 `{t['debit']}`은 왼쪽, 대변 `{t['credit']}`은 오른쪽)."
                         + (f" 전표(`{t['entry']}`)마다 묶고" if t.get("entry") else " 한 묶음으로 그리고")
                         + " 그 아래에 차변 합계, 대변 합계와 대차 일치 여부를 보여요. 나머지 열은 묶음의 머리말에 나오고, 오른쪽 위에서 표로 바꿔 볼 수 있어요")
        if g.edit:
            e = g.edit
            facts.append(f"- 입력: `{e['column']}` 칸에 값을 넣어요 (저장하지 않음). 처음 값과 되돌아갈 값은 `{e['base']}`."
                         " 고친 칸은 주황 테두리와 원래 값이 보이고, 그 칸의 ↺를 누르면 그 행만 되돌아가요."
                         + (f" 그리드 오른쪽 위 `{e['reset']}` 버튼은 모든 행을 되돌려요." if e.get("reset") else "")
                         + (f" 행 추가는 찾기 팝업 `{e['add']}`로 골라요." if e.get("add") else "")
                         + f" 넣은 값은 `{e['param']}` 조건(`행키:값,…`)으로 오른쪽 그리드에 가요")
        if g.pick:
            k = g.pick
            facts.append(f"- 체크박스: 행 앞의 체크박스로 여러 행을 골라요. 체크한 행의 키(`{g.key or g.columns[0].name}`)를 쉼표로 이어"
                         f" `{k['param']}`로 보내요(그 값을 받는 그리드는 체크를 바꿀 때마다 다시 계산)."
                         + (f" `{k['when']}`이 참인 행만 체크할 수 있어요." if k.get("when") else "")
                         + (f" `{k['group']}` 값이 같은 행은 함께 체크되고 함께 풀려요(묶음은 전부 아니면 전무)." if k.get("group") else "")
                         + " 머리의 체크박스는 모두 체크·해제, 행에서 스페이스로도 체크해요")
        if g.draft:
            d = g.draft
            facts.append(f"- 화면에서 쓰는 줄: `{d['label']}` 버튼의 입력 창으로 한 줄씩 더해요(저장하지 않음, 더한 줄은 체크된 채로 들어와요)."
                         f" 줄은 `줄번호:{':'.join(f.name for f in d['fields'])}`을 쉼표로 이어 `{d['param']}`로 보내요."
                         + (f" `{d['remove']}` 버튼은 체크한 쓴 줄을 빼요." if d.get("remove") else "")
                         + f" 다른 화면이 주소의 `{d['param']}`로 줄을 넘기면 그 줄들이 체크된 채로 들어와요")
        if g.handoff:
            o = g.handoff
            facts.append(f"- 넘기기: `{o['label']}` 버튼이 모든 행의 `{o['column']}` 값을 이어 {o['screen']}의 `{o['param']}`(화면에서 쓰는 줄)로 보내요")
        if g.totals:
            facts.append(f"- 합계: {', '.join(f'`{t}`' for t in g.totals)}")
        if g.meta:
            facts.append(f"- 상태 표시: {', '.join(f'{v} (`{k}`)' for k, v in g.meta.items())}")
        if g.drill:
            params = ", ".join(f"`{k}` ← `{v}`" for k, v in g.drill.get("params", {}).items())
            if g.drill.get("account"):
                params = ", ".join(x for x in (params, f"계좌 ← `{g.drill['account']}`") if x)
            where = (f"`{g.drill['by']}` 열이 가리키는 화면({', '.join(g.drill['screens'])})" if g.drill.get("by")
                     else g.drill["screen"])
            facts.append(f"- 행 더블클릭·Enter: {where}으로 이동 ({params})" if g.drill.get("by")
                         else f"- 행 더블클릭·Enter: {where}로 이동 ({params})")
        if g.live:
            facts.append(f"- 실시간: DB가 바뀌면 마지막 조회조건으로 다시 조회, 바뀐 칸 강조 (행 식별 열 `{g.key or g.columns[0].name}`)")
        if g.chart:
            facts.append(f"- 차트: `{g.chart['x']}` 대비 `{g.chart['y']}` 선 그래프")
        out += facts + ["", "| 열 | 한글명 | DB 컬럼 | DB 주석 | 형식 | 정렬 | 색 | 표시 |", "|---|---|---|---|---|---|---|---|"]
        for c, cd in zip(g.columns, gd["columns"]):
            comment = column_comment(catalog, c.source) if c.source else "(화면 계산·집계 값)"
            shown = DISPLAY_TEXT[c.display]
            if cd["inside"]:
                host = next(x for x in g.columns if x.name == cd["inside"])
                shown = (f"`{host.name}` 칸 아래에 작게 표시" if host.sub == c.name
                         else f"`{host.name}` {INSIDE_TEXT[host.display]} 안에 표시")
            elif c.hidden:
                shown = "숨김 (행 식별·연결에만 씀)"
            elif c.of:
                shown = f"비율 막대(`{c.of}` 대비, 값 / 전체)"
            elif c.steps:
                shown = "진행 단계(" + " → ".join(c.steps) + "; 끝·진행 중·멈춤·전)"
            if cd.get("codes"):      # the cell shows the code's name, the code is in the tooltip
                shown = ", ".join(x for x in (shown, f"코드명(공통코드 `{cd['codes']}`)") if x)
            out.append(f"| `{c.name}` | {_cell(cd['label'])} | {_cell(c.source or '-')} | {_cell(comment)} "
                       f"| {FORMAT_TEXT[c.format]} | {'오른쪽' if c.align == 'right' else '왼쪽'} "
                       f"| {COLOR_TEXT[c.color]} | {shown} |")
        out.append("")
        for b, bd in zip(g.actions, gd["actions"]):
            target = "고른 행" if b.needs_row else "행을 고르지 않아도 됨"
            if b.when:
                target += " (" + ", ".join(f"`{k}`가 {'·'.join(map(str, v))}일 때만" for k, v in b.when.items()) + ")"
            out += [f"### 처리 버튼 `{b.id}` {b.label}", "", f"- 위치: 그리드 오른쪽 위",
                    f"- 처리 트랜잭션: `{b.tx}` {bd['title']}", f"- 대상: {target}"]
            if b.shows:
                out.append(f"- 입력 창 위에 보여 주는 행의 값: {', '.join(f'`{c}`' for c in b.shows)}")
            if b.note:
                out.append(f"- 안내: {b.note}")
            given = [p for p in (g.pick, g.draft) if p and p["param"] in ACTIONS[b.tx].params]
            if given:
                out.append(f"- 그리드가 채우는 입력: {', '.join(f'`{p['param']}`' for p in given)} (체크한 행, 화면에서 쓴 줄)")
            if b.lock:
                out.append(f"- 잠금: 트랜잭션 `{g.tx}`의 상태값 `{b.lock}`이 비어 있지 않으면 잠기고 그 글이 이유로 보여요."
                           " 서버는 누를 때 같은 검사를 다시 해요")
            if b.clears:
                out.append("- 끝나면: 화면에서 쓴 줄 가운데 체크한 줄을 지워요 (요청으로 넘어갔으니까)")
            if not b.fields:
                out += ["- 입력 창: 입력 항목 없이 확인만 받아요", ""]
                continue
            out += ["", "| 입력 항목 | 한글명 | 형식 | DB 컬럼 | 필수 | 처음 값 | 선택값 | 찾기 팝업 |",
                    "|---|---|---|---|---|---|---|---|"]
            for f, fd in zip(b.fields, bd["fields"]):
                start = f"행의 `{f.column}`" if f.column else _cell(f.default)
                if f.readonly:
                    start += " (고칠 수 없음)"
                out.append(f"| `{f.name}` | {_cell(fd['label'])} | {f.type} | {_cell(f.source or '-')} "
                           f"| {'예' if fd['required'] else ''} | {start} | {_cell(', '.join(f.options))} "
                           f"| {_lookup_text(f)} |")
            out.append("")
    for g, gd in zip(screen.grids, r["grids"]):
        if not g.draft:
            continue
        out += [f"### 줄 추가 `{g.id}` {g.draft['label']}", "", "- 위치: 그리드 오른쪽 위", "- 서버에 가지 않아요. 확인을 누르면 줄 하나가 그리드에 더해져요",
                "", "| 입력 항목 | 한글명 | 형식 | DB 컬럼 | 필수 | 선택값 | 찾기 팝업 |", "|---|---|---|---|---|---|---|"]
        for f, fd in zip(g.draft["fields"], gd["draft"]["fields"]):
            out.append(f"| `{f.name}` | {_cell(fd['label'])} | {f.type} | {_cell(f.source or '-')} | {'예' if fd['required'] else ''} "
                       f"| {_cell(', '.join(f.options))} | {_lookup_text(f)} |")
        out.append("")
    if r["lookups"]:
        out += ["## 찾기 팝업", "", "입력 칸 옆 찾기 버튼(또는 F2)으로 여는 목록이에요. 여러 화면이 같은 정의(`lookups.yaml`)를 같이 써요.",
                "", "| 이름 | 제목 | 조회 트랜잭션 | 고르면 들어가는 열 | 함께 보여 주는 열 | 목록의 열 |", "|---|---|---|---|---|---|"]
        for name, lk in r["lookups"].items():
            cols = ", ".join(f"{c['label']}(`{c['name']}`)" for c in lk["columns"])
            out.append(f"| `{name}` | {lk['title']} | `{lk['tx']}` | `{lk['value']}` | "
                       f"{'`' + lk['shows'] + '`' if lk['shows'] else ''} | {cols} |")
        out.append("")
    return "\n".join(out)


def index_markdown(screens: dict[str, Screen], catalog: dict, menu=None) -> str:
    menu = load_menu() if menu is None else menu
    out = ["# STP-UI 화면 목록과 영향도", "",
           "> `python scripts/gen_screen_docs.py`로 생성돼요. 화면 정의(YAML)와 DB 주석에서 만들어지니 직접 고치지 마세요.",
           "", "## 화면 분류", "",
           "화면번호 `STP-ABCD`가 곧 분류예요. 첫 자리 A는 대분류(거래가 흘러가는 순서), 둘째 자리까지 AB는 중분류(그 단계에서 다루는 대상), "
           "셋째 자리 C는 같은 대상을 다루는 화면의 순번이에요. 끝자리 D가 0이면 대표 화면이고, 1~9면 그 대표 화면(ABC0)에서 "
           "들어가는 딸린 화면이라 메뉴트리에 따로 나오지 않고 아래 표에서 대표 화면 뒤 괄호에 적어요. 조회와 처리는 번호가 아니라 트랜잭션 ID로 갈라요(R 조회, U 처리). "
           "분류는 [menu.yaml](../../src/ministp/ui/menu.yaml) 한 곳에 적고, 서버는 시작할 때 모든 화면번호가 분류에 들어가는지, "
           "화면이 없는 분류가 없는지 확인해요. 왼쪽 메뉴트리와 첫 화면도 이 표를 그대로 그려요.",
           "", "| 대분류 | 중분류 | 화면 |", "|---|---|---|"]
    for area in menu_tree(menu, screens):
        for n, group in enumerate(area["groups"]):
            head = f"**{area['id']} {area['name']}**<br>{_cell(area['note'])}" if n == 0 else ""
            shown = ", ".join(f"[{x['id']}]({x['id']}.md) {x['title']}"
                              + (" (딸린 화면 " + ", ".join(f"[{y['id']}]({y['id']}.md) {y['title']}" for y in x["subs"]) + ")"
                                 if x["subs"] else "") for x in group["screens"])
            out.append(f"| {head} | {group['id']} {group['name']} | {shown} |")
    out += ["", "## 화면", "", "| 화면번호 | 화면명 | 그리드 | 트랜잭션 |", "|---|---|---|---|"]
    for s in screens.values():
        txs = ", ".join(dict.fromkeys([*(f"`{g.tx}`" for g in s.grids),
                                       *(f"`{b.tx}`" for g in s.grids for b in g.actions)]))
        out.append(f"| [{s.id}]({s.id}.md) | {s.title} | {len(s.grids)} | {txs} |")
    out += ["", "## 바로 가기 창 (Ctrl+K)", "",
            "어느 화면에서나 Ctrl+K로 열어 화면번호나 화면명을 넣고 Enter로 옮겨 가요. "
            "조회조건이 찾기 팝업으로 받는 코드는 그 코드로 화면을 바로 열 수 있어요(여러 화면이 받으면 다음 단계에서 화면을 골라요).",
            "", "| 찾는 것 | 목록을 주는 트랜잭션 | 열 수 있는 화면 (넘기는 조회조건) |", "|---|---|---|",
            f"| 화면 | - | 화면 {len(screens)}개 모두 |"]
    for j in jump_targets(screens, catalog):
        into = ", ".join(f"{t['id']} {t['title']}(`{t['param']}`)" for t in j["into"])
        out.append(f"| {j['title'].removesuffix('찾기').strip()} | `{j['tx']}` | {into} |")
    out += ["", "## 영향도: DB 컬럼 → 화면", "",
            "스키마를 바꾸기 전에 이 표에서 그 컬럼을 쓰는 화면을 확인해요.", "",
            "| DB 컬럼 | 한글명 | 쓰는 화면·그리드 |", "|---|---|---|"]
    for col, where in usages(screens).items():
        out.append(f"| `{col}` | {_cell(short(column_comment(catalog, col)))} | {', '.join(where)} |")
    return "\n".join(out) + "\n"


def all_docs(screens: dict[str, Screen], catalog: dict, menu=None) -> dict[str, str]:
    """File name -> content for docs/screens/."""
    docs = {f"{s.id}.md": screen_markdown(s, catalog, menu=menu) + "\n" for s in screens.values()}
    docs["README.md"] = index_markdown(screens, catalog, menu)
    return docs
