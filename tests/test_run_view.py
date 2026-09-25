"""Directory isolation and the within-run observation timeline."""

from types import SimpleNamespace

import pytest

from uav_debugger.run_view import _seconds, run_activity_chart, uploaded_run_files
from uav_debugger.saved_run import GateInterval, Observation, TraceEvent


class Uploaded:
    def __init__(self, name, data=b"{}", size=None):
        self.name = name
        self.data = data
        self.size = len(data) if size is None else size
        self.reads = 0

    def getvalue(self):
        self.reads += 1
        return self.data


def test_directory_upload_preserves_nested_artifact_identity_and_original_bytes():
    data, name, ignored = uploaded_run_files(
        [Uploaded("baseline/run.json"), Uploaded("baseline/simulator/profile.parm", b"VALUE 1\n")]
    )
    assert name == "baseline"
    assert ignored == ()
    assert data == {"run.json": b"{}", "simulator/profile.parm": b"VALUE 1\n"}


def test_simulator_working_files_are_identified_without_reading_their_bytes():
    parameter = Uploaded("baseline/simulator/profile.parm", b"VALUE 1\n")
    extra = Uploaded("baseline/simulator/eeprom.bin", b"simulator state")
    contents, _, ignored = uploaded_run_files([Uploaded("baseline/run.json"), parameter, extra])
    assert contents == {"run.json": b"{}", "simulator/profile.parm": b"VALUE 1\n"}
    assert ignored == ("simulator/eeprom.bin",)
    assert parameter.reads == 1 and extra.reads == 0


@pytest.mark.parametrize(
    "names",
    [
        ["first/run.json", "second/run.json"],
        ["first/run.json", "second/receiver.tlog"],
        ["first/run.json", "first/run.json"],
        ["first/run.json", "first/../receiver.tlog"],
        ["first/run.json", "/etc/passwd"],
        ["first/run.json", "first\\receiver.tlog"],
        ["nested/first/run.json"],
        ["receiver.tlog"],
    ],
)
def test_ambiguous_or_unsafe_upload_names_reject_before_reading(names):
    uploaded = [Uploaded(name) for name in names]
    with pytest.raises(ValueError):
        uploaded_run_files(uploaded)
    assert all(item.reads == 0 for item in uploaded)


def test_total_size_and_file_count_are_checked_before_copying_uploaded_bytes():
    uploaded = [Uploaded("run.json", size=64 * 1024 * 1024 + 1)]
    with pytest.raises(ValueError, match="64 MiB"):
        uploaded_run_files(uploaded)
    assert uploaded[0].reads == 0
    with pytest.raises(ValueError, match="64 uploaded files"):
        uploaded_run_files([Uploaded(str(index)) for index in range(65)])


def test_timeline_counts_boundaries_keeps_startup_and_uses_monotonic_observations():
    origin = 123_000_000_000

    def event(action, elapsed, line):
        return TraceEvent(
            "actions.jsonl",
            line,
            {
                "action": action,
                "monotonic_ns": origin + elapsed,
                "elapsed_ns": elapsed,
                "unix_us": 1_700_000_000_000_000,
            },
            True,
        )

    actions = (
        event("producer_started", 0, 1),
        event("measurement_started", 1_000_000_000, 2),
        event("forwarding_disabled", 2_000_000_000, 3),
        event("forwarding_enabled", 4_000_000_000, 4),
        event("producer_stopped", 6_000_000_000, 5),
    )
    observations = tuple(
        Observation(
            "observations.jsonl",
            index + 1,
            {
                "point": point,
                "record_index": index,
                "elapsed_ns": index * 20_000_000,
                "monotonic_ns": origin + index * 20_000_000,
                # A regressing wall clock must not move these monotonic bins.
                "unix_us": 1_700_000_000_000_000 - index,
            },
            index != 150,
        )
        for point in ("relay-input", "receiver")
        for index in range(301)
    )
    run = SimpleNamespace(
        observations=observations,
        actions=actions,
        origin_monotonic_ns=origin,
        measurement_monotonic_ns=origin + 1_000_000_000,
        gate_intervals=(GateInterval(actions[2], actions[3]),),
    )
    chart = run_activity_chart(run)
    for trace in chart.data:
        assert len(trace.y) <= 200
        assert sum(trace.y) == 300
        assert trace.y[0] > 0 and trace.y[-1] > 0
    spans = {(shape.x0, shape.x1) for shape in chart.layout.shapes}
    assert (0, 1.0) in spans  # Recorded startup, not a shifted first record.
    assert (2.0, 4.0) in spans  # Applied two-second gate.
    assert "monotonic" in chart.layout.xaxis.title.text


def test_an_open_gate_is_a_start_marker_without_fabricated_duration():
    start = TraceEvent("actions.jsonl", 1, {"elapsed_ns": 100, "monotonic_ns": 200}, True)
    run = SimpleNamespace(
        observations=(),
        actions=(start,),
        origin_monotonic_ns=100,
        measurement_monotonic_ns=None,
        gate_intervals=(GateInterval(start, None),),
    )
    chart = run_activity_chart(run)
    assert all(shape.type == "line" and shape.x0 == shape.x1 for shape in chart.layout.shapes)


@pytest.mark.parametrize("value", [None, "20", [], {}, True])
def test_invalid_trace_time_is_not_formatted_as_an_established_duration(value):
    assert _seconds(value) == "Unavailable"
