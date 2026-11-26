"""Screen definitions (YAML) and the validator that ties them to the DB schema.

A definition says what a screen shows, never how: search conditions, grids, the
transaction each grid calls and, for every column, the DB column it comes from.
Korean column names are not typed into the definition; they are read from the
schema COMMENTs, so the ERD, pgAdmin and the screens always use the same words.
"""
import re
from dataclasses import asdict, dataclass, field
from importlib.resources import files

import yaml

from .actions import ACTIONS
from .transactions import TRANSACTIONS

FORMATS = {"text", "code", "number", "money", "rate", "date", "datetime", "time", "json", "series", "steps"}
RIGHT = {"number", "money", "rate"}
COLORS = {None, "sign", "side", "status", "verdict"}  # verdict: the row's ok field
# 셀 표시 유형: how a cell is drawn on top of its value. The value stays; the picture sits next to it.
#   badge  종목 배지: 이름 첫 글자, 아래에 코드. 색은 코드에서 정해지고 모양은 자산 유형
#   bar    셀 안 막대: 이 열에서 가장 큰 값 대비 크기
#   meter  비율 막대: 0~1 값을 %와 막대로 (of가 없으면 막대 끝은 그 목록에서 가장 큰 값)
#   spark  추세선: 숫자열(오래된 값부터)
#   pill   상태 표시: 색 규칙이 고른 색의 둥근 표시
#   steps  진행 단계: 단계마다 점 하나 (끝 · 진행 중 · 멈춤 · 전), 값은 단계별 상태의 목록
DISPLAYS = {None, "badge", "bar", "meter", "spark", "pill", "steps"}
DISPLAY_FORMATS = {"badge": {"text"}, "bar": {"number", "money"}, "meter": {"rate", "number"}, "spark": {"series"},
                   "pill": {"text", "code"}, "steps": {"steps"}}
DRAWN_ONLY = {"series": "spark", "steps": "steps"}   # formats that have no text form: they are always drawn
STEP_STATES = {"done", "now", "stop", "wait"}
MAX_STEPS = 6
PILL_COLORS = {"status", "side", "verdict"}
# 그리드 보기: grid 표 · cards 한 행 요약 카드 · calendar 달력(하루가 한 칸, 칸 색은 상태 열의 색 규칙)
#            · taccount T계정(분개 한 줄이 계정의 차변은 왼쪽, 대변은 오른쪽에. 표로 바꿔 볼 수 있음)
#            · map 구성 지도(행 하나가 타일 하나, 면적은 크기 열, 색은 부호 열. 표 보기의 그리드에 map만 적으면 바꿔 보는 보기로 붙음)
VIEWS = {"grid", "cards", "calendar", "taccount", "map"}
VIEW_NAMES = {"cards": "카드", "calendar": "달력", "taccount": "T계정", "map": "구성 지도"}
MAP_NEEDS = {"label", "size"}       # map: 타일의 이름 열, 면적이 되는 열
MAP_KEYS = MAP_NEEDS | {"tone"}     # tone: 색이 되는 열 (크기 대비 +는 빨강, -는 파랑)
TACCOUNT_NEEDS = {"account", "debit", "credit"}       # view: taccount 에 꼭 적는 열
TACCOUNT_KEYS = TACCOUNT_NEEDS | {"name", "entry"}     # name: 계정명 열 · entry: 전표를 가르는 열
LOADS = {"search", "open", "master", "input"}
EDIT_NEEDS = {"column", "base", "param"}
EDIT_KEYS = EDIT_NEEDS | {"reset", "add"}
LAYOUTS = {"stack", "split"}   # stack: grids one under another · split: 목록 왼쪽, 상세 오른쪽
MAX_DETAILS = 3                # a split screen shows at most this many detail grids next to its list
CONDITION_TYPES = {"date", "text", "select", "bool", "number"}
FIELD_TYPES = CONDITION_TYPES  # an input window takes the same kinds of value as a search condition
FIELD_KINDS = {"date": {"date"}, "text": {"text", "select"}, "int": {"number", "text", "select"}, "bool": {"bool"}}
SCREEN_ID = re.compile(r"^STP-\d{4}$")


class DefinitionError(ValueError):
    pass


@dataclass
class Column:
    name: str
    source: str | None = None   # table.column whose COMMENT gives the label
    label: str | None = None    # only for values that have no DB column (computed)
    format: str = "text"
    color: str | None = None
    width: int | None = None
    display: str | None = None  # 셀 표시 유형 (DISPLAYS)
    # badge: two other columns of the same grid are drawn inside the badge cell instead of as columns of their own
    code: str | None = None     # the column written under the name (종목코드); it also picks the colour
    shape: str | None = None    # the column whose value picks the shape (자산유형)
    # meter on a quantity: the column that is the whole ("4 / 10"); it is drawn inside this cell too
    of: str | None = None
    steps: list[str] = field(default_factory=list)   # steps: the name of each step, in order
    sub: str | None = None      # another column written small under this value (its time, its rate); drawn here
    hidden: bool = False        # in the rows (row key, link, button values) but not drawn as a column

    @property
    def align(self) -> str:
        return "right" if self.format in RIGHT and self.display != "meter" else "left"


