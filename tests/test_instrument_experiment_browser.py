"""Explicit Instrument execution with real owned workers and offline handoffs."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest
from test_experiment_browser import (
    host_pid_for_argv,
    read_manifest,
    run_directories,
    wait_for_measurement,
)
from test_instrument_browser import (
    ROOT,
    download_report,
    hold_next_response,
    load_example,
    release_response,
)
from test_instrument_browser import instrument_page as instrument_page
from test_instrument_comparison_browser import assert_counts, chart, comparison, comparison_report
from test_instrument_runs_browser import run_section

from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser


@pytest.fixture
def experiment_root(tmp_path):
    return tmp_path / "experiment-output"


@pytest.fixture
def instrument_server(experiment_root, tmp_path):
    """Keep controller history and process ownership isolated for every workflow."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    command = [
        sys.executable,
        "-m",
        "uav_debugger.instrument",
        "--port",
        str(port),
        "--experiment-root",
        str(experiment_root),
    ]
    if os.environ.get("UAV_DEBUGGER_SITL_BINARY"):
        command.extend(["--sitl-binary", os.environ["UAV_DEBUGGER_SITL_BINARY"]])
    log_path = tmp_path / "instrument.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Instrument exited:\n{log_path.read_text(errors='replace')}")
                try:
                    with urlopen(f"{base_url}/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.05)
            else:
                pytest.fail(f"Instrument did not start:\n{log_path.read_text(errors='replace')}")
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            assert not owned_workers(experiment_root), "Server teardown left an owned worker."


def owned_workers(root):
    prefix = os.fsencode(str(root) + os.sep)
    result = []
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            arguments = path.read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b"uav_debugger.experiment_worker" in arguments and any(
            argument.startswith(prefix) for argument in arguments
        ):
            result.append(path.parent)
    return result


def wait_for_disk(predicate, *, timeout=20, message="Evidence did not reach the expected state"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.025)
    pytest.fail(message)


def control(directory):
    return json.loads((directory.parent / "control.json").read_bytes())


def actions(directory):
    path = directory / "actions.jsonl"
    if not path.exists():
        return []
    result = []
    for line in path.read_bytes().splitlines():
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # A concurrent append can leave only its last line incomplete.
    return result


def open_experiment(page, expect):
    page.locator("#experiment-button").click()
    expect(page.locator("#experiment-view")).to_be_visible()
    expect(page.locator("#experiment-connection")).to_have_text("Connected")


def configure(
    page,
    *,
    duration=1.5,
    scenario="baseline",
    source="synthetic",
    blackout_at=0.2,
    blackout_duration=0.5,
):
    page.locator("#experiment-source").select_option(source)
    page.locator("#experiment-scenario").select_option(scenario)
    page.locator("#experiment-duration").fill(str(duration))
    if scenario == "blackout":
        page.locator("#experiment-blackout-at").fill(str(blackout_at))
        page.locator("#experiment-blackout-duration").fill(str(blackout_duration))


def start(page, expect, root, **kwargs):
    configure(page, **kwargs)
    before = run_directories(root)
    with page.expect_response(
        lambda response: urlsplit(response.url).path == "/api/experiment/start"
    ) as pending:
        page.locator("#experiment-start").click()
    assert pending.value.ok, pending.value.text()
    created = wait_for_disk(lambda: run_directories(root) - before)
    assert len(created) == 1, "One explicit Start must create exactly one worker."
    directory = created.pop()
    expect(page.locator("#experiment-run-id")).to_have_text(directory.parent.name)
    return directory


def finished(page, expect, directory, outcome="completed"):
    expect(page.locator("#experiment-run-id")).to_have_text(directory.parent.name)
    expect(page.locator("#experiment-process-state")).to_have_text("finished", timeout=30_000)
    expect(page.locator("#experiment-outcome")).to_have_text(outcome)
    expect(page.locator("#experiment-start")).to_be_enabled()
    assert read_manifest(directory)["outcome"] == outcome
    return load_run_directory(directory)


def stop(page, expect, directory):
    page.locator("#experiment-stop").click()
    return finished(page, expect, directory, "interrupted")


def open_run(page, expect, directory):
    run = load_run_directory(directory)
    page.locator("#experiment-open-analyze").click()
    expect(page.locator("#saved-input-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#capture-name")).to_have_text("receiver.tlog")
    expect(page.locator("#record-count")).to_have_text(str(len(run.captures["receiver"].records)))
    expect(page.locator("#download-report")).to_be_enabled()
    return run


def open_peer(page, expect, url):
    peer = page.context.new_page()
    peer.goto(url, wait_until="networkidle")
    open_experiment(peer, expect)
    return peer


def test_analyze_and_experiment_discovery_never_create_a_worker_or_output_root(
    instrument_page, experiment_root
):
    page, expect = instrument_page
    requests = []
    page.on(
        "request", lambda request: requests.append((request.method, urlsplit(request.url).path))
    )
    load_example(page, expect)
    open_experiment(page, expect)
    expect(page.locator("#experiment-process-state")).to_have_text("Idle")
    expect(page.locator("#experiment-stop")).to_be_disabled()
    expect(page.locator("#experiment-open-analyze")).to_be_disabled()
    expect(page.locator("#experiment-blackout-at")).to_be_disabled()
    expect(page.locator("#experiment-blackout-duration")).to_be_disabled()
    expect(page.locator("#experiment-startup-timeout")).to_be_disabled()
    configure(page, scenario="blackout")
    expect(page.locator("#experiment-blackout-at")).to_be_enabled()
    expect(page.locator("#experiment-blackout-duration")).to_be_enabled()
    page.locator("#experiment-refresh").click()
    expect(page.locator("#experiment-connection")).to_have_text("Connected")
    page.locator("#analyze-button").click()
    expect(page.locator("#record-count")).to_have_text("12")
    page.locator("#catalog-input-tab").click()
    expect(page.locator("#catalog-view")).to_be_visible()
    assert not experiment_root.exists()
    assert not owned_workers(experiment_root)
    assert all(method == "GET" for method, _ in requests)
    assert not any(path.endswith(("/start", "/stop")) for _, path in requests)


def test_real_baseline_and_custom_blackout_compare_and_export_original_evidence(
    instrument_page, experiment_root, tmp_path
):
    page, expect = instrument_page
    open_experiment(page, expect)
    baseline = start(page, expect, experiment_root)
    before = finished(page, expect, baseline)
    page.locator("#experiment-use-baseline").click()
    expect(page.locator("#experiment-baseline-name")).to_contain_text(baseline.parent.name)
    expect(page.locator("#experiment-view")).to_be_visible()
    blackout = start(page, expect, experiment_root, scenario="blackout", blackout_duration=0.35)
    after = finished(page, expect, blackout)
    page.locator("#experiment-use-blackout").click()
    expect(page.locator("#experiment-blackout-name")).to_contain_text(blackout.parent.name)
    expected = compare_runs(before, after)
    assert expected.comparable, expected.issues
    assert after.requested["blackout_duration_s"] == 0.35
    assert after.manifest["counters"]["relay_dropped"] > 0
    assert after.gate_intervals[0].duration_ns >= 350_000_000
    page.locator("#experiment-compare").click()
    comparison(page, expect)
    assert_counts(chart(page), expected, "receiver")
    report, blocks = comparison_report(page, tmp_path / "real-experiment-comparison.md")
    assert run_section(blocks, "comparison_status")["comparable"] is True
    assert run_section(blocks, "blackout_requested")["blackout_duration_s"] == 0.35
    for run in (before, after):
        assert run.identity in report
        assert all(capture.sha256 in report for capture in run.captures.values())
    assert "forwarding_disabled" in report and "forwarding_enabled" in report
    page.locator("#comparison-inspect-blackout").click()
    expect(page.locator("#run-identity")).to_have_text(after.identity)
    expect(page.locator("#capture-name")).to_have_text("receiver.tlog")
    page.locator("#observation-point").select_option("relay-input")
    expect(page.locator("#record-count")).to_have_text(
        str(len(after.captures["relay-input"].records))
    )
    _, blocks = download_report(page, tmp_path / "real-run-report.md")
    assert run_section(blocks, "run_provenance")["bundle_sha256"] == after.identity
    assert run_section(blocks, "run_provenance")["selected_point"] == "relay-input"
    assert run_directories(experiment_root) == {baseline, blackout}


def test_stop_during_active_gate_preserves_interrupted_capture_and_report(
    instrument_page, experiment_root, tmp_path
):
    page, expect = instrument_page
    open_experiment(page, expect)
    directory = start(
        page,
        expect,
        experiment_root,
        scenario="blackout",
        duration=30,
        blackout_at=0.1,
        blackout_duration=20,
    )
    wait_for_disk(
        lambda: any(item["action"] == "forwarding_disabled" for item in actions(directory))
    )
    run = stop(page, expect, directory)
    assert run.evidence_status == "consistent", run.issues
    assert run.captures["receiver"].records
    assert 0 < run.gate_intervals[0].duration_ns < 20_000_000_000
    assert run.gate_intervals[0].end.data["reason"] == "shutdown"
    stored_control = control(directory)
    assert stored_control["stop_reason"] == "user"
    assert stored_control["forced_termination"] is False
    assert (
        stored_control["stop_requested"]["monotonic_ns"]
        <= stored_control["finished"]["monotonic_ns"]
    )
    open_run(page, expect, directory)
    _, blocks = download_report(page, tmp_path / "interrupted-gate.md")
    assert run_section(blocks, "status")["declared_outcome"] == "interrupted"
    assert run_section(blocks, "run_provenance")["bundle_sha256"] == run.identity
    assert "control.json" not in run_section(blocks, "run_provenance")["files"]
    open_experiment(page, expect)
    page.locator("#experiment-catalog").click()
    expect(page.locator("#catalog-view")).to_be_visible()
    key = directory.parent.name + "/evidence"
    entry = page.get_by_role("button", name=f"Open {key} in Analyze", exact=True)
    expect(entry).to_be_enabled()
    entry.click()
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    assert run_directories(experiment_root) == {directory}


@pytest.mark.parametrize(
    "duration,blackout_at,blackout_duration",
    [
        (0, None, None),
        (61, None, None),
        (1, 0.4, 2),
        (1, 0.05, 0.5),
    ],
)
def test_invalid_settings_create_neither_worker_nor_output_directory(
    instrument_page, experiment_root, duration, blackout_at, blackout_duration
):
    page, expect = instrument_page
    open_experiment(page, expect)
    configure(
        page,
        duration=duration,
        scenario="baseline" if blackout_at is None else "blackout",
        blackout_at=blackout_at,
        blackout_duration=blackout_duration,
    )
    page.locator("#experiment-start").click()
    expect(page.locator("#experiment-error")).to_be_visible()
    expect(page.locator("#experiment-start")).to_be_enabled()
    expect(page.locator("#experiment-process-state")).to_have_text("Idle")
    assert not experiment_root.exists()
    assert not owned_workers(experiment_root)


def test_an_open_peer_discovers_active_worker_and_disables_all_launch_settings(
    instrument_page, instrument_server, experiment_root
):
    page, expect = instrument_page
    open_experiment(page, expect)
    peer = open_peer(page, expect, instrument_server)
    try:
        directory = start(page, expect, experiment_root, duration=30)
        wait_for_measurement(directory)
        expect(peer.locator("#experiment-run-id")).to_have_text(directory.parent.name)
        expect(peer.locator("#experiment-process-state")).to_have_text("running")
        for view in (page, peer):
            for name in (
                "source",
                "scenario",
                "duration",
                "blackout-at",
                "blackout-duration",
                "startup-timeout",
                "start",
            ):
                expect(view.locator(f"#experiment-{name}")).to_be_disabled()
            expect(view.locator("#experiment-stop")).to_be_enabled()
            expect(view.locator("#experiment-open-analyze")).to_be_disabled()
        stop(peer, expect, directory)
        finished(page, expect, directory, "interrupted")
        assert run_directories(experiment_root) == {directory}
    finally:
        peer.close()


@pytest.mark.parametrize("action", ["analyze", "reload", "close"])
def test_leaving_or_closing_browser_does_not_stop_or_restart_the_owned_worker(
    instrument_page, instrument_server, experiment_root, action
):
    page, expect = instrument_page
    owner = open_peer(page, expect, instrument_server)
    try:
        directory = start(owner, expect, experiment_root, duration=30)
        wait_for_measurement(directory)
        if action == "analyze":
            owner.locator("#analyze-button").click()
            load_example(owner, expect)
        elif action == "reload":
            owner.reload(wait_until="networkidle")
            expect(owner.locator("#load-example")).to_be_visible()
        else:
            owner.close()
        open_experiment(page, expect)
        expect(page.locator("#experiment-run-id")).to_have_text(directory.parent.name)
        expect(page.locator("#experiment-process-state")).to_have_text("running")
        assert control(directory)["stop_requested"] is None
        assert len(owned_workers(experiment_root)) == 1
        stop(page, expect, directory)
        assert run_directories(experiment_root) == {directory}
    finally:
        if not owner.is_closed():
            owner.close()


def test_stale_idle_tab_cannot_start_a_second_worker(
    instrument_page, instrument_server, experiment_root
):
    page, expect = instrument_page
    open_experiment(page, expect)
    idle = page.request.get(instrument_server + "/api/experiment/status").json()
    page.route("**/api/experiment/status", lambda route: route.fulfill(json=idle))
    peer = open_peer(page, expect, instrument_server)
    try:
        directory = start(peer, expect, experiment_root, duration=30)
        wait_for_measurement(directory)
        expect(page.locator("#experiment-start")).to_be_enabled()
        with page.expect_response("**/api/experiment/start") as pending:
            page.locator("#experiment-start").click()
        assert pending.value.status == 409
        expect(page.locator("#experiment-error")).to_be_visible()
        assert run_directories(experiment_root) == {directory}
        page.unroute("**/api/experiment/status")
        page.locator("#experiment-refresh").click()
        expect(page.locator("#experiment-start")).to_be_disabled()
        stop(peer, expect, directory)
    finally:
        peer.close()


def test_delayed_stop_targets_its_original_run_and_never_a_new_worker(
    instrument_page, instrument_server, experiment_root
):
    page, expect = instrument_page
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=1.5)
    peer = open_peer(page, expect, instrument_server)
    pending_routes = []
    page.route("**/api/experiment/stop", lambda route: pending_routes.append(route))
    try:
        page.locator("#experiment-stop").click()
        page.wait_for_timeout(50)
        assert len(pending_routes) == 1
        assert json.loads(pending_routes[0].request.post_data)["run_id"] == directory.parent.name
        finished(peer, expect, directory)
        newer = start(peer, expect, experiment_root, duration=30)
        wait_for_measurement(newer)
        with page.expect_response("**/api/experiment/stop") as response:
            pending_routes.pop().continue_()
        assert response.value.ok
        assert control(directory)["stop_requested"] is None
        assert control(newer)["state"] == "running"
        assert control(newer)["stop_requested"] is None
        assert run_directories(experiment_root) == {directory, newer}
        stop(peer, expect, newer)
    finally:
        for route in pending_routes:
            route.abort()
        peer.close()


