"""Exact window input and shared plot bins preserve comparison evidence."""

import pytest
from test_comparison import fixture_files

from uav_debugger.comparison import compare_runs
from uav_debugger.comparison_view import comparison_activity_chart, window_nanoseconds
from uav_debugger.saved_run import load_run_files


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0", 0),
        ("0.000000001", 1),
        ("1.23456789", 1_234_567_890),
        ("1.0000000000", 1_000_000_000),
        ("60", 60_000_000_000),
    ],
)
def test_window_seconds_keep_exact_nanoseconds(value, expected):
    assert window_nanoseconds(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "NaN",
        "Infinity",
        "-0.1",
        "60.000000001",
        "0.0000000001",
        "abc",
        "1.0000000000000000000000000001",
        "59.9999999999999999999999999999",
    ],
)
def test_invalid_or_more_precise_seconds_are_not_silently_rounded(value):
    with pytest.raises(ValueError):
        window_nanoseconds(value)


def test_shared_bins_count_every_selected_reference_and_keep_actual_gate_position():
    runs = [
        load_run_files(fixture_files(role, version=2, wall_regresses=True))
        for role in ("baseline", "blackout")
    ]
    comparison = compare_runs(*runs, start_ns=100_000_000, end_ns=2_500_000_000)
    assert comparison.comparable, comparison.issues
    chart = comparison_activity_chart(comparison, "receiver")
    assert {trace.name: sum(trace.y) for trace in chart.data} == {"Baseline": 4, "Blackout": 1}
    assert chart.data[0].x == chart.data[1].x
    assert len(chart.data[0].x) <= 200
    assert tuple(chart.layout.xaxis.range) == (0.1, 2.5)
    assert any(shape.x0 == 0.2 and shape.x1 == 2.2 for shape in chart.layout.shapes)
    assert "each run's measurement start" in chart.layout.xaxis.title.text
    assert chart.layout.title.text == "Receiver observation activity"


def test_blocked_evidence_never_draws_zero_filled_metrics():
    baseline = load_run_files(fixture_files("baseline"))
    damaged = fixture_files("blackout")
    del damaged["observations.jsonl"]
    comparison = compare_runs(baseline, load_run_files(damaged))
    assert not comparison.comparable
    assert len(comparison_activity_chart(comparison, "receiver").data) == 0
