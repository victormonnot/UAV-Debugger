"""Verify ownership, actual local captures and controller/evidence separation."""

import dataclasses
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from uav_debugger import experiment_control as control
from uav_debugger.experiment import ExperimentConfig
from uav_debugger.experiment_control import ExperimentController
from uav_debugger.saved_run import load_run_directory


def eventually(function, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("Owned experiment did not reach the expected state before the test deadline")


def finished(controller, run_id):
    return eventually(
        lambda: snapshot if not (snapshot := controller.snapshot(run_id)).active else None
    )


def started(output):
    path = output / "actions.jsonl"
    return path.exists() and '"producer_started"' in path.read_text()


@pytest.fixture
def controller(tmp_path):
    instance = ExperimentController(tmp_path / "runs")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def actual_runs(tmp_path_factory):
    controller = ExperimentController(tmp_path_factory.mktemp("controlled-runs"))
    result = []
    try:
        for config in (
            ExperimentConfig(duration_s=0.25),
            ExperimentConfig(scenario="blackout", duration_s=2.3, blackout_at_s=0.1),
        ):
            initial = controller.start(config)
            result.append(finished(controller, initial.run_id))
        yield tuple(result)
    finally:
        controller.close()


@pytest.mark.parametrize("index", (0, 1))
def test_actual_worker_evidence_retains_independent_analyze_contract(actual_runs, index):
    snapshot = actual_runs[index]
    run = load_run_directory(snapshot.output)
    assert snapshot.state == "finished"
    assert snapshot.returncode == 0
    assert snapshot.declared_outcome == run.declared_outcome == "completed"
    assert run.evidence_status == "consistent"
    assert snapshot.error is None
    assert len(run.captures["relay-input"].records) > 0
    assert len(run.captures["receiver"].records) > 0
    if index:
        assert len(run.captures["relay-input"].records) > len(run.captures["receiver"].records)
        assert run.gate_intervals[0].duration_ns >= 2_000_000_000
    else:
        assert [record.raw_frame for record in run.captures["relay-input"].records] == [
            record.raw_frame for record in run.captures["receiver"].records
        ]
    assert "control.json" not in run.files
    metadata = json.loads(snapshot.control_file.read_text())
    assert metadata["state"] == "finished"
    assert metadata["requested"] == dict(snapshot.requested) == dict(run.requested)
    assert metadata["created"]["monotonic_ns"] < run.origin_monotonic_ns
    assert snapshot.finished.monotonic_ns >= run.manifest["end"]["monotonic_ns"]
    assert metadata["stop_requested"] is None
    assert snapshot.output.parent == snapshot.control_file.parent


def test_stop_is_idempotent_and_separate_from_applied_runner_action(controller):
    initial = controller.start(ExperimentConfig(duration_s=60))
    eventually(lambda: started(initial.output))
    stopped = controller.stop(initial.run_id)
    repeated = controller.stop(initial.run_id)
    assert stopped.state == "stopping"
    assert stopped.stop_requested == repeated.stop_requested
    snapshot = finished(controller, initial.run_id)
    run = load_run_directory(snapshot.output)
    assert snapshot.state == "finished"
    assert snapshot.declared_outcome == "interrupted"
    assert snapshot.stop_reason == "user"
    assert snapshot.forced_termination is False
    assert snapshot.created.monotonic_ns <= snapshot.stop_requested.monotonic_ns
    applied = next(event for event in run.actions if event.data["action"] == "producer_stopped")
    assert applied.monotonic_ns >= snapshot.stop_requested.monotonic_ns
    assert snapshot.finished.monotonic_ns >= applied.monotonic_ns
    assert run.evidence_status == "consistent"
    assert controller.stop(initial.run_id) == snapshot


@pytest.mark.parametrize("length", [None, 4.0])
def test_stop_during_blackout_preserves_actual_shortened_gate(controller, length):
    initial = controller.start(
        ExperimentConfig(
            scenario="blackout",
            duration_s=6,
            blackout_at_s=0.1,
            blackout_duration_s=length,
        )
    )
    actions = initial.output / "actions.jsonl"
    eventually(lambda: actions.exists() and '"forwarding_disabled"' in actions.read_text())
    controller.stop(initial.run_id)
    snapshot = finished(controller, initial.run_id)
    run = load_run_directory(snapshot.output)
    assert run.declared_outcome == "interrupted"
    requested_length = 2.0 if length is None else length
    assert 0 < run.gate_intervals[0].duration_ns < round(requested_length * 1e9)
    assert run.gate_intervals[0].end.data["reason"] == "shutdown"
    assert snapshot.requested["blackout_duration_s"] == requested_length
    assert run.requested["blackout_duration_s"] == requested_length


def test_custom_blackout_is_transmitted_to_real_owned_worker_and_final_evidence(controller):
    initial = controller.start(
        ExperimentConfig(
            scenario="blackout",
            duration_s=0.9,
            blackout_at_s=0.1,
            blackout_duration_s=0.35,
        )
    )
    snapshot = finished(controller, initial.run_id)
    assert snapshot.state == "finished", snapshot.error
    assert snapshot.declared_outcome == "completed"
    run = load_run_directory(snapshot.output)
    assert run.evidence_status == "consistent"
    assert run.requested["blackout_duration_s"] == snapshot.requested["blackout_duration_s"] == 0.35
    assert run.gate_intervals[0].duration_ns >= 350_000_000
    assert len(run.captures["relay-input"].records) > len(run.captures["receiver"].records) > 0
    stored_control = json.loads(snapshot.control_file.read_text())
    assert stored_control["requested"]["blackout_duration_s"] == 0.35


def test_one_active_worker_and_sequential_unique_outputs(controller):
    first = controller.start(ExperimentConfig(duration_s=60))
    with pytest.raises(RuntimeError, match="already active"):
        controller.start(ExperimentConfig(duration_s=0.1))
    controller.stop(first.run_id)
    first_done = finished(controller, first.run_id)
    original = (first.output / "run.json").read_bytes()
    second = controller.start(ExperimentConfig(duration_s=0.1))
    second_done = finished(controller, second.run_id)
    assert first.output != second.output
    assert (first.output / "run.json").read_bytes() == original
    assert controller.history() == (second_done, first_done)


def test_concurrent_start_calls_serialize_launches(controller):
    from concurrent.futures import ThreadPoolExecutor

    def launch():
        try:
            return controller.start(ExperimentConfig(duration_s=60))
        except RuntimeError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: launch(), range(2)))
    assert sum(isinstance(item, RuntimeError) for item in result) == 1
    assert len(controller.history()) == 1