@dataclass
class Field:
    """One input of a 처리 button's window."""
    name: str                   # the 처리 transaction parameter it fills
    type: str = "text"
    source: str | None = None   # table.column whose COMMENT gives the label
    label: str | None = None
    column: str | None = None   # starts with this column of the selected row (or the value the grid was opened with)
    readonly: bool = False      # shown, not typed: the key of the row being changed
    options: list[str] = field(default_factory=list)
    default: str | None = None  # "today" or a literal
    lookup: str | None = None   # 찾기 팝업: a name in lookups.yaml
    lookup_params: dict[str, str] = field(default_factory=dict)   # fixed parameters of that list


@dataclass
class Button:
    """처리 버튼: shown at the top right of its grid, opens an input window, calls one 처리 transaction."""
    id: str
    label: str
    tx: str
    fields: list[Field] = field(default_factory=list)
    needs_row: bool = False     # works on the selected row; off until a row is picked
    when: dict[str, list] = field(default_factory=dict)   # only for rows whose column holds one of these values
    shows: list[str] = field(default_factory=list)        # columns of that row shown at the top of the window
    note: str | None = None     # one line in the window: what pressing 확인 does


@dataclass
class Lookup:
    """찾기 팝업: a list a field opens to pick a code from, shared by every screen (lookups.yaml)."""
    name: str
    title: str
    tx: str                     # a read transaction (of any screen)
    value: str                  # the column that goes into the field
    columns: list[Column]
    shows: str | None = None    # the column shown next to the field once a row is picked (the code's name)


@dataclass
class Grid:
    id: str
    title: str
    tx: str
    columns: list[Column]
    dataset: str = "main"
    load: str = "search"        # search: 조회 button · open: on screen open · master: on master row
                                # · input: again on every condition change (주문가능 재계산)
    master: str | None = None
    link: dict[str, str] = field(default_factory=dict)   # tx param -> master column
    totals: list[str] = field(default_factory=list)
    view: str = "grid"          # VIEWS: grid | cards (one-row summaries) | calendar (a cell a day) | taccount
    calendar: dict | None = None  # view: calendar: {date: column, state: column}; the rest show on hover
    taccount: dict | None = None  # view: taccount: {account, debit, credit[, name][, entry]}: columns of this grid
    map: dict | None = None       # 구성 지도: {label, size[, tone]}. With view: grid it is a second view next to the table
    meta: dict[str, str] = field(default_factory=dict)   # tx meta key -> label
    drill: dict | None = None   # {screen: STP-xxxx, params: {cond: column}} or {by: column, screens: [...], params}
    chart: dict | None = None   # {x: column, y: column}: a line over the rows
    live: bool = False          # reload when the DB changes (SSE), changed rows highlighted
    key: str | None = None      # column that identifies a row across reloads (default: first column)
    weight: int = 1             # split layout: this grid's share of its side's height
    actions: list[Button] = field(default_factory=list)   # 처리 버튼, top right of the grid
    # 입력 그리드: one column is typed in on the screen (nothing is saved). {column, base, param[, reset][, add]}
    #   column 고쳐 쓰는 열 · base 되돌아갈 값이 있는 열 · param 이 열의 값들을 받는 같은 화면 트랜잭션의 조건
    #   (`행키:값,행키:값`) · reset 모두 되돌리는 버튼 이름 · add 행을 더할 때 여는 찾기 팝업
    edit: dict | None = None
    # 묶음 머리: a row over the column headers, [{label, span}] covering the visible columns left to right
    #   (시산표의 차변 | 계정과목 | 대변). The table is then read in its own order and not sorted.
    bands: list[dict] = field(default_factory=list)
    emphasis: str | None = None   # a column whose value is the row's level: class and total rows are drawn bold


@dataclass
class Condition:
    name: str
    type: str = "text"
    source: str | None = None
    label: str | None = None
    options: list[str] = field(default_factory=list)
    default: str | None = None  # "today" or a literal
    required: bool = False      # grids are not loaded until it has a value
    lookup: str | None = None   # 찾기 팝업: a name in lookups.yaml
    lookup_params: dict[str, str] = field(default_factory=dict)


@dataclass
class Screen:
    id: str
    title: str
    purpose: str
    conditions: list[Condition]
    grids: list[Grid]
    file: str = ""
    layout: str = "stack"       # split: the list that has details on the left, its details on the right
    left: int = 45              # split: width of the left side in percent


def _build(raw: dict, file: str) -> Screen:
    try:
        grids = [Grid(**{**g, "columns": [Column(**c) for c in g["columns"]],
                         "actions": [Button(**{**a, "fields": [Field(**f) for f in a.get("fields", [])]})
                                     for a in g.get("actions", [])]}) for g in raw["grids"]]
        conds = [Condition(**c) for c in raw.get("conditions", [])]
        return Screen(raw["id"], raw["title"], raw.get("purpose", ""), conds, grids, file,
                      raw.get("layout", "stack"), raw.get("left", 45))
    except (KeyError, TypeError) as e:
        raise DefinitionError(f"{file}: 형식 오류 {e}") from e


def load_screens(directory=None) -> dict[str, Screen]:
    base = directory or files("ministp.ui").joinpath("screens")
    out = {}
    for f in sorted(base.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".yaml"):
            s = _build(yaml.safe_load(f.read_text(encoding="utf-8")), f.name)
            if s.id in out:
                raise DefinitionError(f"화면번호 중복: {s.id}")
            out[s.id] = s
    return out


