"""Explicit local browser execution, shutdown, and offline evidence handoff."""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from test_analyze_browser import download_blocks, expect_metric, wait_for_render
from test_comparison_browser import chart_data
from test_saved_run_browser import await_saved_capture, run_report_block

from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def experiment_root(tmp_path):
    return tmp_path / "experiment-output"


@pytest.fixture
def experiment_server():
    return {}


@pytest.fixture
def analyze_server(experiment_root, experiment_server, tmp_path):
    """Give each workflow a real server with an explicit private output directory."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    command = [
        sys.executable,
        *experiment_server.get("launcher", ["-m", "uav_debugger.analyze"]),
        "--port",
        str(port),
        "--experiment-root",
        str(experiment_root),
    ]
    if os.environ.get("UAV_DEBUGGER_SITL_BINARY"):
        command.extend(["--sitl-binary", os.environ["UAV_DEBUGGER_SITL_BINARY"]])
    log_path = tmp_path / "streamlit.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        experiment_server["process"] = process
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Application exited:\n{log_path.read_text(errors='replace')}")
                try:
                    with urlopen(f"{base_url}/_stcore/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(f"Application did not start:\n{log_path.read_text(errors='replace')}")
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def settle_controls(page):
    """Await completed widget replacement; Experiment has no report while running."""
    # Locator retries eventually poll every 500 ms, the same period as the
    # history fragment. Check each animation frame so short idle windows are
    # not repeatedly missed while the fragment keeps refreshing.
    page.wait_for_function(
        """() => document.querySelector(
            '[data-testid="stApp"][data-test-script-state="notRunning"]'
        )?.checkVisibility()"""
    )
    page.wait_for_function("!document.querySelector('[data-testid=stSkeleton]')")


def select_option(page, expect, label, value):
    settle_controls(page)
    control = page.get_by_role("combobox", name=label, exact=True)
    control.scroll_into_view_if_needed()
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
    control.fill(value)
    control.press("ArrowDown")
    page.get_by_role("option", name=value, exact=True).click()
    expect(control).to_have_value(value)
    settle_controls(page)


def set_number(page, label, value):
    control = page.get_by_role("spinbutton", name=label, exact=True)
    control.fill(str(value))
    control.press("Tab")
    settle_controls(page)


def open_experiment(page, expect):
    page.get_by_test_id("stRadio").get_by_text("Experiment", exact=True).click()
    expect(page.get_by_role("heading", name="Experiment", exact=True)).to_be_visible()
    settle_controls(page)


def run_directories(root):
    return {path.parent for path in root.glob("*/evidence/run.json")}


def wait_for_new_run(root, before):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        created = run_directories(root) - before
        if created:
            assert len(created) == 1, "One Start action must create exactly one run."
            return created.pop()
        time.sleep(0.05)
    pytest.fail("Start did not create an experiment manifest.")


def start_run(
    page,
    expect,
    root,
    *,
    duration=3.0,
    scenario="Baseline",
    source="Synthetic",
    blackout_duration=None,
):
    select_option(page, expect, "Experiment source", source)
    select_option(page, expect, "Scenario", scenario)
    set_number(page, "Duration (s)", duration)
    if scenario == "Blackout":
        set_number(page, "Blackout start (s)", 0.4)
        if blackout_duration is not None:
            set_number(page, "Blackout duration (s)", blackout_duration)
    before = run_directories(root)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    directory = wait_for_new_run(root, before)
    # Disk creation precedes rendering. Completion must belong to this run,
    # rather than a finished predecessor still visible during the Start rerun.
    expected_id = f"Run ID: {directory.parent.name}"
    expect(page.get_by_text(expected_id, exact=True)).to_have_text([expected_id])
    settle_controls(page)
    return directory


def await_finished(page, expect, outcome="completed"):
    expect_metric(page, expect, "Process state", "finished", timeout=30_000)
    expect_metric(page, expect, "Declared outcome", outcome)
    settle_controls(page)
    expect(page.get_by_role("button", name="Start experiment", exact=True)).to_be_enabled()


def read_manifest(directory):
    return json.loads((directory / "run.json").read_text())


def host_pid_for_argv(arguments):
    """Find this run's process using its exact unique profile path in argv.

    The simulator PID saved inside the worker's PID namespace is not a host PID.
    """
    expected = b"\0".join(os.fsencode(argument) for argument in arguments) + b"\0"
    matches = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if path.read_bytes() == expected:
                matches.append(int(path.parent.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    assert len(matches) == 1, f"Expected one owned simulator, found {matches}"
    return matches[0]


def wait_for_measurement(directory):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        manifest = read_manifest(directory)
        path = directory / "receiver.tlog"
        actions_path = directory / "actions.jsonl"
        action_lines = actions_path.read_text().splitlines() if actions_path.exists() else []
        # The initial manifest is a snapshot. Read flushed lifecycle evidence to
        # distinguish SITL readiness from telemetry received during warmup.
        actions = []
        for line in action_lines:
            try:
                actions.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # A concurrently appended final line may be incomplete.
        started_action = (
            "measurement_started"
            if manifest["requested"]["source"] == "arducopter-sitl"
            else "producer_started"
        )
        if (
            any(action.get("action") == started_action for action in actions)
            and path.exists()
            and path.stat().st_size
        ):
            return manifest
        if manifest.get("outcome") == "failed":
            pytest.fail(f"Experiment failed: {manifest.get('error')}")
        time.sleep(0.05)
    pytest.fail("No observed receiver evidence arrived after measurement started.")


def test_start_baseline_and_blackout_then_compare_observed_evidence(
    analyze_page, experiment_root, tmp_path
):
    page, expect = analyze_page
    assert not run_directories(experiment_root)
    open_experiment(page, expect)
    baseline = start_run(page, expect, experiment_root)
    await_finished(page, expect)
    page.get_by_role("button", name="Use as baseline", exact=True).click()
    settle_controls(page)
    blackout = start_run(page, expect, experiment_root, scenario="Blackout")
    await_finished(page, expect)
    page.get_by_role("button", name="Use as blackout", exact=True).click()
    settle_controls(page)
    before, after = load_run_directory(baseline), load_run_directory(blackout)
    comparison = compare_runs(before, after)
    assert comparison.comparable, comparison.issues
    assert after.manifest["counters"]["relay_dropped"] > 0
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_role("heading", name="Compare experiments", exact=True)).to_be_visible()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    chart = chart_data(page)
    assert {trace["name"].lower(): sum(trace["y"]) for trace in chart["traces"]} == {
        role: comparison.metrics[role]["receiver"].count for role in ("baseline", "blackout")
    }
    report_path = tmp_path / "live-to-comparison.md"
    download_blocks(page, report_path)
    report = report_path.read_text()
    for run in (before, after):
        assert run.identity in report
        assert all(capture.sha256 in report for capture in run.captures.values())
    assert "forwarding_disabled" in report and "forwarding_enabled" in report
    assert len(run_directories(experiment_root)) == 2


def test_stop_preserves_interrupted_evidence_and_opens_it_in_analyze(
    analyze_page, experiment_root, tmp_path
):
    page, expect = analyze_page
    open_experiment(page, expect)
    directory = start_run(page, expect, experiment_root, duration=30)
    wait_for_measurement(directory)
    page.get_by_role("button", name="Stop experiment", exact=True).click()
    await_finished(page, expect, outcome="interrupted")
    manifest = read_manifest(directory)
    assert manifest["outcome"] == "interrupted"
    assert manifest["end"]["monotonic_ns"] - manifest["start"]["monotonic_ns"] < 30_000_000_000
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    captured = await_saved_capture(page, expect, directory)
    assert captured.records
    expect_metric(page, expect, "Declared outcome", "interrupted")
    report_path = tmp_path / "interrupted.md"
    blocks = download_blocks(page, report_path)
    assert run_report_block(blocks, "status")["declared_outcome"] == "interrupted"
    assert (
        run_report_block(blocks, "run_provenance")["files"]["relay-input.tlog"]["sha256"]
        == captured.sha256
    )
    assert len(run_directories(experiment_root)) == 1


def test_invalid_blackout_configuration_creates_no_run(analyze_page, experiment_root):
    page, expect = analyze_page
    open_experiment(page, expect)
    select_option(page, expect, "Scenario", "Blackout")
    set_number(page, "Duration (s)", 1)
    set_number(page, "Blackout start (s)", 0.4)
    page.get_by_role("button", name="Start experiment", exact=True).click()
    expect(
        page.get_by_test_id("stAlert").filter(has_text=re.compile("blackout", re.I))
    ).to_be_visible()
    settle_controls(page)
    assert not run_directories(experiment_root)
    assert not experiment_root.exists() or not list(experiment_root.iterdir())


def test_shared_server_prevents_concurrent_starts_and_analyze_never_restarts_run(
    analyze_page, analyze_server, experiment_root
):
    page, expect = analyze_page
    open_experiment(page, expect)
    second = page.context.new_page()
    try:
        second.goto(analyze_server, wait_until="domcontentloaded")
        expect(second.get_by_role("heading", name="Analyze", exact=True)).to_be_visible()
        open_experiment(second, expect)
        expect_metric(second, expect, "Process state", "idle")
        # An already-open idle peer must discover another tab's launch without
        # a manual refresh or another Start request.
        directory = start_run(page, expect, experiment_root, duration=30)
        wait_for_measurement(directory)
        expect(second.get_by_role("button", name="Start experiment", exact=True)).to_be_disabled()
        expect_metric(second, expect, "Process state", re.compile("starting|running"))
        expect(second.get_by_role("button", name="Stop experiment", exact=True)).to_be_visible()
        second.get_by_test_id("stRadio").get_by_text("Analyze", exact=True).click()
        expect(second.get_by_role("heading", name="Analyze", exact=True)).to_be_visible()
        second.get_by_role("button", name="Load example", exact=True).click()
        expect_metric(second, expect, "Imported records", "12")
        wait_for_render(second)
        page.get_by_test_id("stRadio").get_by_text("Analyze", exact=True).click()
        expect(page.get_by_role("heading", name="Analyze", exact=True)).to_be_visible()
        open_experiment(page, expect)
        expect(page.get_by_role("button", name="Start experiment", exact=True)).to_be_disabled()
        assert run_directories(experiment_root) == {directory}
        page.get_by_role("button", name="Stop experiment", exact=True).click()
        await_finished(page, expect, outcome="interrupted")
        open_experiment(second, expect)
        await_finished(second, expect, outcome="interrupted")
        assert run_directories(experiment_root) == {directory}
        expect(second.get_by_test_id("stException")).to_have_count(0)
    finally:
        second.close()


def test_stopping_server_finalizes_active_run_and_reaps_owned_worker(
    analyze_page, experiment_root, experiment_server
):
    page, expect = analyze_page
    open_experiment(page, expect)
    directory = start_run(page, expect, experiment_root, duration=30)
    wait_for_measurement(directory)
    matches = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = path.read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"uav_debugger.experiment_worker" in arguments and os.fsencode(directory) in arguments:
            matches.append(path.parent)
    assert len(matches) == 1
    process = experiment_server["process"]
    process.terminate()
    process.wait(timeout=10)
    assert not matches[0].exists(), "Server shutdown must reap its owned worker."
    manifest = read_manifest(directory)
    assert manifest["outcome"] == "interrupted"
    assert manifest["end"] is not None
    run = load_run_directory(directory)
    assert run.evidence_status == "consistent", run.issues
    assert run.captures["receiver"].records
    control = json.loads((directory.parent / "control.json").read_text())
    assert control["state"] == "finished"
    assert control["stop_reason"] == "controller_shutdown"
    assert control["forced_termination"] is False


@pytest.mark.skipif(
    not os.environ.get("UAV_DEBUGGER_SITL_BINARY"),
    reason="Set UAV_DEBUGGER_SITL_BINARY to verify the pinned native Experiment UI",
)
def test_opt_in_sitl_start_stop_isolated_worker_and_open_observed_capture(
    analyze_page, experiment_root, tmp_path
):
    page, expect = analyze_page
    open_experiment(page, expect)
    directory = start_run(page, expect, experiment_root, duration=30, source="ArduCopter SITL")
    active = wait_for_measurement(directory)
    simulator_pid = host_pid_for_argv(active["simulator"]["argv"])
    assert Path(f"/proc/{simulator_pid}/ns/net").readlink() != Path("/proc/self/ns/net").readlink()
    page.get_by_role("button", name="Stop experiment", exact=True).click()
    await_finished(page, expect, outcome="interrupted")
    manifest = read_manifest(directory)
    assert manifest["requested"]["source"] == "arducopter-sitl"
    assert manifest["simulator"]["shutdown"]["returncode"] is not None
    assert not Path(f"/proc/{simulator_pid}").exists()
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    captured = await_saved_capture(page, expect, directory)
    assert {"HEARTBEAT", "ATTITUDE"} <= {record.message_name for record in captured.records}
    assert all(
        record.fields["base_mode"] & 128 == 0
        for record in captured.records
        if record.message_name == "HEARTBEAT"
    )
    report_path = tmp_path / "sitl-interrupted.md"
    blocks = download_blocks(page, report_path)
    assert run_report_block(blocks, "status")["declared_outcome"] == "interrupted"
    assert load_run_directory(directory).identity in report_path.read_text()
