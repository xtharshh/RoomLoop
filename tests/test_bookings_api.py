"""API-level tests. These read like the acceptance criteria in the brief."""

from __future__ import annotations

from fastapi.testclient import TestClient


def book(client: TestClient, room_id=1, user="mira", start="2026-11-02T09:00:00",
         end="2026-11-02T10:00:00"):
    return client.post(
        "/bookings",
        json={"room_id": room_id, "user": user, "start": start, "end": end},
    )


def recurring(client: TestClient, **overrides):
    payload = {
        "room_id": 1,
        "user": "platform",
        "start": "2026-11-02T09:00:00",
        "end": "2026-11-02T10:00:00",
        "repeat_until": "2026-11-30",
        "conflict_policy": "skip",
    }
    payload.update(overrides)
    return client.post("/bookings/recurring", json=payload)


class TestRoomsContract:
    """C2 and R5."""

    def test_rooms_payload_keeps_the_legacy_shape(self, client):
        response = client.get("/rooms")
        assert response.status_code == 200
        body = response.json()
        assert body == [
            {"id": 1, "name": "Aurora", "capacity": 8},
            {"id": 2, "name": "Basalt", "capacity": 4},
            {"id": 3, "name": "Cinder", "capacity": 12},
            {"id": 4, "name": "Dune", "capacity": 6},
            {"id": 5, "name": "Ember", "capacity": 10},
        ]

    def test_room_ids_are_sequential_for_iteration(self, client):
        ids = [room["id"] for room in client.get("/rooms", params={"detail": "true"}).json()]
        assert ids == list(range(1, len(ids) + 1))

    def test_detail_mode_is_additive(self, client):
        detail = client.get("/rooms", params={"detail": "true"}).json()
        assert {"id", "name", "capacity", "office", "timezone"} <= set(detail[0])