def load_lookups(path=None) -> dict[str, Lookup]:
    f = path or files("ministp.ui").joinpath("lookups.yaml")
    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    try:
        return {name: Lookup(name, **{**d, "columns": [Column(**c) for c in d["columns"]]}) for name, d in raw.items()}
    except (KeyError, TypeError) as e:
        raise DefinitionError(f"lookups.yaml: 형식 오류 {e}") from e


# ---------- 화면 분류 (menu.yaml) ----------
# 화면번호 STP-ABCC: A 대분류, AB 중분류, CC 그 안의 순번. The number is the classification, so the
# menu tree, the breadcrumb and the 화면정의서 cannot disagree about where a screen belongs.

@dataclass(frozen=True)
class MenuGroup:
    id: str                     # 중분류: two digits, the first is its 대분류
    name: str
    first: tuple[str, ...] = ()  # screens shown first, in this order (the rest follow in number order)


@dataclass(frozen=True)
class MenuArea:
    id: str                     # 대분류: one digit
    name: str
    note: str
    groups: tuple[MenuGroup, ...]


def load_menu(path=None) -> list[MenuArea]:
    f = path or files("ministp.ui").joinpath("menu.yaml")
    raw = yaml.safe_load(f.read_text(encoding="utf-8")) or []
    try:
        return [MenuArea(str(a["id"]), a["name"], a.get("note", ""),
                         tuple(MenuGroup(str(g["id"]), g["name"], tuple(g.get("first", ()))) for g in a["groups"])) for a in raw]
    except (KeyError, TypeError) as e:
        raise DefinitionError(f"menu.yaml: 형식 오류 {e}") from e


def menu_problems(menu: list[MenuArea], screens: dict[str, Screen]) -> list[str]:
    """Every screen number falls in one 대분류 and one 중분류 of menu.yaml, and nothing there is empty."""
    errs = []
    areas = [a.id for a in menu]
    groups = [g.id for a in menu for g in a.groups]
    for a in menu:
        if not re.fullmatch(r"[1-9]", a.id):
            errs.append(f"menu.yaml: 대분류 번호는 1~9 한 자리 ({a.id} {a.name})")
        for g in a.groups:
            if not re.fullmatch(r"[1-9][0-9]", g.id) or g.id[0] != a.id:
                errs.append(f"menu.yaml: 중분류 {g.id} {g.name}은 대분류 {a.id}로 시작하는 두 자리여야 해요")
    errs += [f"menu.yaml: 대분류 번호 중복 {x}" for x in sorted({x for x in areas if areas.count(x) > 1})]
    errs += [f"menu.yaml: 중분류 번호 중복 {x}" for x in sorted({x for x in groups if groups.count(x) > 1})]
    used = set()
    for s in screens.values():
        if not SCREEN_ID.match(s.id):
            continue                                    # validate() reports the number's format
        digits = s.id.removeprefix("STP-")
        if digits[0] not in areas:
            errs.append(f"{s.id} {s.title}: 대분류 {digits[0]}이 menu.yaml에 없어요")
        elif digits[:2] not in groups:
            errs.append(f"{s.id} {s.title}: 중분류 {digits[:2]}가 menu.yaml에 없어요")
        used.add(digits[:2])
    errs += [f"menu.yaml: 중분류 {g.id} {g.name}에 화면이 없어요" for a in menu for g in a.groups if g.id not in used]
    errs += [f"menu.yaml: 중분류 {g.id}의 first에 적은 {sid}는 그 중분류의 화면이 아니에요"
             for a in menu for g in a.groups for sid in g.first
             if sid not in screens or sid.removeprefix("STP-")[:2] != g.id]
    return errs


def menu_tree(menu: list[MenuArea], screens: dict[str, Screen]) -> list[dict]:
    """대분류 → 중분류 → 화면 in menu.yaml's order (a group's `first` screens, then the rest in number order):
    what the left menu, the home page and the index doc draw."""
    by_group: dict[str, list[dict]] = {}
    for s in sorted(screens.values(), key=lambda s: s.id):
        by_group.setdefault(s.id.removeprefix("STP-")[:2], []).append({"id": s.id, "title": s.title})

    def ordered(g: MenuGroup) -> list[dict]:
        listed = by_group.get(g.id, [])
        rank = {sid: i for i, sid in enumerate(g.first)}
        return sorted(listed, key=lambda x: (rank.get(x["id"], len(rank)), x["id"]))
    return [{"id": a.id, "name": a.name, "note": a.note,
             "groups": [{"id": g.id, "name": g.name, "screens": ordered(g)} for g in a.groups]}
            for a in menu]


def menu_path(menu: list[MenuArea], screen_id: str) -> list[str]:
    """[대분류 이름, 중분류 이름] of a screen number; empty when menu.yaml does not hold it."""
    digits = screen_id.removeprefix("STP-")
    for a in menu:
        for g in a.groups:
            if g.id == digits[:2]:
                return [a.name, g.name]
    return []


def short(comment: str | None) -> str:
    return (comment or "").split(" (")[0].strip()


def column_comment(catalog: dict, source: str) -> str | None:
    table, _, col = source.partition(".")
    info = catalog.get(table)
    if not info:
        return None
    return next((c["comment"] for c in info["columns"] if c["name"] == col), None)


