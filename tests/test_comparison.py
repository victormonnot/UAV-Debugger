"""Exact, file-only comparisons retain window and evidence semantics."""

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from uav_debugger import import_bytes
from uav_debugger.comparison import available_selections, compare_runs
from uav_debugger.experiment import ExperimentConfig, run_experiment
from uav_debugger.saved_run import load_run_directory, load_run_files

ROOT = Path(__file__).resolve().parents[1]
ORIGIN_NS = 1_000_000_000_000


def _lines(values):
    return b"".join((json.dumps(value) + "\n").encode() for value in values)


def finalize(files, manifest):
    files = dict(files)
    manifest["artifacts"] = {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        for name, raw in files.items()
        if name != "run.json"
    }
    files["run.json"] = json.dumps(manifest).encode()
    return files


def fixture_files(
    scenario,
    *,
    version=1,
    duration_s=3.0,
    origin_ns=ORIGIN_NS,
    startup_ns=1_000_000_000,
    producer_stop_ns=None,
    wall_regresses=False,
    blackout_at_s=0.2,
    blackout_duration_s=2.0,
    gate_start_ns=200_000_000,
    gate_end_ns=None,
):
    """Construct evidence from public fixture frames; never claim native execution."""
    capture = import_bytes((ROOT / "tests/fixtures/telemetry-gap.tlog").read_bytes())
    frame = next(
        record.raw_frame
        for record in capture.records
        if record.message_name == "ATTITUDE" and record.system_id == 1
    )
    origin = origin_ns
    measurement = startup_ns if version == 2 else 0
    duration = round(duration_s * 1e9)
    if gate_end_ns is None:
        gate_end_ns = gate_start_ns + round(blackout_duration_s * 1e9)

    def stamp(elapsed):
        unix = 1_700_000_000_000_000 + (-1 if wall_regresses else 1) * elapsed // 1000
        return {"monotonic_ns": origin + elapsed, "elapsed_ns": elapsed, "unix_us": unix}

    observations, actions, datagrams = [], [], []
    captures = {point: bytearray() for point in ("relay-input", "receiver")}
    counts = dict.fromkeys(captures, 0)
    actions.append({"action": "producer_started", **stamp(0)})
    if version == 2:
        actions.append({"action": "measurement_started", **stamp(measurement)})
    if scenario == "blackout":
        actions.extend(
            [
                {
                    "action": "forwarding_disabled",
                    "requested_elapsed_ns": measurement + round(blackout_at_s * 1e9),
                    **stamp(measurement + gate_start_ns),
                },
                {
                    "action": "forwarding_enabled",
                    "reason": "blackout_elapsed",
                    **stamp(measurement + gate_end_ns),
                },
            ]
        )
    times = [
        value
        for value in (0, 100_000_000, 250_000_000, 1_000_000_000, 2_100_000_000, 2_500_000_000)
        if value < duration
    ] + [duration]
    if version == 2:
        times = [-100_000_000, *times, duration + 50_000_000]
    drops = 0
    for index, relative in enumerate(times):
        elapsed = measurement + relative
        dropped = scenario == "blackout" and gate_start_ns <= relative < gate_end_ns
        if version == 1:
            actions.append(
                {"action": "sender_submitted", "sequence": index, **stamp(max(0, elapsed - 1))}
            )
        for point in captures:
            if point == "receiver" and dropped:
                continue
            clock = stamp(elapsed)
            target = captures[point]
            observation = {
                "point": point,
                "record_index": counts[point],
                "offset": len(target),
                "frame_size_bytes": len(frame),
                "frame_sha256": hashlib.sha256(frame).hexdigest(),
                **clock,
            }
            if version == 2:
                observation.update(datagram_index=counts[point], datagram_offset=0)
                datagrams.append(
                    {
                        "point": point,
                        "datagram_index": counts[point],
                        "raw_hex": frame.hex(),
                        **clock,
                    }
                )
            observations.append(observation)
            target.extend(clock["unix_us"].to_bytes(8, "big") + frame)
            counts[point] += 1
        actions.append(
            {
                "action": "relay_dropped" if dropped else "relay_forwarded",
                "point": "relay-input",
                "record_index": index,
                **stamp(elapsed + 1),
            }
        )
        drops += dropped
    stop = duration + 100_000_000 if producer_stop_ns is None else producer_stop_ns
    actions.append(
        {"action": "producer_stopped", "reason": "duration_elapsed", **stamp(measurement + stop)}
    )
    manifest = {
        "schema": f"uav-debugger-experiment-v{version}",
        "origin_monotonic_ns": origin,
        "start": stamp(0),
        "measurement_start": stamp(measurement),
        "end": stamp(measurement + duration + 200_000_000),
        "outcome": "completed",
        "capture_profile": "qgc-timestamped-mavlink-v1",
        "error": None,
        "requested": {
            "scenario": scenario,
            "duration_s": duration_s,
            "source": "synthetic" if version == 1 else "arducopter-sitl",
            "blackout_at_s": blackout_at_s if scenario == "blackout" else None,
            "blackout_duration_s": blackout_duration_s if scenario == "blackout" else None,
            "rate_hz": 20 if version == 1 else None,
        },
        "counters": {
            "relay_observed": counts["relay-input"],
            "receiver_observed": counts["receiver"],
            "relay_forwarded": counts["receiver"],
            "relay_dropped": drops,
            "sender_submitted": counts["relay-input"] if version == 1 else None,
            "missed_emission_slots": 0 if version == 1 else None,
        },
        "environment": {"uav_debugger": "constructed-fixture", "pymavlink": "2.4.49"},
        "topology": {
            "sender": {"host": "127.0.0.1", "port": 20001 if scenario == "baseline" else 20002}
        },
    }
    files = {f"{point}.tlog": bytes(raw) for point, raw in captures.items()}
    files["actions.jsonl"] = _lines(sorted(actions, key=lambda item: item["monotonic_ns"]))
    files["observations.jsonl"] = _lines(
        sorted(observations, key=lambda item: item["monotonic_ns"])
    )
    if version == 2:
        parameters = b"CONSTRUCTED_FIXTURE 1\n"
        files.update(
            {
                "simulator/profile.parm": parameters,
                "simulator.log": b"Constructed evidence only.\n",
                "datagrams.jsonl": _lines(sorted(datagrams, key=lambda item: item["monotonic_ns"])),
            }
        )
        manifest["datagrams_observed"] = counts
        manifest["simulator"] = {
            "binary": {
                "profile": "arducopter-4.6.3-linux-x86_64-v1",
                "sha256": "a" * 64,
                "version": "ArduCopter V4.6.3",
                "git_commit": "declared-test-artifact",
                "size_bytes": 123,
            },
            "defaults_sha256": hashlib.sha256(parameters).hexdigest(),
            "defaults_path": f"/unavailable/{scenario}/profile.parm",
            "pid": 123 if scenario == "baseline" else 456,
            "argv": [
                f"/unavailable/{scenario}/arducopter",
                "--model",
                "quad",
                "--speedup",
                "1",
                "--wipe",
                "--serial1",
                f"udpclient:127.0.0.1:{20001 if scenario == 'baseline' else 20002}",
                "--defaults",
                f"/unavailable/{scenario}/profile.parm",
            ],
            "cwd": f"/unavailable/{scenario}",
        }
    return finalize(files, manifest)