@pytest.mark.parametrize("active", [False, True])
def test_poll_failure_disables_active_commands_then_recovers_without_restarting(
    instrument_page, experiment_root, active
):
    page, expect = instrument_page
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=30) if active else None

    def unavailable(route):
        route.fulfill(status=503, json={"error": "Temporarily unavailable"})

    page.route("**/api/experiment/status", unavailable)
    page.locator("#experiment-refresh").click()
    expect(page.locator("#experiment-connection")).to_have_text("Unavailable")
    expect(page.locator("#experiment-start")).to_be_disabled()
    expect(page.locator("#experiment-stop")).to_be_disabled()
    page.unroute("**/api/experiment/status", unavailable)
    page.locator("#experiment-refresh").click()
    expect(page.locator("#experiment-connection")).to_have_text("Connected")
    if active:
        expect(page.locator("#experiment-run-id")).to_have_text(directory.parent.name)
        expect(page.locator("#experiment-stop")).to_be_enabled()
        stop(page, expect, directory)
        assert run_directories(experiment_root) == {directory}
    else:
        expect(page.locator("#experiment-start")).to_be_enabled()
        assert not experiment_root.exists()


def test_history_refresh_preserves_an_explicit_older_selection(
    instrument_page, instrument_server, experiment_root
):
    page, expect = instrument_page
    open_experiment(page, expect)
    first = start(page, expect, experiment_root, duration=0.3)
    finished(page, expect, first)
    second = start(page, expect, experiment_root, duration=0.3)
    finished(page, expect, second)
    page.locator("#experiment-history").select_option(first.parent.name)
    expect(page.locator("#experiment-run-id")).to_have_text(first.parent.name)
    peer = open_peer(page, expect, instrument_server)
    try:
        latest = start(peer, expect, experiment_root, duration=1.5)
        expect(page.locator("#experiment-history option")).to_have_count(3)
        expect(page.locator("#experiment-history")).to_have_value(first.parent.name)
        expect(page.locator("#experiment-run-id")).to_have_text(first.parent.name)
        finished(peer, expect, latest)
        page.locator("#experiment-refresh").click()
        expect(page.locator("#experiment-history")).to_have_value(first.parent.name)
        open_run(page, expect, first)
        assert run_directories(experiment_root) == {first, second, latest}
    finally:
        peer.close()