@pytest.mark.parametrize(
    "config",
    (
        ExperimentConfig(duration_s=0),
        ExperimentConfig(duration_s=float("nan")),
        ExperimentConfig(duration_s=61),
        ExperimentConfig(scenario="other"),
        ExperimentConfig(blackout_at_s=1),
        ExperimentConfig(blackout_duration_s=2),
        ExperimentConfig(scenario="blackout", duration_s=2),
        ExperimentConfig(scenario="blackout", blackout_duration_s=float("inf")),
        ExperimentConfig(scenario="blackout", blackout_duration_s=4),
    ),
)
def test_invalid_configuration_does_not_create_output_or_worker(controller, config):
    with pytest.raises(ValueError):
        controller.start(config)
    assert not controller.output_root.exists()
    assert controller.history() == ()


def test_wrong_sitl_binary_is_rejected_without_execution(controller, tmp_path):
    binary = tmp_path / "fake executable"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o755)
    with pytest.raises(ValueError, match="SHA-256"):
        controller.start(ExperimentConfig(sitl_binary=binary))
    assert not controller.output_root.exists()


def test_output_creation_failure_is_explicit_and_preserves_existing_file(tmp_path):
    target = tmp_path / "existing"
    target.write_bytes(b"preserve")
    controller = ExperimentController(target)
    with pytest.raises(FileExistsError):
        controller.start(ExperimentConfig(duration_s=0.1))
    assert target.read_bytes() == b"preserve"
    assert controller.history() == ()
    controller.close()


