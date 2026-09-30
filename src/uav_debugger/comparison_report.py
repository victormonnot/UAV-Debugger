"""Bounded reports for an explicit offline baseline/blackout comparison."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from .report import _application_version
from .run_report import (
    MAX_ISSUE_MESSAGE_CHARACTERS,
    MAX_REPORTED_EVENTS,
    MAX_REPORTED_ISSUES,
    MAX_REPORTED_OBSERVATIONS,
    POINTS,
    _action_reference,
    _bounded_block,
    _bounded_value,
    _observation_reference,
    _observed_point,
)

if TYPE_CHECKING:
    from .comparison import ComparedObservation, PointMetrics, RunComparison
    from .saved_run import SavedRun, TraceEvent

MAX_REPORTED_DIFFERENCES = 100
ROLES = ("baseline", "blackout")
LIFECYCLE_ACTIONS = {
    "producer_started",
    "producer_stopped",
    "simulator_started",
    "simulator_stopped",
    "measurement_started",
    "forwarding_disabled",
    "forwarding_enabled",
    "drain_finished",
}


def _relative_action(event: TraceEvent, run: SavedRun) -> dict:
    return {
        **_action_reference(event),
        "measurement_ns": (
            event.monotonic_ns - run.measurement_monotonic_ns
            if event.valid
            and type(event.monotonic_ns) is int
            and run.measurement_monotonic_ns is not None
            else None
        ),
    }


def _compared_reference(item: ComparedObservation) -> dict:
    return {
        **_observation_reference(item.observation),
        "measurement_ns": item.measurement_ns,
        "capture_timestamp_us": item.record.timestamp_us,
        "system_id": item.record.system_id,
        "component_id": item.record.component_id,
        "message_id": item.record.message_id,
        "message_name": item.record.message_name,
        "checksum_status": item.record.checksum_status,
    }


def _point_metrics(metrics: PointMetrics, run: SavedRun, point: str, role: str) -> dict:
    longest = metrics.longest_interval
    return {
        "capture_sha256": run.captures[point].sha256,
        "count": metrics.count,
        "rate_hz": metrics.rate_hz,
        "first": _compared_reference(metrics.first) if metrics.first is not None else None,
        "last": _compared_reference(metrics.last) if metrics.last is not None else None,
        "longest_interval": (
            {
                "duration_ns": longest.duration_ns,
                "previous": _compared_reference(longest.previous),
                "current": _compared_reference(longest.current),
            }
            if longest is not None
            else None
        ),
        "reference_excerpt_section": f"{role}_{point.replace('-', '_')}_selection_references",
    }


def _metrics(comparison: RunComparison) -> dict:
    points = []
    for point in POINTS:
        item = {"point": point, "baseline": None, "blackout": None, "delta": None}
        if comparison.comparable:
            before = comparison.metrics["baseline"][point]
            after = comparison.metrics["blackout"][point]
            item.update(
                baseline=_point_metrics(before, comparison.baseline, point, "baseline"),
                blackout=_point_metrics(after, comparison.blackout, point, "blackout"),
                delta={
                    "count": after.count - before.count,
                    "rate_hz": after.rate_hz - before.rate_hz,
                    "longest_interval_ns": (
                        after.longest_interval.duration_ns - before.longest_interval.duration_ns
                        if after.longest_interval is not None
                        and before.longest_interval is not None
                        else None
                    ),
                },
            )
        points.append(item)
    return {
        "available": comparison.comparable,
        "delta_direction": "blackout minus baseline",
        "points": points,
    }


def _run_provenance(run: SavedRun) -> dict:
    return {
        "source_name": run.source_name[:4096],
        "source_name_omitted_characters": max(0, len(run.source_name) - 4096),
        "bundle_sha256": run.identity,
        "schema": run.schema,
        "declared_outcome": _bounded_value(
            run.declared_outcome, source_file="run.json", limit=1024
        ),
        "evidence_status": run.evidence_status,
        "capture_profile": run.manifest.get("capture_profile"),
        "declared_simulator": _bounded_value(
            run.manifest.get("simulator"), source_file="run.json", limit=8192
        ),
        "declared_decoder": _bounded_value(
            run.manifest.get("environment", {}).get("pymavlink")
            if isinstance(run.manifest.get("environment"), Mapping)
            else None,
            source_file="run.json",
            limit=4096,
        ),
        "files": {
            name: {"sha256": item.sha256, "size_bytes": item.size_bytes}
            for name, item in run.files.items()
        },
    }


def _run_actions(run: SavedRun) -> dict:
    intervals = run.gate_intervals[:MAX_REPORTED_EVENTS]
    lifecycle = tuple(
        item
        for item in run.actions
        if isinstance(item.data.get("action"), str) and item.data["action"] in LIFECYCLE_ACTIONS
    )
    references = lifecycle[:MAX_REPORTED_EVENTS]
    return {
        "interval_count": len(run.gate_intervals),
        "included_interval_count": len(intervals),
        "omitted_interval_count": len(run.gate_intervals) - len(intervals),
        "intervals": [
            {
                "start": _relative_action(interval.start, run),
                "end": _relative_action(interval.end, run) if interval.end is not None else None,
                "duration_ns": interval.duration_ns,
            }
            for interval in intervals
        ],
        "lifecycle_entry_count": len(lifecycle),
        "included_lifecycle_reference_count": len(references),
        "omitted_lifecycle_reference_count": len(lifecycle) - len(references),
        "lifecycle_references": [_relative_action(item, run) for item in references],
    }


def _run_issues(run: SavedRun) -> dict:
    chosen = [index for index, item in enumerate(run.issues) if item.severity == "error"]
    positions = set(chosen[:MAX_REPORTED_ISSUES])
    for index in range(len(run.issues)):
        if len(positions) >= MAX_REPORTED_ISSUES:
            break
        positions.add(index)
    return {
        "total_count": len(run.issues),
        "included_count": len(positions),
        "omitted_count": len(run.issues) - len(positions),
        "items": [
            {
                "code": item.code,
                "severity": item.severity,
                "file_name": item.file_name,
                "line_number": item.line_number,
                "message": item.message[:MAX_ISSUE_MESSAGE_CHARACTERS],
                "message_omitted_characters": max(
                    0, len(item.message) - MAX_ISSUE_MESSAGE_CHARACTERS
                ),
            }
            for index, item in enumerate(run.issues)
            if index in positions
        ],
    }


def build_comparison_markdown_report(comparison: RunComparison) -> str:
    """Export selected evidence and blocking reasons without executing either run.

    Original evidence references retain their own clocks. Report excerpts are
    bounded; counts and metric values describe the complete eligible selection.
    """
    runs = {role: getattr(comparison, role) for role in ROLES}
    reasons = comparison.issues[:MAX_REPORTED_ISSUES]
    differences = comparison.differences[:MAX_REPORTED_DIFFERENCES]
    sections = [
        "# UAV Debugger saved Experiment comparison report",
        "This offline report compares one baseline and one blackout using an explicit common "
        "measurement window. Requested settings, applied actions and observed messages remain "
        "separate. Opening this evidence does not run an experiment.",
        "## Provenance and eligibility",
        _bounded_block(
            "comparison_provenance",
            {
                "application": {"name": "UAV Debugger", "version": _application_version()},
                "runs": {role: _run_provenance(run) for role, run in runs.items()},
            },
        ),
        _bounded_block(
            "comparison_status",
            {
                "comparable": comparison.comparable,
                "blocking_reason_count": len(comparison.issues),
                "included_reason_count": len(reasons),
                "omitted_reason_count": len(comparison.issues) - len(reasons),
                "reasons": [
                    {
                        "code": item.code,
                        "run_role": item.run_role,
                        "message": item.message[:MAX_ISSUE_MESSAGE_CHARACTERS],
                        "message_omitted_characters": max(
                            0, len(item.message) - MAX_ISSUE_MESSAGE_CHARACTERS
                        ),
                    }
                    for item in reasons
                ],
            },
        ),
        "Declared outcomes and evidence consistency are independent. Blocked comparisons "
        "retain inspectable evidence but have no comparison metrics or deltas. Consistency "
        "checks do not authenticate supplied files or establish complete recording.",
        "## Selection and common measurement window",
        _bounded_block(
            "comparison_selection",
            {
                "source": {"system_id": comparison.source[0], "component_id": comparison.source[1]},
                "message_type": comparison.message_type,
                "points": POINTS,
                "window": {
                    "start_ns": comparison.start_ns,
                    "end_ns": comparison.end_ns,
                    "duration_ns": comparison.duration_ns,
                    "bounds": "half-open [start, end)",
                    "clock": "observation monotonic_ns minus its run measurement_monotonic_ns",
                },
            },
        ),
        "Each run has its own monotonic origin. Relative measurement time permits equal "
        "windows; it does not synchronize separate clocks, align frames or match payload "
        "events. Recorded SITL startup and post-measurement drain are excluded. Synthetic "
        "v1 retains its run origin, including initial setup before producer start. Unix "
        "capture time and device time remain unchanged and do not align the comparison.",
        "## Requested settings and configuration differences",
        _bounded_block(
            "comparison_differences",
            {
                "total_count": len(comparison.differences),
                "included_count": len(differences),
                "omitted_count": len(comparison.differences) - len(differences),
                "items": [
                    {
                        "field": item.field,
                        "baseline": _bounded_value(
                            item.baseline, source_file="run.json", limit=4096
                        ),
                        "blackout": _bounded_value(
                            item.blackout, source_file="run.json", limit=4096
                        ),
                        "blocking": item.blocking,
                    }
                    for item in differences
                ],
            },
        ),
    ]
    for role, run in runs.items():
        sections.extend(
            [
                _bounded_block(f"{role}_requested", run.requested, source_file="run.json"),
                _bounded_block(
                    f"{role}_clocks",
                    {
                        "origin_monotonic_ns": run.origin_monotonic_ns,
                        "measurement_monotonic_ns": run.measurement_monotonic_ns,
                        "declared_start": run.manifest.get("start"),
                        "declared_measurement_start": run.manifest.get("measurement_start"),
                        "declared_end": run.manifest.get("end"),
                        "declared_clock_descriptions": _bounded_value(
                            run.manifest.get("clocks"), source_file="run.json", limit=8192
                        ),
                    },
                    source_file="run.json",
                ),
            ]
        )
    sections.extend(
        [
            "## Observed comparison",
            _bounded_block("comparison_metrics", _metrics(comparison)),
            "Counts include only validated observation references for the selected source, "
            "message type and window. Rate is count divided by the common window duration. "
            "The longest interval is between consecutive selected observations within that "
            "window; it does not include unobserved edges. Fewer than two observations leave "
            "the interval unavailable. Delta means blackout minus baseline. No sequence "
            "matching, inter-run packet correspondence or statistical significance is inferred. "
            "A frame can straddle a window boundary between observation points, so receiver "
            "counts may exceed relay-input counts. Their difference is not a delivery ratio "
            "or a packet-loss estimate.",
        ]
    )
    if comparison.comparable:
        for role, run in runs.items():
            for point in POINTS:
                selected = comparison.metrics[role][point].observations
                references = selected[:MAX_REPORTED_OBSERVATIONS]
                sections.append(
                    _bounded_block(
                        f"{role}_{point.replace('-', '_')}_selection_references",
                        {
                            "point": point,
                            "bundle_sha256": run.identity,
                            "capture_sha256": run.captures[point].sha256,
                            "total_reference_count": len(selected),
                            "included_reference_count": len(references),
                            "omitted_reference_count": len(selected) - len(references),
                            "references": [_compared_reference(item) for item in references],
                        },
                        source_file="observations.jsonl",
                    )
                )
    sections.append("## Applied actions and original evidence")
    for role, run in runs.items():
        sections.extend(
            [
                _bounded_block(
                    f"{role}_applied_actions", _run_actions(run), source_file="actions.jsonl"
                ),
                _bounded_block(
                    f"{role}_observed_evidence",
                    {"points": [_observed_point(run, point) for point in POINTS]},
                    source_file="observations.jsonl",
                ),
                _bounded_block(f"{role}_issues", _run_issues(run)),
            ]
        )
    sections.extend(
        [
            "Applied gate boundaries retain the original action clocks and measurement-relative "
            "positions, including boundaries outside the selected window. A missing closure "
            "has no established duration; requested duration never substitutes for it. Original "
            "evidence counts above cover the supplied runs independently of the comparison "
            "window. JSONL lines are one-based; capture indices and byte offsets are zero-based.",
            "## Limits and retained evidence",
            "At most 100 configuration differences and blocking reasons, 100 gate intervals, "
            "100 lifecycle references and 100 reader issues per run, and 20 observation excerpts "
            "per point and selection are shown. First, last and longest-interval references "
            "are retained separately. Omission counts are explicit; reader errors take priority. "
            "Messages are capped at 256 characters and source labels at 4,096 characters. "
            "Each JSON section is capped at 65,536 characters; oversized content receives an "
            "explicit omission notice. Fingerprints identify the original files, which should "
            "be retained alongside this derived report.",
            "Observations and count differences do not establish physical packet loss, exact "
            "transport latency, vehicle behavior, failsafe response or an automatic causal "
            "diagnosis. Matching settings do not prove equivalent operating conditions.",
        ]
    )
    return "\n\n".join(sections) + "\n"
