"""Comparison exports preserve selected evidence, unavailable values and both origins."""

import json
import re
from dataclasses import replace

import pytest
from test_run_report import finalize, fixture_files, lines

from uav_debugger.comparison import ConfigurationDifference, compare_runs
from uav_debugger.comparison_report import build_comparison_markdown_report
from uav_debugger.saved_run import RunIssue, TraceEvent, load_run_files


def blocks(report):
    return [
        json.loads(match.group("content"))
        for match in re.finditer(
            r"(?P<fence>`{3,})json\n(?P<content>.*?)\n(?P=fence)", report, re.S
        )
    ]


def summaries(report):
    return {key: value for block in blocks(report) for key, value in block.items()}


def run_files(scenario, *, origin_shift=0):
    """Adapt independently encoded public frames into two declared saved-run fixtures."""
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["requested"].update(
        scenario=scenario,
        duration_s=3,
        source="synthetic",
        rate_hz=20,
        message="ATTITUDE",
        wire_version=2,
        dialect="common",
        system_id=1,
        component_id=1,
        drain_timeout_s=0.2,
    )
    actions = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
    observations = [json.loads(line) for line in files["observations.jsonl"].splitlines()]
    if scenario == "baseline":
        manifest["requested"].update(blackout_at_s=None, blackout_duration_s=None)
        actions = [item for item in actions if not item["action"].startswith("forwarding_")]
        for item in actions:
            if item["action"] == "relay_dropped":
                item["action"] = "relay_forwarded"
        observations = [item for item in observations if item["point"] == "relay-input"]
        observations += [{**item, "point": "receiver"} for item in observations]
        observations.sort(key=lambda item: item["monotonic_ns"])
        files["receiver.tlog"] = files["relay-input.tlog"]
        manifest["counters"].update(receiver_observed=3, relay_forwarded=3, relay_dropped=0)
    manifest["origin_monotonic_ns"] += origin_shift
    for event in [
        manifest["start"],
        manifest["measurement_start"],
        manifest["end"],
        *actions,
        *observations,
    ]:
        event["monotonic_ns"] += origin_shift
    files["actions.jsonl"] = lines(actions)
    files["observations.jsonl"] = lines(observations)
    return finalize(files, manifest)


@pytest.fixture
def pair():
    return (
        load_run_files(run_files("baseline"), source_name="baseline directory"),
        load_run_files(
            run_files("blackout", origin_shift=8_000_000_000_000), source_name="blackout directory"
        ),
    )


def compare(pair, **kwargs):
    result = compare_runs(*pair, **kwargs)
    assert result.comparable, result.issues
    return result


def test_both_fingerprints_selection_clock_origins_and_configuration_differences(pair):
    comparison = compare(pair)
    report = build_comparison_markdown_report(comparison)
    result = summaries(report)
    assert result["comparison_status"]["comparable"] is True
    provenance = result["comparison_provenance"]["runs"]
    for role, run in zip(("baseline", "blackout"), pair, strict=True):
        assert provenance[role]["bundle_sha256"] == run.identity
        assert provenance[role]["source_name"] == run.source_name
        assert provenance[role]["files"] == {
            name: {"sha256": item.sha256, "size_bytes": item.size_bytes}
            for name, item in run.files.items()
        }
        assert result[f"{role}_requested"] == run.requested
        assert result[f"{role}_clocks"]["measurement_monotonic_ns"] == run.measurement_monotonic_ns
    selection = result["comparison_selection"]
    assert selection["source"] == {"system_id": 1, "component_id": 1}
    assert selection["message_type"] == "ATTITUDE"
    assert selection["window"]["start_ns"] == 0
    assert selection["window"]["end_ns"] == selection["window"]["duration_ns"] == 3_000_000_000
    assert selection["window"]["bounds"] == "half-open [start, end)"
    differences = {item["field"]: item for item in result["comparison_differences"]["items"]}
    assert differences["requested.scenario"] == {
        "field": "requested.scenario",
        "baseline": "baseline",
        "blackout": "blackout",
        "blocking": False,
    }
    assert "do not establish physical packet loss" in report