def test_output_path_is_never_interpreted_by_a_shell(tmp_path):
    target = tmp_path / "spaces ; $(touch unwanted) `literal`"
    controller = ExperimentController(target)
    try:
        snapshot = finished(controller, controller.start(ExperimentConfig(duration_s=0.1)).run_id)
        assert snapshot.declared_outcome == "completed"
        assert snapshot.output.is_relative_to(target)
        assert not (tmp_path / "unwanted").exists()
    finally:
        controller.close()


def test_history_eviction_retains_original_files(tmp_path):
    controller = ExperimentController(tmp_path / "runs", history_limit=1)
    try:
        first = finished(controller, controller.start(ExperimentConfig(duration_s=0.1)).run_id)
        original = (first.output / "run.json").read_bytes()
        second = finished(controller, controller.start(ExperimentConfig(duration_s=0.1)).run_id)
        assert controller.history() == (second,)
        assert (first.output / "run.json").read_bytes() == original
    finally:
        controller.close()


def test_snapshots_are_immutable_and_wall_clock_does_not_schedule_stop(controller, monkeypatch):
    initial = controller.start(ExperimentConfig(duration_s=60))
    eventually(lambda: started(initial.output))
    with pytest.raises(dataclasses.FrozenInstanceError):
        initial.state = "finished"
    with pytest.raises(TypeError):
        initial.requested["duration_s"] = 0
    monkeypatch.setattr(control.time, "time_ns", lambda: 1000)
    stopped = controller.stop(initial.run_id)
    final = finished(controller, initial.run_id)
    assert stopped.stop_requested.unix_us < initial.created.unix_us
    assert stopped.stop_requested.monotonic_ns >= initial.created.monotonic_ns
    assert final.declared_outcome == "interrupted"
    assert final.forced_termination is False


def test_close_stops_and_reaps_owned_worker(controller):
    initial = controller.start(ExperimentConfig(duration_s=60))
    eventually(lambda: started(initial.output))
    worker = controller._workers[initial.run_id]
    controller.close()
    snapshot = controller.snapshot(initial.run_id)
    assert snapshot.state == "finished"
    assert snapshot.stop_reason == "controller_shutdown"
    assert worker.process.poll() == 0
    assert worker.process.stdin.closed
    assert worker.process.stderr.closed
    with pytest.raises(RuntimeError, match="closed"):
        controller.start(ExperimentConfig(duration_s=0.1))