def fixture_run(scenario, **kwargs):
    run = load_run_files(fixture_files(scenario, **kwargs), source_name=f"constructed-{scenario}")
    assert run.evidence_status == "consistent", run.issues
    return run


def edited(run, update_manifest=None, update_files=None):
    files = {name: evidence.raw_bytes for name, evidence in run.files.items()}
    manifest = json.loads(files["run.json"])
    if update_manifest:
        update_manifest(manifest)
    if update_files:
        update_files(files)
    return load_run_files(finalize(files, manifest), source_name=run.source_name)


@pytest.fixture
def pair():
    return fixture_run("baseline"), fixture_run("blackout", origin_ns=ORIGIN_NS * 3)


@pytest.mark.parametrize("version", [1, 2])
def test_exact_counts_rates_interval_references_and_native_phase_exclusion(version):
    baseline = fixture_run("baseline", version=version, startup_ns=1_000_000_000)
    blackout = fixture_run(
        "blackout", version=version, origin_ns=ORIGIN_NS * 2, startup_ns=2_000_000_000
    )
    result = compare_runs(baseline, blackout)
    assert result.comparable, result.issues
    assert result.start_ns == 0 and result.end_ns == 3_000_000_000
    assert result.duration_ns == 3_000_000_000
    assert result.metrics["baseline"]["relay-input"].count == 6
    assert result.metrics["baseline"]["receiver"].rate_hz == 2
    assert result.metrics["blackout"]["relay-input"].count == 6
    receiver = result.metrics["blackout"]["receiver"]
    assert receiver.count == 3 and receiver.rate_hz == 1
    assert receiver.first.measurement_ns == 0
    assert receiver.last.measurement_ns == 2_500_000_000
    interval = receiver.longest_interval
    assert interval.duration_ns == 2_400_000_000
    assert interval.previous.measurement_ns == 100_000_000
    assert interval.current.measurement_ns == 2_500_000_000
    assert interval.previous.record.offset == interval.previous.observation.data["offset"]
    assert interval.current.observation.file_name == "observations.jsonl"
    assert interval.current.observation.line_number > 0
    (gate,) = result.gates["blackout"]
    assert (gate.start_ns, gate.end_ns, gate.duration_ns) == (
        200_000_000,
        2_200_000_000,
        2_000_000_000,
    )
    assert result.gates["baseline"] == ()
    assert all(not difference.blocking for difference in result.differences)