def test_metrics_delta_and_exact_observation_references(pair):
    comparison = compare(pair)
    result = summaries(build_comparison_markdown_report(comparison))
    metrics = result["comparison_metrics"]
    assert metrics["available"] is True
    assert metrics["delta_direction"] == "blackout minus baseline"
    before, after = metrics["points"]
    assert before["point"] == "relay-input"
    assert before["baseline"]["count"] == before["blackout"]["count"] == 2
    assert before["delta"] == {"count": 0, "rate_hz": 0.0, "longest_interval_ns": 0}
    assert after["point"] == "receiver"
    assert after["baseline"]["count"] == 2
    assert after["blackout"]["count"] == 1
    assert after["delta"]["count"] == -1
    assert after["delta"]["rate_hz"] == pytest.approx(-1 / 3)
    assert after["blackout"]["longest_interval"] is None
    assert after["delta"]["longest_interval_ns"] is None
    for role, run in zip(("baseline", "blackout"), pair, strict=True):
        summary = before[role]
        assert summary["capture_sha256"] == run.captures["relay-input"].sha256
        for reference in result[summary["reference_excerpt_section"]]["references"]:
            observation = run.observations[reference["line_number"] - 1]
            record = run.captures["relay-input"].records[reference["record_index"]]
            assert reference["monotonic_ns"] == observation.monotonic_ns
            assert reference["elapsed_ns"] == observation.elapsed_ns
            assert reference["unix_us"] == reference["capture_timestamp_us"] == record.timestamp_us
            assert (
                reference["measurement_ns"]
                == observation.monotonic_ns - run.measurement_monotonic_ns
            )
            assert reference["offset"] == record.offset
            assert reference["frame_size_bytes"] == len(record.raw_frame)
        longest = summary["longest_interval"]
        assert longest["duration_ns"] == 200_000_000
        assert longest["previous"] == summary["first"]
        assert longest["current"] == summary["last"]


def test_actual_gate_boundaries_are_not_shifted_to_requested_or_selected_window(pair):
    result = summaries(build_comparison_markdown_report(compare(pair, start_ns=2_350_000_000)))
    assert result["blackout_requested"]["blackout_at_s"] == 0.2
    gate = result["blackout_applied_actions"]["intervals"][0]
    assert gate["start"]["measurement_ns"] == 250_000_000
    assert gate["end"]["measurement_ns"] == 2_350_000_000
    assert gate["duration_ns"] == 2_100_000_000
    assert gate["start"]["monotonic_ns"] == pair[1].gate_intervals[0].start.monotonic_ns
    assert gate["start"]["line_number"] == pair[1].gate_intervals[0].start.line_number
    assert result["comparison_selection"]["window"]["start_ns"] == 2_350_000_000
    assert result["baseline_applied_actions"]["interval_count"] == 0


def test_empty_eligible_window_has_zero_counts_and_unknown_intervals(pair):
    result = summaries(build_comparison_markdown_report(compare(pair, end_ns=50_000_000)))
    for point in result["comparison_metrics"]["points"]:
        for role in ("baseline", "blackout"):
            assert point[role]["count"] == point[role]["rate_hz"] == 0
            assert (
                point[role]["first"]
                is point[role]["last"]
                is point[role]["longest_interval"]
                is None
            )
            assert result[point[role]["reference_excerpt_section"]]["references"] == []
        assert point["delta"] == {"count": 0, "rate_hz": 0.0, "longest_interval_ns": None}


def test_half_open_window_excludes_record_at_exact_end_and_retains_original_indices(pair):
    result = summaries(
        build_comparison_markdown_report(
            compare(pair, start_ns=2_200_000_000, end_ns=2_400_000_000)
        )
    )
    before = result["comparison_metrics"]["points"][0]
    for role in ("baseline", "blackout"):
        assert before[role]["count"] == 1
        assert before[role]["first"]["record_index"] == 1
        assert before[role]["first"]["measurement_ns"] == 2_200_000_000


@pytest.mark.parametrize(
    "change", ["missing_receiver", "invalid_hash", "failed", "missing_duration"]
)
def test_blocked_report_retains_evidence_and_reasons_without_metrics(pair, change):
    files = run_files("blackout")
    manifest = json.loads(files["run.json"])
    if change == "missing_receiver":
        del files["receiver.tlog"]
    elif change == "invalid_hash":
        manifest["artifacts"]["receiver.tlog"]["sha256"] = "0" * 64
    elif change == "failed":
        manifest["outcome"] = "failed"
    else:
        del manifest["requested"]["duration_s"]
    files["run.json"] = json.dumps(manifest).encode()
    run = load_run_files(files)
    comparison = compare_runs(pair[0], run)
    assert not comparison.comparable
    result = summaries(build_comparison_markdown_report(comparison))
    assert result["comparison_status"]["blocking_reason_count"] > 0
    assert result["comparison_status"]["reasons"]
    assert result["comparison_metrics"]["available"] is False
    for point in result["comparison_metrics"]["points"]:
        assert point["baseline"] is point["blackout"] is point["delta"] is None
    assert result["comparison_provenance"]["runs"]["blackout"]["bundle_sha256"] == run.identity
    assert result["blackout_observed_evidence"]["points"][0]["capture"]["record_count"] == 3
    if change == "missing_receiver":
        assert result["blackout_observed_evidence"]["points"][1]["capture"]["record_count"] is None
    if change == "missing_duration":
        assert result["comparison_selection"]["window"]["end_ns"] is None


def test_unclosed_gate_does_not_gain_requested_duration(pair):
    files = run_files("blackout")
    actions = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
    files["actions.jsonl"] = lines(
        item for item in actions if item["action"] != "forwarding_enabled"
    )
    run = load_run_files(finalize(files, json.loads(files["run.json"])))
    result = summaries(build_comparison_markdown_report(compare_runs(pair[0], run)))
    assert result["comparison_status"]["comparable"] is False
    assert result["blackout_requested"]["blackout_duration_s"] == 2
    gate = result["blackout_applied_actions"]["intervals"][0]
    assert gate["end"] is gate["duration_ns"] is None