def edit_params(screen: Screen) -> set[str]:
    return {g.edit["param"] for g in screen.grids if g.edit and "param" in g.edit}


def follows_edit(g: Grid, screen: Screen) -> bool:
    """A grid recomputed from what is typed into an 입력 그리드 of the same screen."""
    return g.load == "input" and bool(edit_params(screen) & set(TRANSACTIONS[g.tx].params if g.tx in TRANSACTIONS else ()))


def split_sides(screen: Screen) -> tuple[list[Grid], list[Grid], list[Grid]]:
    """A split screen's grids as (left: the list with details, right: its details, below: the rest).

    An 입력 그리드 is a list too: what is typed there on the left, what it works out to on the right."""
    masters = {g.master for g in screen.grids if g.load == "master"}
    left = [g for g in screen.grids if g.id in masters or g.edit]
    right = [g for g in screen.grids if g.load == "master" or follows_edit(g, screen)]
    below = [g for g in screen.grids if g not in left and g not in right]
    return left, right, below


def _layout_problems(s: Screen, where: str) -> list[str]:
    if s.layout not in LAYOUTS:
        return [f"{where}: layout은 {sorted(LAYOUTS)} 중 하나"]
    if s.layout != "split":
        return []
    errs = []
    left, right, _ = split_sides(s)
    if len(left) != 1:
        errs.append(f"{where}: 좌우 구성(split)은 상세가 딸린 목록 그리드가 하나여야 해요 (지금 {len(left)}개)")
    if len(right) > MAX_DETAILS:
        errs.append(f"{where}: 좌우 구성의 상세 그리드는 {MAX_DETAILS}개까지예요 (지금 {len(right)}개). 화면을 나눠 주세요")
    if not isinstance(s.left, int) or not 25 <= s.left <= 75:
        errs.append(f"{where}: left(왼쪽 너비 %)는 25~75 사이 정수")
    return errs


def _column_problems(c: Column, where: str, catalog: dict) -> list[str]:
    errs = []
    if c.format not in FORMATS:
        errs.append(f"{where}.{c.name}: 없는 형식 {c.format}")
    if c.color not in COLORS:
        errs.append(f"{where}.{c.name}: 없는 색 규칙 {c.color}")
    if c.source:
        if column_comment(catalog, c.source) is None:
            errs.append(f"{where}.{c.name}: DB에 없는 컬럼 {c.source}")
        if c.label:
            errs.append(f"{where}.{c.name}: DB 컬럼이 있으면 한글명은 DB 주석을 써요 (label 제거)")
    elif not c.label:
        errs.append(f"{where}.{c.name}: source(DB 컬럼)나 label 중 하나가 필요해요")
    return errs + _display_problems(c, where)


def _display_problems(c: Column, where: str) -> list[str]:
    at = f"{where}.{c.name}"
    if c.display not in DISPLAYS:
        return [f"{at}: 없는 표시 유형 {c.display}"]
    errs = []
    if c.format == "series" and c.display != "spark":
        errs.append(f"{at}: 숫자열(series)은 추세선(display: spark)으로만 보여요")
    if c.format == "steps" and c.display != "steps":
        errs.append(f"{at}: 단계 상태(steps)는 진행 단계(display: steps)로만 보여요")
    if c.display and c.format not in DISPLAY_FORMATS[c.display]:
        errs.append(f"{at}: 표시 유형 {c.display}의 형식은 {sorted(DISPLAY_FORMATS[c.display])} 중 하나예요 (지금 {c.format})")
    if c.display == "pill" and c.color not in PILL_COLORS:
        errs.append(f"{at}: 상태 표시(pill)는 색 규칙이 있어야 해요 ({sorted(PILL_COLORS)} 중 하나)")
    if c.display != "badge" and (c.code or c.shape):
        errs.append(f"{at}: code와 shape는 배지(display: badge)에만 써요")
    if c.of and c.display != "meter":
        errs.append(f"{at}: of(전체가 되는 열)는 비율 막대(display: meter)에만 써요")
    if c.display == "meter" and (c.format == "number") != bool(c.of):
        errs.append(f"{at}: 비율 막대는 비율(rate)이거나, 수량(number)에 of로 전체가 되는 열을 적어요")
    if c.display == "steps" and not 2 <= len(c.steps) <= MAX_STEPS:
        errs.append(f"{at}: 진행 단계는 steps에 단계 이름을 2~{MAX_STEPS}개 적어요")
    if c.display != "steps" and c.steps:
        errs.append(f"{at}: steps(단계 이름)는 진행 단계(display: steps)에만 써요")
    if c.hidden and c.display:
        errs.append(f"{at}: 숨긴 열에는 표시 유형을 쓰지 않아요")
    return errs


def _calendar_problems(g: Grid, gw: str) -> list[str]:
    if g.view != "calendar":
        return [f"{gw}: calendar는 달력 보기(view: calendar)에만 써요"] if g.calendar else []
    cal = g.calendar or {}
    if set(cal) != {"date", "state"}:
        return [f"{gw}: 달력 보기는 calendar에 date(날짜 열)와 state(상태 열)를 적어요"]
    errs = []
    cols = {c.name: c for c in g.columns}
    day, state = cols.get(cal["date"]), cols.get(cal["state"])
    if day is None or day.format != "date":
        errs.append(f"{gw}: 달력의 날짜 열 {cal['date']}는 이 그리드의 날짜(date) 형식 열이어야 해요")
    if state is None or state.color != "status":
        errs.append(f"{gw}: 달력의 상태 열 {cal['state']}는 색 규칙이 status인 이 그리드의 열이어야 해요")
    if (g.key or g.columns[0].name) != cal["date"]:
        errs.append(f"{gw}: 달력은 하루가 한 칸이라 행 식별 열(key)이 날짜 열이어야 해요")
    if g.totals or g.chart or g.drill:
        errs.append(f"{gw}: 달력 보기에는 합계·차트·화면 이동을 쓰지 않아요")
    return errs


