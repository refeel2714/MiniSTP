"""Helpers for scripts run on the owner's PC."""
import datetime as dt
import getpass

from .calendar import BusinessCalendar

PROMPTS = [
    ("MINISTP_KIS_APP_KEY", "모의투자 앱키"),
    ("MINISTP_KIS_APP_SECRET", "모의투자 앱시크릿"),
    ("MINISTP_KIS_ACCOUNT", "모의투자 계좌번호 (예: 12345678-01)"),
]


def ask_missing(environ) -> dict:
    """Ask for anything not set in the environment. Input is hidden and not saved."""
    env = dict(environ)
    for key, label in PROMPTS:
        if not env.get(key):
            env[key] = getpass.getpass(f"{label} (입력 내용은 화면에 표시되지 않아요): ").strip()
    return env


# KRX holidays needed for T+2 around the paper-trading period. Extend yearly
# (spec R-10: dates in an unloaded year raise instead of guessing).
KRX_HOLIDAYS = {
    dt.date(2026, 12, 25), dt.date(2026, 12, 31),
    dt.date(2027, 1, 1),
}


def krx_calendar() -> BusinessCalendar:
    return BusinessCalendar({2026, 2027}, KRX_HOLIDAYS)
