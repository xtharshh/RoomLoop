"""Concurrency: R1 must hold under parallel requests, not just sequentially.

SQLite serialises writers with ``BEGIN IMMEDIATE``; these tests prove the
service actually relies on that, by racing many requests at the same slot.
"""

from __future__ import annotations

import threading

from roomloop.db import Database
from roomloop.service import BookingService


def _race(workers: int, fn) -> list:
    barrier = threading.Barrier(workers)
    results: list = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        try:
            outcome = ("ok", fn(index))
        except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
            outcome = ("error", exc)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_only_one_of_eight_concurrent_bookings_wins(tmp_path) -> None:
    db = Database(str(tmp_path / "race.db"))
    service = BookingService(db)

    def attempt(index: int):
        return service.create_booking(
            room_id=1,
            user=f"user{index}@example.com",
            start="2026-11-02T09:00:00",
            end="2026-11-02T10:00:00",
        )

    results = _race(8, attempt)
    successes = [r for r in results if r[0] == "ok"]
    failures = [r for r in results if r[0] == "error"]

    assert len(successes) == 1, f"expected exactly one winner, got {len(successes)}"
    assert len(failures) == 7
    assert all(getattr(exc, "code", None) == "SLOT_UNAVAILABLE" for _, exc in failures)

    listed = db.bookings_between(0, 2**31 - 1)
    assert len(listed) == 1


def test_concurrent_series_never_interleave(tmp_path) -> None:
    """Two racing series on the same room/time must not both half-apply."""
    db = Database(str(tmp_path / "race-series.db"))
    service = BookingService(db)

    def attempt(index: int):
        return service.create_recurring(
            room_id=1,
            user=f"series{index}@example.com",
            start="2026-11-02T14:00:00",
            end="2026-11-02T15:00:00",
            repeat_until="2026-11-30",
            conflict_policy="skip",
        )

    results = _race(4, attempt)
    # create_recurring returns (response, was_replay)
    successes = [payload[0] for kind, payload in results if kind == "ok"]
    assert len(successes) == 1

    rows = db.bookings_between(0, 2**31 - 1)
    assert len(rows) == 5
    assert {row["user"] for row in rows} == {successes[0]["user"]}
