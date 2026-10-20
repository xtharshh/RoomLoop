"""HTTP surface for RoomLoop.

Design notes that matter for reviewers:

* ``GET /rooms`` returns *exactly* the legacy payload shape
  ``[{"id", "name", "capacity"}]`` by default, because the facilities dashboard
  renders whatever comes back (constraint C2). Richer data is opt-in via
  ``?detail=true`` or a separate endpoint, so a dashboard build cannot break.
* Errors always use one envelope: ``{"error": {"code", "message", "details"}}``.
  Including Pydantic's own validation failures, which are normalised.
* ``Idempotency-Key`` is honoured on both create endpoints because a booking
  request is exactly the kind of thing a mobile client retries.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import Database
from .domain import (
    MAX_SERIES_OCCURRENCES,
    TZ_DATA_HINT,
    RoomLoopError,
    SeriesConflictFailFast,
    ValidationError,
    timezone_database_available,
)
from .service import BookingService

STATIC_DIR = Path(__file__).parent / "static"

DESCRIPTION = """
RoomLoop is the booking core for our internal meeting-room service.

* **Wall clock in, wall clock out.** Naive timestamps mean "local time at the
  room's office" (Berlin or Denver). Offset-bearing timestamps are respected as
  exact instants.
* **Half-open intervals.** 10:00-11:00 and 11:00-12:00 do not conflict.
* **Weekly series** repeat at the same *wall-clock* time, so 09:00 Berlin stays
  09:00 across DST. Occurrences that fall in a spring-forward gap are shifted
  forward and reported as a warning.
* **Recurring creation is all-or-nothing and forgiving**: clashing occurrences
  are skipped (and reported) while the rest are saved. Use
  `conflict_policy: "fail"` for strict all-or-nothing.
