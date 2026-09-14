"""Activity aggregation retains all counts and does not round source timestamps."""

from dataclasses import replace
from pathlib import Path

from uav_debugger import import_file
from uav_debugger.charts import activity_chart

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
