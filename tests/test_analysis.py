"""Exact source/time selection and observation intervals from saved evidence."""

from decimal import localcontext
from pathlib import Path

import pytest

from uav_debugger import import_bytes, import_file
from uav_debugger.analysis import (
    Selection,
    observed_intervals,
    seconds_to_timestamp,
    select_records,
    timestamp_to_seconds,
)

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"
EPOCH_US = 1_700_000_000_000_000
UINT64_MAX = 2**64 - 1


@pytest.fixture
def session():
    return import_file(FIXTURE)


def _recording(session, entries):
    return import_bytes(
        b"".join(
            timestamp.to_bytes(8, "big") + session.records[index].raw_frame
            for index, timestamp in entries
        )
    )


def test_default_selection_preserves_all_records_and_identity(session):
    selected = select_records(session, Selection())

    assert isinstance(selected, tuple)
    assert selected == session.records
    assert all(
        actual is original for actual, original in zip(selected, session.records, strict=True)
    )


@pytest.mark.parametrize("selection", [Selection(sources=()), Selection(message_ids=())])
def test_explicit_empty_filters_select_nothing(session, selection):
    assert select_records(session, selection) == ()
    assert observed_intervals(session, selection) == ()


def test_source_type_and_time_filters_combine_with_inclusive_bounds(session):
    selection = Selection(
        sources=((1, 1),),
        message_ids=(30,),
        start_us=EPOCH_US + 1_000_000,
        end_us=EPOCH_US + 5_000_000,
    )

    assert [record.index for record in select_records(session, selection)] == [2, 8]
    exactly_at_end = Selection(start_us=EPOCH_US + 5_000_000, end_us=EPOCH_US + 5_000_000)
    assert [record.index for record in select_records(session, exactly_at_end)] == [8, 9]


def test_source_identity_includes_component_and_absent_filters_are_empty(session):
    assert select_records(session, Selection(sources=((1, 42),))) == ()
    assert select_records(session, Selection(message_ids=(999_999,))) == ()
    assert [record.index for record in select_records(session, Selection(sources=((2, 1),)))] == [
        1,
        3,
        4,
        5,
        6,
        7,
        9,
        11,
    ]


def test_selection_does_not_sort_or_repair_a_regressing_capture_clock(session):
    result = _recording(session, [(2, 120), (2, 110), (2, 115)])

    selected = select_records(result, Selection(start_us=110, end_us=120))

    assert [record.timestamp_us for record in selected] == [120, 110, 115]
    assert [record.index for record in selected] == [0, 1, 2]
    assert result.records == selected


@pytest.mark.parametrize(
    "arguments",
    [
        {"start_us": -1},
        {"end_us": UINT64_MAX + 1},
        {"start_us": 2, "end_us": 1},
    ],
)
def test_invalid_time_bounds_are_rejected(arguments):
    with pytest.raises(ValueError):
        Selection(**arguments)


def test_fixture_gap_intervals_are_per_source_and_message_type(session):
    intervals = observed_intervals(session, Selection())
    actual = {
        (item.previous_index, item.index): (
            item.system_id,
            item.component_id,
            item.message_id,
            item.delta_us,
        )
        for item in intervals
    }

    assert actual == {
        (0, 10): (1, 1, 0, 6_000_000),
        (1, 5): (2, 1, 0, 3_000_000),
        (5, 11): (2, 1, 0, 3_000_000),
        (2, 8): (1, 1, 30, 4_000_000),
        (3, 4): (2, 1, 30, 1_000_000),
        (4, 6): (2, 1, 30, 1_000_000),
        (6, 7): (2, 1, 30, 1_000_000),
        (7, 9): (2, 1, 30, 1_000_000),
    }
    assert len(intervals) == len(actual)


def test_interval_endpoints_both_belong_to_the_active_selection(session):
    selection = Selection(sources=((1, 1),), message_ids=(30,), start_us=EPOCH_US + 2_000_000)

    assert [record.index for record in select_records(session, selection)] == [8]
    assert observed_intervals(session, selection) == ()


