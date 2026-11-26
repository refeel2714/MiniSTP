"""Helpers for scripts run on the owner's PC."""
import datetime as dt
import getpass

from .calendar import BusinessCalendar

PROMPTS = [
    ("APP_KEY", "모의투자 앱키"),
    ("APP_SECRET", "모의투자 앱시크릿"),
    ("ACCOUNT", "모의투자 계좌번호 (예: 12345678-01)"),
]


def ask_missing(environ, prefix: str = "MINISTP_KIS", who: str = "") -> dict:
    """Ask for anything not set in the environment. Input is hidden and not saved.

    `prefix` picks one account's variables (`MINISTP_KIS_2_APP_KEY` ...); `who` names
    that account in the prompt so a key is not typed in for the wrong one."""
    env = dict(environ)
    for name, label in PROMPTS:
        key = f"{prefix}_{name}"
        if not env.get(key):
            env[key] = getpass.getpass(f"{who}{label} (입력 내용은 화면에 표시되지 않아요): ").strip()
    return env


# KRX holidays needed for T+2 around the paper-trading period. Extend yearly
# (spec R-10: dates in an unloaded year raise instead of guessing).
KRX_HOLIDAYS = {
    dt.date(2026, 12, 25), dt.date(2026, 12, 31),
    dt.date(2027, 1, 1),
}
# 2027 is loaded only so that late-December trades can settle in January. Its other
# closures (설 연휴 in early February onwards) are not registered yet, so dates after
# this raise instead of being taken for business days. Move it when KRX publishes
# the 2027 holidays and they are added above.
KRX_VERIFIED_UNTIL = dt.date(2027, 1, 31)


def krx_calendar() -> BusinessCalendar:
    return BusinessCalendar({2026, 2027}, KRX_HOLIDAYS, verified_until=KRX_VERIFIED_UNTIL)
