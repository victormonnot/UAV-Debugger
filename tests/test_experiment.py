"""Verify real loopback experiments and their independently imported evidence."""

import hashlib
import itertools
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from uav_debugger import experiment, import_file
from uav_debugger.experiment import ExperimentConfig, run_experiment


def read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def invoke(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "uav_debugger.experiment", *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


@pytest.fixture(scope="module")
def baseline_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("baseline-parent") / "run"
    result = run_experiment(output, ExperimentConfig(scenario="baseline", duration_s=0.3))
    return output, result


@pytest.fixture(scope="module")
def blackout_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("blackout-parent") / "run"
    result = run_experiment(
        output,
        ExperimentConfig(scenario="blackout", duration_s=2.4, blackout_at_s=0.2),
    )
    return output, result


def test_baseline_preserves_frames_across_actual_udp_receives(baseline_run):
    output, result = baseline_run
    before = import_file(output / "relay-input.tlog")
    after = import_file(output / "receiver.tlog")

    assert result["outcome"] == "completed"
    assert json.loads((output / "run.json").read_text()) == result
    assert before.traversal == after.traversal == "complete"
    assert before.decoded_count == after.decoded_count > 0
    assert before.opaque_count == after.opaque_count == 0
    assert [record.raw_frame for record in before.records] == [
        record.raw_frame for record in after.records
    ]
    assert {record.message_name for record in before.records} == {"ATTITUDE"}
    assert {
        (record.system_id, record.component_id, record.wire_version) for record in before.records
    } == {(1, 1, 2)}
    assert [record.sequence for record in before.records] == list(range(len(before.records)))
    assert all(record.fields["time_boot_ms"] < 1_000 for record in before.records)
    counts = result["counters"]
    assert counts["sender_submitted"] == counts["relay_observed"] == len(before.records)
    assert counts["relay_forwarded"] == counts["receiver_observed"] == len(after.records)
    assert counts["relay_dropped"] == 0
    actions = read_lines(output / "actions.jsonl")
    assert not any(action["action"] == "forwarding_disabled" for action in actions)
    assert sum(action["action"] == "producer_started" for action in actions) == 1
    assert sum(action["action"] == "producer_stopped" for action in actions) == 1


@pytest.mark.parametrize("run_fixture", ["baseline_run", "blackout_run"])
def test_manifest_separates_requested_settings_from_outcome_and_fingerprints(request, run_fixture):
    output, result = request.getfixturevalue(run_fixture)
    requested = result["requested"]
    assert result["schema"] == "uav-debugger-experiment-v1"
    assert requested["duration_s"] == (0.3 if run_fixture == "baseline_run" else 2.4)
    assert requested["blackout_duration_s"] == (None if run_fixture == "baseline_run" else 2.0)
    assert requested["blackout_at_s"] == (None if run_fixture == "baseline_run" else 0.2)
    assert requested["rate_hz"] == 20
    assert result["error"] is None
    assert result["end"]["monotonic_ns"] > result["start"]["monotonic_ns"]
    assert result["origin_monotonic_ns"] <= result["start"]["monotonic_ns"]
    assert set(result["artifacts"]) == {
        "relay-input.tlog",
        "receiver.tlog",
        "actions.jsonl",
        "observations.jsonl",
    }
    for name, metadata in result["artifacts"].items():
        raw = (output / name).read_bytes()
        assert metadata["size_bytes"] == len(raw)
        assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("run_fixture", ["baseline_run", "blackout_run"])
def test_observation_sidecars_identify_exact_capture_bytes_and_clocks(request, run_fixture):
    output, _ = request.getfixturevalue(run_fixture)
    observations = read_lines(output / "observations.jsonl")
    assert {observation["point"] for observation in observations} == {"relay-input", "receiver"}
    for point in ("relay-input", "receiver"):
        captured = import_file(output / f"{point}.tlog")
        at_point = [observation for observation in observations if observation["point"] == point]
        assert len(at_point) == len(captured.records)
        for observation, record in zip(at_point, captured.records, strict=True):
            assert observation["record_index"] == record.index
            assert observation["offset"] == record.offset
            assert observation["frame_size_bytes"] == len(record.raw_frame)
            assert observation["frame_sha256"] == hashlib.sha256(record.raw_frame).hexdigest()
            assert observation["unix_us"] == record.timestamp_us
            assert isinstance(observation["monotonic_ns"], int)
            assert isinstance(observation["elapsed_ns"], int)
            assert observation["monotonic_ns"] > 0
            assert observation["elapsed_ns"] >= 0
        assert [item["monotonic_ns"] for item in at_point] == sorted(
            item["monotonic_ns"] for item in at_point
        )


def test_blackout_records_actual_drop_window_and_resumption_without_replay(blackout_run):
    output, result = blackout_run
    before = import_file(output / "relay-input.tlog")
    after = import_file(output / "receiver.tlog")
    actions = read_lines(output / "actions.jsonl")
    disabled = [item for item in actions if item["action"] == "forwarding_disabled"]
    enabled = [item for item in actions if item["action"] == "forwarding_enabled"]
    assert result["outcome"] == "completed"
    assert before.traversal == after.traversal == "complete"
    assert before.decoded_count > after.decoded_count > 0
    assert len(disabled) == len(enabled) == 1
    start, end = disabled[0]["monotonic_ns"], enabled[0]["monotonic_ns"]
    assert end - start >= 2_000_000_000
    drops = [item for item in actions if item["action"] == "relay_dropped"]
    forwards = [item for item in actions if item["action"] == "relay_forwarded"]
    assert drops
    assert all(start <= item["monotonic_ns"] < end for item in drops)
    assert any(item["monotonic_ns"] < start for item in forwards)
    assert any(item["monotonic_ns"] >= end for item in forwards)
    assert sorted(item["record_index"] for item in drops + forwards) == list(
        range(len(before.records))
    )
    assert [record.raw_frame for record in after.records] == [
        before.records[item["record_index"]].raw_frame for item in forwards
    ]
    assert not {before.records[item["record_index"]].raw_frame for item in drops} & {
        record.raw_frame for record in after.records
    }
    assert result["counters"]["relay_dropped"] == len(drops)
    assert result["counters"]["relay_forwarded"] == len(forwards) == len(after.records)
    observed = read_lines(output / "observations.jsonl")
    assert any(
        item["point"] == "relay-input" and start <= item["monotonic_ns"] < end for item in observed
    )
    receiver_times = [item["monotonic_ns"] for item in observed if item["point"] == "receiver"]
    assert max(
        current - previous
        for previous, current in zip(receiver_times, receiver_times[1:], strict=False)
    ) >= (1_900_000_000)


@pytest.mark.parametrize(
    "settings",
    [
        {"duration_s": 0},
        {"duration_s": 0.099},
        {"duration_s": 60.01},
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"scenario": "unknown"},
        {"scenario": "baseline", "blackout_at_s": 1},
        {"scenario": "blackout", "duration_s": 2},
        {"scenario": "blackout", "blackout_at_s": 0},
        {"scenario": "blackout", "blackout_at_s": float("nan")},
        {"scenario": "blackout", "duration_s": 3, "blackout_at_s": 1},
    ],
)
def test_invalid_settings_do_not_create_output(tmp_path, settings):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run_experiment(output, ExperimentConfig(**settings))
    assert not output.exists()