@pytest.mark.parametrize("hidden_template", [0, 3])
def test_regression_hidden_by_source_or_type_filter_invalidates_the_crossing_interval(
    session, hidden_template
):
    result = _recording(session, [(2, 100), (hidden_template, 90), (2, 110), (2, 120)])
    selection = Selection(sources=((1, 1),), message_ids=(30,))

    assert [record.index for record in select_records(result, selection)] == [0, 2, 3]
    intervals = observed_intervals(result, selection)

    assert [(item.previous_index, item.index, item.delta_us) for item in intervals] == [
        (0, 2, None),
        (2, 3, 10),
    ]


def test_regression_hidden_by_time_filter_still_invalidates_the_crossing_interval(session):
    result = _recording(session, [(2, 100), (3, 90), (2, 110)])

    intervals = observed_intervals(result, Selection(start_us=100))

    assert [(item.previous_index, item.index, item.delta_us) for item in intervals] == [
        (0, 2, None)
    ]


def test_repeated_capture_time_is_zero_interval_without_inventing_a_positive_duration(session):
    result = _recording(session, [(2, 100), (2, 100)])

    intervals = observed_intervals(result, Selection())

    assert len(intervals) == 1
    assert intervals[0].delta_us == 0
    assert [issue.code for issue in result.issues] == ["timestamp_repeated"]


def test_unknown_messages_remain_selectable_but_their_intervals_are_unverified():
    frame = bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00")
    result = import_bytes(b"".join(timestamp.to_bytes(8, "big") + frame for timestamp in (10, 20)))
    selection = Selection(sources=((3, 1),), message_ids=(0xFFFFFF,))

    assert [record.index for record in select_records(result, selection)] == [0, 1]
    intervals = observed_intervals(result, selection)

    assert len(intervals) == 1
    assert (intervals[0].previous_index, intervals[0].index, intervals[0].delta_us) == (0, 1, None)


def test_selection_keeps_distinct_uint64_timestamps_beyond_float_precision(session):
    result = _recording(session, [(2, UINT64_MAX - 1), (2, UINT64_MAX)])

    selected = select_records(result, Selection(start_us=UINT64_MAX, end_us=UINT64_MAX))

    assert [record.index for record in selected] == [1]
    assert observed_intervals(result, Selection())[0].delta_us == 1


@pytest.mark.parametrize(
    "text, expected_offset",
    [
        ("0", 0),
        ("4", 4_000_000),
        ("0.000001", 1),
        ("-0.000001", -1),
        ("1.234567", 1_234_567),
        ("1.0000000", 1_000_000),
    ],
)
def test_relative_seconds_are_converted_to_exact_microseconds(text, expected_offset):
    assert seconds_to_timestamp(text, EPOCH_US) == EPOCH_US + expected_offset


@pytest.mark.parametrize("text", ["", "NaN", "Infinity", "-Infinity", "abc", "0.0000001"])
def test_invalid_or_sub_microsecond_times_are_rejected_instead_of_rounded(text):
    with pytest.raises(ValueError):
        seconds_to_timestamp(text, EPOCH_US)


@pytest.mark.parametrize("text, origin", [("-0.000001", 0), ("0.000001", UINT64_MAX)])
def test_relative_seconds_cannot_overflow_the_original_uint64_clock(text, origin):
    with pytest.raises(ValueError):
        seconds_to_timestamp(text, origin)


@pytest.mark.parametrize(
    "timestamp, origin, expected",
    [
        (EPOCH_US, EPOCH_US, "0"),
        (EPOCH_US + 1, EPOCH_US, "0.000001"),
        (EPOCH_US - 1, EPOCH_US, "-0.000001"),
        (EPOCH_US + 1_250_000, EPOCH_US, "1.25"),
        (UINT64_MAX, 0, "18446744073709.551615"),
    ],
)
def test_relative_timestamp_display_is_exact_and_round_trips(timestamp, origin, expected):
    assert timestamp_to_seconds(timestamp, origin) == expected
    assert seconds_to_timestamp(expected, origin) == timestamp


def test_decimal_context_cannot_round_microseconds_or_large_capture_times():
    with localcontext() as context:
        context.prec = 6
        assert seconds_to_timestamp("18446744073709.551615", 0) == UINT64_MAX
        assert timestamp_to_seconds(UINT64_MAX, 0) == "18446744073709.551615"
        assert seconds_to_timestamp("0.000001", UINT64_MAX - 1) == UINT64_MAX
