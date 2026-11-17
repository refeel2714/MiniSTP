"""Daily reconciliation: compare internal vs broker values (spec R-70~74)."""
import datetime as dt
from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    MATCH = "MATCH"
    BREAK = "BREAK"


class Cause(StrEnum):
    MISSING_EXEC = "MISSING_EXEC"
    FEE_DIFF = "FEE_DIFF"
    TAX_DIFF = "TAX_DIFF"
    ROUNDING = "ROUNDING"
    CORP_ACTION = "CORP_ACTION"
    TIMING = "TIMING"
    UNKNOWN = "UNKNOWN"


AMOUNT_ITEMS = {"BOOK_COST", "CASH_D0", "CASH_PROJECTED"}
COUNT_ITEMS = {"EXEC_COUNT", "EXEC_QTY"}


@dataclass(frozen=True)
class ReconItem:
    item: str
    key: str
    internal: int
    broker: int


@dataclass(frozen=True)
class ReconResult:
    date: dt.date
    item: str
    key: str
    internal: int
    broker: int
    diff: int
    status: Status
    cause: Cause | None


def candidate_cause(item: ReconItem) -> Cause:
    """First-pass cause candidate (R-73); the human makes the final call."""
    diff = item.broker - item.internal
    if item.item in AMOUNT_ITEMS and abs(diff) == 1:
        return Cause.ROUNDING
    if item.item in COUNT_ITEMS and diff > 0:
        return Cause.MISSING_EXEC
    return Cause.UNKNOWN


def reconcile(date: dt.date, items: list[ReconItem]) -> list[ReconResult]:
    out = []
    for it in items:
        diff = it.broker - it.internal
        status = Status.MATCH if diff == 0 else Status.BREAK  # tolerance 0 (R-71)
        out.append(ReconResult(date, it.item, it.key, it.internal, it.broker, abs(diff), status,
                               None if status is Status.MATCH else candidate_cause(it)))
    return out
