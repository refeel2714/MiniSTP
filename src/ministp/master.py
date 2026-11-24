"""시장 전체 종목정보: KIS 종목정보 마스터 파일을 받아 두고 점검한다 (R-90~92).

KIS publishes one master file per market every morning, without a key:
``kospi_code.mst.zip`` and ``kosdaq_code.mst.zip``. Each line is one listed
security (stocks, ETF, ETN, REIT, ...). The head of a line is the short code
(9 columns), the ISIN (12) and the Korean name (the rest, cp949); the tail is a
fixed-width ASCII block of 227 (KOSPI) or 221 (KOSDAQ) characters. The field
widths follow KIS's own reader (koreainvestment/open-trading-api,
``stocks_info/kis_kospi_code_mst.py`` and ``kis_kosdaq_code_mst.py``).

Nothing here writes to the database. ``fetch`` keeps the zip files exactly as
received in a folder per day, with their SHA-256 in ``manifest.json``, so a
later check can always be re-run on the same bytes. ``check`` reads those
files and compares them with the day before, with the instruments the books
use, and with the closes the books were valued at.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .instruments import AssetType, Instrument, Market

BASE_URL = "https://new.real.download.dws.co.kr/common/master/"
FILES = {Market.KOSPI: "kospi_code", Market.KOSDAQ: "kosdaq_code"}
TAIL = {Market.KOSPI: 227, Market.KOSDAQ: 221}   # fixed-width ASCII block at the end of each line
MAX_BYTES = 30 * 1024 * 1024
BAD_ROW_LIMIT = 0.01      # more bad rows than this and the layout has probably changed

# (start, width) inside the tail block, per market.
_FIELDS = {
    Market.KOSPI: {
        "group": (0, 2), "spac": (29, 1), "base_price": (41, 9), "lot": (50, 5), "halted": (60, 1),
        "liquidation": (61, 1), "managed": (62, 1), "warning": (63, 2), "lock": (68, 2), "margin": (74, 3),
        "prev_volume": (81, 12), "par_value": (93, 12), "listed_on": (105, 8), "listed_shares": (113, 15),
        "preferred": (158, 1), "market_cap": (212, 9),
    },
    Market.KOSDAQ: {
        "group": (0, 2), "spac": (24, 1), "base_price": (36, 9), "lot": (45, 5), "halted": (55, 1),
        "liquidation": (56, 1), "managed": (57, 1), "warning": (58, 2), "lock": (63, 2), "margin": (69, 3),
        "prev_volume": (76, 12), "par_value": (88, 12), "listed_on": (100, 8), "listed_shares": (108, 15),
        "preferred": (153, 1), "market_cap": (206, 9),
    },
}
_NUMBERS = ("base_price", "lot", "margin", "prev_volume", "par_value", "listed_shares", "market_cap")
_FLAGS = ("spac", "halted", "liquidation", "managed")

# 증권그룹구분코드. Codes outside this list are kept and counted, not rejected.
GROUPS = {
    "ST": "주권", "EF": "ETF", "EN": "ETN", "RT": "리츠", "MF": "증권투자회사", "SC": "선박투자회사",
    "IF": "인프라투융자회사", "DR": "주식예탁증서", "BC": "수익증권", "FS": "외국주권", "FE": "해외ETF",
    "SW": "신주인수권증권", "SR": "신주인수권증서", "EW": "ELW",
}
ASSET_GROUP = {AssetType.STOCK: "ST", AssetType.ETF: "EF"}   # how the books' asset types map to group codes


@dataclass(frozen=True)
class MasterRow:
    symbol: str
    isin: str
    name: str
    market: Market
    group: str
    base_price: int        # 기준가: normally the previous close, adjusted after 권리락·액면변경
    lot: int               # 매매수량단위
    halted: bool           # 거래정지
    liquidation: bool      # 정리매매
    managed: bool          # 관리종목
    warning: str           # 시장경고 구분 ("00" none, "01" 투자주의, "02" 투자경고, "03" 투자위험)
    lock: str              # 락구분 (권리락·배당락 등, "00" none)
    spac: bool
    preferred: str         # 우선주 구분 ("0" 보통주)
    margin: int            # 증거금 비율 (%)
    listed_on: dt.date | None
    par_value: int
    prev_volume: int
    listed_shares: int     # unit as KIS gives it (not converted)
    market_cap: int        # unit as KIS gives it (not converted)

    @property
    def group_name(self) -> str:
        return GROUPS.get(self.group, f"기타({self.group})")


@dataclass
class Parsed:
    market: Market
    rows: list[MasterRow] = field(default_factory=list)
    bad: list[tuple[int, str]] = field(default_factory=list)   # (line number, reason)

    @property
    def layout_ok(self) -> bool:
        total = len(self.rows) + len(self.bad)
        return total > 0 and len(self.bad) <= total * BAD_ROW_LIMIT


class MasterError(Exception):
    pass


def _flag(v: str, what: str) -> bool:
    if v not in ("Y", "N", " ", ""):
        raise ValueError(f"{what}={v!r}")
    return v == "Y"


def _num(v: str, what: str) -> int:
    v = v.strip()
    if v == "":
        return 0
    sign = -1 if v.startswith("-") else 1
    digits = v.lstrip("+-")
    if not digits.isdigit():
        raise ValueError(f"{what}={v!r}")
    return sign * int(digits)


def parse_line(line: str, market: Market) -> MasterRow:
    tail_len = TAIL[market]
    if len(line) < tail_len + 22:
        raise ValueError(f"줄 길이 {len(line)}")
    head, tail = line[:-tail_len], line[-tail_len:]
    if not tail.isascii():
        raise ValueError("고정폭 부분에 한글이 섞임 (길이가 맞지 않음)")
    symbol, isin, name = head[0:9].strip(), head[9:21].strip(), head[21:].strip()
    if not (symbol.isalnum() and symbol.isascii() and 6 <= len(symbol) <= 9):
        raise ValueError(f"단축코드={symbol!r}")
    if not (len(isin) == 12 and isin.isalnum() and isin.isascii()):
        raise ValueError(f"표준코드={isin!r}")
    if not name:
        raise ValueError("종목명 없음")
    f = {k: tail[s:s + w] for k, (s, w) in _FIELDS[market].items()}
    group = f["group"].strip()
    if not (len(group) == 2 and group.isalpha()):
        raise ValueError(f"그룹코드={group!r}")
    nums = {k: _num(f[k], k) for k in _NUMBERS}
    flags = {k: _flag(f[k], k) for k in _FLAGS}
    listed = f["listed_on"].strip()
    listed_on = dt.datetime.strptime(listed, "%Y%m%d").date() if listed and listed != "00000000" else None
    return MasterRow(symbol, isin, name, market, group, nums["base_price"], nums["lot"], flags["halted"],
                     flags["liquidation"], flags["managed"], f["warning"].strip() or "00",
                     f["lock"].strip() or "00", flags["spac"], f["preferred"].strip() or "0", nums["margin"],
                     listed_on, nums["par_value"], nums["prev_volume"], nums["listed_shares"], nums["market_cap"])


def parse(data: bytes, market: Market) -> Parsed:
    """Parse a .mst file (bytes, cp949). Bad lines are kept with their reason, not skipped silently."""
    out = Parsed(market)
    text = data.decode("cp949", errors="replace")
    for no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            out.rows.append(parse_line(line, market))
        except ValueError as e:
            out.bad.append((no, str(e)))
    return out


def read_zip(data: bytes, market: Market) -> bytes:
    name = FILES[market] + ".mst"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if name not in z.namelist():
            raise MasterError(f"{FILES[market]}.mst.zip 안에 {name}이 없어요: {z.namelist()}")
        return z.read(name)


# ---------- fetch: keep the files exactly as received ----------

def urllib_download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:   # normal TLS verification
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise MasterError(f"{url}: {MAX_BYTES:,}바이트보다 커요")
    return data


def default_dir(environ) -> Path:
    return Path(environ.get("MINISTP_MASTER_DIR") or Path("data") / "master")


@dataclass(frozen=True)
class FetchResult:
    folder: Path
    rows: dict[str, int]
    changed: bool          # False: same bytes as already kept for that day


def fetch(root: Path, day: dt.date, now: dt.datetime,
          download: Callable[[str], bytes] = urllib_download) -> FetchResult:
    """R-90: download both files, refuse ones that do not parse, keep them under root/<day>/."""
    got: dict[Market, bytes] = {}
    rows: dict[str, int] = {}
    for market, stem in FILES.items():
        data = download(BASE_URL + stem + ".mst.zip")
        parsed = parse(read_zip(data, market), market)
        if not parsed.layout_ok:
            sample = "; ".join(f"{n}행 {r}" for n, r in parsed.bad[:3])
            raise MasterError(f"{market.value} 파일 형식이 예상과 달라요 ({len(parsed.bad)}행 오류: {sample}). "
                              "저장하지 않았어요.")
        got[market], rows[market.value] = data, len(parsed.rows)

    folder = root / day.isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    manifest_path = folder / "manifest.json"
    old = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    files = {FILES[m] + ".mst.zip": {"sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d), "rows": rows[m.value]}
             for m, d in got.items()}
    if old and {k: v["sha256"] for k, v in old["files"].items()} == {k: v["sha256"] for k, v in files.items()}:
        return FetchResult(folder, rows, changed=False)
    if old:   # same day, new content: keep the earlier bytes next to the new ones
        stamp = dt.datetime.fromisoformat(old["fetched_at"]).strftime("%H%M%S")
        for name in old["files"]:
            if (folder / name).exists():
                (folder / name).replace(folder / f"{name}.{stamp}")
    for m, d in got.items():
        (folder / (FILES[m] + ".mst.zip")).write_bytes(d)
    history = (old.get("history", []) + [{"fetched_at": old["fetched_at"], "files": old["files"]}]) if old else []
    manifest = {"date": day.isoformat(), "fetched_at": now.isoformat(timespec="seconds"), "source": BASE_URL,
                "files": files, "history": history}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return FetchResult(folder, rows, changed=True)


def kept_days(root: Path) -> list[dt.date]:
    days = []
    for p in root.iterdir() if root.exists() else ():
        try:
            d = dt.date.fromisoformat(p.name)
        except ValueError:
            continue
        if (p / "manifest.json").exists():
            days.append(d)
    return sorted(days)


def load_day(root: Path, day: dt.date) -> dict[str, MasterRow]:
    folder = root / day.isoformat()
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    out: dict[str, MasterRow] = {}
    for market, stem in FILES.items():
        name = stem + ".mst.zip"
        data = (folder / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["files"][name]["sha256"]:
            raise MasterError(f"{folder / name}: 받은 뒤 파일이 바뀌었어요 (SHA-256 불일치)")
        for row in parse(read_zip(data, market), market).rows:
            out[row.symbol] = row
    return out


def write_csv(rows: dict[str, MasterRow], path: Path) -> None:
    """For opening in Excel: UTF-8 with BOM, Korean headers."""
    import csv
    heads = ["단축코드", "표준코드", "종목명", "시장", "그룹", "그룹명", "기준가", "매매수량단위", "거래정지",
             "정리매매", "관리종목", "시장경고", "락구분", "SPAC", "우선주", "증거금비율", "상장일", "액면가",
             "전일거래량", "상장주수(KIS 단위)", "시가총액(KIS 단위)"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(heads)
        for r in sorted(rows.values(), key=lambda r: (r.market.value, r.symbol)):
            w.writerow([r.symbol, r.isin, r.name, r.market.value, r.group, r.group_name, r.base_price, r.lot,
                        "Y" if r.halted else "", "Y" if r.liquidation else "", "Y" if r.managed else "",
                        r.warning, r.lock, "Y" if r.spac else "", r.preferred, r.margin,
                        r.listed_on.isoformat() if r.listed_on else "", r.par_value, r.prev_volume,
                        r.listed_shares, r.market_cap])


# ---------- check ----------

OK, WARN, BAD = "정상", "주의", "확인 필요"


@dataclass(frozen=True)
class Finding:
    level: str
    topic: str
    text: str


@dataclass
class Report:
    day: dt.date
    prev_day: dt.date | None
    counts: dict[tuple[str, str], int]        # (market, group name) -> rows
    flags: dict[str, int]
    new: list[MasterRow]
    gone: list[MasterRow]
    renamed: list[tuple[MasterRow, MasterRow]]   # (before, after)
    findings: list[Finding]

    @property
    def level(self) -> str:
        levels = {f.level for f in self.findings}
        return BAD if BAD in levels else WARN if WARN in levels else OK


def check(day: dt.date, rows: dict[str, MasterRow], prev_day: dt.date | None, prev: dict[str, MasterRow] | None,
          instruments: dict[str, Instrument], held: set[str],
          closes: dict[dt.date, dict[str, int]]) -> Report:
    """R-91, R-92. `closes` are the closes the books were valued at, by day (pricecheck.closes_by_day)."""
    counts: dict[tuple[str, str], int] = {}
    for r in rows.values():
        counts[(r.market.value, r.group_name)] = counts.get((r.market.value, r.group_name), 0) + 1
    flags = {"거래정지": sum(r.halted for r in rows.values()),
             "정리매매": sum(r.liquidation for r in rows.values()),
             "관리종목": sum(r.managed for r in rows.values()),
             "시장경고": sum(r.warning != "00" for r in rows.values()),
             "권리락 등(락구분)": sum(r.lock != "00" for r in rows.values())}
    findings: list[Finding] = []

    new, gone, renamed = [], [], []
    if prev is not None:
        new = [rows[s] for s in sorted(rows.keys() - prev.keys())]
        gone = [prev[s] for s in sorted(prev.keys() - rows.keys())]
        renamed = [(prev[s], rows[s]) for s in sorted(rows.keys() & prev.keys()) if prev[s].name != rows[s].name]
        if prev and len(gone) > len(prev) * 0.05:
            findings.append(Finding(BAD, "전일 대비", f"전일 파일보다 {len(gone)}종목이 빠졌어요. 파일이 잘렸을 수 있어요."))

    # R-91: every instrument the books use must be in the market master as the same thing.
    for sym, inst in sorted(instruments.items()):
        r = rows.get(sym)
        if r is None:
            findings.append(Finding(BAD, "등록 종목", f"{sym} {inst.name}: 마스터에 없어요 (상장폐지 또는 잘못된 코드)"))
            continue
        diffs = []
        if r.market != inst.market:
            diffs.append(f"시장 {inst.market.value} → 마스터 {r.market.value}")
        if ASSET_GROUP.get(inst.asset_type) != r.group:
            diffs.append(f"구분 {inst.asset_type.value} → 마스터 {r.group_name}")
        if diffs:
            findings.append(Finding(BAD, "등록 종목", f"{sym} {inst.name}: " + ", ".join(diffs)))
        if r.name.replace(" ", "") != inst.name.replace(" ", ""):
            findings.append(Finding(WARN, "등록 종목", f"{sym}: 등록 이름 '{inst.name}', 마스터 '{r.name}'"))
        state = [w for w, on in (("거래정지", r.halted), ("정리매매", r.liquidation), ("관리종목", r.managed)) if on]
        if r.warning != "00":
            state.append(f"시장경고 {r.warning}")
        if state:
            findings.append(Finding(BAD if sym in held else WARN, "등록 종목", f"{sym} {r.name}: " + ", ".join(state)))

    # R-92: the master's 기준가 is a second source for the close the books took.
    valued = sorted(d for d in closes if d < day)
    for sym in sorted(held):
        r = rows.get(sym)
        if r is None:
            continue
        last = next((d for d in reversed(valued) if sym in closes[d]), None)
        if last is None:
            findings.append(Finding(WARN, "기준가 대조", f"{sym}: 원장에 아직 종가 평가가 없어요"))
            continue
        close = closes[last][sym]
        if r.base_price == close:
            findings.append(Finding(OK, "기준가 대조", f"{sym} 기준가 {r.base_price:,} = 원장 {last} 종가"))
        elif r.lock != "00":
            findings.append(Finding(WARN, "기준가 대조", f"{sym} 기준가 {r.base_price:,}, 원장 {last} 종가 {close:,}: "
                                    f"락구분 {r.lock}라 권리 조정일 수 있어요"))
        else:
            earlier = [d for d in valued if d < last and closes[d].get(sym) == r.base_price]
            hint = f" (원장 {earlier[-1]} 종가와 같아요: 파일이 하루 전 것일 수 있어요)" if earlier else ""
            findings.append(Finding(BAD, "기준가 대조",
                                    f"{sym} 기준가 {r.base_price:,} ≠ 원장 {last} 종가 {close:,}{hint}"))
    return Report(day, prev_day, counts, flags, new, gone, renamed, findings)