def test_window_is_half_open_in_integer_nanoseconds_and_does_not_fill_edges(pair):
    result = compare_runs(*pair, start_ns=100_000_000, end_ns=2_500_000_000)
    assert result.comparable
    receiver = result.metrics["blackout"]["receiver"]
    assert receiver.count == 1
    assert receiver.first.measurement_ns == 100_000_000
    assert receiver.longest_interval is None
    assert receiver.rate_hz == pytest.approx(1 / 2.4)
    clipped = compare_runs(*pair, start_ns=100_000_001, end_ns=2_500_000_000)
    receiver = clipped.metrics["blackout"]["receiver"]
    assert receiver.count == 0
    assert receiver.rate_hz == 0
    assert receiver.first is receiver.last is receiver.longest_interval is None
    assert clipped.gates["blackout"][0].start_ns == 200_000_000


def test_wall_clock_epochs_and_regressions_do_not_align_or_change_comparison():
    baseline = fixture_run("baseline", origin_ns=1_000_000_000)
    blackout = fixture_run("blackout", origin_ns=9_000_000_000_000, wall_regresses=True)
    result = compare_runs(baseline, blackout)
    assert result.comparable, result.issues
    metric = result.metrics["blackout"]["receiver"]
    assert metric.first.record.timestamp_us > metric.last.record.timestamp_us
    assert metric.longest_interval.duration_ns == 2_400_000_000


def test_default_window_uses_shorter_requested_duration_and_exposes_difference():
    result = compare_runs(fixture_run("baseline", duration_s=4), fixture_run("blackout"))
    assert result.comparable, result.issues
    assert result.end_ns == 3_000_000_000
    difference = next(item for item in result.differences if item.field == "requested.duration_s")
    assert (difference.baseline, difference.blackout, difference.blocking) == (4, 3, False)


def test_legacy_v1_normalizes_source_without_inventing_a_measurement_start(pair):
    baseline = edited(
        pair[0],
        lambda manifest: (manifest["requested"].pop("source"), manifest.pop("measurement_start")),
    )
    result = compare_runs(baseline, pair[1])
    assert result.comparable, result.issues
    assert not any(item.field == "requested.source" for item in result.differences)
    assert baseline.measurement_monotonic_ns == baseline.origin_monotonic_ns


@pytest.mark.parametrize("outcome", ["running", "failed", "interrupted"])
def test_non_completed_run_is_retained_but_yields_no_comparable_metrics(pair, outcome):
    changed = edited(pair[1], lambda manifest: manifest.update(outcome=outcome))
    result = compare_runs(pair[0], changed)
    assert not result.comparable
    assert result.metrics == {}
    assert result.blackout is changed
    assert any(issue.code == "run_not_completed" for issue in result.issues)


def test_missing_capture_is_unavailable_not_zero(pair):
    changed = edited(pair[1], update_files=lambda files: files.pop("receiver.tlog"))
    result = compare_runs(pair[0], changed)
    assert result.metrics == {}
    assert any(issue.code == "capture_unavailable" for issue in result.issues)


