"""SQLite persistence layer for RoomLoop.

Timestamps are stored as **UTC epoch seconds (integers)**. Rationale: interval
overlap is a pure integer comparison, DST never corrupts stored data, and the
legacy naive-local wire format (constraint C1) is a rendering concern handled at
the edge. Each room carries its office timezone so the rendering is unambiguous.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from .domain import get_zone, naive_iso, offset_string

ROOMS_SEED = [
    # id, name, capacity, office, timezone
    (1, "Aurora", 8, "Berlin", "Europe/Berlin"),
    (2, "Basalt", 4, "Berlin", "Europe/Berlin"),
    (3, "Cinder", 12, "Berlin", "Europe/Berlin"),
    (4, "Dune", 6, "Denver", "America/Denver"),
    (5, "Ember", 10, "Denver", "America/Denver"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id        INTEGER PRIMARY KEY,
    name      TEXT    NOT NULL UNIQUE,
    capacity  INTEGER NOT NULL CHECK (capacity > 0),
    office    TEXT    NOT NULL,
    timezone  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS booking_series (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         INTEGER NOT NULL REFERENCES rooms(id),
    user            TEXT    NOT NULL,
    local_start     TEXT    NOT NULL,
    local_end       TEXT    NOT NULL,
    duration_min    INTEGER NOT NULL,
    repeat_until    TEXT    NOT NULL,
    timezone        TEXT    NOT NULL,
    created_at      INTEGER NOT NULL,
    idempotency_key TEXT    UNIQUE,
    fingerprint     TEXT
);

CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id         INTEGER NOT NULL REFERENCES rooms(id),
    user            TEXT    NOT NULL,
    start_at        INTEGER NOT NULL,
    end_at          INTEGER NOT NULL,
    series_id       INTEGER REFERENCES booking_series(id),
    occurrence_index INTEGER,
    created_at      INTEGER NOT NULL,
    cancelled_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_bookings_room_window
    ON bookings (room_id, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_bookings_series ON bookings (series_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    booking_id  INTEGER NOT NULL REFERENCES bookings(id),
    created_at  INTEGER NOT NULL,
    fingerprint TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    booking_id INTEGER,
    series_id  INTEGER,
    payload    TEXT
);
"""


def utc_now_epoch() -> int:
    return int(time.time())


