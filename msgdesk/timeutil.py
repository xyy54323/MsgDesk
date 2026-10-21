"""UTC storage helpers with configurable local display timezone."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
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


def has_explicit_time(value: str | None) -> bool:
    """Whether a source value contains a concrete hour and minute."""
    if not value:
        return False
    return bool(re.search(r"(?:T|\s)\d{1,2}:\d{2}", value.strip()))


_SOURCE_DATETIME_RE = re.compile(
    r"(?P<year>20\d{2})[-/.年](?P<month>\d{1,2})[-/.月](?P<day>\d{1,2})"
    r"(?:日)?[T\s]+(?P<hour>\d{1,2})[:：](?P<minute>\d{2})"
    r"(?:[:：](?P<second>\d{2}))?"
    r"(?P<zone>Z|[+-]\d{2}:?\d{2})?"
)


def parse_source_datetime(value: str | None, zone_name: str) -> datetime | None:
    """Parse a source's local date/time text without treating date-only as UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    match = _SOURCE_DATETIME_RE.search(text)
    if match:
        second = int(match.group("second") or 0)
        zone_text = match.group("zone")
        if zone_text:
            normalized = (
                f"{match.group('year')}-{int(match.group('month')):02d}-"
                f"{int(match.group('day')):02d}T{int(match.group('hour')):02d}:"
                f"{int(match.group('minute')):02d}:{second:02d}"
                f"{'+00:00' if zone_text == 'Z' else zone_text}"
            )
            return parse_datetime(normalized)
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            second,
            tzinfo=local_zone(zone_name),
        ).astimezone(UTC)
    if has_explicit_time(text):
        return parse_datetime(text)
    return None


def source_published_datetime(
    values: list[str], fallback_date: str, zone_name: str
) -> tuple[str, bool]:
    """Return UTC text and whether the source supplied a concrete time."""
    for value in values:
        parsed = parse_source_datetime(value, zone_name)
        if parsed is not None:
            return iso_utc(parsed), True
    fallback = datetime.combine(
        date.fromisoformat(fallback_date), time.min, local_zone(zone_name)
    )
    return iso_utc(fallback), False


_FALLBACK_OFFSETS = {"Asia/Shanghai": 8, "Asia/Chongqing": 8, "UTC": 0, "Etc/UTC": 0}


def local_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        # The embedded Python distribution may not ship the IANA database.
        # Keep the app usable on those installs; other zones should
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

