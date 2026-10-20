"""Data-driven acceptance tests.

The scenarios live in ``data/scenarios.json`` so the test data is reviewable on
its own, and so a new edge case is a data change rather than a code change.
Each scenario runs against a **fresh** database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roomloop.api import create_app

SCENARIO_FILE = Path(__file__).resolve().parent.parent / "data" / "scenarios.json"
SCENARIOS = json.loads(SCENARIO_FILE.read_text())["scenarios"]


@pytest.fixture()
def fresh_client() -> TestClient:
    return TestClient(create_app(":memory:"))


def _create_single(client: TestClient, payload: dict) -> dict:
    body = {k: v for k, v in payload.items() if k not in ("cancel",)}
    response = client.post("/bookings", json=body)
    assert response.status_code == 201, response.text
    booking = response.json()
    if payload.get("cancel"):
        cancel = client.delete(f"/bookings/{booking['id']}", params={"scope": "single"})
        assert cancel.status_code == 200
    return booking


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_scenario(scenario: dict, fresh_client: TestClient) -> None:
    client = fresh_client
    expect = scenario["expect"]

    for given in scenario["given"]:
        _create_single(client, given)

    when = scenario["when"]
    if when["kind"] == "recurring":
        body = {k: v for k, v in when.items() if k != "kind"}
        response = client.post("/bookings/recurring", json=body)
    else:
        body = {k: v for k, v in when.items() if k != "kind"}
        response = client.post("/bookings", json=body)

    assert response.status_code == expect["status"], response.text
    payload = response.json()

    if expect["status"] >= 400:
        assert payload["error"]["code"] == expect["error_code"]
    else:
        if "booking_start" in expect:
            assert payload["start"] == expect["booking_start"]

    if "created_count" in expect:
        assert payload["created_count"] == expect["created_count"]
        assert payload["requested_count"] >= payload["created_count"]
    if "skipped_count" in expect:
        assert payload["skipped_count"] == expect["skipped_count"]
    if "created_starts" in expect:
        assert [item["start"] for item in payload["created"]] == expect["created_starts"]
    if "created_utc" in expect:
        assert [item["start_utc"] for item in payload["created"]] == expect["created_utc"]
    if "created_offsets" in expect:
        assert [item["utc_offset"] for item in payload["created"]] == expect["created_offsets"]
    if "skipped_starts" in expect:
        assert [item["start"] for item in payload["skipped"]] == expect["skipped_starts"]
        assert payload["skipped"][0]["reason"] == expect["skipped_reason"]
    if "warning_codes" in expect:
        codes = [warning["code"] for warning in payload.get("warnings", [])]
        assert codes == expect["warning_codes"]

    if "bookings_total" in expect:
        listed = client.get(
            "/bookings",
            params={"start": "2026-01-01T00:00:00Z", "end": "2028-01-01T00:00:00Z"},
        ).json()
        assert listed["count"] == expect["bookings_total"]
    if "series_total" in expect:
        assert client.get("/series").json()["count"] == expect["series_total"]


def test_scenario_file_is_well_formed() -> None:
    assert SCENARIOS, "no scenarios found"
    names = [scenario["name"] for scenario in SCENARIOS]
    assert len(names) == len(set(names)), "scenario names must be unique"
    for scenario in SCENARIOS:
        assert scenario["why"], f"{scenario['name']} should explain why it matters"
        assert scenario["when"]["kind"] in ("single", "recurring")
        assert isinstance(scenario["expect"], dict)
