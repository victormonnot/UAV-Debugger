"""Saved-run reports retain independent settings, application and observation evidence."""

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from uav_debugger.analysis import Selection
from uav_debugger.report import build_markdown_report
from uav_debugger.run_report import build_run_markdown_report
from uav_debugger.saved_run import RunIssue, load_run_files

ORIGIN_NS = 1_000_000_000_000
EPOCH_US = 1_700_000_000_000_000
MANIFEST = json.loads(
    (Path(__file__).parent / "fixtures" / "telemetry-gap.expected.json").read_text()
)
FRAMES = [bytes.fromhex(MANIFEST["records"][index]["frame_hex"]) for index in (0, 2, 8)]
OPAQUE = bytes.fromhex("fd010000000101ffffff000000")


def blocks(report):
    return [
        json.loads(match.group("content"))
        for match in re.finditer(
            r"(?P<fence>`{3,})json\n(?P<content>.*?)\n(?P=fence)", report, re.S
        )
    ]


def summaries(report):
    return {
        key: value
        for block in blocks(report)
        if isinstance(block, dict)
        for key, value in block.items()
    }


def stamp(elapsed_ns):
    return {
        "monotonic_ns": ORIGIN_NS + elapsed_ns,
        "elapsed_ns": elapsed_ns,
        "unix_us": EPOCH_US + elapsed_ns // 1000,
    }


def lines(values):
    return b"".join((json.dumps(value) + "\n").encode() for value in values)