def test_launch_error_has_no_invented_runner_outcome(controller, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("launch unavailable")

    monkeypatch.setattr(control.subprocess, "Popen", fail)
    snapshot = controller.start(ExperimentConfig(duration_s=0.1))
    assert snapshot.state == "failed"
    assert snapshot.declared_outcome is None
    assert snapshot.returncode is None
    assert "launch unavailable" in snapshot.error
    assert not snapshot.output.exists()
    assert json.loads(snapshot.control_file.read_text())["state"] == "failed"


def test_worker_crash_preserves_running_manifest_and_bounded_diagnostics(controller, monkeypatch):
    program = """import pathlib, sys
output = pathlib.Path(sys.argv[1])
output.mkdir()
(output / 'run.json').write_text('{"outcome":"running"}')
sys.stderr.write('x' * 20000 + 'last diagnostic')
sys.exit(7)
"""
    monkeypatch.setattr(
        control, "_command", lambda output, config: [sys.executable, "-c", program, str(output)]
    )
    snapshot = finished(controller, controller.start(ExperimentConfig(duration_s=0.1)).run_id)
    assert snapshot.state == "failed"
    assert snapshot.declared_outcome == "running"
    assert snapshot.returncode == 7
    assert "without a finalized" in snapshot.error
    assert snapshot.diagnostics.endswith("last diagnostic")
    assert len(snapshot.diagnostics.encode()) == control.DIAGNOSTIC_BYTES
    assert json.loads((snapshot.output / "run.json").read_text()) == {"outcome": "running"}


def test_stuck_worker_gets_bounded_owned_process_termination(controller, monkeypatch):
    monkeypatch.setattr(control, "STOP_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(
        control,
        "_command",
        lambda output, config: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    initial = controller.start(ExperimentConfig(duration_s=60))
    controller.stop(initial.run_id)
    snapshot = finished(controller, initial.run_id)
    assert snapshot.state == "failed"
    assert snapshot.forced_termination is True
    assert snapshot.returncode == -signal.SIGKILL
    assert snapshot.declared_outcome is None
    assert "forced termination" in snapshot.error
    assert controller._workers[initial.run_id].process.poll() == -signal.SIGKILL


def test_watchdog_uses_its_own_deadline_without_browser_polling(controller, monkeypatch):
    monkeypatch.setattr(control, "RUN_MARGIN_SECONDS", -0.1)
    monkeypatch.setattr(control, "STOP_GRACE_SECONDS", 0.1)
    monkeypatch.setattr(
        control,
        "_command",
        lambda output, config: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    initial = controller.start(ExperimentConfig(duration_s=0.1))
    worker = controller._workers[initial.run_id]
    assert worker.done.wait(timeout=4)
    snapshot = controller.snapshot(initial.run_id)
    assert snapshot.stop_reason == "watchdog"
    assert snapshot.forced_termination is True
    assert snapshot.state == "failed"


@pytest.mark.parametrize("contents", ("{", '{"outcome": {}}', '{"outcome": "invented"}'))
def test_invalid_final_manifest_does_not_wedge_controller(controller, monkeypatch, contents):
    program = (
        "import pathlib, sys; p = pathlib.Path(sys.argv[1]); p.mkdir(); "
        "(p/'run.json').write_text(sys.argv[2])"
    )
    monkeypatch.setattr(
        control,
        "_command",
        lambda output, config: [sys.executable, "-c", program, str(output), contents],
    )
    snapshot = finished(controller, controller.start(ExperimentConfig(duration_s=0.1)).run_id)
    assert snapshot.state == "failed"
    assert snapshot.declared_outcome is None
    assert "unavailable" in snapshot.error
    assert controller._active is None


@pytest.mark.parametrize("kind", ("fifo", "symlink", "oversized"))
def test_nonregular_or_oversized_manifest_does_not_block_reaping(controller, monkeypatch, kind):
    program = """import os, pathlib, sys
output = pathlib.Path(sys.argv[1])
output.mkdir()
manifest = output / 'run.json'
kind = sys.argv[2]
if kind == 'fifo':
    os.mkfifo(manifest)
elif kind == 'symlink':
    target = output.parent / 'unrelated.json'
    target.write_text('{"outcome": "completed"}')
    manifest.symlink_to(target)
else:
    manifest.write_bytes(b' ' * (1024 * 1024 + 1))
"""
    monkeypatch.setattr(
        control,
        "_command",
        lambda output, config: [sys.executable, "-c", program, str(output), kind],
    )
    initial = controller.start(ExperimentConfig(duration_s=0.1))
    worker = controller._workers[initial.run_id]
    assert worker.done.wait(timeout=3), "Manifest inspection blocked process finalization"
    snapshot = controller.snapshot(initial.run_id)
    assert snapshot.state == "failed"
    assert snapshot.declared_outcome is None
    assert snapshot.returncode == 0
    assert "unavailable" in snapshot.error
    assert controller._active is None
    assert worker.process.poll() == 0
    if kind == "symlink":
        assert (initial.output.parent / "unrelated.json").read_text() == '{"outcome": "completed"}'


def test_owner_pipe_eof_stops_worker_without_parent_signals(tmp_path):
    output = tmp_path / "evidence"
    process = subprocess.Popen(
        control._command(output, ExperimentConfig(duration_s=60)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        eventually(lambda: started(output))
        process.stdin.close()
        process.wait(timeout=8)
        assert process.returncode == 0
        run = load_run_directory(output)
        assert run.declared_outcome == "interrupted"
        assert run.evidence_status == "consistent"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


def test_abrupt_parent_exit_still_requests_worker_cleanup(tmp_path):
    program = """import os, pathlib, sys, time
from uav_debugger.experiment import ExperimentConfig
from uav_debugger.experiment_control import ExperimentController
controller = ExperimentController(pathlib.Path(sys.argv[1]))
run = controller.start(ExperimentConfig(duration_s=60))
path = run.output / 'actions.jsonl'
deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if path.exists() and '"producer_started"' in path.read_text():
        print(str(run.output), flush=True)
        os._exit(0)
    time.sleep(0.01)
raise RuntimeError('worker did not start')
"""
    parent = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "runs")],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    output = Path(parent.stdout.strip())
    manifest = output / "run.json"
    eventually(lambda: json.loads(manifest.read_text())["outcome"] == "interrupted")
    run = load_run_directory(output)
    assert run.evidence_status == "consistent"
    # No parent was alive to invent or finalize its separate process state.
    assert json.loads((output.parent / "control.json").read_text())["state"] == "running"


def test_sitl_command_has_process_tree_and_network_isolation(tmp_path):
    command = control._command(tmp_path / "run", ExperimentConfig(sitl_binary=Path("/binary")))
    assert command[:7] == [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--pid",
        "--fork",
        "--kill-child=SIGKILL",
    ]
    assert "--isolated" in command


def test_worker_rejects_sitl_without_owned_namespace(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uav_debugger.experiment_worker",
            "--output",
            str(tmp_path / "out"),
            "--scenario",
            "baseline",
            "--duration",
            "0.1",
            "--sitl-binary",
            "/missing",
            "--isolated",
        ],
        input=b"",
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 1
    assert b"must be PID 1" in result.stderr
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("scenario", ("baseline", "blackout"))
def test_native_sitl_owned_controller_is_optional(tmp_path, scenario):
    configured = os.environ.get("UAV_DEBUGGER_SITL_BINARY")
    if not configured:
        pytest.skip("Set UAV_DEBUGGER_SITL_BINARY to verify the owned native SITL worker")
    controller = ExperimentController(tmp_path / "runs")
    try:
        initial = controller.start(
            ExperimentConfig(scenario=scenario, duration_s=6, sitl_binary=Path(configured))
        )
        snapshot = eventually(
            lambda: (
                current if not (current := controller.snapshot(initial.run_id)).active else None
            ),
            timeout=30,
        )
        assert snapshot.state == "finished", (snapshot.error, snapshot.diagnostics)
        run = load_run_directory(snapshot.output)
        assert run.declared_outcome == "completed"
        assert run.evidence_status == "consistent"
        assert run.manifest["simulator"]["shutdown"]["returncode"] is not None
        assert run.captures["receiver"].records
        if scenario == "blackout":
            assert run.gate_intervals[0].duration_ns >= 2_000_000_000
    finally:
        controller.close()


def test_native_forced_teardown_removes_simulator_separate_session(tmp_path, monkeypatch):
    configured = os.environ.get("UAV_DEBUGGER_SITL_BINARY")
    if not configured:
        pytest.skip("Set UAV_DEBUGGER_SITL_BINARY to verify forced native process-tree cleanup")
    monkeypatch.setattr(control, "STOP_GRACE_SECONDS", 0.1)
    controller = ExperimentController(tmp_path / "runs")
    try:
        initial = controller.start(ExperimentConfig(duration_s=60, sitl_binary=Path(configured)))
        eventually(lambda: started(initial.output), timeout=20)
        wrapper = controller._workers[initial.run_id].process

        def children(pid):
            return [
                int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
            ]

        (namespace_init,) = children(wrapper.pid)
        (simulator,) = children(namespace_init)
        assert os.getpgid(simulator) == simulator
        assert os.getpgid(simulator) != os.getpgid(namespace_init)
        # Suspend the worker to make its cooperative EOF handler unavailable.
        os.kill(namespace_init, signal.SIGSTOP)
        controller.stop(initial.run_id)
        snapshot = finished(controller, initial.run_id)
        assert snapshot.forced_termination is True
        assert snapshot.state == "failed"
        assert snapshot.declared_outcome == "running"
        eventually(lambda: not Path(f"/proc/{simulator}").exists())
        eventually(lambda: not Path(f"/proc/{namespace_init}").exists())
        assert wrapper.poll() is not None
        # A kill does not forge the ordinary runner finalization path.
        assert json.loads((initial.output / "run.json").read_text())["outcome"] == "running"
    finally:
        controller.close()
