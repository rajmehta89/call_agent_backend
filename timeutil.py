"""One clock for the whole backend: every time saved to MongoDB is UTC.

- utc_now():  naive UTC datetime for MongoDB fields (pymongo stores naive datetimes as UTC).
- utc_iso():  UTC timestamp string for transcript lines and other string fields, e.g. "2026-10-20T04:10:00Z".
- api_time(): how a stored datetime is sent to the browser (ISO with "Z"), so the browser shows it in local time.
- local_midnight_utc(): start of the server's local day, in UTC, for "today" style counts.
"""

from datetime import datetime, timedelta, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_iso(value: datetime | None = None) -> str:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def api_time(value: Any) -> Any:
    """Naive datetimes are UTC (see utc_now); mark them so browsers convert them to local time."""
    if isinstance(value, datetime):
        return value.isoformat() + "Z" if value.tzinfo is None else utc_iso(value)
    return value


def local_midnight_utc(days_ago: int = 0) -> datetime:
    local = datetime.now().astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)