"""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class BookingCreate(BaseModel):
    room_id: int = Field(..., ge=1, examples=[1])
    user: str = Field(..., min_length=1, max_length=120, examples=["mira@example.com"])
    start: str = Field(..., examples=["2026-11-02T09:00:00"],
                       description="Naive = wall clock at the room's office. Offset accepted.")
    end: str = Field(..., examples=["2026-11-02T10:00:00"])

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"room_id": 1, "user": "mira@example.com",
                 "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00"}
            ]
        }
    }


class RecurringCreate(BaseModel):
    room_id: int = Field(..., ge=1, examples=[1])
    user: str = Field(..., min_length=1, max_length=120, examples=["mira@example.com"])
    start: str = Field(..., examples=["2026-11-02T09:00:00"],
                       description="First occurrence, wall clock at the room's office.")
    end: str = Field(..., examples=["2026-11-02T10:00:00"])
    repeat_until: str = Field(..., examples=["2027-01-25"],
                              description="Last local date an occurrence may fall on (inclusive).")
    conflict_policy: Literal["skip", "fail"] = Field(
        "skip",
        description="skip: save the non-conflicting occurrences (R2). "
                    "fail: strict all-or-nothing, roll back on any clash (R1).",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"room_id": 1, "user": "platform@example.com",
                 "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00",
                 "repeat_until": "2027-01-25", "conflict_policy": "skip"}
            ]
        }
    }


class CancelRequest(BaseModel):
    scope: Literal["series", "single"] = Field(
        "series",
        description="series: cancel this and all future occurrences (default). "
                    "single: cancel only this occurrence, leaving the series running.",
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(db_path: str | None = None) -> FastAPI:
    resolved_path = db_path or os.environ.get("ROOMLOOP_DB", "roomloop.db")
    db = Database(resolved_path)
    service = BookingService(db)

    app = FastAPI(
        title="RoomLoop Booking Service",
        version="1.0.0",
        description=DESCRIPTION,
        contact={"name": "RoomLoop engineering"},
        openapi_tags=[
            {"name": "rooms", "description": "Room directory and availability."},
            {"name": "bookings", "description": "Create, inspect and cancel bookings."},
            {"name": "series", "description": "Recurring booking series."},
            {"name": "ops", "description": "Health, policy metadata and audit trail."},
        ],
    )
    app.state.db = db
    app.state.service = service

    if not timezone_database_available():
        # Fail loudly at boot rather than with a confusing 422 on the first
        # availability request. Windows and slim containers hit this.
        logging.getLogger("roomloop").warning(
            "NO IANA TIMEZONE DATABASE ON THIS HOST — %s", TZ_DATA_HINT
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # -- error handling ----------------------------------------------------
    @app.exception_handler(RoomLoopError)
    async def _domain_error(_: Request, exc: RoomLoopError) -> JSONResponse:
        headers = {}
        if isinstance(exc, SeriesConflictFailFast):
            # Tell a retrying client this failure is deterministic.
            headers["X-RoomLoop-Retryable"] = "false"
        return JSONResponse(status_code=exc.status, content={"error": exc.to_dict()}, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for err in exc.errors():
            problems.append(
                {
                    "field": ".".join(str(part) for part in err.get("loc", []) if part != "body"),
                    "problem": err.get("msg"),
                    "type": err.get("type"),
                }
            )
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": "The request body did not match the schema",
                    "details": {"problems": problems},
                }
            },
        )

    # -- pages -------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/console")

    @app.get("/console", include_in_schema=False)
    async def console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # -- ops ---------------------------------------------------------------
    @app.get("/health", tags=["ops"], summary="Liveness probe")
    async def health() -> dict[str, Any]:
        tz_ok = timezone_database_available()
        return {
            "status": "ok" if tz_ok else "degraded",
            "service": "roomloop",
            "rooms": db.room_count(),
            "database": "memory" if resolved_path == ":memory:" else "file",
            "timezone_data": "ok" if tz_ok else "missing",
            **({} if tz_ok else {"hint": TZ_DATA_HINT}),
        }

    @app.get("/meta", tags=["ops"], summary="Policy constants the clients need")
    async def meta() -> dict[str, Any]:
        return {
            "max_series_occurrences": MAX_SERIES_OCCURRENCES,
            "max_booking_hours": 24,
            "assumed_working_hours": {"start": "08:00", "end": "19:00"},
            "conflict_policies": ["skip", "fail"],
            "timestamp_contract": {
                "legacy_fields": ["start", "end"],
                "format": "naive local ISO-8601, second precision, no offset",
                "meaning": "wall clock at the room's office",
                "additive_fields": ["start_utc", "end_utc", "utc_offset", "timezone"],
            },
            "offices": ["Berlin", "Denver"],
            "environment": {
                "timezone_data": "ok" if timezone_database_available() else "missing",
            },
        }

    @app.get("/audit", tags=["ops"], summary="Recent activity trail")
    async def audit(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        return {"entries": service.audit_feed(limit)}

    # -- rooms -------------------------------------------------------------
    @app.get("/rooms", tags=["rooms"], summary="Room directory (legacy shape)",
             response_description="Array of rooms; only id, name and capacity by default")
    async def list_rooms(
        detail: bool = Query(False, description="Include office, timezone and capacity metadata"),
        office: str | None = Query(None, description="Filter by office name"),
    ) -> list[dict[str, Any]]:
        rooms = service.iter_rooms()
        if office:
            rooms = [room for room in rooms if room["office"].lower() == office.lower()]
        if not detail:
            # Constraint C2: the dashboard renders whatever comes back. Do not
            # add fields to the default payload.
            return [{"id": r["id"], "name": r["name"], "capacity": r["capacity"]} for r in rooms]
        return [
            {
                "id": room["id"],
                "name": room["name"],
                "capacity": room["capacity"],
                "office": room["office"],
                "timezone": room["timezone"],
            }
            for room in rooms
        ]

    @app.get("/rooms/{room_id}", tags=["rooms"], summary="One room")
    async def get_room(room_id: int) -> dict[str, Any]:
        return service.get_room(room_id)

    @app.get("/availability", tags=["rooms"],
             summary="Which rooms are free for a window (iterates rooms 1..N)")
    async def availability(
        start: str = Query(..., examples=["2026-11-02T09:00:00Z"]),
        end: str = Query(..., examples=["2026-11-02T10:00:00Z"]),
        capacity: int | None = Query(None, ge=1, description="Minimum capacity"),
        room_id: int | None = Query(None, ge=1, description="Check a single room"),
    ) -> dict[str, Any]:
        return service.availability(
            window_start=start, window_end=end, capacity=capacity, room_id=room_id
        )

    @app.get("/availability/day", tags=["rooms"],
             summary="Free/busy grid for one local day, per room")
    async def availability_day(
        day: date = Query(..., description="Local calendar date, e.g. 2026-11-02"),
        office: str | None = Query(None),
        day_start: str = Query("08:00", description="Assumed working-hours start"),
        day_end: str = Query("19:00", description="Assumed working-hours end"),
        slot_minutes: int = Query(30, ge=5, le=240),
    ) -> dict[str, Any]:
        return service.day_view(
            day=day.isoformat(), office=office, day_start=day_start,
            day_end=day_end, slot_minutes=slot_minutes,
        )

    # -- bookings ----------------------------------------------------------
    @app.post("/bookings", tags=["bookings"], status_code=201,
              summary="Create one booking",
              responses={409: {"description": "The room is busy for that window"}})
    async def create_booking(
        payload: BookingCreate,
        response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        booking, replay = service.create_booking(
            room_id=payload.room_id,
            user=payload.user,
            start=payload.start,
            end=payload.end,
            idempotency_key=idempotency_key,
        )
        response.status_code = 200 if replay else 201
        return booking

    @app.post("/bookings/recurring", tags=["bookings"], status_code=201,
              summary="Create a weekly recurring booking",
              responses={
                  409: {"description": "All occurrences clashed, or strict policy hit a clash"},
                  422: {"description": "Invalid recurrence"},
              })
    async def create_recurring(
        payload: RecurringCreate,
        response: Response,
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        result, replay = service.create_recurring(
            room_id=payload.room_id,
            user=payload.user,
            start=payload.start,
            end=payload.end,
            repeat_until=payload.repeat_until,
            conflict_policy=payload.conflict_policy,
            idempotency_key=idempotency_key,
        )
        response.status_code = 200 if replay else 201
        return result

    @app.get("/bookings", tags=["bookings"], summary="List active bookings in a window")
    async def list_bookings(
        start: str | None = Query(None, description="Window start; naive means UTC"),
        end: str | None = Query(None, description="Window end; naive means UTC"),
        room_id: int | None = Query(None, ge=1),
        user: str | None = Query(None),
        limit: int = Query(500, ge=1, le=2000),
    ) -> dict[str, Any]:
        return service.list_bookings(
            window_start=start, window_end=end, room_id=room_id, user=user, limit=limit
        )

    @app.get("/bookings/{booking_id}", tags=["bookings"], summary="One booking")
    async def get_booking(booking_id: int) -> dict[str, Any]:
        return service.get_booking(booking_id)

    @app.delete("/bookings/{booking_id}", tags=["bookings"],
                summary="Cancel a booking (series-aware by default)")
    async def cancel_booking(
        booking_id: int,
        scope: Literal["series", "single"] = Query("series"),
        cancelled_by: str | None = Query(None, description="Who triggered the cancellation"),
    ) -> dict[str, Any]:
        return service.cancel(booking_id, scope=scope, cancelled_by=cancelled_by)

    @app.post("/bookings/{booking_id}/cancel", tags=["bookings"],
              summary="Cancel a booking (POST alias for clients that cannot DELETE)")
    async def cancel_booking_post(
        booking_id: int,
        payload: CancelRequest | None = Body(None),
        scope: Literal["series", "single"] | None = Query(None),
        cancelled_by: str | None = Query(None),
    ) -> dict[str, Any]:
        resolved = scope or (payload.scope if payload else "series")
        return service.cancel(booking_id, scope=resolved, cancelled_by=cancelled_by)

    # -- series ------------------------------------------------------------
    @app.get("/series", tags=["series"], summary="Recurring series overview")
    async def list_series() -> dict[str, Any]:
        return service.list_series()

    @app.get("/series/{series_id}", tags=["series"], summary="Every instance of a series")
    async def get_series(series_id: int) -> dict[str, Any]:
        return service.series_detail(series_id)

    @app.delete("/series/{series_id}", tags=["series"],
                summary="Cancel the remaining future instances of a series")
    async def cancel_series(series_id: int) -> dict[str, Any]:
        return service.cancel_series(series_id)

    # -- demo --------------------------------------------------------------
    @app.post("/admin/reset-demo", tags=["ops"], include_in_schema=True,
              summary="Wipe bookings and re-seed demo data (demo builds only)")
    async def reset_demo() -> dict[str, Any]:
        if os.environ.get("ROOMLOOP_DEMO", "1") != "1":
            raise ValidationError("Demo endpoints are disabled")
        from .demo import reset_to_demo_state

        return reset_to_demo_state(db)

    return app


app = create_app()
