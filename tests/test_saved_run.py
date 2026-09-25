"""Saved evidence stays inspectable without importing execution or opening transports."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from uav_debugger import import_bytes, saved_run
from uav_debugger.experiment import ExperimentConfig, run_experiment
from uav_debugger.saved_run import load_run_directory, load_run_files

ROOT = Path(__file__).resolve().parents[1]


def json_bytes(value):
    return (json.dumps(value) + "\n").encode()


def lines_bytes(values):
    return b"".join(json_bytes(value) for value in values)


def finalized(files):
    files = dict(files)
    manifest = json.loads(files["run.json"])
    manifest["artifacts"] = {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in files.items()
        if name != "run.json"
    }
    files["run.json"] = json_bytes(manifest)
    return files


@pytest.fixture(scope="module")
def real_runs(tmp_path_factory):
    parent = tmp_path_factory.mktemp("saved-reader")
    result = {}
    for scenario in ("baseline", "blackout"):
        directory = parent / scenario
        config = ExperimentConfig(
            scenario,
            0.15 if scenario == "baseline" else 2.3,
            None if scenario == "baseline" else 0.1,
        )
        run_experiment(directory, config)
        result[scenario] = directory
    return result


@pytest.fixture
def baseline_files(real_runs):
    return {path.name: path.read_bytes() for path in real_runs["baseline"].iterdir()}


@pytest.fixture
def sitl_files():
    """Constructed v2 trace, not a claim that this test executed ArduPilot."""
    parsed = import_bytes((ROOT / "tests/fixtures/telemetry-gap.tlog").read_bytes())
    heartbeat = next(item.raw_frame for item in parsed.records if item.message_name == "HEARTBEAT")
    attitude = next(item.raw_frame for item in parsed.records if item.message_name == "ATTITUDE")
    # A structurally complete unknown v2 message stays opaque with unverified CRC.
    unknown = bytearray(attitude)
    assert unknown[0] == 0xFD
    unknown[7:10] = (50000).to_bytes(3, "little")
    origin = 1_000_000_000
    wall = 1_700_000_000_000_000

    def stamp(elapsed):
        return {
            "monotonic_ns": origin + elapsed,
            "elapsed_ns": elapsed,
            "unix_us": wall + elapsed // 1000,
        }

    captures = {point: bytearray() for point in saved_run.CAPTURE_POINTS}
    observation_counts = dict.fromkeys(saved_run.CAPTURE_POINTS, 0)
    datagram_counts = dict.fromkeys(saved_run.CAPTURE_POINTS, 0)
    datagrams, observations, actions = [], [], []

    def datagram(point, elapsed, frames):
        number = datagram_counts[point]
        datagram_counts[point] += 1
        data = b"".join(frames)
        datagrams.append(
            {"point": point, "datagram_index": number, "raw_hex": data.hex(), **stamp(elapsed)}
        )
        if data == b"0 ":
            return
        offset = 0
        for frame in frames:
            target = captures[point]
            observations.append(
                {
                    "point": point,
                    "record_index": observation_counts[point],
                    "offset": len(target),
                    "frame_size_bytes": len(frame),
                    "frame_sha256": hashlib.sha256(frame).hexdigest(),
                    "datagram_index": number,
                    "datagram_offset": offset,
                    **stamp(elapsed),
                }
            )
            target.extend(stamp(elapsed)["unix_us"].to_bytes(8, "big") + frame)
            observation_counts[point] += 1
            offset += len(frame)

    datagram("relay-input", 10, [b"0 "])
    datagram("receiver", 20, [b"0 "])
    datagram("relay-input", 100, [heartbeat, attitude, bytes(unknown)])
    datagram("receiver", 200, [heartbeat, attitude, bytes(unknown)])
    datagram("relay-input", 600, [attitude])
    datagram("relay-input", 2_000_000_600, [attitude])
    datagram("receiver", 2_000_000_700, [attitude])

    def action(name, elapsed, **details):
        value = {"action": name, **stamp(elapsed), **details}
        if elapsed >= 300:
            value["measurement_elapsed_ns"] = elapsed - 300
        actions.append(value)

    action("producer_started", 1)
    action("sitl_startup_preamble", 11, point="relay-input", datagram_index=0)
    action("datagram_forwarded", 12, point="relay-input", datagram_index=0)
    action("sitl_startup_preamble", 21, point="receiver", datagram_index=0)
    action("datagram_forwarded", 110, point="relay-input", datagram_index=1)
    for number in range(3):
        action("relay_forwarded", 111 + number, point="relay-input", record_index=number)
    action("measurement_started", 301, readiness_messages=["HEARTBEAT", "ATTITUDE"])
    action("forwarding_disabled", 500, requested_elapsed_ns=500)
    action("datagram_dropped", 610, point="relay-input", datagram_index=2)
    action("relay_dropped", 611, point="relay-input", record_index=3)
    action("forwarding_enabled", 2_000_000_500, reason="blackout_elapsed")
    action("datagram_forwarded", 2_000_000_610, point="relay-input", datagram_index=3)
    action("relay_forwarded", 2_000_000_611, point="relay-input", record_index=4)
    action("producer_stopped", 2_100_000_000, reason="duration_elapsed")
    action("drain_finished", 2_300_000_000, reason="deadline_elapsed")
    manifest = {
        "schema": saved_run.SCHEMAS[1],
        "outcome": "completed",
        "error": None,
        "origin_monotonic_ns": origin,
        "start": stamp(0),
        "end": stamp(3_000_000_000),
        "measurement_start": stamp(300),
        "capture_profile": "qgc-timestamped-mavlink-v1",
        "requested": {
            "source": "arducopter-sitl",
            "scenario": "blackout",
            "duration_s": 2.1,
            "blackout_at_s": 0.1,
            "blackout_duration_s": 2,
        },
        "counters": {
            "relay_observed": 5,
            "receiver_observed": 4,
            "relay_forwarded": 4,
            "relay_dropped": 1,
            "sender_submitted": None,
            "missed_emission_slots": None,
        },
        "datagrams_observed": datagram_counts,
        "simulator": {"executable": "/unavailable/not-to-be-executed", "pid": 42},
    }
    files = {
        "run.json": json_bytes(manifest),
        "actions.jsonl": lines_bytes(actions),
        "observations.jsonl": lines_bytes(observations),
        "datagrams.jsonl": lines_bytes(datagrams),
        "simulator.log": b"Constructed test evidence only.\n",
        "simulator/profile.parm": b"FRAME_CLASS 1\n",
    }
    files.update({f"{point}.tlog": bytes(raw) for point, raw in captures.items()})
    return finalized(files)


@pytest.mark.parametrize("scenario", ["baseline", "blackout"])
def test_real_runner_evidence_is_consistent_and_byte_exact(real_runs, scenario):
    directory = real_runs[scenario]
    run = load_run_directory(directory)
    assert run.evidence_status == "consistent"
    assert run.issues == ()
    assert run.declared_outcome == "completed"
    assert run.requested["scenario"] == scenario
    assert run.measurement_monotonic_ns == run.origin_monotonic_ns
    assert all(item.valid for item in (*run.actions, *run.observations))
    for point, capture in run.captures.items():
        assert capture.raw_bytes == (directory / f"{point}.tlog").read_bytes()
        assert len(capture.records) == sum(item.point == point for item in run.observations)
    if scenario == "blackout":
        (interval,) = run.gate_intervals
        assert interval.duration_ns >= 2_000_000_000
        assert interval.start.data["action"] == "forwarding_disabled"
        assert interval.end.data["action"] == "forwarding_enabled"
    else:
        assert run.gate_intervals == ()


def test_old_v1_uses_run_origin_without_new_sitl_fields(baseline_files):
    manifest = json.loads(baseline_files["run.json"])
    manifest.pop("measurement_start")
    manifest.pop("simulator")
    manifest["requested"].pop("source")
    manifest["requested"].pop("startup_timeout_s")
    manifest["clocks"].pop("measurement_elapsed_ns")
    actions = [json.loads(line) for line in baseline_files["actions.jsonl"].splitlines()]
    for action in actions:
        action.pop("measurement_elapsed_ns", None)
    baseline_files["actions.jsonl"] = lines_bytes(actions)
    baseline_files["run.json"] = json_bytes(manifest)
    run = load_run_files(finalized(baseline_files))
    assert run.evidence_status == "consistent"
    assert run.measurement_monotonic_ns == manifest["origin_monotonic_ns"]


def test_constructed_v2_preserves_grouped_frames_preambles_and_distinct_origin(sitl_files):
    run = load_run_files(sitl_files)
    assert run.evidence_status == "consistent"
    assert run.issues == ()
    assert run.measurement_monotonic_ns - run.origin_monotonic_ns == 300
    assert len(run.datagrams) == 7
    assert all(event.valid for event in (*run.datagrams, *run.observations, *run.actions))
    assert sum(item.raw_bytes == b"0 " for item in run.datagrams) == 2
    assert run.captures["relay-input"].opaque_count == 1
    assert run.captures["receiver"].opaque_count == 1
    (interval,) = run.gate_intervals
    assert interval.duration_ns == 2_000_000_000
    references = [item for item in run.observations if item.point == "relay-input"]
    assert references[0].monotonic_ns == references[1].monotonic_ns
    assert references[0].data["datagram_offset"] == 0
    assert references[1].data["datagram_offset"] > 0


def test_requested_blackout_never_creates_an_applied_interval(baseline_files):
    manifest = json.loads(baseline_files["run.json"])
    manifest["requested"].update(scenario="blackout", blackout_at_s=2, blackout_duration_s=2)
    baseline_files["run.json"] = json_bytes(manifest)
    assert load_run_files(baseline_files).gate_intervals == ()


@pytest.mark.parametrize("outcome", ["interrupted", "failed", "running"])
def test_partial_declared_outcomes_remain_separate_from_evidence(baseline_files, outcome):
    manifest = json.loads(baseline_files["run.json"])
    manifest["outcome"] = outcome
    if outcome == "running":
        manifest["end"] = None
        manifest["artifacts"] = {}
    baseline_files["run.json"] = json_bytes(manifest)
    run = load_run_files(baseline_files)
    assert run.declared_outcome == outcome
    assert run.captures["relay-input"].records
    assert run.evidence_status == ("incomplete" if outcome == "running" else "consistent")


def test_unclosed_actual_gate_has_unknown_end_not_requested_duration(sitl_files):
    actions = [json.loads(line) for line in sitl_files["actions.jsonl"].splitlines()]
    actions = [item for item in actions if item["action"] != "forwarding_enabled"]
    sitl_files["actions.jsonl"] = lines_bytes(actions)
    run = load_run_files(finalized(sitl_files))
    (interval,) = run.gate_intervals
    assert interval.end is None
    assert interval.duration_ns is None
    assert any(item.code == "open_gate_interval" for item in run.issues)


@pytest.mark.parametrize(
    "name", ["actions.jsonl", "observations.jsonl", "relay-input.tlog", "datagrams.jsonl"]
)
def test_artifact_hash_mismatch_cannot_supply_valid_references(sitl_files, name):
    manifest = json.loads(sitl_files["run.json"])
    manifest["artifacts"][name]["sha256"] = "0" * 64
    sitl_files["run.json"] = json_bytes(manifest)
    run = load_run_files(sitl_files)
    assert run.evidence_status == "invalid"
    assert any(
        issue.code == "artifact_mismatch" and issue.file_name == name for issue in run.issues
    )
    assert run.captures["relay-input"].records
    if name == "actions.jsonl":
        assert not any(event.valid for event in run.actions)
        assert not run.gate_intervals
    elif name in {"observations.jsonl", "datagrams.jsonl"}:
        assert not any(event.valid for event in run.observations)
    else:
        assert not any(event.valid for event in run.observations if event.point == "relay-input")


@pytest.mark.parametrize(
    "field,value",
    [
        ("offset", 9999),
        ("frame_size_bytes", True),
        ("frame_sha256", "0" * 64),
        ("unix_us", 4),
        ("elapsed_ns", 4),
        ("monotonic_ns", -1),
        ("datagram_offset", 1),
        ("datagram_index", 9999),
        ("record_index", 9999),
        ("point", []),
        ("record_index", {}),
        ("monotonic_ns", 2**80),
    ],
)
def test_bad_observation_references_are_retained_but_unverified(sitl_files, field, value):
    rows = [json.loads(line) for line in sitl_files["observations.jsonl"].splitlines()]
    rows[0][field] = value
    sitl_files["observations.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert not run.observations[0].valid
    assert run.observations[0].line_number == 1
    assert run.evidence_status == "invalid"
    assert run.captures["relay-input"].records


def test_duplicate_observations_invalidate_both_references(baseline_files):
    rows = baseline_files["observations.jsonl"].splitlines(keepends=True)
    baseline_files["observations.jsonl"] = rows[0] + b"".join(rows)
    run = load_run_files(finalized(baseline_files))
    assert not run.observations[0].valid
    assert not run.observations[1].valid
    assert any(item.code == "duplicate_observation" for item in run.issues)


@pytest.mark.parametrize("name", ["actions.jsonl", "observations.jsonl", "datagrams.jsonl"])
def test_truncated_jsonl_retains_accepted_prefix_and_raw_tail(sitl_files, name):
    original = sitl_files[name]
    sitl_files[name] += b'{"truncated": '
    run = load_run_files(finalized(sitl_files))
    assert run.evidence_status == "invalid"
    assert any(item.code == "invalid_jsonl" for item in run.issues)
    assert run.files[name].raw_bytes == original + b'{"truncated": '
    assert run.actions and run.observations and run.datagrams


def test_damaged_capture_retains_imported_prefix(sitl_files):
    sitl_files["receiver.tlog"] += b"bad-tail"
    run = load_run_files(finalized(sitl_files))
    assert run.captures["receiver"].traversal == "stopped"
    assert len(run.captures["receiver"].records) == 4
    assert any(item.code == "capture_stopped" for item in run.issues)


def test_missing_files_do_not_invent_empty_captures_or_read_manifest_paths(sitl_files, tmp_path):
    outside = tmp_path / "outside.tlog"
    outside.write_bytes(b"must remain unrelated")
    manifest = json.loads(sitl_files["run.json"])
    manifest["artifacts"][str(outside)] = {"size_bytes": 1, "sha256": "0" * 64}
    sitl_files["run.json"] = json_bytes(manifest)
    del sitl_files["receiver.tlog"]
    run = load_run_files(sitl_files)
    assert "receiver" not in run.captures
    assert str(outside) not in run.files
    assert any(item.code == "unknown_artifact" for item in run.issues)
    assert any(item.code == "missing_file" for item in run.issues)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"schema":"unknown"}',
        b"[]",
        b'{"schema":"uav-debugger-experiment-v1","schema":"uav-debugger-experiment-v1"}',
        b'{"x":NaN}',
        b'{"x":1e309}',
        b'{"x":' + b"[" * 2000 + b"]" * 2000 + b"}",
        b"\xff",
    ],
)
def test_unsupported_or_malformed_manifest_fails_explicitly(raw):
    with pytest.raises(ValueError):
        load_run_files({"run.json": raw})


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", {}),
        ("point", {}),
        ("record_index", []),
        ("monotonic_ns", True),
        ("elapsed_ns", None),
    ],
)
def test_malformed_action_values_never_escape_as_python_errors(sitl_files, field, value):
    rows = [json.loads(line) for line in sitl_files["actions.jsonl"].splitlines()]
    item = next(item for item in rows if item["action"] == "relay_forwarded")
    item[field] = value
    sitl_files["actions.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert run.evidence_status == "invalid"


@pytest.mark.parametrize(
    "field,value",
    [
        ("point", {}),
        ("datagram_index", []),
        ("raw_hex", "xyz"),
        ("raw_hex", "30 20"),
        ("monotonic_ns", 2**80),
    ],
)
def test_malformed_datagram_values_are_retained_without_valid_references(sitl_files, field, value):
    rows = [json.loads(line) for line in sitl_files["datagrams.jsonl"].splitlines()]
    rows[0][field] = value
    sitl_files["datagrams.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert not run.datagrams[0].valid
    assert run.evidence_status == "invalid"


def test_counters_are_declarations_checked_against_supplied_evidence(baseline_files):
    manifest = json.loads(baseline_files["run.json"])
    manifest["counters"]["receiver_observed"] += 1
    baseline_files["run.json"] = json_bytes(manifest)
    assert any(item.code == "counter_mismatch" for item in load_run_files(baseline_files).issues)


def test_identity_uses_names_and_all_original_file_bytes(baseline_files):
    original = load_run_files(baseline_files)
    repeated = load_run_files(dict(reversed(list(baseline_files.items()))), source_name="renamed")
    assert repeated.identity == original.identity
    baseline_files["actions.jsonl"] += b"\n"
    changed = load_run_files(baseline_files)
    assert changed.identity != original.identity


def test_file_count_name_types_and_sizes_are_bounded(baseline_files, monkeypatch):
    with pytest.raises(ValueError, match="requires run.json"):
        load_run_files({})
    with pytest.raises(ValueError, match="Unsupported.*file name"):
        load_run_files({**baseline_files, "../elsewhere": b""})
    with pytest.raises(TypeError, match="immutable"):
        load_run_files({"run.json": bytearray(b"{}")})
    monkeypatch.setitem(saved_run.FILE_LIMITS, "run.json", 2)
    with pytest.raises(ValueError, match="size limit"):
        load_run_files(baseline_files)


def test_aggregate_and_trace_event_bounds_keep_prefix(baseline_files, monkeypatch):
    monkeypatch.setattr(saved_run, "MAX_RUN_BYTES", 1)
    with pytest.raises(ValueError, match="total size"):
        load_run_files(baseline_files)
    monkeypatch.setattr(saved_run, "MAX_RUN_BYTES", 64 * 1024 * 1024)
    monkeypatch.setattr(saved_run, "MAX_TRACE_EVENTS", 2)
    run = load_run_files(baseline_files)
    assert len(run.actions) == 2
    assert len(run.observations) == 2
    assert any(item.code == "trace_limit" for item in run.issues)


def test_directory_rejects_symlinks_fifo_and_nested_symlink(baseline_files, tmp_path):
    for name, raw in baseline_files.items():
        (tmp_path / name).write_bytes(raw)
    unrelated = tmp_path / "external"
    unrelated.write_bytes(b"private")
    target = tmp_path / "receiver.tlog"
    target.unlink()
    target.symlink_to(unrelated)
    with pytest.raises(OSError):
        load_run_directory(tmp_path)
    target.unlink()
    os.mkfifo(target)
    with pytest.raises(ValueError, match="regular"):
        load_run_directory(tmp_path)
    target.unlink()
    target.write_bytes(baseline_files["receiver.tlog"])
    (tmp_path / "simulator").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        load_run_directory(tmp_path)


def test_reader_import_and_loading_do_not_import_runner_or_open_network(real_runs):
    script = """
