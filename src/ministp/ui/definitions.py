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
#   meter  비율 막대: 0~1 값을 %와 막대로
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
VIEWS = {"grid", "cards", "calendar"}
VIEW_NAMES = {"cards": "카드", "calendar": "달력"}
LOADS = {"search", "open", "master", "input"}
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
    view: str = "grid"          # VIEWS: grid | cards (one-row summaries) | calendar (a cell a day)
    calendar: dict | None = None  # view: calendar: {date: column, state: column}; the rest show on hover
    meta: dict[str, str] = field(default_factory=dict)   # tx meta key -> label
    drill: dict | None = None   # {screen: STP-xxxx, params: {cond: column}}
    chart: dict | None = None   # {x: column, y: column}: a line over the rows
    live: bool = False          # reload when the DB changes (SSE), changed rows highlighted
    key: str | None = None      # column that identifies a row across reloads (default: first column)
    weight: int = 1             # split layout: this grid's share of its side's height
    actions: list[Button] = field(default_factory=list)   # 처리 버튼, top right of the grid


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


def short(comment: str | None) -> str:
    return (comment or "").split(" (")[0].strip()


def column_comment(catalog: dict, source: str) -> str | None:
    table, _, col = source.partition(".")
    info = catalog.get(table)
    if not info:
        return None
    return next((c["comment"] for c in info["columns"] if c["name"] == col), None)


def split_sides(screen: Screen) -> tuple[list[Grid], list[Grid], list[Grid]]:
    """A split screen's grids as (left: the list with details, right: its details, below: the rest)."""
    masters = {g.master for g in screen.grids if g.load == "master"}
    left = [g for g in screen.grids if g.id in masters]
    right = [g for g in screen.grids if g.load == "master"]
    below = [g for g in screen.grids if g.id not in masters and g.load != "master"]
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
                if c.of and c.of not in {x.name for x in g.columns if x.format == "number" and x.name != c.name}:
                    errs.append(f"{gw}.{c.name}: 전체가 되는 열 {c.of}가 이 그리드의 다른 수량(number) 열이 아니에요")
                if c.hidden and g.view != "grid":
                    errs.append(f"{gw}.{c.name}: 숨긴 열은 표 보기에만 써요")
            if all(c.hidden or any(c.name in (x.code, x.shape, x.of) for x in g.columns) for c in g.columns):
                errs.append(f"{gw}: 보이는 열이 하나도 없어요")
            if g.view not in VIEWS:
                errs.append(f"{gw}: view는 {sorted(VIEWS)} 중 하나")
            errs += _calendar_problems(g, gw)
            errs += _button_problems(g, s, gw, catalog, lookups)
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
                target = screens.get(g.drill.get("screen"))
                if target is None:
                    errs.append(f"{gw}: 없는 화면으로 이동 {g.drill.get('screen')}")
                else:
                    conds = {c.name for c in target.conditions}
                    for cond, col in g.drill.get("params", {}).items():
                        if cond not in conds:
                            errs.append(f"{gw}: {target.id}에 없는 조회조건 {cond}로 이동")
                        if col not in {c.name for c in g.columns}:
                            errs.append(f"{gw}: 이동 값 {col}이 열에 없어요")
            if g.chart and not {g.chart.get("x"), g.chart.get("y")} <= {c.name for c in g.columns}:
                errs.append(f"{gw}: 차트 열 {g.chart}이 열에 없어요")
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
    return {x.lookup for x in [*screen.conditions, *fields] if x.lookup}


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
            cd["inside"] = next((x.name for x in g.columns if c.name in (x.code, x.shape, x.of)), None)
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
