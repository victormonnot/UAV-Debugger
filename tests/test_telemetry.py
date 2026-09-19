"""Selected ATTITUDE observations preserve source evidence and display coverage."""

import math
from dataclasses import replace
from pathlib import Path

import pytest

from uav_debugger import import_bytes, import_file
from uav_debugger.analysis import Selection
from uav_debugger.telemetry import (
    ATTITUDE_POINT_LIMIT,
    attitude_plot_summary,
    build_attitude_view,
)

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"


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


def test_selected_samples_retain_record_identity_original_order_and_radians(session):
    selection = Selection(sources=((1, 1),), message_ids=(30,))

    view = build_attitude_view(session, selection)

    assert view.status == "ready"
    assert view.sources == ((1, 1),)
    assert view.source == (1, 1)
    assert view.record_count == 2
    assert [sample.record.index for sample in view.samples] == [2, 8]
    assert all(sample.record is session.records[sample.record.index] for sample in view.samples)
    assert [(sample.roll, sample.pitch, sample.yaw) for sample in view.samples] == [
        (0.25, -0.5, 1.0),
        (0.25, -0.5, 1.0),
    ]


@pytest.mark.parametrize(
    "selection",
    [
        Selection(sources=()),
        Selection(sources=((1, 2),)),
        Selection(message_ids=(0,)),
        Selection(message_ids=()),
        Selection(end_us=100),
    ],
)
def test_filters_can_explicitly_exclude_attitude(session, selection):
    view = build_attitude_view(session, selection)

    assert view.status == "empty"
    assert view.source is None
    assert view.sources == view.samples == ()
    assert view.record_count == 0


def test_time_bounds_are_inclusive_and_do_not_sort_regressing_records(session):
    result = _recording(session, [(2, 120), (2, 110), (2, 115), (2, 125)])

    view = build_attitude_view(result, Selection(start_us=110, end_us=120))

    assert [sample.record.timestamp_us for sample in view.samples] == [120, 110, 115]
    assert [sample.segment for sample in view.samples] == [0, 1, 1]


@pytest.mark.parametrize("hidden_template", [0, 3])
def test_regression_hidden_by_source_or_type_still_separates_samples(session, hidden_template):
    result = _recording(session, [(2, 100), (hidden_template, 90), (2, 110), (2, 120)])

    view = build_attitude_view(result, Selection(sources=((1, 1),), message_ids=(30,)))

    assert [sample.record.index for sample in view.samples] == [0, 2, 3]
    assert [sample.segment for sample in view.samples] == [0, 1, 1]


def test_regression_hidden_by_time_still_separates_samples(session):
    result = _recording(session, [(2, 100), (3, 90), (2, 110)])

    view = build_attitude_view(result, Selection(start_us=100))

    assert view.source == (1, 1)
    assert [sample.segment for sample in view.samples] == [0, 1]


def test_multiple_attitude_sources_require_an_explicit_selection(session):
    view = build_attitude_view(session, Selection())

    assert view.status == "multiple_sources"
    assert view.sources == ((1, 1), (2, 1))
    assert view.source is None
    assert view.record_count == 7
    assert view.samples == ()


def test_other_message_sources_do_not_prevent_a_single_attitude_plot(session):
    result = _recording(session, [(2, 100), (1, 110), (2, 120)])

    view = build_attitude_view(result, Selection())

    assert view.status == "ready"
    assert view.sources == ((1, 1),)
    assert view.record_count == len(view.samples) == 2


@pytest.mark.parametrize("count", [ATTITUDE_POINT_LIMIT, ATTITUDE_POINT_LIMIT + 1])
def test_point_limit_is_explicit_without_silently_discarding_selected_records(session, count):
    result = _recording(session, ((2, index) for index in range(count)))

    view = build_attitude_view(result, Selection())

    assert view.record_count == count
    assert view.point_limit == ATTITUDE_POINT_LIMIT
    if count == ATTITUDE_POINT_LIMIT:
        assert view.status == "ready"
        assert len(view.samples) == count
        assert view.samples[-1].record is result.records[-1]
    else:
        assert view.status == "too_many"
        assert view.samples == ()
        narrower = build_attitude_view(result, Selection(start_us=count - 2))
        assert narrower.status == "ready"
        assert [sample.record.index for sample in narrower.samples] == [count - 2, count - 1]


@pytest.mark.parametrize("invalid", [None, "0.5", True, math.nan, math.inf, -math.inf, 2**2000])
def test_invalid_fields_remain_unplotted_without_changing_original_values(session, invalid):
    original = session.records[2]
    fields = dict(original.fields, roll=invalid)
    record = replace(original, fields=fields)
    result = replace(session, records=(record,))

    view = build_attitude_view(result, Selection())

    assert view.samples[0].roll is None
    assert (view.samples[0].pitch, view.samples[0].yaw) == (-0.5, 1.0)
    assert record.fields["roll"] is invalid
    assert record.raw_frame is original.raw_frame
    assert attitude_plot_summary(view)["invalid_value_counts"] == {
        "roll": 1,
        "pitch": 0,
        "yaw": 0,
    }


@pytest.mark.parametrize(
    "changes",
    [{"fields": None}, {"checksum_status": "unverified"}, {"message_name": None}],
)
def test_unverified_or_undecoded_records_have_no_invented_plottable_values(session, changes):
    result = replace(session, records=(replace(session.records[2], **changes),))

    view = build_attitude_view(result, Selection())

    assert view.status == "ready"
    assert (view.samples[0].roll, view.samples[0].pitch, view.samples[0].yaw) == (None, None, None)
    assert attitude_plot_summary(view)["plotted_record_count"] == 0


def test_summary_distinguishes_records_with_partial_fields_and_no_plottable_fields(session):
    base = session.records[2]
    fields = dict(base.fields)
    del fields["pitch"]
    fields["roll"] = math.nan
    result = replace(
        session,
        records=(base, replace(base, fields=fields), replace(base, fields=None)),
    )

    summary = attitude_plot_summary(build_attitude_view(result, Selection(), max_gap_us=123_456))

    assert summary == {
        "status": "ready",
        "sources": [[1, 1]],
        "source": [1, 1],
        "record_count": 3,
        "plotted_record_count": 2,
        "point_limit": ATTITUDE_POINT_LIMIT,
        "max_gap_us": 123_456,
        "unit": "rad",
        "fields": ["roll", "pitch", "yaw"],
        "invalid_value_counts": {"roll": 2, "pitch": 2, "yaw": 1},
    }


def test_unrendered_summary_does_not_claim_value_validation(session):
    summary = attitude_plot_summary(build_attitude_view(session, Selection()))

    assert summary["record_count"] == 7
    assert summary["plotted_record_count"] == 0
    assert summary["invalid_value_counts"] is None


@pytest.mark.parametrize("invalid_gap", [0, -1, True, 1.0, "1000", None])
def test_line_gap_requires_positive_integer_microseconds(session, invalid_gap):
    with pytest.raises(ValueError, match="positive integer"):
        build_attitude_view(session, Selection(), max_gap_us=invalid_gap)