def test_external_text_is_json_escaped_without_changing_parsed_content(pair):
    label = "<img src=x onerror=alert(1)>\n```\n# injected & ```json"
    run = replace(
        pair[0],
        source_name=label,
        manifest={**pair[0].manifest, "requested": {**pair[0].requested, "note": label}},
    )
    report = build_comparison_markdown_report(compare_runs(run, pair[1]))
    result = summaries(report)
    assert "<img" not in report
    assert "\\u003cimg" in report
    assert result["comparison_provenance"]["runs"]["baseline"]["source_name"] == label
    assert result["baseline_requested"]["note"] == label
    outside_blocks = re.sub(r"(?P<fence>`{3,})json\n.*?\n(?P=fence)", "", report, flags=re.S)
    assert "# injected" not in outside_blocks


def test_oversized_requested_and_simulator_metadata_keep_provenance_and_status(pair):
    run = replace(
        pair[0],
        source_name="S" * 5000,
        manifest={
            **pair[0].manifest,
            "requested": {**pair[0].requested, "note": "N" * 70_000},
            "simulator": {"note": "X" * 10_000},
            "outcome": "O" * 70_000,
            "environment": {"pymavlink": "D" * 70_000},
        },
    )
    report = build_comparison_markdown_report(compare_runs(run, pair[1]))
    result = summaries(report)
    provenance = result["comparison_provenance"]["runs"]["baseline"]
    assert provenance["bundle_sha256"] == run.identity
    assert provenance["source_name_omitted_characters"] == 904
    assert len(provenance["source_name"]) == 4096
    assert provenance["declared_simulator"]["content_omitted"] is True
    assert provenance["declared_outcome"]["content_omitted"] is True
    assert provenance["declared_decoder"]["content_omitted"] is True
    assert result["baseline_requested"]["content_omitted"] is True
    assert result["baseline_requested"]["limit_characters"] == 65_536
    assert result["comparison_status"]["comparable"] is False


def test_issue_and_difference_caps_preserve_error_priority_and_explicit_omissions(pair):
    issues = tuple(RunIssue("warning", "warning", "run.json", None, "W" * 300) for _ in range(110))
    issues += (RunIssue("last_error", "error", "actions.jsonl", 500, "E" * 300),)
    run = replace(pair[0], issues=issues)
    comparison = compare_runs(run, pair[1])
    comparison = replace(
        comparison,
        differences=tuple(
            ConfigurationDifference(f"requested.test_{index}", 1, 2, True) for index in range(105)
        ),
    )
    result = summaries(build_comparison_markdown_report(comparison))
    summary = result["baseline_issues"]
    assert summary["total_count"] == 111
    assert summary["included_count"] == 100
    assert summary["omitted_count"] == 11
    assert summary["items"][-1]["code"] == "last_error"
    assert all(len(item["message"]) == 256 for item in summary["items"])
    assert all(item["message_omitted_characters"] == 44 for item in summary["items"])
    assert result["comparison_differences"]["total_count"] == 105
    assert result["comparison_differences"]["included_count"] == 100
    assert result["comparison_differences"]["omitted_count"] == 5


def test_malformed_action_name_remains_reportable_without_executing_anything(pair):
    event = TraceEvent("actions.jsonl", 99, {"action": ["not", "an", "action"]}, False)
    run = replace(
        pair[0],
        actions=(*pair[0].actions, event),
        issues=(RunIssue("invalid_action", "error", "actions.jsonl", 99, "Invalid action name."),),
    )
    result = summaries(build_comparison_markdown_report(compare_runs(run, pair[1])))
    assert result["comparison_status"]["comparable"] is False
    assert result["baseline_issues"]["items"][0]["code"] == "invalid_action"


def test_reference_excerpts_cannot_displace_aggregate_metrics_from_report(pair):
    comparison = compare(pair)
    metrics = {
        role: {
            point: replace(
                metric,
                count=len(metric.observations) * 100,
                rate_hz=len(metric.observations) * 100 / 3,
                observations=metric.observations * 100,
            )
            for point, metric in points.items()
        }
        for role, points in comparison.metrics.items()
    }
    result = summaries(build_comparison_markdown_report(replace(comparison, metrics=metrics)))
    for point in result["comparison_metrics"]["points"]:
        for role in ("baseline", "blackout"):
            summary = point[role]
            selected = metrics[role][point["point"]]
            assert summary["count"] == len(selected.observations)
            assert summary["first"] is not None
            assert summary["last"] is not None
            excerpt = result[summary["reference_excerpt_section"]]
            assert excerpt["total_reference_count"] == len(selected.observations)
            assert excerpt["included_reference_count"] == len(excerpt["references"]) == 20
            assert excerpt["omitted_reference_count"] == len(selected.observations) - 20