def test_completed_start_response_after_navigation_does_not_reopen_or_duplicate_execution(
    instrument_page, experiment_root
):
    page, expect = instrument_page
    open_experiment(page, expect)
    configure(page, duration=30)
    hold_next_response(page, "/api/experiment/start")
    page.locator("#experiment-start").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    directories = wait_for_disk(lambda: run_directories(experiment_root))
    assert len(directories) == 1
    directory = directories.pop()
    page.locator("#analyze-button").click()
    load_example(page, expect)
    release_response(page)
    expect(page.locator("#experiment-view")).to_be_hidden()
    expect(page.locator("#record-count")).to_have_text("12")
    open_experiment(page, expect)
    expect(page.locator("#experiment-run-id")).to_have_text(directory.parent.name)
    stop(page, expect, directory)
    assert run_directories(experiment_root) == {directory}


@pytest.mark.parametrize("destination", ["analyze", "baseline"])
def test_completed_evidence_handoff_cannot_override_navigation_or_a_new_selection(
    instrument_page, experiment_root, destination
):
    page, expect = instrument_page
    open_experiment(page, expect)
    first = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, first)
    second = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, second)
    hold_next_response(page, "/api/experiment/open")
    button = "#experiment-open-analyze" if destination == "analyze" else "#experiment-use-baseline"
    page.locator(button).click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if destination == "analyze":
        page.locator("#analyze-button").click()
        load_example(page, expect)
    else:
        page.locator("#experiment-history").select_option(first.parent.name)
    release_response(page)
    if destination == "analyze":
        expect(page.locator("#record-count")).to_have_text("12")
        expect(page.locator("#recording-input-tab")).to_have_attribute("aria-selected", "true")
    else:
        expect(page.locator("#experiment-run-id")).to_have_text(first.parent.name)
        expect(page.locator("#experiment-baseline-name")).to_have_text("No baseline selected")
        expect(page.locator("#experiment-compare")).to_be_disabled()
    assert run_directories(experiment_root) == {first, second}


