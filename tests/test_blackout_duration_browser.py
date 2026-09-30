"""Requested blackout duration reaches execution and stays distinct from observations."""

import json
import time

import pytest
from test_analyze_browser import download_blocks, expect_metric, wait_for_render
from test_comparison_browser import chart_data
from test_experiment_browser import analyze_server as analyze_server
from test_experiment_browser import (
    await_finished,
    open_experiment,
    read_manifest,
    run_directories,
    select_option,
    set_number,
    settle_controls,
    start_run,
)
from test_experiment_browser import experiment_root as experiment_root
from test_experiment_browser import experiment_server as experiment_server
from test_saved_run_browser import await_saved_capture, run_report_block

from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser


def controller_state(directory):
    return json.loads((directory.parent / "control.json").read_text())


def wait_for_closed_gate(directory):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        actions = []
        for line in (directory / "actions.jsonl").read_text().splitlines():
            try:
                actions.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # The last concurrently appended line may be incomplete.
        closed = [event for event in actions if event.get("action") == "forwarding_disabled"]
        opened = [event for event in actions if event.get("action") == "forwarding_enabled"]
        if closed:
            assert not opened, "The Stop request must occur while the requested gate is active."
            return closed[0]
        time.sleep(0.025)
    pytest.fail("No forwarding_disabled action arrived before the Stop check.")


def test_custom_duration_reaches_worker_saved_evidence_and_comparison_report(
    analyze_page, experiment_root, tmp_path
):
    page, expect = analyze_page
    open_experiment(page, expect)
    duration_control = page.get_by_role("spinbutton", name="Blackout duration (s)", exact=True)
    expect(duration_control).to_be_disabled()
    assert float(duration_control.input_value()) == 2.0
    baseline_path = start_run(page, expect, experiment_root)
    await_finished(page, expect)
    page.get_by_role("button", name="Use as baseline", exact=True).click()
    settle_controls(page)

    blackout_path = start_run(
        page, expect, experiment_root, scenario="Blackout", blackout_duration=0.5
    )
    await_finished(page, expect)
    control = controller_state(blackout_path)
    manifest = read_manifest(blackout_path)
    assert control["requested"]["blackout_duration_s"] == 0.5
    assert manifest["requested"]["blackout_duration_s"] == 0.5
    baseline, blackout = [load_run_directory(path) for path in (baseline_path, blackout_path)]
    assert blackout.evidence_status == "consistent", blackout.issues
    assert len(blackout.gate_intervals) == 1
    gate = blackout.gate_intervals[0]
    assert 500_000_000 <= gate.duration_ns < 2_000_000_000
    assert gate.end.data["reason"] == "blackout_elapsed"
    assert manifest["counters"]["relay_dropped"] > 0
    page.get_by_role("button", name="Use as blackout", exact=True).click()
    settle_controls(page)
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, blackout_path)
    saved_report = tmp_path / "half-second-saved.md"
    saved_blocks = download_blocks(page, saved_report)
    assert run_report_block(saved_blocks, "requested")["blackout_duration_s"] == 0.5
    applied = run_report_block(saved_blocks, "applied_actions")["intervals"][0]
    assert applied["duration_ns"] == gate.duration_ns
    assert applied["start"]["monotonic_ns"] == gate.start.monotonic_ns
    assert applied["end"]["monotonic_ns"] == gate.end.monotonic_ns
    assert blackout.identity in saved_report.read_text()

    open_experiment(page, expect)
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    comparison = compare_runs(baseline, blackout)
    assert comparison.comparable, comparison.issues
    chart = chart_data(page)
    assert {trace["name"].lower(): sum(trace["y"]) for trace in chart["traces"]} == {
        role: comparison.metrics[role]["receiver"].count for role in ("baseline", "blackout")
    }
    comparison_report = tmp_path / "half-second-comparison.md"
    blocks = download_blocks(page, comparison_report)
    assert run_report_block(blocks, "blackout_requested")["blackout_duration_s"] == 0.5
    assert run_report_block(blocks, "comparison_status")["comparable"] is True
    assert (
        run_report_block(blocks, "blackout_applied_actions")["intervals"][0]["duration_ns"]
        == gate.duration_ns
    )
    differences = run_report_block(blocks, "comparison_differences")["items"]
    difference = next(
        item for item in differences if item["field"] == "requested.blackout_duration_s"
    )
    assert difference["baseline"] is None
    assert difference["blackout"] == 0.5
    assert difference["blocking"] is False
    for run in (baseline, blackout):
        assert run.identity in comparison_report.read_text()
        assert all(
            capture.sha256 in comparison_report.read_text() for capture in run.captures.values()
        )
    assert len(run_directories(experiment_root)) == 2


