"""Application service: the booking rules from the brief live here.

Requirement mapping
-------------------
R1  all-or-nothing   -> ``create_recurring`` runs inside one ``BEGIN IMMEDIATE``
                        transaction; any error rolls the whole series back.
R2  skip clashes      -> conflicts are collected, not raised; only the rest are
                        inserted and every skip is reported with its blocker.
R3  same wall clock   -> ``expand_weekly`` steps naive datetimes +7 days and
                        resolves each occurrence in the office timezone.
R4  half-open overlap -> ``start_a < end_b and start_b < end_a``.
R5  rooms 1..N        -> ``iter_rooms`` walks the sequential room ids.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from typing import Any

from .db import Database, project_booking, utc_now_epoch
from .domain import (
    MAX_SERIES_OCCURRENCES,
    UTC,
    IdempotencyKeyReused,
    InvalidInterval,
    NotFoundError,
    Occurrence,
    RepeatUntilInvalid,
    SeriesConflictFailFast,
    SeriesFullyConflicted,
    SlotUnavailable,
    ValidationError,
    expand_weekly,
    floor_seconds,
    get_zone,
    naive_iso,
    parse_instant,
    parse_iso,
    parse_until,
    resolve_wall_clock,
)

SERIES_CONFLICT_POLICIES = ("skip", "fail")


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _require_text(value: str | None, field: str, *, max_len: int = 120) -> str:
    if value is None or not str(value).strip():
        raise ValidationError(f"{field} is required", details={"field": field})
    cleaned = str(value).strip()
    if len(cleaned) > max_len:
        raise ValidationError(
            f"{field} must be at most {max_len} characters", details={"field": field}
        )
    return cleaned


class BookingService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ rooms
    def iter_rooms(self) -> list[dict]:
        """R5: rooms are numbered sequentially from 1, so we iterate ids, not a list."""
        rooms = self.db.rooms()
        by_id = {room["id"]: room for room in rooms}
        highest = max(by_id) if by_id else 0
        return [by_id[room_id] for room_id in range(1, highest + 1) if room_id in by_id]

    def get_room(self, room_id: int) -> dict:
        room = self.db.room(room_id)
        if room is None:
            raise NotFoundError(f"Room {room_id} does not exist", details={"room_id": room_id})
        return room

    # --------------------------------------------------------------- bookings
    def create_booking(
        self,
        *,
        room_id: int,
        user: str,
        start: str,
        end: str,
        idempotency_key: str | None = None,
    ) -> tuple[dict, bool]:
        """Create one booking. Returns ``(booking, was_idempotent_replay)``."""
        room = self.get_room(room_id)
        user = _require_text(user, "user")
        tz_name = room["timezone"]

        start_utc, start_warn = parse_instant(start, tz_name)
        end_utc, end_warn = parse_instant(end, tz_name)
        if end_utc <= start_utc:
            raise InvalidInterval(
                "end must be after start",
                details={"start": start, "end": end},
            )
        if end_utc - start_utc > timedelta(hours=24):
            raise InvalidInterval("A single booking may not exceed 24 hours")

        start_epoch = int(floor_seconds(start_utc).timestamp())
        end_epoch = int(floor_seconds(end_utc).timestamp())
        request = {
            "kind": "single",
            "room_id": room_id,
            "user": user,
            "start": naive_iso(start_utc),
            "end": naive_iso(end_utc),
        }
        fingerprint = _fingerprint(request)

        if idempotency_key:
            replay = self._check_idempotency(idempotency_key, fingerprint)
            if replay is not None:
                return replay, True

        with self.db.transaction() as conn:
            if idempotency_key:
                # Re-check inside the write lock: two concurrent retries must not
                # both insert.
                replay = self._check_idempotency(idempotency_key, fingerprint, conn=conn)
                if replay is not None:
                    return replay, True

            conflicts = self.db.conflicting_bookings(conn, room_id, start_epoch, end_epoch)
            if conflicts:
                raise SlotUnavailable(
                    "That room is already booked for part of this window",
                    details={
                        "room_id": room_id,
                        "requested": {"start": start, "end": end},
                        "conflicts": [self._conflict_payload(room, row) for row in conflicts],
                    },
                )

            now = utc_now_epoch()
            booking_id = self.db.insert_booking(
                conn,
                room_id=room_id,
                user=user,
                start_at=start_epoch,
                end_at=end_epoch,
                series_id=None,
                occurrence_index=None,
                created_at=now,
            )
            if idempotency_key:
                self.db.record_idempotency(conn, idempotency_key, booking_id, now, fingerprint)
            self.db.audit(
                conn,
                "booking.created",
                booking_id=booking_id,
                payload={"room_id": room_id, "user": user, "start": start_epoch, "end": end_epoch},
            )

        payload = self._booking_payload(
            {
                "id": booking_id,
                "room_id": room_id,
                "user": user,
                "start_at": start_epoch,
                "end_at": end_epoch,
                "series_id": None,
                "occurrence_index": None,
            },
            room,
        )
        warnings = [w for w in (start_warn, end_warn) if w]
        if warnings:
            payload["warnings"] = warnings
        return payload, False

    def create_recurring(
        self,
        *,
        room_id: int,
        user: str,
        start: str,
        end: str,
        repeat_until: str,
        conflict_policy: str = "skip",
        idempotency_key: str | None = None,
    ) -> tuple[dict, bool]:
        room = self.get_room(room_id)
        user = _require_text(user, "user")
        tz_name = room["timezone"]

        if conflict_policy not in SERIES_CONFLICT_POLICIES:
            raise ValidationError(
                "conflict_policy must be 'skip' or 'fail'",
                details={"allowed": list(SERIES_CONFLICT_POLICIES)},
            )

        start_naive = parse_iso(start)
        end_naive = parse_iso(end)
        if start_naive.tzinfo is not None or end_naive.tzinfo is not None:
            # Normalise offset-bearing input to office wall clock, then recur weekly.
            start_naive = start_naive.astimezone(get_zone(tz_name)).replace(tzinfo=None)
            end_naive = end_naive.astimezone(get_zone(tz_name)).replace(tzinfo=None)

        start_naive = floor_seconds(start_naive)
        duration = floor_seconds(end_naive) - start_naive
        if duration <= timedelta(0):
            raise InvalidInterval("end must be after start", details={"start": start, "end": end})
        if duration > timedelta(hours=24):
            raise InvalidInterval("A single booking may not exceed 24 hours")

        repeat_until_date, _ = parse_until(repeat_until, tz_name)
        if repeat_until_date < start_naive.date():
            raise RepeatUntilInvalid(
                "repeat_until must be on or after the first occurrence",
                details={"repeat_until": repeat_until_date.isoformat()},
            )

        occurrences = expand_weekly(
            first_start_naive=start_naive,
            duration=duration,
            tz_name=tz_name,
            repeat_until=repeat_until_date,
            max_occurrences=MAX_SERIES_OCCURRENCES,
        )

        request = {
            "kind": "recurring",
            "room_id": room_id,
            "user": user,
            "start": naive_iso(start_naive),
            "end": naive_iso(start_naive + duration),
            "repeat_until": repeat_until_date.isoformat(),
            "conflict_policy": conflict_policy,
        }
        fingerprint = _fingerprint(request)

        if idempotency_key:
            replay = self._check_idempotency(idempotency_key, fingerprint, series=True)
            if replay is not None:
                return replay, True

        created: list[dict] = []
        skipped: list[dict] = []

        with self.db.transaction() as conn:
            if idempotency_key:
                replay = self._check_idempotency(idempotency_key, fingerprint, series=True, conn=conn)
                if replay is not None:
                    return replay, True

            now = utc_now_epoch()
            accepted: list[Occurrence] = []
            for occurrence in occurrences:
                start_epoch = int(occurrence.start_utc.timestamp())
                end_epoch = int(occurrence.end_utc.timestamp())
                conflicts = self.db.conflicting_bookings(conn, room_id, start_epoch, end_epoch)
                # Two instances of the same request can never overlap (7 days
                # apart, max 24h), but check anyway so the rule is explicit.
                in_request = next(
                    (
                        other
                        for other in accepted
                        if other.start_utc < occurrence.end_utc and occurrence.start_utc < other.end_utc
                    ),
                    None,
                )
                if in_request is not None:
                    skipped.append(
                        {
                            "occurrence_index": occurrence.index,
                            "start": naive_iso(occurrence.start_local),
                            "end": naive_iso(occurrence.end_local),
                            "reason": "OVERLAPS_SIBLING_INSTANCE",
                            "conflicts_with": {"occurrence_index": in_request.index},
                        }
                    )
                    continue
                if conflicts:
                    skipped.append(
                        {
                            "occurrence_index": occurrence.index,
                            "start": naive_iso(occurrence.start_local),
                            "end": naive_iso(occurrence.end_local),
                            "reason": "ROOM_BUSY",
                            "conflicts_with": [self._conflict_payload(room, row) for row in conflicts],
                        }
                    )
                    continue
                accepted.append(occurrence)

            if dropped := len(skipped):
                if not accepted:
                    raise SeriesFullyConflicted(
                        "Every occurrence conflicts with an existing booking, so nothing was saved",
                        details={"occurrences": len(occurrences), "skipped": skipped},
                    )
                if conflict_policy == "fail":
                    raise SeriesConflictFailFast(
                        f"{dropped} of {len(occurrences)} occurrences conflict; "
                        "nothing was saved (conflict_policy=fail)",
                        details={"occurrences": len(occurrences), "skipped": skipped},
                    )

            series_id = self.db.insert_series(
                conn,
                room_id=room_id,
                user=user,
                local_start=naive_iso(start_naive),
                local_end=naive_iso(start_naive + duration),
                duration_min=int(duration.total_seconds() // 60),
                repeat_until=repeat_until_date.isoformat(),
                tz_name=tz_name,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                created_at=now,
            )

            for occurrence in accepted:
                booking_id = self.db.insert_booking(
                    conn,
                    room_id=room_id,
                    user=user,
                    start_at=int(occurrence.start_utc.timestamp()),
                    end_at=int(occurrence.end_utc.timestamp()),
                    series_id=series_id,
                    occurrence_index=occurrence.index,
                    created_at=now,
                )
                created.append(
                    {
                        **project_booking(
                            {
                                "id": booking_id,
                                "room_id": room_id,
                                "user": user,
                                "start_at": int(occurrence.start_utc.timestamp()),
                                "end_at": int(occurrence.end_utc.timestamp()),
                                "series_id": series_id,
                                "occurrence_index": occurrence.index,
                            },
                            tz_name,
                        ),
                        "utc_offset": _offset(occurrence.start_utc, tz_name),
                    }
                )

            self.db.audit(
                conn,
                "series.created",
                series_id=series_id,
                payload={
                    "room_id": room_id,
                    "user": user,
                    "requested": len(occurrences),
                    "created": len(created),
                    "skipped": len(skipped),
                },
            )

        warnings = []
        shifted = [o.index for o in accepted if o.warning == "DST_GAP_SHIFTED"]
        ambiguous = [o.index for o in accepted if o.warning == "DST_AMBIGUOUS_FIRST_INSTANT"]
        if shifted:
            warnings.append(
                {
                    "code": "DST_GAP_SHIFTED",
                    "message": "Some occurrences fell in a DST spring-forward gap and were moved "
                               "forward to the first valid wall-clock time.",
                    "occurrence_indexes": shifted,
                }
            )
        if ambiguous:
            warnings.append(
                {
                    "code": "DST_AMBIGUOUS_FIRST_INSTANT",
                    "message": "Some occurrences fell in a DST fall-back hour; the first of the "
                               "two possible instants was used.",
                    "occurrence_indexes": ambiguous,
                }
            )

        return (
            {
                "series_id": series_id,
                "room_id": room_id,
                "room_name": room["name"],
                "office": room["office"],
                "user": user,
                "timezone": tz_name,
                "recurrence": {
                    "frequency": "weekly",
                    "interval": 1,
                    "local_start": naive_iso(start_naive),
                    "local_end": naive_iso(start_naive + duration),
                    "duration_minutes": int(duration.total_seconds() // 60),
                    "repeat_until": repeat_until_date.isoformat(),
                },
                "requested_count": len(occurrences),
                "created_count": len(created),
                "skipped_count": len(skipped),
                "created": created,
                "skipped": sorted(skipped, key=lambda item: item["occurrence_index"]),
                "warnings": warnings,
            },
            False,
        )

    # --------------------------------------------------------------- cancelling
    def cancel(
        self,
        booking_id: int,
        *,
        scope: str = "series",
        now_epoch: int | None = None,
        cancelled_by: str | None = None,
    ) -> dict:
        if scope not in ("series", "single"):
            raise ValidationError("scope must be 'series' or 'single'")

        now = utc_now_epoch() if now_epoch is None else now_epoch
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Booking {booking_id} does not exist")

            if row["cancelled_at"] is not None:
                return {
                    "booking_id": booking_id,
                    "already_cancelled": True,
                    "cancelled_count": 0,
                    "series_id": row["series_id"],
                    "message": "Booking was already cancelled; nothing changed",
                }

            if scope == "single" or row["series_id"] is None:
                changed = self.db.cancel_booking(conn, booking_id, now)
                self.db.audit(
                    conn, "booking.cancelled", booking_id=booking_id, series_id=row["series_id"],
                    payload={"scope": "single", "cancelled_count": changed, "by": cancelled_by},
                )
                kept = 0
                series_id = row["series_id"]
                if series_id is not None:
                    kept = conn.execute(
                        "SELECT COUNT(*) FROM bookings WHERE series_id = ? AND cancelled_at IS NULL",
                        (series_id,),
                    ).fetchone()[0]
                return {
                    "booking_id": booking_id,
                    "series_id": series_id,
                    "scope": "single",
                    "cancelled_count": changed,
                    "kept_instances": kept,
                    "message": "Only this occurrence was cancelled",
                }

            series_id = row["series_id"]
            # Past instances (already started) are untouched by design.
            cancelled = self.db.cancel_series_future(conn, series_id, now, now)
            kept = conn.execute(
                """
                SELECT COUNT(*) FROM bookings
                 WHERE series_id = ? AND cancelled_at IS NULL AND start_at < ?
                """,
                (series_id, now),
            ).fetchone()[0]
            self.db.audit(
                conn, "series.cancelled", booking_id=booking_id, series_id=series_id,
                payload={"scope": "series", "cancelled_count": cancelled, "kept_past": kept,
                         "by": cancelled_by},
            )

        return {
            "booking_id": booking_id,
            "series_id": series_id,
            "scope": "series",
            "cancelled_count": cancelled,
            "kept_instances": kept,
            "message": (
                "Series cancelled from now on; past occurrences were kept"
                if kept else "Series cancelled from now on"
            ),
        }

    def cancel_series(self, series_id: int, *, cancelled_by: str | None = None) -> dict:
        """Cancel a whole series by id without needing one of its booking ids."""
        info = self.db.series_info(series_id)
        if info is None:
            raise NotFoundError(f"Series {series_id} does not exist")
        now = utc_now_epoch()
        with self.db.transaction() as conn:
            cancelled = self.db.cancel_series_future(conn, series_id, now, now)
            kept = conn.execute(
                """SELECT COUNT(*) FROM bookings
                    WHERE series_id = ? AND cancelled_at IS NULL AND start_at < ?""",
                (series_id, now),
            ).fetchone()[0]
            self.db.audit(
                conn, "series.cancelled", series_id=series_id,
                payload={"scope": "series", "cancelled_count": cancelled, "kept_past": kept,
                         "by": cancelled_by},
            )
        return {
            "series_id": series_id,
            "scope": "series",
            "cancelled_count": cancelled,
            "kept_instances": kept,
            "message": "Series cancelled from now on" if not kept else
                       "Series cancelled from now on; past occurrences were kept",
        }

    # ------------------------------------------------------------------ reads
    def get_booking(self, booking_id: int) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Booking {booking_id} does not exist")
        room = self.get_room(row["room_id"])
        payload = project_booking(row, room["timezone"])
        payload["room_name"] = room["name"]
        payload["office"] = room["office"]
        payload["status"] = "cancelled" if row["cancelled_at"] else "confirmed"
        return payload

    def list_bookings(
        self,
        *,
        window_start: str | None = None,
        window_end: str | None = None,
        room_id: int | None = None,
        user: str | None = None,
        limit: int = 500,
    ) -> dict:
        """List active bookings overlapping a window.

        Window bounds without an offset are interpreted as **UTC** here, because
        a listing can span offices and therefore has no single wall clock.
        """
        now = utc_now_epoch()
        start_epoch = _window_epoch(window_start, now - 7 * 86400)
        end_epoch = _window_epoch(window_end, now + 21 * 86400)
        if end_epoch <= start_epoch:
            raise ValidationError("window_end must be after window_start")

        rows = self.db.bookings_between(start_epoch, end_epoch)
        items = []
        for row in rows:
            if room_id is not None and row["room_id"] != room_id:
                continue
            if user is not None and row["user"].lower() != user.lower():
                continue
            room = self.get_room(row["room_id"])
            items.append(project_booking(row, room["timezone"]))

        items = items[:limit]
        return {
            "window": {
                "start_utc": _utc_iso(start_epoch),
                "end_utc": _utc_iso(end_epoch),
                "assumption": "naive bounds are interpreted as UTC",
            },
            "count": len(items),
            "bookings": items,
        }

    def series_detail(self, series_id: int) -> dict:
        info = self.db.series_info(series_id)
        if info is None:
            raise NotFoundError(f"Series {series_id} does not exist")
        room = self.get_room(info["room_id"])
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE series_id = ? ORDER BY start_at", (series_id,)
            ).fetchall()
        instances = []
        for row in rows:
            payload = project_booking(row, room["timezone"])
            payload["status"] = "cancelled" if row["cancelled_at"] else "confirmed"
            instances.append(payload)
        return {
            "series_id": series_id,
            "room_id": room["id"],
            "room_name": room["name"],
            "office": room["office"],
            "user": info["user"],
            "timezone": room["timezone"],
            "recurrence": {
                "frequency": "weekly",
                "interval": 1,
                "local_start": info["local_start"],
                "local_end": info["local_end"],
                "duration_minutes": info["duration_min"],
                "repeat_until": info["repeat_until"],
            },
            "created_at": _utc_iso(info["created_at"]),
            "instance_count": len(instances),
            "instances": instances,
        }

    def list_series(self) -> dict:
        summaries = []
        for row in self.db.series_summary():
            room = self.db.room(row["room_id"]) or {"name": "?", "office": "?", "timezone": "UTC"}
            summaries.append(
                {
                    "series_id": row["id"],
                    "room_id": row["room_id"],
                    "room_name": room["name"],
                    "office": room["office"],
                    "user": row["user"],
                    "timezone": row["timezone"],
                    "local_start": row["local_start"],
                    "local_end": row["local_end"],
                    "repeat_until": row["repeat_until"],
                    "created_at": _utc_iso(row["created_at"]),
                    "total_instances": row["total_instances"],
                    "cancelled_instances": row["cancelled_instances"] or 0,
                }
            )
        return {"count": len(summaries), "series": summaries}

    # ------------------------------------------------------------ availability
    def availability(
        self,
        *,
        window_start: str,
        window_end: str,
        capacity: int | None = None,
        room_id: int | None = None,
    ) -> dict:
        """R5: iterate rooms by id from 1 to N and test each one for the window."""
        start_epoch = _window_epoch(window_start, utc_now_epoch())
        end_epoch = _window_epoch(window_end, utc_now_epoch())
        if end_epoch <= start_epoch:
            raise InvalidInterval("end must be after start")

        rooms = self.iter_rooms()
        results: list[dict[str, Any]] = []
        for room in rooms:
            if room_id is not None and room["id"] != room_id:
                continue
            if capacity is not None and room["capacity"] < capacity:
                continue
            busy = self.db.busy_bookings(room["id"], start_epoch, end_epoch)
            free = not busy
            results.append(
                {
                    "room_id": room["id"],
                    "name": room["name"],
                    "capacity": room["capacity"],
                    "office": room["office"],
                    "available": free,
                    "conflicts": [
                        {
                            "id": row["id"],
                            "start": self._legacy(row, room, "start_at"),
                            "end": self._legacy(row, room, "end_at"),
                        }
                        for row in busy
                    ],
                }
            )

        return {
            "window": {
                "start": naive_iso(datetime.fromtimestamp(start_epoch, UTC)),
                "end": naive_iso(datetime.fromtimestamp(end_epoch, UTC)),
            },
            "assumption": "naive window bounds are interpreted as UTC",
            "available_rooms": [room["room_id"] for room in results if room["available"]],
            "rooms": results,
        }

    def day_view(self, *, day: str, office: str | None = None, day_start: str = "08:00",
                 day_end: str = "19:00", slot_minutes: int = 30) -> dict:
        """Free/busy grid for one local calendar day, plus bookable free slots.

        Working hours (08:00-19:00) are an assumption; see DECISIONS.md.
        """
        if slot_minutes < 5 or slot_minutes > 240:
            raise ValidationError("slot_minutes must be between 5 and 240")
        try:
            target = date.fromisoformat(day)
        except ValueError as exc:
            raise ValidationError("day must be a YYYY-MM-DD date") from exc

        def _minutes(value: str) -> int:
            try:
                hours, minutes = (int(part) for part in value.split(":"))
                return hours * 60 + minutes
            except ValueError as exc:
                raise ValidationError("day_start/day_end must look like HH:MM") from exc

        open_min, close_min = _minutes(day_start), _minutes(day_end)
        if close_min <= open_min:
            raise InvalidInterval("day_end must be after day_start")

        output = []
        for room in self.iter_rooms():
            if office and room["office"].lower() != office.lower():
                continue
            tz = get_zone(room["timezone"])
            day_start_dt = resolve_wall_clock(datetime.combine(target, time.min), tz).local
            window_start = int((day_start_dt + timedelta(minutes=open_min)).astimezone(UTC).timestamp())
            window_end = int((day_start_dt + timedelta(minutes=close_min)).astimezone(UTC).timestamp())
            busy_rows = self.db.busy_bookings(room["id"], window_start, window_end)

            busy = []
            for row in busy_rows:
                busy.append(
                    {
                        "id": row["id"],
                        "series_id": row["series_id"],
                        "user": row["user"],
                        "start": naive_iso(datetime.fromtimestamp(row["start_at"], UTC).astimezone(tz)),
                        "end": naive_iso(datetime.fromtimestamp(row["end_at"], UTC).astimezone(tz)),
                        "start_epoch": row["start_at"],
                        "end_epoch": row["end_at"],
                    }
                )

            free_slots = []
            cursor = window_start
            while cursor + slot_minutes * 60 <= window_end:
                slot_end = cursor + slot_minutes * 60
                if not any(row["start_at"] < slot_end and row["end_at"] > cursor for row in busy_rows):
                    free_slots.append(
                        {
                            "start": naive_iso(datetime.fromtimestamp(cursor, UTC).astimezone(tz)),
                            "end": naive_iso(datetime.fromtimestamp(slot_end, UTC).astimezone(tz)),
                        }
                    )
                cursor = slot_end

            output.append(
                {
                    "room_id": room["id"],
                    "name": room["name"],
                    "capacity": room["capacity"],
                    "office": room["office"],
                    "timezone": room["timezone"],
                    "date": target.isoformat(),
                    "utc_offset": _offset(datetime.fromtimestamp(window_start, UTC), room["timezone"]),
                    "working_hours": {"start": day_start, "end": day_end, "assumed": True},
                    "busy": busy,
                    "free_slots": free_slots,
                    "utilised_minutes": sum(
                        max(0, min(row["end_at"], window_end) - max(row["start_at"], window_start)) // 60
                        for row in busy_rows
                    ),
                    "available_minutes": len(free_slots) * slot_minutes,
                }
            )

        return {
            "date": target.isoformat(),
            "office": office or "all",
            "slot_minutes": slot_minutes,
            "rooms": output,
        }

    # ------------------------------------------------------------- internals
    def _check_idempotency(
        self, key: str, fingerprint: str, *, series: bool = False, conn=None
    ) -> dict | None:
        if series:
            row = None
            if conn is not None:
                row = conn.execute(
                    "SELECT * FROM booking_series WHERE idempotency_key = ?", (key,)
                ).fetchone()
            else:
                with self.db.connect() as connection:
                    row = connection.execute(
                        "SELECT * FROM booking_series WHERE idempotency_key = ?", (key,)
                    ).fetchone()
            if row is None:
                return None
            if row["fingerprint"] and row["fingerprint"] != fingerprint:
                raise IdempotencyKeyReused(
                    "This Idempotency-Key was already used with a different request body"
                )
            detail = self.series_detail(row["id"])
            detail["idempotent_replay"] = True
            return detail

        record = (
            conn.execute("SELECT * FROM idempotency_keys WHERE key = ?", (key,)).fetchone()
            if conn is not None
            else self.db.lookup_idempotency(key)
        )
        if record is None:
            return None
        if record["fingerprint"] and record["fingerprint"] != fingerprint:
            raise IdempotencyKeyReused(
                "This Idempotency-Key was already used with a different request body"
            )
        booking = self.get_booking(record["booking_id"])
        booking["idempotent_replay"] = True
        return booking

    @staticmethod
    def _conflict_payload(room: dict, row) -> dict:
        start = datetime.fromtimestamp(row["start_at"], UTC)
        end = datetime.fromtimestamp(row["end_at"], UTC)
        zone = get_zone(room["timezone"])
        return {
            "id": row["id"],
            "user": row["user"],
            "start": naive_iso(start.astimezone(zone)),
            "end": naive_iso(end.astimezone(zone)),
        }

    @staticmethod
    def _legacy(row, room: dict, field: str) -> str:
        zone = get_zone(room["timezone"])
        return naive_iso(datetime.fromtimestamp(row[field], UTC).astimezone(zone))

    def _booking_payload(self, row: dict, room: dict) -> dict:
        payload = project_booking(row, room["timezone"])
        payload["room_name"] = room["name"]
        payload["office"] = room["office"]
        payload["status"] = "confirmed"
        return payload

    def audit_feed(self, limit: int = 20) -> list[dict]:
        feed = []
        for entry in self.db.recent_audit(limit):
            item = {
                "id": entry["id"],
                "action": entry["action"],
                "at": _utc_iso(entry["at"]),
                "booking_id": entry["booking_id"],
                "series_id": entry["series_id"],
            }
            if entry["payload"]:
                try:
                    item["payload"] = json.loads(entry["payload"])
                except json.JSONDecodeError:  # pragma: no cover
                    item["payload"] = {}
            feed.append(item)
        return feed


def _offset(dt: datetime, tz_name: str) -> str:
    local = dt.astimezone(get_zone(tz_name))
    total = int(local.utcoffset().total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def _utc_iso(epoch_value: int) -> str:
    return naive_iso(datetime.fromtimestamp(epoch_value, UTC)) + "Z"


def _window_epoch(value: str | None, default: int) -> int:
    if value is None or not str(value).strip():
        return default
    parsed = parse_iso(value)
    if parsed.tzinfo is None:
        return int(parsed.replace(tzinfo=UTC).timestamp())
    return int(parsed.astimezone(UTC).timestamp())