def test_corrupt_reference_excludes_entire_aggregate_comparison(pair):
    def corrupt(files):
        rows = [json.loads(row) for row in files["observations.jsonl"].splitlines()]
        rows[0]["offset"] = 9999
        files["observations.jsonl"] = _lines(rows)

    changed = edited(pair[1], update_files=corrupt)
    result = compare_runs(pair[0], changed)
    assert result.metrics == {}
    assert any(issue.code == "run_evidence" for issue in result.issues)


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda manifest: manifest["requested"].update(duration_s=None), "duration"),
        (lambda manifest: manifest["requested"].update(duration_s=True), "duration"),
        (lambda manifest: manifest["requested"].update(duration_s=10**400), "duration"),
        (lambda manifest: manifest["requested"].update(scenario="baseline"), "scenario"),
        (lambda manifest: manifest["requested"].update(rate_hz=10), "configuration_difference"),
        (lambda manifest: manifest["requested"].update(source="unknown"), "source_profile"),
        (lambda manifest: manifest["requested"].update(blackout_duration_s=0), "blackout_request"),
    ],
)
def test_invalid_or_incompatible_settings_have_explicit_reasons(pair, change, code):
    result = compare_runs(pair[0], edited(pair[1], change))
    assert not result.comparable
    assert result.metrics == {}
    assert any(issue.code == code for issue in result.issues)


def test_missing_actual_production_stop_blocks_window_even_when_manifest_says_completed(pair):
    def remove_stop(files):
        rows = [json.loads(row) for row in files["actions.jsonl"].splitlines()]
        files["actions.jsonl"] = _lines(
            [row for row in rows if row["action"] != "producer_stopped"]
        )

    changed = edited(pair[1], update_files=remove_stop)
    assert changed.evidence_status == "consistent"
    result = compare_runs(pair[0], changed)
    assert any(issue.code == "production_evidence" for issue in result.issues)
    assert result.metrics == {}


def test_actual_stop_must_cover_window_and_requested_phase_excludes_drain(pair):
    short_stop = fixture_run("blackout", producer_stop_ns=2_900_000_000)
    result = compare_runs(pair[0], short_stop)
    assert any(issue.code == "production_coverage" for issue in result.issues)
    result = compare_runs(*pair, end_ns=3_050_000_000)
    assert any(issue.code == "window_coverage" for issue in result.issues)


@pytest.mark.parametrize("start,end", [(100_000_000, 2_100_000_000), (200_000_000, 2_100_000_000)])
def test_actual_gate_must_follow_request_and_cover_full_two_seconds(pair, start, end):
    result = compare_runs(pair[0], fixture_run("blackout", gate_start_ns=start, gate_end_ns=end))
    assert not result.comparable
    assert any(issue.code == "blackout_gate" for issue in result.issues)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("field", ["blackout_at_s", "blackout_duration_s"])
def test_baseline_with_requested_blackout_timing_blocks_comparison(version, field):
    baseline = edited(
        fixture_run("baseline", version=version),
        lambda manifest: manifest["requested"].update({field: 0.5}),
    )
    assert baseline.evidence_status == "consistent"
    result = compare_runs(baseline, fixture_run("blackout", version=version))
    assert not result.comparable
    assert result.metrics == {}
    assert any(
        item.code == "baseline_request" and item.run_role == "baseline" for item in result.issues
    )


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("missing", [False, True])
def test_baseline_null_or_absent_legacy_blackout_settings_remain_comparable(version, missing):
    baseline = fixture_run("baseline", version=version)
    if missing:

        def omit_timing(manifest):
            for field in ("blackout_at_s", "blackout_duration_s"):
                del manifest["requested"][field]

        baseline = edited(baseline, omit_timing)
    result = compare_runs(baseline, fixture_run("blackout", version=version))
    assert result.comparable, result.issues


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("length_s", [0.1, 0.35, 2.0, 4.0, 59.8])
def test_configurable_gate_durations_and_legacy_two_seconds_remain_comparable(version, length_s):
    duration_s = max(3, length_s + 0.2)
    baseline = fixture_run("baseline", version=version, duration_s=duration_s)
    blackout = fixture_run(
        "blackout",
        version=version,
        duration_s=duration_s,
        blackout_at_s=0.1,
        blackout_duration_s=length_s,
        gate_start_ns=100_000_000,
    )
    result = compare_runs(baseline, blackout)
    assert result.comparable, result.issues
    assert result.gates["blackout"][0].duration_ns == round(length_s * 1e9)
    difference = next(
        item for item in result.differences if item.field == "requested.blackout_duration_s"
    )
    assert difference.baseline is None
    assert difference.blackout == length_s
    assert not difference.blocking


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("length_s", [0.1, 0.1000000006, 0.35, 4.0])
def test_applied_gate_one_nanosecond_shorter_than_requested_blocks_comparison(version, length_s):
    duration_s = max(3, length_s + 0.3)
    requested_ns = round(length_s * 1e9)
    baseline = fixture_run("baseline", version=version, duration_s=duration_s)
    blackout = fixture_run(
        "blackout",
        version=version,
        duration_s=duration_s,
        blackout_duration_s=length_s,
        gate_end_ns=200_000_000 + requested_ns - 1,
    )
    result = compare_runs(baseline, blackout)
    assert not result.comparable
    assert result.metrics == {}
    assert result.gates["blackout"][0].duration_ns == requested_ns - 1
    assert any(item.code == "blackout_gate" for item in result.issues)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        False,
        float("nan"),
        float("inf"),
        -float("inf"),
        "2",
        [],
        0,
        -0.1,
        0.0999999999,
        59.8000000001,
        60,
        1e200,
        10**400,
    ],
)
def test_invalid_requested_gate_length_never_falls_back_to_two_seconds(pair, value):
    # Nonfinite JSON is rejected earlier by the reader; the comparison API also
    # validates directly supplied SavedRun values without trusting a numeric type.
    blackout = replace(
        pair[1],
        manifest={
            **pair[1].manifest,
            "requested": {**pair[1].requested, "blackout_duration_s": value},
        },
    )
    result = compare_runs(pair[0], blackout)
    assert not result.comparable
    assert result.metrics == {}
    assert any(item.code == "blackout_request" for item in result.issues)
    assert result.gates["blackout"][0].duration_ns == 2_000_000_000