def test_stop_during_long_custom_gate_preserves_shorter_interval_and_blocks_comparison(
    analyze_page, experiment_root, tmp_path
):
    page, expect = analyze_page
    open_experiment(page, expect)
    start_run(page, expect, experiment_root)
    await_finished(page, expect)
    page.get_by_role("button", name="Use as baseline", exact=True).click()
    settle_controls(page)
    directory = start_run(
        page,
        expect,
        experiment_root,
        duration=15,
        scenario="Blackout",
        blackout_duration=10,
    )
    disabled = wait_for_closed_gate(directory)
    page.get_by_role("button", name="Stop experiment", exact=True).click()
    await_finished(page, expect, outcome="interrupted")
    control = controller_state(directory)
    run = load_run_directory(directory)
    assert run.requested["blackout_duration_s"] == 10
    assert control["requested"]["blackout_duration_s"] == 10
    assert run.evidence_status == "consistent", run.issues
    assert len(run.gate_intervals) == 1
    gate = run.gate_intervals[0]
    assert gate.start.monotonic_ns == disabled["monotonic_ns"]
    assert 0 < gate.duration_ns < 10_000_000_000
    assert gate.end.data["reason"] == "shutdown"
    assert control["stop_requested"]["monotonic_ns"] <= gate.end.monotonic_ns
    page.get_by_role("button", name="Use as blackout", exact=True).click()
    settle_controls(page)
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, directory)
    expect_metric(page, expect, "Declared outcome", "interrupted")
    saved_blocks = download_blocks(page, tmp_path / "interrupted-custom-saved.md")
    assert run_report_block(saved_blocks, "requested")["blackout_duration_s"] == 10
    assert (
        run_report_block(saved_blocks, "applied_actions")["intervals"][0]["duration_ns"]
        == gate.duration_ns
    )

    open_experiment(page, expect)
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison unavailable", exact=True)).to_be_visible()
    wait_for_render(page)
    blocks = download_blocks(page, tmp_path / "interrupted-custom-comparison.md")
    assert run_report_block(blocks, "blackout_requested")["blackout_duration_s"] == 10
    assert run_report_block(blocks, "comparison_metrics")["available"] is False
    assert any(
        reason["code"] == "run_not_completed" and reason["run_role"] == "blackout"
        for reason in run_report_block(blocks, "comparison_status")["reasons"]
    )
    assert (
        run_report_block(blocks, "blackout_applied_actions")["intervals"][0]["duration_ns"]
        == gate.duration_ns
    )


def test_invalid_custom_window_creates_no_output_and_baseline_ignores_disabled_value(
    analyze_page, experiment_root
):
    page, expect = analyze_page
    open_experiment(page, expect)
    select_option(page, expect, "Scenario", "Blackout")
    set_number(page, "Duration (s)", 3)
    set_number(page, "Blackout start (s)", 0.4)
    set_number(page, "Blackout duration (s)", 3)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    expect(
        page.get_by_test_id("stAlert").filter(has_text="before and after its requested window")
    ).to_be_visible()
    settle_controls(page)
    assert not experiment_root.exists()

    select_option(page, expect, "Scenario", "Baseline")
    duration_control = page.get_by_role("spinbutton", name="Blackout duration (s)", exact=True)
    expect(duration_control).to_be_disabled()
    assert float(duration_control.input_value()) == 3.0
    directory = start_run(page, expect, experiment_root, duration=0.3)
    await_finished(page, expect)
    for requested in (
        read_manifest(directory)["requested"],
        controller_state(directory)["requested"],
    ):
        assert requested["scenario"] == "baseline"
        assert requested["blackout_duration_s"] is None
        assert requested["blackout_at_s"] is None
    assert not load_run_directory(directory).gate_intervals
    assert len(run_directories(experiment_root)) == 1