def _taccount_problems(g: Grid, s: Screen, gw: str) -> list[str]:
    if g.view != "taccount":
        return [f"{gw}: taccount는 T계정 보기(view: taccount)에만 써요"] if g.taccount else []
    t = g.taccount or {}
    if not TACCOUNT_NEEDS <= set(t) <= TACCOUNT_KEYS:
        return [f"{gw}: T계정 보기는 taccount에 account(계정 열), debit(차변 열), credit(대변 열)을 적어요"
                " (name 계정명 열, entry 전표를 가르는 열은 있으면)"]
    errs = []
    cols = {c.name: c for c in g.columns}
    for role, name in t.items():
        if name not in cols:
            errs.append(f"{gw}: T계정의 {role} 열 {name}가 이 그리드에 없어요")
        elif role in ("debit", "credit") and cols[name].format != "money":
            errs.append(f"{gw}: T계정의 {role} 열 {name}는 금액(money) 형식이어야 해요")
    if len(set(t.values())) != len(t):
        errs.append(f"{gw}: T계정의 열은 서로 달라야 해요")
    if g.chart or g.drill:
        errs.append(f"{gw}: T계정 보기에는 차트·화면 이동을 쓰지 않아요")
    # a T shows an account, not a row: nothing can hang on a picked row
    if any(b.needs_row for b in g.actions) or any(x.master == g.id for x in s.grids):
        errs.append(f"{gw}: T계정 보기는 행을 고르지 않아요. 고른 행에 쓰는 버튼이나 이 그리드를 목록으로 삼는 상세를 둘 수 없어요")
    return errs


def _map_problems(g: Grid, gw: str) -> list[str]:
    if g.map is None:
        return [f"{gw}: 구성 지도 보기는 map에 label(이름 열)과 size(면적이 되는 열)를 적어요"] if g.view == "map" else []
    if g.view not in ("grid", "map"):
        return [f"{gw}: map(구성 지도)은 표 보기나 구성 지도 보기의 그리드에만 써요"]
    if not MAP_NEEDS <= set(g.map) <= MAP_KEYS:
        return [f"{gw}: 구성 지도는 map에 label(이름 열)과 size(면적이 되는 열)를 적어요 (tone 색이 되는 열은 있으면)"]
    errs = []
    cols = {c.name: c for c in g.columns}
    for role, name in g.map.items():
        if name not in cols:
            errs.append(f"{gw}: 구성 지도의 {role} 열 {name}가 이 그리드에 없어요")
        elif role != "label" and cols[name].format not in ("number", "money"):
            errs.append(f"{gw}: 구성 지도의 {role} 열 {name}는 수량(number)이나 금액(money) 형식이어야 해요")
    if len(set(g.map.values())) != len(g.map):
        errs.append(f"{gw}: 구성 지도의 열은 서로 달라야 해요")
    if g.chart:
        errs.append(f"{gw}: 구성 지도와 차트를 한 그리드에 같이 두지 않아요")
    return errs


EMPHASIS_LEVELS = {"class", "total"}


def _band_problems(g: Grid, gw: str) -> list[str]:
    errs = []
    names = {c.name for c in g.columns}
    if g.emphasis and g.emphasis not in names:
        errs.append(f"{gw}: 강조 기준 열 {g.emphasis}가 이 그리드에 없어요")
    if not g.bands:
        return errs
    if g.view != "grid":
        errs.append(f"{gw}: 묶음 머리(bands)는 표 보기에만 써요")
    if any(not isinstance(b, dict) or set(b) != {"label", "span"} or not isinstance(b["span"], int) or b["span"] < 1
           for b in g.bands):
        return errs + [f"{gw}: 묶음 머리는 [{{label, span}}]이고 span은 1 이상 정수예요"]
    inside = {n for x in g.columns for n in (x.code, x.shape, x.of, x.sub) if n}
    visible = sum(1 for c in g.columns if not c.hidden and c.name not in inside)
    if sum(b["span"] for b in g.bands) != visible:
        errs.append(f"{gw}: 묶음 머리의 span 합({sum(b['span'] for b in g.bands)})이 보이는 열 수({visible})와 달라요")
    return errs


def lookup_problems(lookups: dict[str, Lookup], catalog: dict) -> list[str]:
    """찾기 팝업 definitions against the schema catalog and the read transactions."""
    errs = []
    for lk in lookups.values():
        where = f"찾기 {lk.name}"
        if lk.tx not in TRANSACTIONS:
            errs.append(f"{where}: 없는 트랜잭션 {lk.tx}")
        names = {c.name for c in lk.columns}
        if lk.value not in names:
            errs.append(f"{where}: 고르는 값 {lk.value}가 열에 없어요")
        if lk.shows and lk.shows not in names:
            errs.append(f"{where}: 함께 보여 줄 열 {lk.shows}가 열에 없어요")
        for c in lk.columns:
            errs += _column_problems(c, where, catalog)
            if c.display:
                errs.append(f"{where}.{c.name}: 찾기 팝업 열에는 표시 유형을 쓰지 않아요")
    return errs


