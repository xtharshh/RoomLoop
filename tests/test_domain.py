"""Domain unit tests: the rules that are easy to get subtly wrong."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from roomloop.domain import (
    InvalidInterval,
    RecurrenceTooLong,
    RepeatUntilInvalid,
    expand_weekly,
    get_zone,
    naive_iso,
    overlaps,
    parse_instant,
    parse_iso,
    resolve_wall_clock,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TestOverlap:
    """R4: overlap in the same room; back-to-back is not a conflict."""

    def test_back_to_back_is_not_a_conflict(self):
        assert not overlaps(dt("2026-11-02T09:00"), dt("2026-11-02T10:00"),
                            dt("2026-11-02T10:00"), dt("2026-11-02T11:00"))

    def test_back_to_back_reversed_is_not_a_conflict(self):
        assert not overlaps(dt("2026-11-02T10:00"), dt("2026-11-02T11:00"),
                            dt("2026-11-02T09:00"), dt("2026-11-02T10:00"))

    def test_partial_overlap(self):
        assert overlaps(dt("2026-11-02T09:00"), dt("2026-11-02T10:30"),
                        dt("2026-11-02T10:00"), dt("2026-11-02T11:00"))

    def test_containment(self):
        assert overlaps(dt("2026-11-02T09:00"), dt("2026-11-02T12:00"),
                        dt("2026-11-02T10:00"), dt("2026-11-02T10:30"))

    def test_one_minute_overlap(self):
        assert overlaps(dt("2026-11-02T09:00"), dt("2026-11-02T10:01"),
                        dt("2026-11-02T10:00"), dt("2026-11-02T11:00"))

    def test_identical(self):
        a = (dt("2026-11-02T09:00"), dt("2026-11-02T10:00"))
        assert overlaps(*a, *a)


class TestWeeklyWallClock:
    """R3: the wall clock stays fixed across DST."""

    def test_berlin_fall_back_keeps_local_hour_and_changes_offset(self):
        # Berlin leaves CEST on 2026-10-25 (03:00 -> 02:00).
        occurrences = expand_weekly(
            first_start_naive=dt("2026-10-12T09:00:00"),
            duration=timedelta(hours=1),
            tz_name="Europe/Berlin",
            repeat_until=date(2026, 11, 2),
        )
        assert [o.start_local.strftime("%H:%M") for o in occurrences] == ["09:00"] * 4
        assert [o.start_utc.strftime("%H:%M") for o in occurrences] == ["07:00", "07:00", "08:00", "08:00"]
        # Instants are stored in UTC; the office offset is derived per instance.
        assert occurrences[0].start_utc.astimezone(get_zone("Europe/Berlin")).utcoffset() == timedelta(hours=2)
        assert occurrences[2].start_utc.astimezone(get_zone("Europe/Berlin")).utcoffset() == timedelta(hours=1)

    def test_denver_fall_back_keeps_local_hour(self):
        # Denver leaves MDT on 2026-11-01 (02:00 -> 01:00).
        occurrences = expand_weekly(
            first_start_naive=dt("2026-10-19T09:00:00"),
            duration=timedelta(minutes=90),
            tz_name="America/Denver",
            repeat_until=date(2026, 11, 9),
        )
        assert [o.start_local.strftime("%H:%M") for o in occurrences] == ["09:00"] * 4
        assert [o.start_utc.strftime("%H:%M") for o in occurrences] == ["15:00", "15:00", "16:00", "16:00"]
        # End time is 10:30 local on the fall-back day, which is 17:30 UTC.
        assert occurrences[2].end_local.strftime("%H:%M") == "10:30"

    def test_berlin_spring_forward_gap_is_shifted_and_flagged(self):
        # 2027-03-28: Berlin jumps 02:00 -> 03:00, so 02:30 does not exist.
        occurrences = expand_weekly(
            first_start_naive=dt("2027-03-21T02:30:00"),
            duration=timedelta(minutes=30),
            tz_name="Europe/Berlin",
            repeat_until=date(2027, 3, 29),
        )
        gap = occurrences[1]
        assert gap.warning == "DST_GAP_SHIFTED"
        assert gap.start_local.strftime("%H:%M") == "03:30"
        assert gap.start_utc.timestamp() == datetime.fromisoformat("2027-03-28T03:30:00+02:00").timestamp()

    def test_ambiguous_wall_clock_uses_first_instant(self):
        resolved = resolve_wall_clock(datetime(2026, 10, 25, 2, 30), get_zone("Europe/Berlin"))
        assert resolved.warning == "DST_AMBIGUOUS_FIRST_INSTANT"
        assert resolved.local.utcoffset() == timedelta(hours=2)  # CEST, the first 02:30

    def test_weekly_series_that_would_cross_a_year_is_capped(self):
        with pytest.raises(RecurrenceTooLong):
            expand_weekly(
                first_start_naive=dt("2026-01-05T09:00:00"),
                duration=timedelta(hours=1),
                tz_name="Europe/Berlin",
                repeat_until=date(2032, 1, 1),
            )

    def test_repeat_until_before_start_is_rejected(self):
        with pytest.raises(RepeatUntilInvalid):
            expand_weekly(
                first_start_naive=dt("2026-11-02T09:00:00"),
                duration=timedelta(hours=1),
                tz_name="Europe/Berlin",
                repeat_until=date(2026, 10, 1),
            )

    def test_empty_duration_is_rejected(self):
        with pytest.raises(InvalidInterval):
            expand_weekly(
                first_start_naive=dt("2026-11-02T09:00:00"),
                duration=timedelta(0),
                tz_name="Europe/Berlin",
                repeat_until=date(2026, 11, 30),
            )


class TestTimestamps:
    """C1: naive-in, naive-out; offsets are respected when supplied."""

    def test_naive_input_is_read_in_office_timezone(self):
        utc, warning = parse_instant("2026-07-02T09:00:00", "Europe/Berlin")
        assert naive_iso(utc) == "2026-07-02T07:00:00"
        assert warning is None

    def test_offset_input_is_used_verbatim(self):
        utc, _ = parse_instant("2026-07-02T09:00:00+02:00", "Europe/Berlin")
        assert naive_iso(utc) == "2026-07-02T07:00:00"

    def test_same_instant_across_offices(self):
        berlin, _ = parse_instant("2026-07-02T17:00:00", "Europe/Berlin")
        denver, _ = parse_instant("2026-07-02T09:00:00", "America/Denver")
        assert berlin == denver

    def test_zulu_suffix_is_accepted(self):
        utc, _ = parse_instant("2026-07-02T07:00:00Z", "Europe/Berlin")
        assert naive_iso(utc) == "2026-07-02T07:00:00"

    def test_date_only_is_midnight_local(self):
        assert parse_iso("2026-07-02") == datetime(2026, 7, 2, 0, 0)

    def test_garbage_is_rejected_with_a_clear_code(self):
        from roomloop.domain import InvalidTimestamp

        with pytest.raises(InvalidTimestamp):
            parse_iso("next tuesday")