def test_controller_clocks_and_diagnostics_are_literal_not_runner_evidence(
    instrument_page, experiment_root, instrument_server
):
    page, expect = instrument_page
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, directory)
    snapshot = page.request.get(instrument_server + "/api/experiment/status").json()
    latest = snapshot["experiment"]["history"][0]
    exact = str((1 << 63) + 123)
    literal = '<img src=x onerror="window.diagnosticsExecuted=true"> & literal stderr'
    latest["created"]["monotonic_ns"] = exact
    latest["diagnostics"] = literal
    page.route("**/api/experiment/status", lambda route: route.fulfill(json=snapshot))
    page.locator("#experiment-refresh").click()
    page.locator("#experiment-details summary").click()
    expect(page.locator("#experiment-clocks")).to_contain_text(exact)
    expect(page.locator("#experiment-diagnostics")).to_have_text(literal)
    assert page.locator("#experiment-view img").count() == 0
    assert page.evaluate("window.diagnosticsExecuted === undefined")
    assert control(directory)["diagnostics"] != literal


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_experiment_controls_and_terminal_evidence_are_readable_across_viewports(
    instrument_page, experiment_root, width, theme
):
    page, expect = instrument_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#theme-select").select_option(theme)
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, directory)
    page.locator("#experiment-details summary").click()
    expect(page.locator("#experiment-clocks")).to_contain_text(
        str(control(directory)["created"]["monotonic_ns"])
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    contrast = page.evaluate(
        """() => {
            const rgba = value => value.match(/[\\d.]+/g).map(Number);
            const over = (front, back) => front.slice(0, 3).map((value, index) =>
                value * (front[3] ?? 1) + back[index] * (1 - (front[3] ?? 1)));
            const background = element => {
                const parents = [];
                for (let current = element; current; current = current.parentElement) {
                    parents.unshift(current);
                }
                return parents.reduce((color, parent) =>
                    over(rgba(getComputedStyle(parent).backgroundColor), color), [255, 255, 255]);
            };
            const luminance = values => values.map(value => {
                const channel = value / 255;
                return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
            }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
            return ['#experiment-connection', '#experiment-process-state', '#experiment-outcome',
                '#experiment-run-id', '#experiment-source', '#experiment-start',
                '#experiment-clocks'].map(selector => {
                    const element = document.querySelector(selector);
                    const backdrop = background(element);
                    const foreground = rgba(getComputedStyle(element).color);
                    let alpha = foreground[3] ?? 1;
                    for (let current = element; current; current = current.parentElement) {
                        alpha *= Number(getComputedStyle(current).opacity);
                    }
                    foreground[3] = alpha;
                    const values = [luminance(over(foreground, backdrop)), luminance(backdrop)]
                        .sort((a, b) => a - b);
                    return {selector, ratio: (values[1] + .05) / (values[0] + .05)};
                });
        }"""
    )
    assert all(item["ratio"] >= 4.5 for item in contrast), contrast
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        destination = ROOT / "local" / "instrument-brick6" / "browser"
        destination.mkdir(parents=True, exist_ok=True)
        page.evaluate("() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }")
        page.screenshot(path=destination / f"experiment-{theme}-{width}.png", full_page=True)


@pytest.mark.skipif(
    not os.environ.get("UAV_DEBUGGER_SITL_BINARY"),
    reason="Set UAV_DEBUGGER_SITL_BINARY to verify pinned native Instrument execution",
)
def test_opt_in_sitl_start_stop_uses_isolated_worker_and_opens_observed_capture(
    instrument_page, experiment_root, tmp_path
):
    page, expect = instrument_page
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=30, source="arducopter-sitl")
    active = wait_for_measurement(directory)
    simulator_pid = host_pid_for_argv(active["simulator"]["argv"])
    assert Path(f"/proc/{simulator_pid}/ns/net").readlink() != Path("/proc/self/ns/net").readlink()
    stop(page, expect, directory)
    manifest = read_manifest(directory)
    assert manifest["simulator"]["shutdown"]["returncode"] is not None
    assert not Path(f"/proc/{simulator_pid}").exists()
    run = open_run(page, expect, directory)
    capture = run.captures["receiver"]
    assert {"HEARTBEAT", "ATTITUDE"} <= {record.message_name for record in capture.records}
    assert all(
        record.fields["base_mode"] & 128 == 0
        for record in capture.records
        if record.message_name == "HEARTBEAT"
    )
    report, blocks = download_report(page, tmp_path / "sitl-interrupted.md")
    assert run_section(blocks, "status")["declared_outcome"] == "interrupted"
    assert run.identity in report