def finalize(files, manifest):
    manifest["artifacts"] = {
        name: {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in files.items()
        if name != "run.json"
    }
    files["run.json"] = json.dumps(manifest).encode()
    return files


def fixture_files(*, version=1, opaque=False):
    """Build declared evidence from existing independently encoded fixture frames."""
    measurement = 1_000_000_000 if version == 2 else 0
    frames = [*FRAMES[:2], OPAQUE if opaque else FRAMES[2]]
    files = {}
    observations = []
    datagrams = []
    for point, selected, receipt_delay in (
        ("relay-input", (0, 1, 2), 0),
        ("receiver", (0, 2), 1_000_000),
    ):
        capture = bytearray()
        for record_index, original_index in enumerate(selected):
            elapsed = measurement + (100_000_000, 2_200_000_000, 2_400_000_000)[original_index]
            clock = stamp(elapsed + receipt_delay)
            frame = frames[original_index]
            observation = {
                "point": point,
                "record_index": record_index,
                "offset": len(capture),
                "frame_size_bytes": len(frame),
                "frame_sha256": hashlib.sha256(frame).hexdigest(),
                **clock,
            }
            if version == 2:
                observation.update(datagram_index=record_index, datagram_offset=0)
                datagrams.append(
                    {
                        "point": point,
                        "datagram_index": record_index,
                        "raw_hex": frame.hex(),
                        **clock,
                    }
                )
            observations.append(observation)
            capture.extend(clock["unix_us"].to_bytes(8, "big") + frame)
        files[f"{point}.tlog"] = bytes(capture)
    actions = [
        {"action": "producer_started", **stamp(0)},
        {
            "action": "relay_forwarded",
            "point": "relay-input",
            "record_index": 0,
            **stamp(measurement + 100_100_000),
        },
        {
            "action": "forwarding_disabled",
            "requested_elapsed_ns": measurement + 200_000_000,
            **stamp(measurement + 250_000_000),
        },
        {
            "action": "relay_dropped",
            "point": "relay-input",
            "record_index": 1,
            **stamp(measurement + 2_200_100_000),
        },
        {
            "action": "forwarding_enabled",
            "reason": "blackout_elapsed",
            **stamp(measurement + 2_350_000_000),
        },
        {
            "action": "relay_forwarded",
            "point": "relay-input",
            "record_index": 2,
            **stamp(measurement + 2_400_100_000),
        },
        {
            "action": "producer_stopped",
            "reason": "duration_elapsed",
            **stamp(measurement + 3_000_000_000),
        },
    ]
    if version == 2:
        actions.append({"action": "measurement_started", **stamp(measurement)})
        files["datagrams.jsonl"] = lines(sorted(datagrams, key=lambda item: item["monotonic_ns"]))
        files["simulator.log"] = b"Synthetic test evidence; no simulator was executed.\n"
        files["simulator/profile.parm"] = b"SYNTHETIC_TEST_PARAMETER 1\n"
    files["actions.jsonl"] = lines(sorted(actions, key=lambda item: item["monotonic_ns"]))
    files["observations.jsonl"] = lines(sorted(observations, key=lambda item: item["monotonic_ns"]))
    manifest = {
        "schema": f"uav-debugger-experiment-v{version}",
        "origin_monotonic_ns": ORIGIN_NS,
        "start": stamp(0),
        "measurement_start": stamp(measurement),
        "end": stamp(measurement + 3_200_000_000),
        "outcome": "completed",
        "error": None,
        "capture_profile": "qgc-timestamped-mavlink-v1",
        "requested": {"scenario": "blackout", "blackout_duration_s": 2.0, "blackout_at_s": 0.2},
        "environment": {"uav_debugger": "test-fixture", "pymavlink": "2.4.49"},
        "topology": {"receiver": {"host": "127.0.0.1", "port": 40000}},
        "clocks": {"unix_us": "Host receipt time", "time_boot_ms": "Independent payload clock"},
        "counters": {
            "sender_submitted": 0 if version == 1 else None,
            "missed_emission_slots": 0 if version == 1 else None,
            "relay_observed": 3,
            "receiver_observed": 2,
            "relay_forwarded": 2,
            "relay_dropped": 1,
        },
    }
    return finalize(files, manifest)


@pytest.mark.parametrize("version", [1, 2])
def test_report_separates_requested_gate_from_actual_actions_and_retains_references(version):
    files = fixture_files(version=version)
    run = load_run_files(files, source_name="synthetic saved-run fixture")
    assert run.evidence_status == "consistent", run.issues

    result = summaries(build_run_markdown_report(run))

    assert result["status"]["declared_outcome"] == "completed"
    assert result["status"]["evidence_status"] == "consistent"
    assert result["requested"]["blackout_duration_s"] == 2.0
    gate = result["applied_actions"]["intervals"][0]
    assert gate["duration_ns"] == 2_100_000_000
    assert gate["start"]["monotonic_ns"] == run.gate_intervals[0].start.monotonic_ns
    assert gate["start"]["file_name"] == "actions.jsonl"
    assert gate["start"]["line_number"] == run.gate_intervals[0].start.line_number
    assert gate["end"]["line_number"] == run.gate_intervals[0].end.line_number
    assert result["run_provenance"]["bundle_sha256"] == run.identity
    assert (
        result["run_provenance"]["files"]["run.json"]["sha256"]
        == hashlib.sha256(files["run.json"]).hexdigest()
    )
    for name, raw in files.items():
        assert result["run_provenance"]["files"][name] == {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
    points = result["observed_evidence"]["points"]
    assert [
        (item["point"], item["capture"]["record_count"], item["valid_reference_count"])
        for item in points
    ] == [("relay-input", 3, 3), ("receiver", 2, 2)]
    assert result["observed_evidence"]["declared_counters"]["relay_dropped"] == 1
    first = points[0]["first_valid_observation"]
    assert (first["file_name"], first["line_number"], first["record_index"], first["offset"]) == (
        "observations.jsonl",
        1,
        0,
        0,
    )


def test_v2_report_keeps_startup_measurement_capture_and_payload_clocks_distinct():
    run = load_run_files(fixture_files(version=2))
    report = build_run_markdown_report(run)
    result = summaries(report)

    assert result["clocks"]["origin_monotonic_ns"] == ORIGIN_NS
    assert result["clocks"]["measurement_monotonic_ns"] == ORIGIN_NS + 1_000_000_000
    assert result["clocks"]["startup_duration_ns"] == 1_000_000_000
    assert (
        result["clocks"]["declared_clock_descriptions"]["time_boot_ms"]
        == "Independent payload clock"
    )
    assert all(item["datagram_trace_present"] for item in result["observed_evidence"]["points"])
    assert [item["valid_datagram_count"] for item in result["observed_evidence"]["points"]] == [
        3,
        2,
    ]
    assert "No exact alignment" in report
    assert "raw_hex" not in report


@pytest.mark.parametrize("outcome", ["failed", "interrupted"])
def test_declared_failed_or_interrupted_outcome_is_independent_of_consistent_evidence(outcome):
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["outcome"] = outcome
    manifest["error"] = "Recorded test failure" if outcome == "failed" else None
    run = load_run_files(finalize(files, manifest))

    status = summaries(build_run_markdown_report(run))["status"]

    assert status["declared_outcome"] == outcome
    assert status["evidence_status"] == "consistent"
    assert status["declared_error"] == manifest["error"]


def test_completed_manifest_does_not_override_mismatched_action_fingerprint():
    files = fixture_files()
    files["actions.jsonl"] += b"\n"
    run = load_run_files(files)
    result = summaries(build_run_markdown_report(run))

    assert result["status"]["declared_outcome"] == "completed"
    assert result["status"]["evidence_status"] == "invalid"
    assert result["applied_actions"]["intervals"] == []
    assert result["action_trace"]["valid_entry_count"] == 0
    assert any(item["code"] == "artifact_mismatch" for item in result["issues"]["items"])


def test_open_gate_has_unknown_end_and_no_requested_duration_substitution():
    files = fixture_files()
    events = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
    files["actions.jsonl"] = lines(
        item for item in events if item["action"] != "forwarding_enabled"
    )
    manifest = json.loads(files["run.json"])
    manifest["outcome"] = "interrupted"
    run = load_run_files(finalize(files, manifest))

    result = summaries(build_run_markdown_report(run))

    assert result["status"]["evidence_status"] == "incomplete"
    assert result["requested"]["blackout_duration_s"] == 2.0
    assert result["applied_actions"]["intervals"][0]["end"] is None
    assert result["applied_actions"]["intervals"][0]["duration_ns"] is None


def test_missing_receiver_is_unknown_and_valid_reference_counts_stay_separate():
    files = fixture_files()
    del files["receiver.tlog"]
    observations = [json.loads(line) for line in files["observations.jsonl"].splitlines()]
    files["observations.jsonl"] = lines(
        item for item in observations if item["point"] != "receiver"
    )
    run = load_run_files(finalize(files, json.loads(files["run.json"])))
    report = build_run_markdown_report(run, point="receiver")
    result = summaries(report)

    receiver = result["observed_evidence"]["points"][1]
    assert receiver["capture"] == {
        "present": False,
        "file_name": "receiver.tlog",
        "record_count": None,
    }
    assert receiver["valid_reference_count"] == 0
    assert result["observed_evidence"]["declared_counters"]["receiver_observed"] == 2
    assert result["status"]["evidence_status"] == "incomplete"
    assert "selected capture is unavailable" in report


def test_invalid_observation_does_not_hide_captured_record_or_declared_counter():
    files = fixture_files()
    observations = [json.loads(line) for line in files["observations.jsonl"].splitlines()]
    observations[0]["offset"] = 999
    files["observations.jsonl"] = lines(observations)
    run = load_run_files(finalize(files, json.loads(files["run.json"])))

    result = summaries(build_run_markdown_report(run))

    before = result["observed_evidence"]["points"][0]
    assert before["capture"]["record_count"] == 3
    assert before["valid_reference_count"] == before["distinct_referenced_record_count"] == 2
    assert before["invalid_reference_count"] == 1
    assert result["observed_evidence"]["declared_counters"]["relay_observed"] == 3
    assert result["status"]["evidence_status"] == "invalid"
    assert any(item["code"] == "invalid_observation" for item in result["issues"]["items"])


def test_partial_capture_reports_retained_prefix_and_original_unprocessed_bytes():
    files = fixture_files()
    files["relay-input.tlog"] = files["relay-input.tlog"][:-1]
    run = load_run_files(finalize(files, json.loads(files["run.json"])))

    report = build_run_markdown_report(run, point="relay-input", selected_indices=(1,))
    result = summaries(report)

    capture = result["observed_evidence"]["points"][0]["capture"]
    assert capture["traversal"] == "stopped"
    assert capture["record_count"] == 2
    assert capture["remaining_bytes"] > 0
    assert capture["consumed_bytes"] + capture["remaining_bytes"] == capture["size_bytes"]
    assert result["observed_evidence"]["declared_counters"]["relay_observed"] == 3
    assert result["status"]["evidence_status"] == "invalid"
    assert blocks(report)[-1]["raw_frame_hex"] == FRAMES[1].hex()


def test_unfinalized_run_keeps_unknown_end_and_missing_fingerprints_visible():
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest.update(outcome="running", end=None, artifacts={})
    files["run.json"] = json.dumps(manifest).encode()
    run = load_run_files(files)

    result = summaries(build_run_markdown_report(run))

    assert result["status"]["declared_outcome"] == "running"
    assert result["status"]["evidence_status"] == "incomplete"
    assert result["clocks"]["declared_end"] is None
    assert {"missing_fingerprint", "missing_clock", "unfinalized_run"} <= {
        item["code"] for item in result["issues"]["items"]
    }


def test_unknown_message_decoding_and_unknown_measurement_clock_remain_explicit():
    files = fixture_files(version=2, opaque=True)
    manifest = json.loads(files["run.json"])
    manifest["measurement_start"] = None
    run = load_run_files(finalize(files, manifest))

    result = summaries(build_run_markdown_report(run))

    assert result["clocks"]["measurement_monotonic_ns"] is None
    assert result["clocks"]["startup_duration_ns"] is None
    assert result["observed_evidence"]["points"][0]["capture"]["opaque_count"] == 1
    assert result["observed_evidence"]["points"][0]["capture"]["traversal"] == "complete"


def test_optional_capture_report_retains_filters_inspector_and_original_json_blocks():
    run = load_run_files(fixture_files())
    selection = Selection(sources=((1, 1),), message_ids=(30,))
    expected = build_markdown_report(
        run.captures["receiver"], selection, selected_indices=(1,), attitude_plot_gap_us=1_000_000
    )

    report = build_run_markdown_report(
        run,
        point="receiver",
        selection=selection,
        selected_indices=(1,),
        attitude_plot_gap_us=1_000_000,
    )

    assert report.endswith(expected)
    assert blocks(report)[-5:] == blocks(expected)
    provenance, selected, coverage, _issues, detail = blocks(report)[-5:]
    assert provenance["sha256"] == run.captures["receiver"].sha256
    assert selected["sources"] == [[1, 1]]
    assert coverage["filtered_record_count"] == 1
    assert detail["index"] == 1
    assert detail["raw_frame_hex"] == FRAMES[2].hex()


@pytest.mark.parametrize(
    "arguments",
    [
        {"selection": Selection()},
        {"selected_indices": (0,)},
        {"point": "unknown"},
        {"point": "receiver", "selected_indices": (99,)},
        {"point": "receiver", "selected_indices": tuple(range(101))},
    ],
)
def test_capture_details_cannot_silently_use_wrong_or_missing_selection(arguments):
    run = load_run_files(fixture_files())
    with pytest.raises(ValueError):
        build_run_markdown_report(run, **arguments)


def test_untrusted_labels_and_nested_metadata_remain_json_data():
    malicious = "```\n# Forged heading\n<script>alert(1)</script>&"
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["requested"]["annotation"] = malicious
    manifest["environment"]["label"] = malicious
    run = load_run_files(finalize(files, manifest), source_name=malicious)

    report = build_run_markdown_report(run)
    result = summaries(report)

    assert result["run_provenance"]["source_name"] == malicious
    assert result["requested"]["annotation"] == malicious
    assert result["metadata"]["environment"]["label"] == malicious
    assert "<script>" not in report
    assert "\n# Forged heading\n" not in report
    assert "````json" in report


def test_report_bounds_metadata_and_issue_messages_without_losing_error_priority():
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["environment"]["large_value"] = "x" * 100_000
    run = load_run_files(finalize(files, manifest))
    warnings = tuple(
        RunIssue("warning", "warning", "run.json", None, "w" * 1000) for _ in range(110)
    )
    failure = RunIssue("important_failure", "error", "actions.jsonl", 123, "Failure retained")
    run = replace(run, issues=warnings + (failure,))

    report = build_run_markdown_report(run)
    result = summaries(report)

    assert result["metadata"]["content_omitted"] is True
    assert result["metadata"]["source_file"] == "run.json"
    assert result["issues"]["total_count"] == 111
    assert result["issues"]["included_count"] == 100
    assert result["issues"]["omitted_count"] == 11
    assert any(item["code"] == "important_failure" for item in result["issues"]["items"])
    assert result["issues"]["items"][0]["message_omitted_characters"] == 744
    assert len(report) < 100_000


def test_oversized_declared_fields_do_not_hide_outcome_or_actual_fingerprints():
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["error"] = "e" * 100_000
    finalize(files, manifest)
    manifest = json.loads(files["run.json"])
    manifest["artifacts"]["actions.jsonl"]["annotation"] = "a" * 100_000
    files["run.json"] = json.dumps(manifest).encode()
    run = load_run_files(files, source_name="s" * 100_000)

    result = summaries(build_run_markdown_report(run))

    assert result["status"]["declared_outcome"] == "completed"
    assert result["status"]["declared_error"]["content_omitted"] is True
    provenance = result["run_provenance"]
    assert provenance["files"]["run.json"]["sha256"] == run.files["run.json"].sha256
    assert provenance["declared_artifacts"]["content_omitted"] is True
    assert provenance["source_name_omitted_characters"] == 100_000 - 4096
    assert len(provenance["source_name"]) == 4096


def test_action_excerpt_retains_late_gate_events_and_reports_omissions():
    files = fixture_files()
    actions = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
    actions.extend(
        {"action": "sender_submitted", "sequence": index, **stamp(index + 1)}
        for index in range(150)
    )
    files["actions.jsonl"] = lines(sorted(actions, key=lambda item: item["monotonic_ns"]))
    manifest = json.loads(files["run.json"])
    manifest["counters"]["sender_submitted"] = 150
    run = load_run_files(finalize(files, manifest))
    assert run.evidence_status == "consistent", run.issues

    result = summaries(build_run_markdown_report(run))

    trace = result["action_trace"]
    assert trace["entry_count"] == 157
    assert trace["included_reference_count"] == 100
    assert trace["omitted_reference_count"] == 57
    assert trace["valid_action_counts"]["sender_submitted"] == 150
    assert {"forwarding_disabled", "forwarding_enabled"} <= {
        item["recorded_fields"]["action"] for item in trace["references"]
    }