def _lookup_use_problems(name: str | None, params: dict, where: str, lookups: dict[str, Lookup]) -> list[str]:
    if name is None:
        return [f"{where}: 찾기 없이 lookup_params만 있어요"] if params else []
    lk = lookups.get(name)
    if lk is None:
        return [f"{where}: 없는 찾기 팝업 {name}"]
    tx = TRANSACTIONS.get(lk.tx)
    return [f"{where}: 찾기 {name}의 {lk.tx}에 없는 조건 {p}" for p in params if tx and p not in tx.params]


def _edit_problems(g: Grid, s: Screen, gw: str, lookups: dict[str, Lookup]) -> list[str]:
    if not g.edit:
        return []
    e = g.edit
    if not EDIT_NEEDS <= set(e) <= EDIT_KEYS:
        return [f"{gw}: 입력 그리드는 edit에 column(고쳐 쓰는 열), base(되돌아갈 값 열), param(값을 받는 조건)을 적어요"
                " (reset 되돌리기 버튼 이름, add 행 추가 찾기는 있으면)"]
    errs = []
    cols = {c.name: c for c in g.columns}
    for role in ("column", "base"):
        c = cols.get(e[role])
        if c is None:
            errs.append(f"{gw}: 입력 그리드의 {role} 열 {e[role]}가 이 그리드에 없어요")
        elif c.format not in ("number", "rate", "money"):
            errs.append(f"{gw}: 입력 그리드의 {role} 열 {e[role]}는 숫자 형식이어야 해요")
    if e["column"] == e["base"]:
        errs.append(f"{gw}: 고쳐 쓰는 열과 되돌아갈 값 열은 달라야 해요")
    takers = [x for x in s.grids if x is not g and x.load == "input" and x.tx in TRANSACTIONS
              and e["param"] in TRANSACTIONS[x.tx].params]
    if not takers:
        errs.append(f"{gw}: 입력 값을 받을 그리드가 없어요 ({e['param']}를 조건으로 받는 load: input 그리드)")
    if e["param"] in {c.name for c in s.conditions}:
        errs.append(f"{gw}: {e['param']}는 조회조건이 아니라 입력 그리드가 채우는 값이에요")
    if g.view != "grid" or g.live or g.load == "master" or g.actions:
        errs.append(f"{gw}: 입력 그리드는 표 보기, 실시간 아님, 마스터 아님, 처리 버튼 없음이어야 해요")
    if e.get("add"):
        errs += _lookup_use_problems(e["add"], {}, f"{gw} 행 추가", lookups)
    return errs


def _button_problems(g: Grid, s: Screen, gw: str, catalog: dict, lookups: dict[str, Lookup]) -> list[str]:
    errs, seen = [], set()
    cols = {c.name for c in g.columns}
    reach = cols | set(g.link)   # what a field can start from: the selected row, or what the grid was opened with
    for b in g.actions:
        bw = f"{gw} 버튼 {b.id}"
        if b.id in seen:
            errs.append(f"{bw}: 버튼 ID 중복")
        seen.add(b.id)
        action = ACTIONS.get(b.tx)
        if action is None:
            errs.append(f"{bw}: 없는 처리 트랜잭션 {b.tx}")
            continue
        if not b.tx.startswith(s.id.replace("-", "")):
            errs.append(f"{bw}: 처리 트랜잭션 {b.tx}는 다른 화면 것")
        for col in [*b.shows, *b.when]:
            if col not in cols:
                errs.append(f"{bw}: 고른 행의 열 {col}이 그리드에 없어요")
        if (b.shows or b.when) and not b.needs_row:
            errs.append(f"{bw}: shows·when은 행을 고르는 버튼(needs_row)에만 써요")
        names = [f.name for f in b.fields]
        for missing in [p for p in action.params if action.required(p) and p not in names]:
            errs.append(f"{bw}: {b.tx}의 필수 입력 {missing}에 해당하는 항목이 없어요")
        for f in b.fields:
            fw = f"{bw}.{f.name}"
            if f.name not in action.params:
                errs.append(f"{fw}: {b.tx}에 없는 입력 항목")
            elif f.type not in FIELD_KINDS[action.params[f.name]]:
                errs.append(f"{fw}: {b.tx}는 {action.params[f.name]} 값을 받는데 입력 형식이 {f.type}")
            if names.count(f.name) > 1:
                errs.append(f"{fw}: 입력 항목 중복")
            if f.type not in FIELD_TYPES:
                errs.append(f"{fw}: 없는 입력 형식 {f.type}")
            if f.type == "select" and not f.options:
                errs.append(f"{fw}: select는 options가 필요해요")
            if f.source and column_comment(catalog, f.source) is None:
                errs.append(f"{fw}: DB에 없는 컬럼 {f.source}")
            if not (f.source or f.label):
                errs.append(f"{fw}: source나 label이 필요해요")
            if f.column:
                if f.column not in reach:
                    errs.append(f"{fw}: 채울 값 {f.column}이 그리드 열에도 연결 값에도 없어요")
                elif f.column in cols - set(g.link) and not b.needs_row:
                    errs.append(f"{fw}: 행의 값({f.column})을 쓰려면 needs_row가 필요해요")
            elif f.readonly and f.default is None:
                errs.append(f"{fw}: 읽기 전용 항목은 채울 값(column이나 default)이 필요해요")
            errs += _lookup_use_problems(f.lookup, f.lookup_params, fw, lookups)
    return errs


