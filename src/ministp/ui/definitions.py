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

from .transactions import TRANSACTIONS

FORMATS = {"text", "code", "number", "money", "rate", "date", "datetime", "json"}
RIGHT = {"number", "money", "rate"}
COLORS = {None, "sign", "side", "status", "verdict"}  # verdict: the row's ok field
LOADS = {"search", "open", "master", "input"}
CONDITION_TYPES = {"date", "text", "select", "bool", "number"}
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

    @property
    def align(self) -> str:
        return "right" if self.format in RIGHT else "left"


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
    view: str = "grid"          # grid | cards (one-row summaries)
    meta: dict[str, str] = field(default_factory=dict)   # tx meta key -> label
    drill: dict | None = None   # {screen: STP-xxxx, params: {cond: column}}
    chart: dict | None = None   # {x: column, y: column}: a line over the rows


@dataclass
class Condition:
    name: str
    type: str = "text"
    source: str | None = None
    label: str | None = None
    options: list[str] = field(default_factory=list)
    default: str | None = None  # "today" or a literal
    required: bool = False      # grids are not loaded until it has a value


@dataclass
class Screen:
    id: str
    title: str
    purpose: str
    conditions: list[Condition]
    grids: list[Grid]
    file: str = ""


def _build(raw: dict, file: str) -> Screen:
    try:
        grids = [Grid(**{**g, "columns": [Column(**c) for c in g["columns"]]}) for g in raw["grids"]]
        conds = [Condition(**c) for c in raw.get("conditions", [])]
        return Screen(raw["id"], raw["title"], raw.get("purpose", ""), conds, grids, file)
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


def short(comment: str | None) -> str:
    return (comment or "").split(" (")[0].strip()


def column_comment(catalog: dict, source: str) -> str | None:
    table, _, col = source.partition(".")
    info = catalog.get(table)
    if not info:
        return None
    return next((c["comment"] for c in info["columns"] if c["name"] == col), None)


def validate(screens: dict[str, Screen], catalog: dict) -> list[str]:
    """Static checks against the schema catalog (erd.read_catalog). Returns problems, empty if fine."""
    errs = []
    for s in screens.values():
        where = f"{s.id} ({s.file})"
        if not SCREEN_ID.match(s.id):
            errs.append(f"{where}: 화면번호 형식은 STP-4자리")
        ids = set()
        search_params: set[str] = set()
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
                if c.format not in FORMATS:
                    errs.append(f"{gw}.{c.name}: 없는 형식 {c.format}")
                if c.color not in COLORS:
                    errs.append(f"{gw}.{c.name}: 없는 색 규칙 {c.color}")
                if c.source:
                    if column_comment(catalog, c.source) is None:
                        errs.append(f"{gw}.{c.name}: DB에 없는 컬럼 {c.source}")
                    if c.label:
                        errs.append(f"{gw}.{c.name}: DB 컬럼이 있으면 한글명은 DB 주석을 써요 (label 제거)")
                elif not c.label:
                    errs.append(f"{gw}.{c.name}: source(DB 컬럼)나 label 중 하나가 필요해요")
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
    return errs


def resolve(screen: Screen, catalog: dict) -> dict:
    """The definition as the runtime gets it: labels filled from the DB comments."""
    out = asdict(screen)
    for g, gd in zip(screen.grids, out["grids"]):
        gd["tx_params"] = list(TRANSACTIONS[g.tx].params)
        for c, cd in zip(g.columns, gd["columns"]):
            cd["label"] = c.label or short(column_comment(catalog, c.source))
            cd["align"] = c.align
    for c, cd in zip(screen.conditions, out["conditions"]):
        cd["label"] = c.label or short(column_comment(catalog, c.source))
    return out


def usages(screens: dict[str, Screen]) -> dict[str, list[str]]:
    """DB column -> screens and grids that show it (영향도 분석: what a schema change touches)."""
    out: dict[str, list[str]] = {}
    for s in screens.values():
        for g in s.grids:
            for c in g.columns:
                if c.source:
                    out.setdefault(c.source, []).append(f"{s.id} {g.id}")
        for c in s.conditions:
            if c.source:
                out.setdefault(c.source, []).append(f"{s.id} 조회조건")
    return dict(sorted(out.items()))