def test_absent_requested_gate_length_is_not_a_legacy_two_second_default(pair):
    blackout = edited(pair[1], lambda manifest: manifest["requested"].pop("blackout_duration_s"))
    result = compare_runs(pair[0], blackout)
    assert not result.comparable
    assert result.metrics == {}
    assert any(item.code == "blackout_request" for item in result.issues)


@pytest.mark.parametrize(
    "at_s,length_s,expected",
    [(0.1, 2.8, True), (0.2, 2.7, True), (0.0999999999, 2, False), (0.2, 2.700000001, False)],
)
def test_requested_gate_margins_use_exact_bounded_nanoseconds(pair, at_s, length_s, expected):
    blackout = fixture_run(
        "blackout",
        blackout_at_s=at_s,
        blackout_duration_s=length_s,
        gate_start_ns=round(at_s * 1e9),
    )
    result = compare_runs(pair[0], blackout)
    assert result.comparable is expected, result.issues
    if not expected:
        assert result.metrics == {}
        assert any(item.code == "blackout_request" for item in result.issues)


def test_mixed_source_families_block_metrics():
    result = compare_runs(fixture_run("baseline"), fixture_run("blackout", version=2))
    assert not result.comparable
    assert result.metrics == {}
    assert any(item.field == "requested.source" and item.blocking for item in result.differences)


@pytest.mark.parametrize("field,value", [("sha256", "b" * 64), ("profile", "different-profile")])
def test_sitl_declared_binary_identity_must_match(field, value):
    baseline = fixture_run("baseline", version=2)
    blackout = edited(
        fixture_run("blackout", version=2),
        lambda manifest: manifest["simulator"]["binary"].update({field: value}),
    )
    result = compare_runs(baseline, blackout)
    assert not result.comparable
    assert any(
        item.field == f"simulator.binary.{field}" and item.blocking for item in result.differences
    )


def test_sitl_parameter_artifact_and_behavior_arguments_must_match():
    baseline, blackout = fixture_run("baseline", version=2), fixture_run("blackout", version=2)
    changed_parameters = b"CONSTRUCTED_FIXTURE 2\n"
    changed = edited(
        blackout,
        lambda manifest: manifest["simulator"].update(
            defaults_sha256=hashlib.sha256(changed_parameters).hexdigest()
        ),
        lambda files: files.update({"simulator/profile.parm": changed_parameters}),
    )
    result = compare_runs(baseline, changed)
    assert any(
        item.field == "simulator.parameters_sha256" and item.blocking for item in result.differences
    )
    changed = edited(
        blackout,
        lambda manifest: manifest["simulator"]["argv"].extend(["--home", "different-home"]),
    )
    assert not compare_runs(baseline, changed).comparable


def test_environment_differences_are_visible_without_treating_them_as_clock_alignment(pair):
    changed = edited(
        pair[1], lambda manifest: manifest["environment"].update(python="other-version")
    )
    result = compare_runs(pair[0], changed)
    assert result.comparable
    assert any(
        item.field == "environment.python" and not item.blocking for item in result.differences
    )
    assert not any("port" in item.field or "pid" in item.field for item in result.differences)


