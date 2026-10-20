"""Pure domain logic for RoomLoop.

This module deliberately has **no** database, HTTP or framework imports. The
parts of the system most likely to be wrong (wall-clock recurrence across DST,
interval overlap, timestamp parsing) live here so they can be reasoned about and
tested in isolation.

Three rules drive the design:

* Bookings are half-open intervals ``[start, end)``. 10:00-11:00 and 11:00-12:00
  do not conflict.
* A recurring booking repeats at the same **wall-clock** time in the room's
  office timezone. Weekly expansion therefore happens on naive local datetimes;
  each occurrence is then resolved against the office timezone independently.
* Naive timestamps on the API mean "wall clock at the office". Timestamps with
  an offset are respected as exact instants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = UTC

#: Upper bound on one generated series. 260 weekly occurrences = ~5 years.
MAX_SERIES_OCCURRENCES = 260

#: Sanity bound for a single booking. Guards against typo'd end dates.
MAX_BOOKING_DURATION = timedelta(hours=24)

#: Wire format required by the nightly reporting job (constraint C1).
NAIVE_ISO = "%Y-%m-%dT%H:%M:%S"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class RoomLoopError(Exception):
    """Base class for errors that are part of the service contract."""

    code = "INTERNAL_ERROR"
    status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ValidationError(RoomLoopError):
    code = "VALIDATION_ERROR"
    status = 422


class InvalidInterval(ValidationError):
    code = "INVALID_INTERVAL"


class InvalidTimestamp(ValidationError):
    code = "INVALID_TIMESTAMP"


class RecurrenceTooLong(ValidationError):
    code = "RECURRENCE_TOO_LONG"


class RepeatUntilInvalid(ValidationError):
    code = "INVALID_REPEAT_UNTIL"


class NotFoundError(RoomLoopError):
    code = "NOT_FOUND"
    status = 404


class ConflictError(RoomLoopError):
    code = "SLOT_UNAVAILABLE"
    status = 409


class SlotUnavailable(ConflictError):
    code = "SLOT_UNAVAILABLE"


class SeriesFullyConflicted(ConflictError):
    code = "SERIES_FULLY_CONFLICTED"


class SeriesConflictFailFast(ConflictError):
    code = "SERIES_CONFLICT_FAIL_FAST"


class IdempotencyKeyReused(ConflictError):
    code = "IDEMPOTENCY_KEY_REUSED"


class TimeZoneDataMissing(RoomLoopError):
    """The host has no IANA timezone database (typical on Windows).

    This is an environment problem, not a bad request: it must not be reported
    as a 422, otherwise the caller blames their payload instead of their install.
    """

    code = "TZ_DATA_MISSING"
    status = 500


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------
def timezone_database_available() -> bool:
    """True when the host can resolve IANA zone names.

    ``zoneinfo`` reads the tz database from the operating system. Windows ships
    without one, so the ``tzdata`` package must be installed; Alpine images have
    the same problem unless ``tzdata`` is added.
    """
    try:
        ZoneInfo("UTC")
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


TZ_DATA_HINT = (
    "This host has no IANA timezone database. Install it with: "
    "python -m pip install tzdata (Windows and minimal container images need this)."
)


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        if not timezone_database_available():
            raise TimeZoneDataMissing(TZ_DATA_HINT, details={"timezone": name}) from exc
        raise ValidationError(f"Unknown timezone {name!r}", details={"timezone": name}) from exc


@dataclass(frozen=True)
class ResolvedWallClock:
    """A wall-clock reading resolved to a real instant."""

    local: datetime  # aware, in the requested zone
    warning: str | None = None


def resolve_wall_clock(naive: datetime, tz: ZoneInfo) -> ResolvedWallClock:
    """Attach ``tz`` to a naive datetime using explicit, documented DST rules.

    * Ambiguous wall clock (clocks fall back, e.g. 02:30 occurs twice):
      the **first** occurrence is used (``fold=0``).
    * Non-existent wall clock (clocks spring forward, e.g. 02:30 does not exist):
      the time is shifted **forward** by the gap so the meeting still happens,
      and a ``DST_GAP_SHIFTED`` warning is returned.
    """
    if naive.tzinfo is not None:
        raise ValueError("resolve_wall_clock expects a naive datetime")

    first = naive.replace(tzinfo=tz, fold=0)
    second = naive.replace(tzinfo=tz, fold=1)
    ambiguous = first.utcoffset() != second.utcoffset()

    round_trip = first.astimezone(UTC).astimezone(tz)
    if round_trip.replace(tzinfo=None) != naive:
        # Inside a spring-forward gap; fold=0 lands after the gap.
        return ResolvedWallClock(round_trip, "DST_GAP_SHIFTED")

    return ResolvedWallClock(first, "DST_AMBIGUOUS_FIRST_INSTANT" if ambiguous else None)


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 date/datetime. Naive values stay naive."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidTimestamp("Expected an ISO-8601 timestamp, e.g. 2026-07-02T09:00:00")
    raw = value.strip()
    if _DATE_ONLY.match(raw):
        return datetime.combine(date.fromisoformat(raw), time.min)
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise InvalidTimestamp(
            "Expected an ISO-8601 timestamp, e.g. 2026-07-02T09:00:00 or 2026-07-02T09:00:00+02:00",
            details={"value": value},
        ) from exc


def parse_instant(value: str, tz_name: str) -> tuple[datetime, str | None]:
    """Parse a timestamp into an aware UTC instant.

    Naive input is interpreted as wall clock in ``tz_name`` (the office of the
    room being booked). Offset-bearing input is used verbatim.
    """
    tz = get_zone(tz_name)
    parsed = parse_iso(value)
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC), None
    resolved = resolve_wall_clock(parsed, tz)
    return resolved.local.astimezone(UTC), resolved.warning


def parse_until(value: str, tz_name: str) -> tuple[date, str | None]:
    """Parse a ``repeat_until`` value into the local date of the last allowed occurrence."""
    tz = get_zone(tz_name)
    parsed = parse_iso(value)
    warning = None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz).replace(tzinfo=None)
    elif parsed.time() == time.min and _DATE_ONLY.match(value.strip()):
        warning = None
    return parsed.date(), warning


def floor_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def naive_iso(dt: datetime) -> str:
    """Serialise to the legacy format: wall clock, seconds, **no offset** (C1)."""
    return floor_seconds(dt).strftime(NAIVE_ISO)


def naive_from_epoch(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)


def iso_utc_from_epoch(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(dt: datetime) -> int:
    return int(floor_seconds(dt.astimezone(UTC)).timestamp())


def offset_string(instant_epoch: int, tz_name: str) -> str:
    local = datetime.fromtimestamp(instant_epoch, UTC).astimezone(get_zone(tz_name))
    total = int(local.utcoffset().total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


# ---------------------------------------------------------------------------
# Interval arithmetic
# ---------------------------------------------------------------------------
def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when two half-open intervals ``[start, end)`` share any instant.

    Back-to-back bookings (``a_end == b_start``) return False by construction.
    """
    return a_start < b_end and b_start < a_end