def validate(screens: dict[str, Screen], catalog: dict, lookups: dict[str, Lookup] | None = None) -> list[str]:
    """Static checks against the schema catalog (erd.read_catalog). Returns problems, empty if fine.

    `lookups` are the shared 찾기 팝업 (default: the shipped lookups.yaml); their own
    definitions are checked by `lookup_problems`.
    """
    errs = []
    if lookups is None:
        lookups = load_lookups()
    for s in screens.values():
        where = f"{s.id} ({s.file})"
        if not SCREEN_ID.match(s.id):
            errs.append(f"{where}: 화면번호 형식은 STP-4자리")
        ids = set()
        search_params: set[str] = set()
        errs += _layout_problems(s, where)
        for g in s.grids:
            gw = f"{where} 그리드 {g.id}"
            tx = TRANSACTIONS.get(g.tx)
            if tx is None:
                errs.append(f"{gw}: 없는 트랜잭션 {g.tx}")
                continue
            if not g.tx.startswith(s.id.replace("-", "")):
                errs.append(f"{gw}: 트랜잭션 {g.tx}는 다른 화면 것")
            if g.load not in LOADS:
                errs.append(f"{gw}: load는 {sorted(LOADS)} 중 하나")
            if g.load == "master":
                if g.master not in ids:
                    errs.append(f"{gw}: 마스터 그리드 {g.master}가 앞에 없어요")
                for param in g.link:
                    if param not in tx.params:
                        errs.append(f"{gw}: {g.tx}에 없는 조회조건 {param}")
            else:  # search and open grids both take the screen conditions
                search_params |= set(tx.params)
            ids.add(g.id)
            for c in g.columns:
                errs += _column_problems(c, gw, catalog)
                if c.display and g.view in VIEW_NAMES:
                    errs.append(f"{gw}.{c.name}: {VIEW_NAMES[g.view]} 보기에는 표시 유형을 쓰지 않아요")
                for inside in (c.code, c.shape):
                    if inside and (inside == c.name or inside not in {x.name for x in g.columns}):
                        errs.append(f"{gw}.{c.name}: 배지 안에 넣을 열 {inside}가 이 그리드의 다른 열이 아니에요")
                if c.sub and (c.sub == c.name or c.sub not in {x.name for x in g.columns if not x.display}):
                    errs.append(f"{gw}.{c.name}: 아래에 적을 열 {c.sub}가 이 그리드의 표시 유형 없는 다른 열이 아니에요")
                if c.sub and c.display:
                    errs.append(f"{gw}.{c.name}: 아래에 적는 열(sub)은 표시 유형이 없는 칸에만 써요")
                if c.of and c.of not in {x.name for x in g.columns if x.format == "number" and x.name != c.name}:
                    errs.append(f"{gw}.{c.name}: 전체가 되는 열 {c.of}가 이 그리드의 다른 수량(number) 열이 아니에요")
                if c.hidden and g.view != "grid":
                    errs.append(f"{gw}.{c.name}: 숨긴 열은 표 보기에만 써요")
            if all(c.hidden or any(c.name in (x.code, x.shape, x.of, x.sub) for x in g.columns) for c in g.columns):
                errs.append(f"{gw}: 보이는 열이 하나도 없어요")
            if g.view not in VIEWS:
                errs.append(f"{gw}: view는 {sorted(VIEWS)} 중 하나")
            errs += _calendar_problems(g, gw)
            errs += _taccount_problems(g, s, gw)
            errs += _map_problems(g, gw)
            errs += _band_problems(g, gw)
            errs += _button_problems(g, s, gw, catalog, lookups)
            errs += _edit_problems(g, s, gw, lookups)
            if not isinstance(g.weight, int) or not 1 <= g.weight <= 5:
                errs.append(f"{gw}: weight(높이 비중)는 1~5 사이 정수")
            if g.key and g.key not in {c.name for c in g.columns}:
                errs.append(f"{gw}: 행 식별 열 {g.key}가 열에 없어요")
            if g.live and g.load == "master":
                errs.append(f"{gw}: 마스터-디테일 그리드는 실시간 갱신 대상이 아니에요 (마스터를 live로)")
            for t in g.totals:
                if t not in {c.name for c in g.columns}:
                    errs.append(f"{gw}: 합계 대상 {t}가 열에 없어요")
            if g.drill:
                # one screen for every row (screen), or the screen a column of the row names (by, one of screens)
                by, names = g.drill.get("by"), {c.name for c in g.columns}
                wanted = list(g.drill.get("screens") or []) if by else [g.drill.get("screen")]
                if by and by not in names:
                    errs.append(f"{gw}: 이동할 화면을 정하는 열 {by}가 열에 없어요")
                if by and not wanted:
                    errs.append(f"{gw}: 행마다 다른 화면으로 이동하려면 갈 수 있는 화면(screens)을 적어요")
                if by and "screen" in g.drill:
                    errs.append(f"{gw}: 화면 이동은 screen(한 화면)이나 by(행이 정하는 화면) 가운데 하나만 적어요")
                for name in wanted:
                    if name not in screens:
                        errs.append(f"{gw}: 없는 화면으로 이동 {name}")
                targets = [screens[name] for name in wanted if name in screens]
                if targets:
                    conds = {c.name for t in targets for c in t.conditions}   # a row sends what its target takes
                    for cond, col in g.drill.get("params", {}).items():
                        if cond not in conds:
                            errs.append(f"{gw}: {'·'.join(t.id for t in targets)}에 없는 조회조건 {cond}로 이동")
                        if col not in names:
                            errs.append(f"{gw}: 이동 값 {col}이 열에 없어요")
            if g.chart and not {g.chart.get("x"), g.chart.get("y")} <= {c.name for c in g.columns}:
                errs.append(f"{gw}: 차트 열 {g.chart}이 열에 없어요")
        search_params -= edit_params(s)   # filled by an 입력 그리드, not by a condition
        for c in s.conditions:
            if c.type not in CONDITION_TYPES:
                errs.append(f"{where} 조회조건 {c.name}: 없는 형식 {c.type}")
            if c.name not in search_params:
                errs.append(f"{where} 조회조건 {c.name}: 이 화면 트랜잭션 어디에도 없는 조건")
            if c.source and column_comment(catalog, c.source) is None:
                errs.append(f"{where} 조회조건 {c.name}: DB에 없는 컬럼 {c.source}")
            if not (c.source or c.label):
                errs.append(f"{where} 조회조건 {c.name}: source나 label이 필요해요")
            errs += _lookup_use_problems(c.lookup, c.lookup_params, f"{where} 조회조건 {c.name}", lookups)
    return errs


