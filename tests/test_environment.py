"""Environment / portability guards.

The bug this file exists for: on Windows (and on slim container images) there is
no IANA timezone database. ``zoneinfo`` then fails for every zone name, and the
original code surfaced that as ``422 VALIDATION_ERROR: Unknown timezone`` — which
blames the caller's payload for a missing OS package. These tests pin both the
root cause and the error contract.
"""

from __future__ import annotations

import zoneinfo

import pytest
from fastapi.testclient import TestClient

from roomloop.api import create_app
from roomloop.domain import (
    TimeZoneDataMissing,
    ValidationError,
    get_zone,
    timezone_database_available,
)


def _simulate_missing_tz_database(monkeypatch) -> None:
    """Reproduce a Windows host: no system tz database and no tzdata package.

    Patching the symbol the module under test actually calls keeps this
    deterministic whether or not ``tzdata`` happens to be installed.
    """

    def _no_database(_key):
        raise zoneinfo.ZoneInfoNotFoundError(_key)

    monkeypatch.setattr("roomloop.domain.ZoneInfo", _no_database)


def test_timezone_database_is_available_in_this_environment() -> None:
    """If this fails the install is incomplete — run: pip install tzdata."""
    assert timezone_database_available() is True
    assert get_zone("Europe/Berlin") is not None
    assert get_zone("America/Denver") is not None


def test_berlin_and_denver_resolve_to_real_offsets() -> None:
    from datetime import datetime

    berlin = datetime(2026, 7, 2, 9, tzinfo=get_zone("Europe/Berlin"))
    denver = datetime(2026, 7, 2, 9, tzinfo=get_zone("America/Denver"))
    assert berlin.utcoffset().total_seconds() == 2 * 3600
    assert denver.utcoffset().total_seconds() == -6 * 3600


def test_unknown_zone_is_still_a_client_error(monkeypatch) -> None:
    if not timezone_database_available():
        pytest.skip("no tz database in this environment")

    with pytest.raises(ValidationError) as info:
        get_zone("Mars/Olympus_Mons")
    assert not isinstance(info.value, TimeZoneDataMissing)
    assert info.value.status == 422


def test_missing_database_is_an_environment_error_not_a_422(monkeypatch) -> None:
    _simulate_missing_tz_database(monkeypatch)

    with pytest.raises(TimeZoneDataMissing) as info:
        get_zone("Europe/Berlin")

    error = info.value
    assert error.status == 500
    assert error.code == "TZ_DATA_MISSING"
    assert "pip install tzdata" in error.message


def test_availability_explains_a_missing_database_instead_of_422(monkeypatch) -> None:
    """The exact symptom that was reported: 422 on /availability/day."""
    client = TestClient(create_app(":memory:"))
    _simulate_missing_tz_database(monkeypatch)

    response = client.get(
        "/availability/day?day=2026-10-20&day_start=08:00&day_end=19:00&slot_minutes=30"
    )

    assert response.status_code == 500, response.text
    body = response.json()["error"]
    assert body["code"] == "TZ_DATA_MISSING"
    assert "pip install tzdata" in body["message"]


def test_health_reports_timezone_data_and_degrades(monkeypatch) -> None:
    client = TestClient(create_app(":memory:"))

    healthy = client.get("/health").json()
    assert healthy["timezone_data"] == "ok"
    assert healthy["status"] == "ok"

    _simulate_missing_tz_database(monkeypatch)
    degraded = client.get("/health").json()
    assert degraded["timezone_data"] == "missing"
    assert degraded["status"] == "degraded"
    assert "tzdata" in degraded["hint"]


def test_meta_reports_environment(monkeypatch) -> None:
    client = TestClient(create_app(":memory:"))
    assert client.get("/meta").json()["environment"]["timezone_data"] == "ok"


def test_seed_cli_refuses_to_run_without_tz_data(monkeypatch, tmp_path) -> None:
    from roomloop import seed

    _simulate_missing_tz_database(monkeypatch)
    code = seed.main(["--db", str(tmp_path / "x.db"), "--reset"])
    assert code == 2