class TestCreateBooking:
    def test_create_returns_legacy_naive_local_timestamps(self, client):
        response = book(client, start="2026-07-02T09:00:00", end="2026-07-02T10:00:00")
        assert response.status_code == 201
        body = response.json()
        # C1: naive local ISO, no offset suffix.
        assert body["start"] == "2026-07-02T09:00:00"
        assert body["end"] == "2026-07-02T10:00:00"
        assert body["start_utc"] == "2026-07-02T07:00:00Z"
        assert body["utc_offset"] == "+02:00"

    def test_denver_room_uses_denver_wall_clock(self, client):
        response = book(client, room_id=4, start="2026-07-02T09:00:00", end="2026-07-02T10:00:00")
        body = response.json()
        assert body["start"] == "2026-07-02T09:00:00"
        assert body["start_utc"] == "2026-07-02T15:00:00Z"
        assert body["utc_offset"] == "-06:00"

    def test_back_to_back_bookings_both_succeed(self, client):
        assert book(client, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00").status_code == 201
        assert book(client, user="jonas", start="2026-11-02T10:00:00", end="2026-11-02T11:00:00").status_code == 201

    def test_overlap_is_rejected_with_409_and_the_blocker(self, client):
        book(client, start="2026-11-02T09:00:00", end="2026-11-02T10:30:00")
        response = book(client, user="jonas", start="2026-11-02T10:00:00", end="2026-11-02T11:00:00")
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "SLOT_UNAVAILABLE"
        assert error["details"]["conflicts"][0]["user"] == "mira"

    def test_same_window_in_a_different_room_is_fine(self, client):
        assert book(client, room_id=1, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00").status_code == 201
        assert book(client, room_id=2, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00").status_code == 201

    def test_same_instant_written_in_another_offset_still_conflicts(self, client):
        # 09:00 Berlin == 08:00Z. The second request states the instant in UTC.
        assert book(client, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00").status_code == 201
        clash = book(client, user="jonas", start="2026-11-02T08:30:00Z", end="2026-11-02T09:30:00Z")
        assert clash.status_code == 409

    def test_end_before_start_is_a_validation_error(self, client):
        response = book(client, start="2026-11-02T10:00:00", end="2026-11-02T09:00:00")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_INTERVAL"

    def test_unknown_room_is_404(self, client):
        assert book(client, room_id=99).status_code == 404

    def test_missing_user_is_normalised_validation_error(self, client):
        response = client.post("/bookings", json={
            "room_id": 1, "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00",
        })
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "REQUEST_VALIDATION_ERROR"
        assert error["details"]["problems"][0]["field"] == "user"

    def test_booking_more_than_24_hours_is_rejected(self, client):
        response = book(client, start="2026-11-02T09:00:00", end="2026-11-03T10:00:00")
        assert response.status_code == 422


class TestRecurring:
    def test_series_creates_every_weekly_occurrence(self, client):
        response = recurring(client)
        assert response.status_code == 201
        body = response.json()
        # 2026-11-02, 09, 16, 23, 30 -> five Mondays.
        assert body["created_count"] == 5
        assert body["skipped_count"] == 0
        assert [item["start"] for item in body["created"]] == [
            "2026-11-02T09:00:00",
            "2026-11-09T09:00:00",
            "2026-11-16T09:00:00",
            "2026-11-23T09:00:00",
            "2026-11-30T09:00:00",
        ]

    def test_each_instance_carries_the_series_id(self, client):
        body = recurring(client).json()
        for item in body["created"]:
            assert item["series_id"] == body["series_id"]

    def test_clashing_occurrence_is_skipped_and_reported(self, client):
        """R2: one clash must not block the other occurrences."""
        book(client, start="2026-11-16T09:15:00", end="2026-11-16T09:45:00")
        body = recurring(client).json()
        assert body["created_count"] == 4
        assert body["skipped_count"] == 1
        skipped = body["skipped"][0]
        assert skipped["start"] == "2026-11-16T09:00:00"
        assert skipped["reason"] == "ROOM_BUSY"
        assert skipped["conflicts_with"][0]["user"] == "mira"
        # The skipped date really is absent from the saved instances.
        saved = [item["start"] for item in body["created"]]
        assert "2026-11-16T09:00:00" not in saved

    def test_strict_policy_saves_nothing(self, client):
        """R1: all-or-nothing must be genuinely atomic."""
        book(client, start="2026-11-16T09:15:00", end="2026-11-16T09:45:00")
        response = recurring(client, conflict_policy="fail")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SERIES_CONFLICT_FAIL_FAST"
        # Nothing at all was written, not even the non-conflicting weeks.
        listed = client.get("/bookings", params={
            "start": "2026-01-01T00:00:00Z", "end": "2027-01-01T00:00:00Z",
        }).json()
        assert listed["count"] == 1  # only the blocking booking

    def test_fully_conflicted_series_fails_and_saves_nothing(self, client):
        # Block every Monday in the range with one long booking per week.
        for week in range(5):
            book(client, start=f"2026-11-{2 + week * 7:02d}T09:00:00",
                 end=f"2026-11-{2 + week * 7:02d}T10:00:00")
        response = recurring(client)
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "SERIES_FULLY_CONFLICTED"
        assert error["details"]["occurrences"] == 5
        assert len(error["details"]["skipped"]) == 5

    def test_repeat_until_before_start_is_rejected(self, client):
        response = recurring(client, repeat_until="2026-10-01")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_REPEAT_UNTIL"

    def test_repeat_until_is_inclusive(self, client):
        body = recurring(client, repeat_until="2026-11-16").json()
        assert body["created_count"] == 3

    def test_too_many_occurrences_is_rejected(self, client):
        response = recurring(client, repeat_until="2035-01-01")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "RECURRENCE_TOO_LONG"

    def test_series_across_dst_keeps_wall_clock_and_reports_offsets(self, client):
        # Berlin's fall-back is 2026-10-25.
        body = recurring(client, start="2026-10-12T09:00:00", end="2026-10-12T10:00:00",
                         repeat_until="2026-11-02").json()
        starts = [(item["start"], item["utc_offset"]) for item in body["created"]]
        assert starts == [
            ("2026-10-12T09:00:00", "+02:00"),
            ("2026-10-19T09:00:00", "+02:00"),
            ("2026-10-26T09:00:00", "+01:00"),
            ("2026-11-02T09:00:00", "+01:00"),
        ]
        utc = [item["start_utc"] for item in body["created"]]
        assert utc[1] == "2026-10-19T07:00:00Z"
        assert utc[2] == "2026-10-26T08:00:00Z"

    def test_denver_series_and_berlin_series_do_not_share_a_timezone(self, client):
        berlin = recurring(client, room_id=1, start="2026-11-02T09:00:00",
                           end="2026-11-02T10:00:00", repeat_until="2026-11-02").json()
        denver = recurring(client, room_id=4, start="2026-11-02T09:00:00",
                           end="2026-11-02T10:00:00", repeat_until="2026-11-02").json()
        assert berlin["created"][0]["start_utc"] == "2026-11-02T08:00:00Z"
        assert denver["created"][0]["start_utc"] == "2026-11-02T16:00:00Z"

    def test_conflict_is_scoped_to_one_room(self, client):
        book(client, room_id=3, start="2026-11-09T09:00:00", end="2026-11-09T10:00:00")
        body = recurring(client, room_id=1).json()
        assert body["skipped_count"] == 0


class TestCancellation:
    def test_cancel_single_booking(self, client):
        booking = book(client).json()
        response = client.delete(f"/bookings/{booking['id']}")
        assert response.status_code == 200
        assert response.json()["cancelled_count"] == 1
        assert client.get(f"/bookings/{booking['id']}").json()["status"] == "cancelled"

    def test_cancelled_booking_frees_the_slot(self, client):
        first = book(client).json()
        client.delete(f"/bookings/{first['id']}")
        assert book(client, user="jonas").status_code == 201

    def test_cancelling_a_series_removes_future_instances_only(self, client, monkeypatch):
        body = recurring(client).json()
        series_id = body["series_id"]
        # Pretend "now" is between the 2nd and 3rd occurrence.
        from roomloop.domain import parse_instant

        cutoff, _ = parse_instant("2026-11-10T00:00:00", "Europe/Berlin")
        monkeypatch.setattr("roomloop.service.utc_now_epoch", lambda: int(cutoff.timestamp()))

        first_booking = body["created"][0]["id"]
        response = client.delete(f"/bookings/{first_booking}")
        assert response.status_code == 200
        result = response.json()
        assert result["scope"] == "series"
        assert result["cancelled_count"] == 3  # 16th, 23rd, 30th
        assert result["kept_instances"] == 2   # 2nd and 9th stay (they already started)

        detail = client.get(f"/series/{series_id}").json()
        statuses = [(i["start"], i["status"]) for i in detail["instances"]]
        assert statuses == [
            ("2026-11-02T09:00:00", "confirmed"),
            ("2026-11-09T09:00:00", "confirmed"),
            ("2026-11-16T09:00:00", "cancelled"),
            ("2026-11-23T09:00:00", "cancelled"),
            ("2026-11-30T09:00:00", "cancelled"),
        ]

    def test_cancelling_one_occurrence_keeps_the_series(self, client):
        body = recurring(client).json()
        response = client.delete(f"/bookings/{body['created'][2]['id']}", params={"scope": "single"})
        assert response.json()["scope"] == "single"
        assert response.json()["kept_instances"] == 4
        detail = client.get(f"/series/{body['series_id']}").json()
        assert [i["status"] for i in detail["instances"]].count("confirmed") == 4

    def test_cancel_is_idempotent(self, client):
        booking = book(client).json()
        client.delete(f"/bookings/{booking['id']}")
        again = client.delete(f"/bookings/{booking['id']}")
        assert again.status_code == 200
        assert again.json()["already_cancelled"] is True

    def test_cancel_unknown_booking_is_404(self, client):
        assert client.delete("/bookings/9999").status_code == 404

    def test_cancel_series_endpoint(self, client):
        body = recurring(client).json()
        response = client.delete(f"/series/{body['series_id']}")
        assert response.status_code == 200
        assert response.json()["cancelled_count"] == 5

    def test_post_cancel_alias_accepts_a_body(self, client):
        body = recurring(client).json()
        response = client.post(f"/bookings/{body['created'][0]['id']}/cancel",
                               json={"scope": "single"})
        assert response.status_code == 200
        assert response.json()["scope"] == "single"

    def test_cancelled_series_can_be_rebooked(self, client, monkeypatch):
        body = recurring(client).json()
        from roomloop.domain import parse_instant

        cutoff, _ = parse_instant("2026-11-01T00:00:00", "Europe/Berlin")
        monkeypatch.setattr("roomloop.service.utc_now_epoch", lambda: int(cutoff.timestamp()))
        client.delete(f"/series/{body['series_id']}")
        monkeypatch.undo()
        assert recurring(client).status_code == 201


class TestAvailability:
    def test_available_rooms_skip_busy_ones(self, client):
        book(client, room_id=1, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00")
        response = client.get("/availability", params={
            "start": "2026-11-02T08:30:00Z",   # 09:30 Berlin = 08:30Z
            "end": "2026-11-02T08:45:00Z",
        })
        assert response.status_code == 200
        body = response.json()
        assert body["available_rooms"] == [2, 3, 4, 5]
        busy = next(room for room in body["rooms"] if room["room_id"] == 1)
        assert busy["available"] is False
        assert busy["conflicts"][0]["id"] is not None

    def test_back_to_back_window_is_available(self, client):
        book(client, room_id=1, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00")
        body = client.get("/availability", params={
            "start": "2026-11-02T10:00:00+01:00",
            "end": "2026-11-02T11:00:00+01:00",
            "room_id": 1,
        }).json()
        assert body["available_rooms"] == [1]

    def test_capacity_filter_uses_sequential_room_ids(self, client):
        body = client.get("/availability", params={
            "start": "2026-11-02T09:00:00Z",
            "end": "2026-11-02T09:30:00Z",
            "capacity": 10,
        }).json()
        assert body["available_rooms"] == [3, 5]

    def test_day_view_lists_free_slots_in_office_time(self, client):
        book(client, room_id=1, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00")
        body = client.get("/availability/day", params={"day": "2026-11-02"}).json()
        room = next(room for room in body["rooms"] if room["room_id"] == 1)
        assert room["busy"][0]["start"] == "2026-11-02T09:00:00"
        assert {"start": "2026-11-02T10:00:00", "end": "2026-11-02T10:30:00"} in room["free_slots"]
        # 08:00-19:00 at 30-minute granularity = 22 slots, one hour booked.
        assert len(room["free_slots"]) == 20


class TestIdempotency:
    def test_retrying_with_the_same_key_does_not_double_book(self, client):
        headers = {"Idempotency-Key": "retry-1"}
        payload = {"room_id": 1, "user": "mira",
                   "start": "2026-11-02T09:00:00", "end": "2026-11-02T10:00:00"}
        first = client.post("/bookings", json=payload, headers=headers)
        second = client.post("/bookings", json=payload, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        listed = client.get("/bookings", params={
            "start": "2026-11-01T00:00:00Z", "end": "2026-11-03T00:00:00Z"}).json()
        assert listed["count"] == 1

    def test_reused_key_with_a_different_body_is_409(self, client):
        headers = {"Idempotency-Key": "retry-2"}
        client.post("/bookings", json={"room_id": 1, "user": "mira",
                                       "start": "2026-11-02T09:00:00",
                                       "end": "2026-11-02T10:00:00"}, headers=headers)
        clash = client.post("/bookings", json={"room_id": 1, "user": "mira",
                                               "start": "2026-11-02T11:00:00",
                                               "end": "2026-11-02T12:00:00"}, headers=headers)
        assert clash.status_code == 409
        assert clash.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_series_retry_is_idempotent(self, client):
        headers = {"Idempotency-Key": "series-1"}
        payload = {"room_id": 1, "user": "platform", "start": "2026-11-02T09:00:00",
                   "end": "2026-11-02T10:00:00", "repeat_until": "2026-11-30"}
        first = client.post("/bookings/recurring", json=payload, headers=headers)
        second = client.post("/bookings/recurring", json=payload, headers=headers)
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["series_id"] == second.json()["series_id"]
        assert client.get("/series").json()["count"] == 1


class TestOps:
    def test_health(self, client):
        assert client.get("/health").json()["status"] == "ok"

    def test_meta_documents_the_timestamp_contract(self, client):
        body = client.get("/meta").json()
        assert body["timestamp_contract"]["legacy_fields"] == ["start", "end"]

    def test_audit_feed_records_creations(self, client):
        book(client)
        entries = client.get("/audit").json()["entries"]
        assert entries[0]["action"] == "booking.created"

    def test_list_bookings_window_filter(self, client):
        book(client, start="2026-11-02T09:00:00", end="2026-11-02T10:00:00")
        book(client, room_id=2, start="2026-12-02T09:00:00", end="2026-12-02T10:00:00")
        body = client.get("/bookings", params={
            "start": "2026-11-01T00:00:00Z", "end": "2026-11-30T00:00:00Z"}).json()
        assert body["count"] == 1