def _used_lookups(screen: Screen) -> set[str]:
    fields = [f for g in screen.grids for b in g.actions for f in b.fields]
    adds = {g.edit["add"] for g in screen.grids if g.edit and g.edit.get("add")}
    return {x.lookup for x in [*screen.conditions, *fields] if x.lookup} | adds


def resolve(screen: Screen, catalog: dict, lookups: dict[str, Lookup] | None = None) -> dict:
    """The definition as the runtime gets it: labels filled from the DB comments."""
    def label(x) -> str:
        return x.label or short(column_comment(catalog, x.source))

    out = asdict(screen)
    for g, gd in zip(screen.grids, out["grids"]):
        gd["tx_params"] = list(TRANSACTIONS[g.tx].params)
        for c, cd in zip(g.columns, gd["columns"]):
            cd["label"] = label(c)
            cd["align"] = c.align
            cd["inside"] = next((x.name for x in g.columns if c.name in (x.code, x.shape, x.of, x.sub)), None)
        for b, bd in zip(g.actions, gd["actions"]):
            bd["title"] = ACTIONS[b.tx].title
            for f, fd in zip(b.fields, bd["fields"]):
                fd["label"] = label(f)
                fd["required"] = ACTIONS[b.tx].required(f.name)
    for c, cd in zip(screen.conditions, out["conditions"]):
        cd["label"] = label(c)
    used = _used_lookups(screen)
    if used and lookups is None:
        lookups = load_lookups()
    out["lookups"] = {}
    for name in sorted(used):
        lk = lookups[name]
        out["lookups"][name] = {**asdict(lk), "columns": [{**asdict(c), "label": label(c), "align": c.align}
                                                          for c in lk.columns]}
    return out


def jump_targets(screens: dict[str, Screen], catalog: dict, lookups: dict[str, Lookup] | None = None) -> list[dict]:
    """바로 가기 창이 화면 말고도 찾아 주는 것: 조회조건에 찾기 팝업이 붙은 코드(종목 등)와, 그 코드로 열 수 있는 화면.

    Nothing is declared for it. A screen whose condition takes a code through a 찾기 팝업 can be
    opened for that code, so the list follows the screen definitions by itself."""
    lookups = load_lookups() if lookups is None else lookups
    into: dict[str, list[dict]] = {}
    for s in screens.values():
        for c in s.conditions:
            if c.lookup in lookups and not c.lookup_params:      # a list that needs a fixed parameter is not one list
                into.setdefault(c.lookup, []).append({"id": s.id, "title": s.title, "param": c.name})
    out = []
    for name, targets in into.items():
        lk = lookups[name]
        out.append({**asdict(lk), "into": targets,
                    "columns": [{**asdict(c), "label": c.label or short(column_comment(catalog, c.source)),
                                 "align": c.align} for c in lk.columns]})
    return out


def usages(screens: dict[str, Screen]) -> dict[str, list[str]]:
    """DB column -> screens and grids that show it (영향도 분석: what a schema change touches)."""
    out: dict[str, list[str]] = {}
    for s in screens.values():
        for g in s.grids:
            for c in g.columns:
                if c.source:
                    out.setdefault(c.source, []).append(f"{s.id} {g.id}")
            for b in g.actions:
                for f in b.fields:
                    if f.source:
                        out.setdefault(f.source, []).append(f"{s.id} {g.id} 버튼 {b.id}")
        for c in s.conditions:
            if c.source:
                out.setdefault(c.source, []).append(f"{s.id} 조회조건")
    return dict(sorted(out.items()))