def test_existing_output_is_preserved(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "relay-input.tlog"
    sentinel.write_bytes(b"existing recording")

    with pytest.raises((OSError, ValueError)):
        run_experiment(output, ExperimentConfig(duration_s=0.1))

    assert sentinel.read_bytes() == b"existing recording"
    assert set(output.iterdir()) == {sentinel}


def test_stop_event_ends_run_and_retains_readable_partial_evidence(tmp_path):
    stop = threading.Event()
    timer = threading.Timer(0.2, stop.set)
    output = tmp_path / "stopped"
    started = time.monotonic()
    timer.start()
    try:
        result = run_experiment(output, ExperimentConfig(duration_s=5), stop=stop)
    finally:
        timer.cancel()
        timer.join(timeout=1)

    assert time.monotonic() - started < 3
    assert result["outcome"] == "interrupted"
    assert json.loads((output / "run.json").read_text())["outcome"] == "interrupted"
    assert import_file(output / "relay-input.tlog").traversal == "complete"
    assert import_file(output / "receiver.tlog").traversal == "complete"
    assert any(
        item["action"] == "producer_stopped" for item in read_lines(output / "actions.jsonl")
    )


@pytest.mark.parametrize("fault", [None, "send", "close"])
def test_real_sockets_are_loopback_bound_and_closed_on_success_or_failure(
    tmp_path, monkeypatch, fault
):
    original_socket = socket.socket
    created = []
    send_calls = 0
    close_failed = False

    class ObservedSocket(original_socket):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.bound_address = None
            self.peer_address = None
            created.append(self)

        def bind(self, address):
            self.bound_address = address
            return super().bind(address)

        def connect(self, address):
            self.peer_address = address
            return super().connect(address)

        def send(self, data, *args):
            nonlocal send_calls
            send_calls += 1
            if fault == "send" and send_calls == 4:
                raise OSError("simulated datagram submission failure")
            return super().send(data, *args)

        def close(self):
            nonlocal close_failed
            super().close()
            if fault == "close" and not close_failed:
                close_failed = True
                raise OSError("simulated socket close failure")

    monkeypatch.setattr(experiment.socket, "socket", ObservedSocket)
    output = tmp_path / "resources"
    result = run_experiment(output, ExperimentConfig(duration_s=0.2))

    assert len(created) == 4
    assert all(sock.family == socket.AF_INET and sock.type == socket.SOCK_DGRAM for sock in created)
    assert all(sock.bound_address == ("127.0.0.1", 0) for sock in created)
    assert all(sock.peer_address[0] == "127.0.0.1" for sock in created)
    assert all(sock.fileno() == -1 for sock in created)
    assert result["outcome"] == ("completed" if fault is None else "failed")
    assert result["error"] is None if fault is None else "simulated" in result["error"]
    assert json.loads((output / "run.json").read_text()) == result
    assert import_file(output / "relay-input.tlog").traversal == "complete"
    assert import_file(output / "receiver.tlog").traversal == "complete"


def test_socket_setup_failure_closes_prior_sockets_and_retains_clock_origin(tmp_path, monkeypatch):
    original_socket = socket.socket
    created = []

    def fail_third_socket(*args, **kwargs):
        if len(created) == 2:
            raise OSError("simulated socket allocation failure")
        sock = original_socket(*args, **kwargs)
        created.append(sock)
        return sock

    monkeypatch.setattr(experiment.socket, "socket", fail_third_socket)
    output = tmp_path / "socket-setup-failure"
    result = run_experiment(output, ExperimentConfig(duration_s=0.2))

    assert result["outcome"] == "failed"
    assert "simulated socket allocation failure" in result["error"]
    assert result["start"] is None
    assert result["end"]["elapsed_ns"] == (
        result["end"]["monotonic_ns"] - result["origin_monotonic_ns"]
    )
    assert result["end"]["elapsed_ns"] >= 0
    assert len(created) == 2
    assert all(sock.fileno() == -1 for sock in created)
    assert json.loads((output / "run.json").read_text()) == result
    assert import_file(output / "relay-input.tlog").traversal == "empty"
    assert import_file(output / "receiver.tlog").traversal == "empty"


def test_wall_clock_regression_is_preserved_without_affecting_duration(tmp_path, monkeypatch):
    samples = itertools.count()
    monkeypatch.setattr(
        experiment.time, "time_ns", lambda: 1_700_000_000_000_000_000 - next(samples) * 1_000_000
    )
    output = tmp_path / "regressing-wall-clock"
    result = run_experiment(output, ExperimentConfig(duration_s=0.2))

    assert result["outcome"] == "completed"
    assert result["end"]["unix_us"] < result["start"]["unix_us"]
    assert result["end"]["monotonic_ns"] > result["start"]["monotonic_ns"]
    for point in ("relay-input", "receiver"):
        captured = import_file(output / f"{point}.tlog")
        assert captured.traversal == "complete"
        assert any(issue.code == "timestamp_regression" for issue in captured.issues)
        assert captured.records[-1].timestamp_us < captured.records[0].timestamp_us


def test_capture_failure_retains_the_written_prefix_and_reports_failed(tmp_path, monkeypatch):
    original_write = experiment._Capture.write

    def fail_after_two_input_records(self, frame, stamp):
        if self.point == "relay-input" and self.count == 2:
            raise OSError("simulated recording storage failure")
        return original_write(self, frame, stamp)

    monkeypatch.setattr(experiment._Capture, "write", fail_after_two_input_records)
    output = tmp_path / "capture-failure"
    result = run_experiment(output, ExperimentConfig(duration_s=0.3))

    assert result["outcome"] == "failed"
    assert "simulated recording storage failure" in result["error"]
    assert json.loads((output / "run.json").read_text()) == result
    before = import_file(output / "relay-input.tlog")
    assert before.traversal == "complete"
    assert before.decoded_count == 2
    assert result["counters"]["relay_observed"] == 2
    assert any(
        item["action"] == "producer_stopped" for item in read_lines(output / "actions.jsonl")
    )


def test_runtime_failure_returns_cli_code_one_with_retained_outcome(tmp_path, monkeypatch, capsys):
    def cannot_write_capture(self, frame, stamp):
        raise OSError("simulated capture write failure")

    monkeypatch.setattr(experiment._Capture, "write", cannot_write_capture)
    output = tmp_path / "failed-command"
    exit_code = experiment.main(["--output", str(output), "--duration", "0.2"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out)["outcome"] == "failed"
    assert "simulated capture write failure" in captured.err
    assert "Traceback" not in captured.err
    assert json.loads((output / "run.json").read_text())["outcome"] == "failed"


def test_sender_sequence_wrap_is_preserved_by_relay_and_receiver(tmp_path, monkeypatch):
    original_encoder = experiment.common.MAVLink

    def encoder_near_sequence_wrap(*args, **kwargs):
        encoder = original_encoder(*args, **kwargs)
        encoder.seq = 254
        return encoder

    monkeypatch.setattr(experiment.common, "MAVLink", encoder_near_sequence_wrap)
    output = tmp_path / "sequence-wrap"
    result = run_experiment(output, ExperimentConfig(duration_s=0.3))

    assert result["outcome"] == "completed"
    for point in ("relay-input", "receiver"):
        capture = import_file(output / f"{point}.tlog")
        assert [record.sequence for record in capture.records[:4]] == [254, 255, 0, 1]
        assert capture.traversal == "complete"


def test_cli_creates_a_run_and_captures_can_be_imported_in_separate_processes(tmp_path):
    output = tmp_path / "cli"
    process = invoke("--output", output, "--scenario", "baseline", "--duration", "0.3")
    assert process.returncode == 0, process.stderr
    assert "completed" in process.stdout
    assert "Traceback" not in process.stderr
    for name in ("relay-input.tlog", "receiver.tlog"):
        imported = subprocess.run(
            [sys.executable, "-m", "uav_debugger", str(output / name)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert imported.returncode == 0, imported.stderr
        assert json.loads(imported.stdout)["decoded_count"] > 0


@pytest.mark.parametrize(
    "arguments",
    [
        ("--duration", "nan"),
        ("--duration", "61"),
        ("--scenario", "baseline", "--blackout-at", "1"),
        ("--scenario", "blackout", "--duration", "2"),
    ],
)
def test_cli_invalid_configuration_fails_without_creating_output(tmp_path, arguments):
    output = tmp_path / "invalid-cli"
    process = invoke("--output", output, *arguments)
    assert process.returncode == 2
    assert "Traceback" not in process.stderr
    assert not output.exists()


@pytest.mark.parametrize(
    "stop_signal, expected_code", [(signal.SIGINT, 130), (signal.SIGTERM, 143)]
)
def test_signal_during_blackout_stops_cleanly_and_retains_applied_evidence(
    tmp_path, stop_signal, expected_code
):
    output = tmp_path / "signalled"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uav_debugger.experiment",
            "--output",
            str(output),
            "--scenario",
            "blackout",
            "--duration",
            "5",
            "--blackout-at",
            "0.2",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(f"Experiment exited before interruption: {process.communicate()}")
            path = output / "actions.jsonl"
            if path.exists() and '"forwarding_disabled"' in path.read_text():
                break
            time.sleep(0.02)
        else:
            pytest.fail("Experiment did not enter its configured blackout")
        process.send_signal(stop_signal)
        stdout, stderr = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)

    assert process.returncode == expected_code, stderr
    assert "interrupted" in stdout
    assert "Traceback" not in stderr
    result = json.loads((output / "run.json").read_text())
    assert result["outcome"] == "interrupted"
    assert import_file(output / "relay-input.tlog").traversal == "complete"
    assert import_file(output / "receiver.tlog").traversal == "complete"
    actions = read_lines(output / "actions.jsonl")
    assert any(item["action"] == "forwarding_disabled" for item in actions)
    assert any(item["action"] == "producer_stopped" for item in actions)


def test_importing_analyze_does_not_import_experiment():
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import uav_debugger; import uav_debugger.analyze; "
            "assert 'uav_debugger.experiment' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert process.returncode == 0, process.stderr
