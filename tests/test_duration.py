import pytest

from matrix_room_media_retention.duration import (
    DEFAULT_MINIMUM_RETAIN_SECONDS,
    InvalidDurationError,
    format_duration_seconds,
    parse_duration_seconds,
)


class TestParseDurationSeconds:
    def test_days(self):
        assert parse_duration_seconds("30d") == 30 * 86400

    def test_hours_below_default_floor_needs_an_explicit_lower_minimum(self):
        # 6h is below the 1-day default floor -- must pass a lower
        # minimum_seconds explicitly to accept it, matching how an
        # operator would actually lower minimum_retain_seconds in config.
        assert parse_duration_seconds("6h", minimum_seconds=0) == 6 * 3600

    def test_minutes_below_default_floor_needs_an_explicit_lower_minimum(self):
        assert parse_duration_seconds("45m", minimum_seconds=0) == 45 * 60

    def test_weeks(self):
        assert parse_duration_seconds("2w") == 2 * 7 * 86400

    def test_case_insensitive(self):
        assert parse_duration_seconds("30D") == 30 * 86400

    def test_whitespace_tolerated(self):
        assert parse_duration_seconds("  30d  ") == 30 * 86400

    def test_bare_number_rejected(self):
        # The exact ambiguity this module's docstring calls out -- "30"
        # alone must never be silently interpreted as any unit.
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("30")

    def test_zero_rejected(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("0d", minimum_seconds=0)

    def test_negative_rejected(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("-5d")

    def test_unknown_unit_rejected(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("30y")

    def test_garbage_rejected(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("forever")


class TestMinimumRetainFloor:
    """roadmap/041 §9 (second review pass): a typo like "retain 30m" meaning
    "retain 30d" must not be silently accepted -- 037 §7.1's own stated
    1-day-minimum discipline for exactly this class of footgun."""

    def test_default_floor_is_one_day(self):
        assert DEFAULT_MINIMUM_RETAIN_SECONDS == 86400

    def test_below_default_floor_rejected_with_no_explicit_override(self):
        with pytest.raises(InvalidDurationError):
            parse_duration_seconds("30m")

    def test_exactly_at_the_floor_is_accepted(self):
        assert parse_duration_seconds("1d") == 86400

    def test_above_the_floor_is_accepted(self):
        assert parse_duration_seconds("2d") == 2 * 86400

    def test_operator_can_lower_the_floor_explicitly(self):
        # Mirrors config.py wiring minimum_retain_seconds through from
        # config.yaml into every parse_duration_seconds() call.
        assert parse_duration_seconds("10m", minimum_seconds=0) == 10 * 60

    def test_rejection_message_names_both_the_given_and_minimum_duration(self):
        with pytest.raises(InvalidDurationError, match="30m"):
            parse_duration_seconds("30m")


class TestFormatDurationSeconds:
    def test_whole_weeks_preferred_over_days(self):
        assert format_duration_seconds(14 * 86400) == "2w"

    def test_whole_days(self):
        assert format_duration_seconds(30 * 86400) == "30d"

    def test_falls_back_to_hours_when_not_whole_days(self):
        assert format_duration_seconds(6 * 3600) == "6h"

    def test_falls_back_to_minutes(self):
        assert format_duration_seconds(45 * 60) == "45m"

    def test_round_trip(self):
        for text in ("30d", "6h", "45m", "2w"):
            assert format_duration_seconds(parse_duration_seconds(text, minimum_seconds=0)) == text