class Database:
    """Thin wrapper around SQLite with explicit, short transactions."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            os.makedirs(directory, exist_ok=True)
        self._memory_uri = path == ":memory:"
        # For in-memory databases a single shared connection is required, since
        # every new connection would otherwise get a fresh empty database. It is
        # shared across FastAPI's worker threads, so access is serialised.
        self._lock = threading.RLock()
        self._shared: sqlite3.Connection | None = (
            sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None) if self._memory_uri else None
        )
        if self._shared is not None:
            self._configure(self._shared)
        self.migrate()

    # -- connection handling -------------------------------------------------
    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._shared is not None:
            with self._lock:
                yield self._shared
            return
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            self._configure(conn)
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = {row["id"] for row in conn.execute("SELECT id FROM rooms")}
            if not existing:
                conn.executemany(
                    "INSERT INTO rooms (id, name, capacity, office, timezone) VALUES (?, ?, ?, ?, ?)",
                    ROOMS_SEED,
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (``BEGIN IMMEDIATE``).

        This is what makes requirement R1 honest: conflict detection and all the
        inserts happen inside one transaction that holds the write lock, so two
        concurrent requests cannot both "win" the same slot.
        """
        with self.connect() as conn:
            if self._shared is not None:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- read helpers --------------------------------------------------------
    def rooms(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, name, capacity, office, timezone FROM rooms ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def room(self, room_id: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, name, capacity, office, timezone FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()
        return dict(row) if row else None

    def room_count(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0])

    def conflicting_bookings(
        self, conn: sqlite3.Connection, room_id: int, start_at: int, end_at: int
    ) -> list[sqlite3.Row]:
        # Half-open overlap: start_at < existing.end AND existing.start < end_at.
        return conn.execute(
            """
            SELECT id, start_at, end_at, user
              FROM bookings
             WHERE room_id = ?
               AND cancelled_at IS NULL
               AND start_at < ?
               AND end_at > ?
             ORDER BY start_at
            """,
            (room_id, end_at, start_at),
        ).fetchall()

    def busy_bookings(
        self, room_id: int, window_start: int, window_end: int
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, series_id, start_at, end_at, user
                  FROM bookings
                 WHERE room_id = ?
                   AND cancelled_at IS NULL
                   AND start_at < ?
                   AND end_at > ?
                 ORDER BY start_at
                """,
                (room_id, window_end, window_start),
            ).fetchall()

    def bookings_between(self, window_start: int, window_end: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT b.*, s.timezone AS series_timezone
                  FROM bookings b
                  LEFT JOIN booking_series s ON s.id = b.series_id
                 WHERE b.cancelled_at IS NULL
                   AND b.start_at < ?
                   AND b.end_at > ?
                 ORDER BY b.start_at
                """,
                (window_end, window_start),
            ).fetchall()

    # -- write helpers -------------------------------------------------------
    def insert_series(
        self,
        conn: sqlite3.Connection,
        *,
        room_id: int,
        user: str,
        local_start: str,
        local_end: str,
        duration_min: int,
        repeat_until: str,
        tz_name: str,
        idempotency_key: str | None,
        fingerprint: str | None,
        created_at: int,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO booking_series
                (room_id, user, local_start, local_end, duration_min, repeat_until,
                 timezone, created_at, idempotency_key, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                room_id, user, local_start, local_end, duration_min, repeat_until,
                tz_name, created_at, idempotency_key, fingerprint,
            ),
        )
        return int(cur.lastrowid)

    def insert_booking(
        self,
        conn: sqlite3.Connection,
        *,
        room_id: int,
        user: str,
        start_at: int,
        end_at: int,
        series_id: int | None,
        occurrence_index: int | None,
        created_at: int,
    ) -> int:
        cur = conn.execute(
            """
            INSERT INTO bookings
                (room_id, user, start_at, end_at, series_id, occurrence_index, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (room_id, user, start_at, end_at, series_id, occurrence_index, created_at),
        )
        return int(cur.lastrowid)

    def cancel_booking(self, conn: sqlite3.Connection, booking_id: int, at: int) -> int:
        cur = conn.execute(
            "UPDATE bookings SET cancelled_at = ? WHERE id = ? AND cancelled_at IS NULL",
            (at, booking_id),
        )
        return cur.rowcount

    def cancel_series_future(self, conn: sqlite3.Connection, series_id: int, from_epoch: int, at: int) -> int:
        cur = conn.execute(
            """
            UPDATE bookings
               SET cancelled_at = ?
             WHERE series_id = ?
               AND cancelled_at IS NULL
               AND start_at >= ?
            """,
            (at, series_id, from_epoch),
        )
        return cur.rowcount

    def series_end_epoch(self, series_id: int) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(end_at) AS last_end FROM bookings WHERE series_id = ? AND cancelled_at IS NULL",
                (series_id,),
            ).fetchone()
        return int(row["last_end"]) if row and row["last_end"] is not None else None

    def series_info(self, series_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM booking_series WHERE id = ?", (series_id,)).fetchone()

    def lookup_idempotency(self, key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM idempotency_keys WHERE key = ?", (key,)
            ).fetchone()

    def record_idempotency(
        self, conn: sqlite3.Connection, key: str, booking_id: int, at: int, fingerprint: str
    ) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys (key, booking_id, created_at, fingerprint) VALUES (?, ?, ?, ?)",
            (key, booking_id, at, fingerprint),
        )

    def audit(self, conn: sqlite3.Connection, action: str, *, booking_id: int | None = None,
              series_id: int | None = None, payload: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log (at, action, booking_id, series_id, payload) VALUES (?, ?, ?, ?, ?)",
            (utc_now_epoch(), action, booking_id, series_id, json.dumps(payload or {})),
        )

    def recent_audit(self, limit: int = 25) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def series_summary(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       COUNT(b.id) AS total_instances,
                       SUM(CASE WHEN b.cancelled_at IS NULL THEN 0 ELSE 1 END) AS cancelled_instances
                  FROM booking_series s
                  LEFT JOIN bookings b ON b.series_id = s.id
                 GROUP BY s.id
                 ORDER BY s.id DESC
                 LIMIT 50
                """
            ).fetchall()
        return [dict(row) for row in rows]


def _utc_iso(value: int) -> str:
    return naive_iso(datetime.fromtimestamp(value, UTC)) + "Z"


def project_booking(row: sqlite3.Row | dict, tz_name: str, *, include_extra: bool = True) -> dict:
    """Render a booking row for the API.

    ``start``/``end`` keep the legacy contract exactly: naive, second-precision
    ISO-8601 with no offset (constraint C1). Their value is the **wall clock at
    the room's office**, which is what the prototype produced. Additive,
    non-breaking keys expose the unambiguous UTC instant and the offset.
    """
    start_at = int(row["start_at"])
    end_at = int(row["end_at"])
    zone = get_zone(tz_name)
    local_start = datetime.fromtimestamp(start_at, UTC).astimezone(zone)
    local_end = datetime.fromtimestamp(end_at, UTC).astimezone(zone)

    payload = {
        "id": row["id"],
        "room_id": row["room_id"],
        "user": row["user"],
        "start": naive_iso(local_start),
        "end": naive_iso(local_end),
    }
    if include_extra:
        payload["timezone"] = tz_name
        payload["start_utc"] = _utc_iso(start_at)
        payload["end_utc"] = _utc_iso(end_at)
        payload["utc_offset"] = offset_string(start_at, tz_name)
    if row["series_id"] is not None:
        payload["series_id"] = row["series_id"]
        payload["occurrence_index"] = row["occurrence_index"]
    return payload
