"""장중 현재가 (R-68): IBOR 평가에만 쓰는 가격.

`run`이 보유 종목과 미체결 주문 종목의 현재가를 주기적으로 받아 증권사 기록
(`broker_snapshots`, kind `INTRADAY_PRICE`)에 남겨요. 현재가는 원장 이벤트가 아니어서
기준가, 분개, 대사, 주문 검증에는 들어가지 않아요. ABOR는 지금처럼 일일 마감의 종가만 써요.

평가 가격은 원장이 아는 가장 최근 종가와 오늘 받은 현재가 가운데 더 늦은 것이에요.
그날 종가가 원장에 들어오면(일일 마감) 그날 현재가보다 종가가 이겨요.
"""
import datetime as dt
from dataclasses import dataclass

import psycopg

KIND = "INTRADAY_PRICE"
STALE_AFTER = dt.timedelta(minutes=5)   # 마지막 현재가가 이보다 오래되면 "갱신 멈춤"


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: int
    at: dt.datetime        # 받은 시각 (`run`이 도는 PC의 현지 시각)


@dataclass(frozen=True)
class Mark:
    """평가에 쓴 가격 하나와 그 가격이 언제 것인지."""
    price: int
    source: str                         # "현재가" | "종가"
    as_of: dt.date | dt.datetime        # 현재가는 받은 시각, 종가는 그 종가의 날짜
    stale: bool = False                 # 현재가가 STALE_AFTER 넘게 갱신되지 않음

    @property
    def label(self) -> str:
        if self.source == "현재가":
            return f"현재가 {self.as_of:%H:%M}" + (" (갱신 멈춤)" if self.stale else "")
        return f"종가 {self.as_of:%m-%d}"


def marks(close_day: dt.date | None, closes: dict[str, int], quotes: dict[str, Quote], now: dt.datetime,
          stale_after: dt.timedelta = STALE_AFTER) -> dict[str, Mark]:
    """종목마다 평가 가격: 가장 최근 종가와 오늘 받은 현재가 가운데 더 늦은 것 (R-68)."""
    out = {s: Mark(p, "종가", close_day) for s, p in closes.items()}
    for s, q in quotes.items():
        if close_day is not None and q.at.date() <= close_day:
            continue                    # 그날(또는 더 늦은 날) 종가가 이미 원장에 있어요
        out[s] = Mark(q.price, "현재가", q.at, now - q.at > stale_after)
    return out


def valid_price(price) -> bool:
    return isinstance(price, int) and not isinstance(price, bool) and price > 0


def latest_quotes(conn: psycopg.Connection, day: dt.date) -> dict[str, Quote]:
    """그날 `run`이 남긴 현재가 가운데 종목마다 마지막 값."""
    rows = conn.execute(
        "select distinct on (p.key) p.key, p.value, s.payload ->> 'at'"
        "  from broker_snapshots s, jsonb_each(s.payload -> 'prices') p"
        " where s.kind = %s and s.date = %s"
        " order by p.key, s.snapshot_id desc", (KIND, day)).fetchall()
    return {sym: Quote(sym, int(price), dt.datetime.fromisoformat(at)) for sym, price, at in rows}