def test_choices_are_common_valid_source_type_references(pair):
    assert available_selections(*pair) == {(1, 1): ("ATTITUDE",)}
    result = compare_runs(*pair, message_type="UNAVAILABLE")
    assert not result.comparable
    assert any(issue.code == "selection_unavailable" for issue in result.issues)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source": [1, 1]},
        {"source": (True, 1)},
        {"source": (256, 1)},
        {"message_type": ""},
        {"message_type": []},
        {"start_ns": -1},
        {"start_ns": 0.5},
        {"start_ns": True},
        {"start_ns": 60_000_000_000},
        {"end_ns": 0},
        {"end_ns": 60_000_000_001},
        {"end_ns": False},
        {"start_ns": 100, "end_ns": 100},
    ],
)
def test_invalid_caller_selection_is_rejected(pair, kwargs):
    with pytest.raises(ValueError):
        compare_runs(*pair, **kwargs)


def test_real_synthetic_pair_uses_actual_observations_and_requires_no_runner_import(tmp_path):
    directories = {role: tmp_path / role for role in ("baseline", "blackout")}
    for role, directory in directories.items():
        config = ExperimentConfig(role, 2.3, 0.1 if role == "blackout" else None)
        run_experiment(directory, config)
    baseline, blackout = (
        load_run_directory(directories[role]) for role in ("baseline", "blackout")
    )
    result = compare_runs(baseline, blackout)
    assert result.comparable, result.issues
    assert (
        result.metrics["blackout"]["receiver"].count < result.metrics["baseline"]["receiver"].count
    )
    assert (
        result.metrics["blackout"]["relay-input"].count
        > result.metrics["blackout"]["receiver"].count
    )
    assert result.gates["blackout"][0].duration_ns >= 2_000_000_000
    script = """
import socket, subprocess, sys
from pathlib import Path
from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_directory
assert 'uav_debugger.experiment' not in sys.modules
assert 'uav_debugger.sitl' not in sys.modules

def forbidden(*args, **kwargs):
    raise AssertionError('Execution or network attempted')
socket.socket = forbidden
subprocess.Popen = forbidden
result = compare_runs(*(load_run_directory(Path(path)) for path in sys.argv[1:]))
assert result.comparable, result.issues
assert 'uav_debugger.experiment' not in sys.modules
assert 'uav_debugger.sitl' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, *map(str, directories.values())],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_empty_default_window_has_no_negative_duration(pair):
    result = compare_runs(*pair, start_ns=4_000_000_000)
    assert not result.comparable
    assert result.duration_ns is None
    assert result.metrics == {}
    assert any(issue.code == "empty_window" for issue in result.issues)


def test_reader_consistent_observations_cannot_precede_recorded_producer_start(pair):
    def move_start(files):
        rows = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
        start = next(row for row in rows if row["action"] == "producer_started")
        for field, delta in (
            ("monotonic_ns", 1_000_000_000),
            ("elapsed_ns", 1_000_000_000),
            ("unix_us", 1_000_000),
        ):
            start[field] += delta
        files["actions.jsonl"] = _lines(sorted(rows, key=lambda row: row["monotonic_ns"]))

    changed = edited(pair[0], update_files=move_start)
    assert changed.evidence_status == "consistent"
    result = compare_runs(changed, pair[1])
    assert not result.comparable
    assert any(issue.code == "production_evidence" for issue in result.issues)


@pytest.mark.parametrize("mode", ["additional", "list", "different", "missing"])
def test_unaccounted_sitl_defaults_arguments_do_not_disappear_from_compatibility(mode):
    baseline, blackout = fixture_run("baseline", version=2), fixture_run("blackout", version=2)

    def change(manifest):
        simulator = manifest["simulator"]
        argv = simulator["argv"]
        index = argv.index("--defaults")
        if mode == "additional":
            argv.extend(["--defaults", "/unaccounted/custom.parm"])
        elif mode == "list":
            argv[index + 1] += ",/unaccounted/custom.parm"
            simulator["defaults_path"] = argv[index + 1]
        elif mode == "different":
            argv[index + 1] = "/unaccounted/profile.parm"
        else:
            del argv[index : index + 2]

    result = compare_runs(baseline, edited(blackout, change))
    assert not result.comparable
    assert any(issue.code == "simulator_arguments" for issue in result.issues)
