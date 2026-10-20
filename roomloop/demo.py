"""Deterministic demo data, used by ``python -m roomloop.seed`` and the console.

The data is intentionally aimed at the behaviours most likely to be wrong:

* a weekly Berlin series that crosses the DST fall-back (offset changes,
  wall-clock time does not),
* a Denver series that crosses *its* (different) DST date, which is what
  produced the "Denver bookings were an hour off" complaint,
* a series where exactly one occurrence clashes, to show R2 skipping,
* past instances that must survive a series cancellation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from .db import Database
from .service import BookingService

UTC = UTC


def _monday(today: date) -> date:
    return today - timedelta(days=today.weekday())


def _iso(day: date, hour: int, minute: int = 0) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"


def demo_plan(today: date | None = None) -> dict:
    """Return the demo dataset as plain data so it can be asserted in tests."""
    today = today or datetime.now(UTC).date()
    monday = _monday(today)
    next_monday = monday + timedelta(days=7)

    singles = [
        # This week's ordinary bookings, across both offices.
        {"room_id": 1, "user": "mira@example.com",
         "start": _iso(monday, 10), "end": _iso(monday, 11)},
        {"room_id": 1, "user": "jonas@example.com",
         "start": _iso(monday + timedelta(days=1), 11), "end": _iso(monday + timedelta(days=1), 12)},
        {"room_id": 3, "user": "priya@example.com",
         "start": _iso(monday + timedelta(days=1), 14), "end": _iso(monday + timedelta(days=1), 15, 30)},
        {"room_id": 4, "user": "sam@example.com",
         "start": _iso(monday + timedelta(days=2), 9), "end": _iso(monday + timedelta(days=2), 10)},
        {"room_id": 5, "user": "lena@example.com",
         "start": _iso(monday + timedelta(days=3), 13), "end": _iso(monday + timedelta(days=3), 14)},
        {"room_id": 2, "user": "ops@example.com",
         "start": _iso(monday + timedelta(days=4), 16), "end": _iso(monday + timedelta(days=4), 17)},
        # Back-to-back pair: must NOT conflict (R4).
        {"room_id": 2, "user": "design@example.com",
         "start": _iso(monday + timedelta(days=2), 10), "end": _iso(monday + timedelta(days=2), 11)},
        {"room_id": 2, "user": "research@example.com",
         "start": _iso(monday + timedelta(days=2), 11), "end": _iso(monday + timedelta(days=2), 12)},
        # A fuller week, so the calendar is useful on any day you open it.
        {"room_id": 1, "user": "nadia@example.com",
         "start": _iso(monday + timedelta(days=1), 14), "end": _iso(monday + timedelta(days=1), 15)},
        {"room_id": 1, "user": "ops@example.com",
         "start": _iso(monday + timedelta(days=3), 10), "end": _iso(monday + timedelta(days=3), 11, 30)},
        {"room_id": 3, "user": "finance@example.com",
         "start": _iso(monday + timedelta(days=2), 9), "end": _iso(monday + timedelta(days=2), 10, 30)},
        {"room_id": 3, "user": "people@example.com",
         "start": _iso(monday + timedelta(days=4), 11), "end": _iso(monday + timedelta(days=4), 12)},
        {"room_id": 3, "user": "mira@example.com",
         "start": _iso(monday + timedelta(days=1), 16), "end": _iso(monday + timedelta(days=1), 17)},
        {"room_id": 2, "user": "hiring@example.com",
         "start": _iso(monday + timedelta(days=3), 9), "end": _iso(monday + timedelta(days=3), 9, 45)},
        {"room_id": 4, "user": "lena@example.com",
         "start": _iso(monday, 13), "end": _iso(monday, 14)},
        {"room_id": 4, "user": "sam@example.com",
         "start": _iso(monday + timedelta(days=1), 16), "end": _iso(monday + timedelta(days=1), 17)},
        {"room_id": 5, "user": "growth@example.com",
         "start": _iso(monday + timedelta(days=1), 10), "end": _iso(monday + timedelta(days=1), 11)},
        {"room_id": 5, "user": "support@example.com",
         "start": _iso(monday + timedelta(days=2), 14), "end": _iso(monday + timedelta(days=2), 15)},
        {"room_id": 5, "user": "sam@example.com",
         "start": _iso(monday + timedelta(days=3), 15), "end": _iso(monday + timedelta(days=3), 16, 30)},
        {"room_id": 5, "user": "ops@example.com",
         "start": _iso(monday + timedelta(days=4), 9, 30), "end": _iso(monday + timedelta(days=4), 10)},
    ]

    series = [
        {
            "label": "Weekly platform sync (Berlin, crosses fall-back)",
            "room_id": 1,
            "user": "platform@example.com",
            "start": _iso(monday - timedelta(days=21), 9),
            "end": _iso(monday - timedelta(days=21), 10),
            "repeat_until": (monday + timedelta(days=56)).isoformat(),
            "conflict_policy": "skip",
        },
        {
            "label": "Weekly design review (Denver, own DST date)",
            "room_id": 4,
            "user": "design@example.com",
            "start": _iso(monday - timedelta(days=14) + timedelta(days=2), 15),
            "end": _iso(monday - timedelta(days=14) + timedelta(days=2), 16, 30),
            "repeat_until": (monday + timedelta(days=63)).isoformat(),
            "conflict_policy": "skip",
        },
    ]

    # Room 2's series clashes on exactly one date because of this one-off booking.
    blocker_day = next_monday + timedelta(days=14)
    singles.append(
        {"room_id": 2, "user": "board@example.com",
         "start": _iso(blocker_day, 9), "end": _iso(blocker_day, 10)}
    )
    series.append(
        {
            "label": "Weekly 1:1 slot (room 2, one occurrence blocked)",
            "room_id": 2,
            "user": "mira@example.com",
            "start": _iso(next_monday, 9),
            "end": _iso(next_monday, 10),
            "repeat_until": (next_monday + timedelta(days=42)).isoformat(),
            "conflict_policy": "skip",
        }
    )

    # Fixed DST showcase: these dates do not move, so the offset change is stable.
    series.append(
        {
            "label": "DST proof: Berlin Tuesdays across 2026-10-25",
            "room_id": 3,
            "user": "jonas@example.com",
            "start": _iso(date(2026, 10, 6), 9),
            "end": _iso(date(2026, 10, 6), 10),
            "repeat_until": "2026-12-08",
            "conflict_policy": "skip",
        }
    )
    series.append(
        {
            "label": "DST proof: Denver Thursdays across 2026-11-01",
            "room_id": 5,
            "user": "sam@example.com",
            "start": _iso(date(2026, 10, 8), 9),
            "end": _iso(date(2026, 10, 8), 10),
            "repeat_until": "2026-12-10",
            "conflict_policy": "skip",
        }
    )

    return {"singles": singles, "series": series}


def reset_to_demo_state(db: Database, today: date | None = None) -> dict:
    """Wipe booking data and re-seed. Rooms are never modified."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM booking_series")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM audit_log")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('bookings', 'booking_series', 'audit_log')")

    service = BookingService(db)
    created_singles = []
    skipped_singles = []
    for item in demo_plan(today)["singles"]:
        try:
            booking, _ = service.create_booking(**item)
            created_singles.append(booking["id"])
        except Exception as exc:  # pragma: no cover - demo robustness
            skipped_singles.append({"item": item, "error": str(exc)})

    created_series = []
    for item in demo_plan(today)["series"]:
        payload = dict(item)
        payload.pop("label", None)
        try:
            result, _ = service.create_recurring(**payload)
            created_series.append(
                {
                    "series_id": result["series_id"],
                    "created": result["created_count"],
                    "skipped": result["skipped_count"],
                }
            )
        except Exception as exc:  # pragma: no cover - demo robustness
            created_series.append({"error": str(exc), "payload": payload})

    return {
        "rooms": db.room_count(),
        "bookings": len(created_singles),
        "series": created_series,
        "skipped_singles": skipped_singles,
    }
