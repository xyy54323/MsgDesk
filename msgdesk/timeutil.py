"""UTC storage helpers with configurable local display timezone."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "Asia/Chongqing": 8, "UTC": 0, "Etc/UTC": 0}


def local_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        # The embedded Python distribution may not ship the IANA database.
        # Keep the workbench usable on those installs; other zones should
        # install the ``tzdata`` package for correct DST handling.
        offset = _FALLBACK_OFFSETS.get(name)
        if offset is None:
            return UTC
        return timezone(timedelta(hours=offset), name)


def local_day_bounds(date_text: str, zone_name: str) -> tuple[str, str]:
    zone = local_zone(zone_name)
    start = datetime.fromisoformat(date_text).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return iso_utc(start), iso_utc(end)


def local_date_of(value: str, zone_name: str) -> str:
    parsed = parse_datetime(value) or utc_now()
    return parsed.astimezone(local_zone(zone_name)).date().isoformat()


def local_now_text(zone_name: str) -> str:
    return utc_now().astimezone(local_zone(zone_name)).strftime("%Y-%m-%d %H:%M")