import socket, subprocess, sys
from pathlib import Path
from uav_debugger.saved_run import load_run_directory
assert 'uav_debugger.experiment' not in sys.modules
assert 'uav_debugger.sitl' not in sys.modules

def prohibited(*args, **kwargs):
    raise AssertionError('Execution or network attempted')
socket.socket = prohibited
subprocess.Popen = prohibited
run = load_run_directory(Path(sys.argv[1]))
assert run.evidence_status == 'consistent'
assert 'uav_debugger.experiment' not in sys.modules
assert 'uav_debugger.sitl' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(real_runs["baseline"])],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_duplicate_conflicting_relay_actions_invalidate_both(sitl_files):
    rows = [json.loads(line) for line in sitl_files["actions.jsonl"].splitlines()]
    index = next(index for index, row in enumerate(rows) if row["action"] == "relay_forwarded")
    duplicate = {**rows[index], "action": "relay_dropped"}
    rows.insert(index + 1, duplicate)
    sitl_files["actions.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert not run.actions[index].valid
    assert not run.actions[index + 1].valid
    assert any(issue.code == "duplicate_action" for issue in run.issues)


def test_ambiguous_gate_transitions_do_not_produce_an_interval(sitl_files):
    rows = [json.loads(line) for line in sitl_files["actions.jsonl"].splitlines()]
    index = next(index for index, row in enumerate(rows) if row["action"] == "forwarding_disabled")
    rows.insert(index + 1, dict(rows[index]))
    sitl_files["actions.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert not run.gate_intervals
    assert any(issue.code == "gate_sequence" for issue in run.issues)


def test_sender_counter_is_checked_but_missed_slots_remain_a_declaration(baseline_files):
    manifest = json.loads(baseline_files["run.json"])
    manifest["counters"]["missed_emission_slots"] = 123
    baseline_files["run.json"] = json_bytes(manifest)
    assert load_run_files(baseline_files).evidence_status == "consistent"
    manifest["counters"]["sender_submitted"] += 1
    baseline_files["run.json"] = json_bytes(manifest)
    assert any(issue.code == "counter_mismatch" for issue in load_run_files(baseline_files).issues)


def test_wall_clock_regression_is_preserved_without_monotonic_reordering(sitl_files):
    rows = [json.loads(line) for line in sitl_files["actions.jsonl"].splitlines()]
    rows[1]["unix_us"] = rows[0]["unix_us"] - 1000
    sitl_files["actions.jsonl"] = lines_bytes(rows)
    run = load_run_files(finalized(sitl_files))
    assert run.evidence_status == "consistent"
    assert run.actions[1].unix_us < run.actions[0].unix_us
    assert run.actions[1].monotonic_ns > run.actions[0].monotonic_ns
