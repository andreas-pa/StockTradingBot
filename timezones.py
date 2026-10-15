"""Time formatting: market time (ET) plus your local time from LOCAL_TIMEZONE in .env."""

import os
from functools import lru_cache
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from strategy import BASE_DIR, MARKET_TZ


@lru_cache(maxsize=1)
def _load_local_tz():
    """Return (time zone or None, error message or None)."""
    load_dotenv(BASE_DIR / ".env")
    name = os.getenv("LOCAL_TIMEZONE", "").strip()
    if not name:
        return None, None
    try:
        return ZoneInfo(name), None
    except Exception:
        return None, f"Unknown LOCAL_TIMEZONE '{name}'. Showing Eastern time only. Use a name like Europe/Athens."


def local_tz():
    return _load_local_tz()[0]


def local_tz_error():
    return _load_local_tz()[1]


def format_time(dt, date_format="%Y-%m-%d", time_format="%H:%M"):
    """Format a time in ET, followed by local time when LOCAL_TIMEZONE is set.

    Example: "2026-10-16 09:30 ET (16:30 EEST)". The local part includes the
    date only when it falls on a different day than the ET time.
    """
    et = dt.astimezone(MARKET_TZ)
    text = f"{et.strftime(f'{date_format} {time_format}').strip()} ET"

    tz = local_tz()
    if tz is None:
        return text
    local = dt.astimezone(tz)
    # Skip the local part when it would show the same clock time as ET.
    if local.replace(tzinfo=None) == et.replace(tzinfo=None):
        return text
    local_format = time_format if local.date() == et.date() else f"{date_format} {time_format}"
    return f"{text} ({local.strftime(local_format).strip()} {local.strftime('%Z')})"
