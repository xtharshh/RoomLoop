"""Demo-seed integrity plus the console shell."""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

from roomloop.api import create_app
from roomloop.db import Database
from roomloop.demo import demo_plan, reset_to_demo_state
from roomloop.domain import overlaps


def test_demo_plan_contains_a_skip_and_two_offices() -> None:
    plan = demo_plan(date(2026, 10, 20))
    assert {item["room_id"] for item in plan["singles"]} >= {1, 2, 3, 4, 5}
    assert len(plan["series"]) >= 4
    assert any(item["conflict_policy"] == "skip" for item in plan["series"])


def test_reset_writes_no_conflicting_bookings(tmp_path) -> None:
    db = Database(str(tmp_path / "demo.db"))
    summary = reset_to_demo_state(db, date(2026, 10, 20))

    assert summary["bookings"] >= 10
    assert all("error" not in entry for entry in summary["series"])

    rows = db.bookings_between(0, 2**31 - 1)
    assert rows, "demo seed produced no bookings"
    by_room: dict[int, list] = {}
    for row in rows:
        by_room.setdefault(row["room_id"], []).append(row)
    for room_rows in by_room.values():
        ordered = sorted(room_rows, key=lambda r: r["start_at"])
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            assert not overlaps(
                earlier["start_at"], earlier["end_at"], later["start_at"], later["end_at"]
            ), "demo data must never double-book a room"


def test_demo_seed_shows_the_skip_path(tmp_path) -> None:
    db = Database(str(tmp_path / "demo2.db"))
    summary = reset_to_demo_state(db, date(2026, 10, 20))
    assert any(entry.get("skipped", 0) >= 1 for entry in summary["series"])


def test_reset_endpoint_respects_the_demo_flag(monkeypatch) -> None:
    client = TestClient(create_app(":memory:"))
    monkeypatch.setenv("ROOMLOOP_DEMO", "1")
    assert client.post("/admin/reset-demo").status_code == 200

    monkeypatch.setenv("ROOMLOOP_DEMO", "0")
    blocked = client.post("/admin/reset-demo")
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "VALIDATION_ERROR"


def test_console_is_served_without_a_build_step() -> None:
    client = TestClient(create_app(":memory:"))
    page = client.get("/console")
    assert page.status_code == 200
    assert "RoomLoop" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_root_redirects_to_the_console() -> None:
    client = TestClient(create_app(":memory:"), follow_redirects=False)
    response = client.get("/")
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/console"
