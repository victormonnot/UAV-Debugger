"""Activity aggregation retains all counts and does not round source timestamps."""

from dataclasses import replace
from pathlib import Path

import pytest

from uav_debugger import import_bytes, import_file
from uav_debugger.analysis import Selection
from uav_debugger.charts import activity_chart, attitude_chart
from uav_debugger.telemetry import build_attitude_view

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"


def test_activity_preserves_count_and_shows_empty_bins_between_selected_observations():
    result = import_file(FIXTURE)
    selected = tuple(r for r in result.records if r.system_id == 1 and r.message_id == 30)
    plot = activity_chart(selected, origin_us=result.records[0].timestamp_us)
    counts = list(plot.data[0].y)
    assert sum(counts) == 2
    assert counts[0] == counts[-1] == 1
    assert any(count == 0 for count in counts[1:-1])
    assert len(counts) <= 200


def test_activity_keeps_distinct_microseconds_at_uint64_limit():
    record = import_file(FIXTURE).records[0]
    origin = 2**64 - 3
    records = tuple(replace(record, timestamp_us=origin + i) for i in range(3))
    plot = activity_chart(records, origin_us=origin)
    assert list(plot.data[0].y) == [1, 1, 1]
    assert list(plot.data[0].customdata[0]) == ["0", "0"]
    assert list(plot.data[0].customdata[-1]) == ["0.000002", "0.000002"]


def test_activity_aggregates_same_time_without_discarding_records():
    record = import_file(FIXTURE).records[0]
    plot = activity_chart((record,) * 2000, origin_us=record.timestamp_us)
    assert list(plot.data[0].y) == [2000]


def test_empty_activity_has_no_invented_observations():
    assert not activity_chart((), origin_us=0).data


def _attitude_recording(entries):
    """Import existing independent fixture frames with explicit capture clocks."""
    session = import_file(FIXTURE)
    return import_bytes(
        b"".join(
            timestamp.to_bytes(8, "big") + session.records[index].raw_frame
            for index, timestamp in entries
        )
    )


def test_attitude_gap_is_display_only_and_points_keep_original_evidence_references():
    result = import_file(FIXTURE)
    selection = Selection(sources=((1, 1),))
    view = build_attitude_view(result, selection)

    plot = attitude_chart(view, origin_us=result.records[0].timestamp_us)

    assert [trace.name for trace in plot.data] == ["Roll", "Pitch", "Yaw"]
    for trace, field in zip(plot.data, ("roll", "pitch", "yaw"), strict=True):
        assert list(trace.x) == [1.0, None, 5.0]
        assert list(trace.y) == [
            result.records[2].fields[field],
            None,
            result.records[8].fields[field],
        ]
        assert trace.connectgaps is False
        assert list(trace.customdata[0]) == [
            2,
            "1700000001000000",
            "1",
            "1 / 1",
            54,
            62,
            90,
        ]
        assert trace.customdata[1] is None
        assert trace.customdata[2][0] == 8
    connected = attitude_chart(
        build_attitude_view(result, selection, max_gap_us=4_000_000),
        origin_us=result.records[0].timestamp_us,
    )
    assert list(connected.data[0].y) == [0.25, 0.25]
    assert [sample.record.index for sample in view.samples] == [2, 8]


def test_attitude_global_regression_breaks_the_line_even_when_regressing_record_is_hidden():
    result = _attitude_recording([(2, 100), (1, 90), (2, 110), (2, 120)])
    view = build_attitude_view(result, Selection(sources=((1, 1),), message_ids=(30,)))

    plot = attitude_chart(view, origin_us=100)

    assert list(plot.data[0].y) == [0.25, None, 0.25, 0.25]
    assert [item[0] for item in plot.data[0].customdata if item is not None] == [0, 2, 3]


def test_repeated_capture_times_retain_both_markers_without_a_connecting_line():
    result = _attitude_recording([(2, 100), (2, 100), (2, 110)])

    plot = attitude_chart(build_attitude_view(result, Selection()), origin_us=100)

    assert list(plot.data[0].x) == [0.0, None, 0.0, 0.00001]
    assert list(plot.data[0].y) == [0.25, None, 0.25, 0.25]
    assert [item[0] for item in plot.data[0].customdata if item is not None] == [0, 1, 2]


def test_missing_field_breaks_only_its_own_line_without_hiding_other_fields():
    result = _attitude_recording([(2, 100), (2, 110), (2, 120)])
    records = list(result.records)
    records[1] = replace(records[1], fields=dict(records[1].fields, roll=float("nan")))
    result = replace(result, records=tuple(records))

    plot = attitude_chart(build_attitude_view(result, Selection()), origin_us=100)

    assert list(plot.data[0].y) == [0.25, None, 0.25]
    assert list(plot.data[1].y) == [-0.5, -0.5, -0.5]
    assert list(plot.data[2].y) == [1.0, 1.0, 1.0]
    assert [item[0] for item in plot.data[0].customdata if item is not None] == [0, 2]
    assert [item[0] for item in plot.data[1].customdata] == [0, 1, 2]


def test_angle_wrap_breaks_the_line_without_unwrapping_or_modifying_original_radians():
    result = _attitude_recording([(2, 100), (2, 110), (2, 120)])
    result = replace(
        result,
        records=tuple(
            replace(record, fields=dict(record.fields, yaw=value))
            for record, value in zip(result.records, (2.9, -2.9, -2.8), strict=True)
        ),
    )

    plot = attitude_chart(build_attitude_view(result, Selection()), origin_us=100)

    assert list(plot.data[2].y) == [2.9, None, -2.9, -2.8]
    assert list(plot.data[0].y) == [0.25, 0.25, 0.25]
    assert [record.fields["yaw"] for record in result.records] == [2.9, -2.9, -2.8]


def test_attitude_point_metadata_keeps_exact_uint64_timestamps():
    origin = 2**64 - 3
    result = _attitude_recording([(2, origin), (2, origin + 1), (2, origin + 2)])

    plot = attitude_chart(build_attitude_view(result, Selection()), origin_us=origin)

    assert list(plot.data[0].x) == [0.0, 0.000001, 0.000002]
    assert [item[1] for item in plot.data[0].customdata] == [str(origin + i) for i in range(3)]
    assert [item[2] for item in plot.data[0].customdata] == ["0", "0.000001", "0.000002"]
    assert '"18446744073709551615"' in plot.to_json()


@pytest.mark.parametrize("status", ["empty", "multiple_sources", "too_many"])
def test_attitude_chart_rejects_views_that_require_a_different_selection(status):
    view = build_attitude_view(import_file(FIXTURE), Selection())

    with pytest.raises(ValueError, match="ready single-source"):
        attitude_chart(replace(view, status=status), origin_us=0)