def validate_window(start: datetime, end: datetime) -> None:
    if end <= start:
        raise InvalidInterval(
            "end must be after start",
            details={"start": start.isoformat(), "end": end.isoformat()},
        )
    if end - start > MAX_BOOKING_DURATION:
        raise InvalidInterval(
            f"A single booking may not exceed {int(MAX_BOOKING_DURATION.total_seconds() // 3600)} hours",
            details={"duration_seconds": int((end - start).total_seconds())},
        )


# ---------------------------------------------------------------------------
# Weekly recurrence
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Occurrence:
    """One generated instance of a recurring series."""

    index: int
    start_utc: datetime
    end_utc: datetime
    start_local: datetime  # naive wall clock at the office
    end_local: datetime
    warning: str | None = None


def expand_weekly(
    *,
    first_start_naive: datetime,
    duration: timedelta,
    tz_name: str,
    repeat_until: date,
    max_occurrences: int = MAX_SERIES_OCCURRENCES,
) -> list[Occurrence]:
    """Expand a weekly series at a fixed wall-clock time.

    ``first_start_naive`` is the requested wall clock (not an instant). Adding
    seven days to a naive datetime keeps the wall clock constant across DST,
    which is requirement R3. Each occurrence is then resolved independently so
    its UTC offset always reflects the office's real offset on that date.
    """
    if duration <= timedelta(0):
        raise InvalidInterval("A booking must have a positive duration")
    if repeat_until < first_start_naive.date():
        raise RepeatUntilInvalid(
            "repeat_until must not be before the first occurrence",
            details={"repeat_until": repeat_until.isoformat()},
        )

    tz = get_zone(tz_name)
    occurrences: list[Occurrence] = []
    cursor = floor_seconds(first_start_naive)

    while cursor.date() <= repeat_until:
        if len(occurrences) >= max_occurrences:
            raise RecurrenceTooLong(
                f"A recurring booking may contain at most {max_occurrences} occurrences",
                details={"max_occurrences": max_occurrences, "repeat_until": repeat_until.isoformat()},
            )
        resolved = resolve_wall_clock(cursor, tz)
        start_utc = resolved.local.astimezone(UTC)
        end_utc = start_utc + duration
        # When a wall clock does not exist (spring-forward gap) the resolved
        # local time is authoritative, so the series reports what actually
        # happened rather than the impossible requested time.
        start_local = resolved.local.replace(tzinfo=None)
        end_local = end_utc.astimezone(tz).replace(tzinfo=None)
        occurrences.append(
            Occurrence(
                index=len(occurrences),
                start_utc=start_utc,
                end_utc=end_utc,
                start_local=start_local,
                end_local=end_local,
                warning=resolved.warning,
            )
        )
        cursor += timedelta(days=7)

    if not occurrences:  # pragma: no cover - guarded by repeat_until check above
        raise RepeatUntilInvalid("Recurring booking produced no occurrences")
    return occurrences
