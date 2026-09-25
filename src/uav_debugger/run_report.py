"""Bounded offline reports for requested, applied and observed Experiment evidence."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .analysis import Selection
from .report import _application_version, _json_block, build_markdown_report

if TYPE_CHECKING:
    from .saved_run import SavedRun, TraceEvent

MAX_REPORTED_EVENTS = 100
MAX_REPORTED_ISSUES = 100
MAX_REPORTED_OBSERVATIONS = 20
MAX_CAPTURE_DETAILS = 100
MAX_JSON_SECTION_CHARACTERS = 65_536
MAX_ISSUE_MESSAGE_CHARACTERS = 256
POINTS = ("relay-input", "receiver")


def _bounded_value(value: object, *, source_file: str, limit: int) -> object:
    size = len(_json_block(value))
    if size <= limit:
        return value
    return {
        "content_omitted": True,
        "reason": "Value exceeds the report size limit; inspect the original evidence.",
        "serialized_characters": size,
        "limit_characters": limit,
        "source_file": source_file,
    }


def _bounded_block(section: str, value: object, *, source_file: str | None = None) -> str:
    block = _json_block({section: value})
    if len(block) <= MAX_JSON_SECTION_CHARACTERS:
        return block
    return _json_block(
        {
            section: {
                "content_omitted": True,
                "reason": "Section exceeds the report size limit; inspect the original evidence.",
                "serialized_characters": len(block),
                "limit_characters": MAX_JSON_SECTION_CHARACTERS,
                "source_file": source_file,
            }
        }
    )


def _reference(event: TraceEvent) -> dict:
    return {
        "file_name": event.file_name,
        "line_number": event.line_number,
        "valid": event.valid,
        "monotonic_ns": event.monotonic_ns,
        "elapsed_ns": event.elapsed_ns,
        "unix_us": event.unix_us,
    }


def _action_reference(event: TraceEvent) -> dict:
    fields = (
        "action",
        "reason",
        "requested_elapsed_ns",
        "measurement_elapsed_ns",
        "point",
        "record_index",
        "datagram_index",
        "returncode",
        "escalated_to_kill",
    )
    return {
        **_reference(event),
        "recorded_fields": {name: event.data[name] for name in fields if name in event.data},
    }


def _observation_reference(event: TraceEvent) -> dict:
    fields = (
        "point",
        "record_index",
        "offset",
        "frame_size_bytes",
        "frame_sha256",
        "datagram_index",
        "datagram_offset",
    )
    return {
        **_reference(event),
        **{name: event.data[name] for name in fields if name in event.data},
    }


def _observed_point(run: SavedRun, point: str) -> dict:
    capture = run.captures.get(point)
    observations = tuple(item for item in run.observations if item.point == point)
    valid = tuple(item for item in observations if item.valid)
    datagrams = tuple(item for item in run.datagrams if item.data.get("point") == point)
    references = valid[:MAX_REPORTED_OBSERVATIONS]
    return {
        "point": point,
        "capture": (
            {
                "present": True,
                "file_name": f"{point}.tlog",
                "sha256": capture.sha256,
                "size_bytes": len(capture.raw_bytes),
                "traversal": capture.traversal,
                "record_count": len(capture.records),
                "decoded_count": capture.decoded_count,
                "opaque_count": capture.opaque_count,
                "consumed_bytes": capture.consumed_bytes,
                "remaining_bytes": capture.remaining_bytes,
                "import_issue_count": len(capture.issues),
            }
            if capture is not None
            else {"present": False, "file_name": f"{point}.tlog", "record_count": None}
        ),
        "observation_trace_present": "observations.jsonl" in run.files,
        "observation_entry_count": len(observations),
        "valid_reference_count": len(valid),
        "invalid_reference_count": len(observations) - len(valid),
        "distinct_referenced_record_count": len({item.record_index for item in valid}),
        "first_valid_observation": _observation_reference(valid[0]) if valid else None,
        "last_valid_observation": _observation_reference(valid[-1]) if valid else None,
        "included_reference_count": len(references),
        "omitted_reference_count": len(valid) - len(references),
        "references": [_observation_reference(item) for item in references],
        "datagram_trace_present": "datagrams.jsonl" in run.files,
        "datagram_entry_count": len(datagrams),
        "valid_datagram_count": sum(item.valid for item in datagrams),
    }


def build_run_markdown_report(
    run: SavedRun,
    *,
    point: str | None = None,
    selection: Selection | None = None,
    selected_indices: tuple[int, ...] = (),
    attitude_plot_gap_us: int | None = None,
) -> str:
    """Summarize saved evidence without execution, invented clocks or raw trace dumps.

    Counts cover complete reader results; bounded reference excerpts identify
    omissions. A selected capture appends the unchanged file-analysis report.
    """
    if point not in (None, *POINTS):
        raise ValueError("point must be relay-input, receiver or None.")
    if not isinstance(selected_indices, tuple) or len(selected_indices) > MAX_CAPTURE_DETAILS:
        raise ValueError("selected_indices must be a tuple containing at most 100 indices.")
    if point is None and (
        selection is not None or selected_indices or attitude_plot_gap_us is not None
    ):
        raise ValueError("A capture point is required for capture filters or record details.")
    if (
        point is not None
        and point not in run.captures
        and (selection is not None or selected_indices or attitude_plot_gap_us is not None)
    ):
        raise ValueError("Capture filters or details require an available capture.")

    manifest = run.manifest
    artifacts = {
        name: {"sha256": item.sha256, "size_bytes": len(item.raw_bytes)}
        for name, item in run.files.items()
    }
    provenance = {
        "application": {"name": "UAV Debugger", "version": _application_version()},
        "source_name": run.source_name[:4096],
        "source_name_omitted_characters": max(0, len(run.source_name) - 4096),
        "bundle_sha256": run.identity,
        "schema": run.schema,
        "selected_point": point,
        "files": artifacts,
        "declared_artifacts": _bounded_value(
            manifest.get("artifacts"), source_file="run.json", limit=8192
        ),
    }
    status = {
        "declared_outcome": run.declared_outcome,
        "evidence_status": run.evidence_status,
        "declared_error": _bounded_value(manifest.get("error"), source_file="run.json", limit=4096),
        "reader_issue_count": len(run.issues),
    }
    metadata = {
        key: manifest.get(key)
        for key in ("environment", "topology", "capture_profile", "simulator")
    }
    clocks = {
        "declared_clock_descriptions": manifest.get("clocks"),
        "origin_monotonic_ns": run.origin_monotonic_ns,
        "measurement_monotonic_ns": run.measurement_monotonic_ns,
        "declared_start": manifest.get("start"),
        "declared_measurement_start": manifest.get("measurement_start"),
        "declared_end": manifest.get("end"),
        "startup_duration_ns": (
            run.measurement_monotonic_ns - run.origin_monotonic_ns
            if run.origin_monotonic_ns is not None
            and run.measurement_monotonic_ns is not None
            and run.measurement_monotonic_ns >= run.origin_monotonic_ns
            else None
        ),
    }
    intervals = run.gate_intervals[:MAX_REPORTED_EVENTS]
    applied = {
        "interval_count": len(run.gate_intervals),
        "included_interval_count": len(intervals),
        "omitted_interval_count": len(run.gate_intervals) - len(intervals),
        "intervals": [
            {
                "start": _action_reference(interval.start),
                "end": _action_reference(interval.end) if interval.end is not None else None,
                "duration_ns": interval.duration_ns,
            }
            for interval in intervals
        ],
    }
    counts = Counter(
        item.data["action"]
        for item in run.actions
        if item.valid and isinstance(item.data.get("action"), str)
    )
    # Keep lifecycle references available even when per-frame actions fill the trace.
    lifecycle = {
        "producer_started",
        "producer_stopped",
        "simulator_started",
        "simulator_stopped",
        "measurement_started",
        "forwarding_disabled",
        "forwarding_enabled",
        "drain_finished",
    }
    positions = [
        index
        for index, item in enumerate(run.actions)
        if isinstance(item.data.get("action"), str) and item.data["action"] in lifecycle
    ][:MAX_REPORTED_EVENTS]
    chosen = set(positions)
    for index in range(len(run.actions)):
        if len(chosen) >= MAX_REPORTED_EVENTS:
            break
        chosen.add(index)
    references = tuple(run.actions[index] for index in sorted(chosen))
    action_summary = {
        "trace_present": "actions.jsonl" in run.files,
        "entry_count": len(run.actions),
        "valid_entry_count": sum(item.valid for item in run.actions),
        "invalid_entry_count": sum(not item.valid for item in run.actions),
        "valid_action_counts": dict(counts),
        "included_reference_count": len(references),
        "omitted_reference_count": len(run.actions) - len(references),
        "references": [_action_reference(item) for item in references],
    }
    observed = {
        "declared_counters": manifest.get("counters"),
        "declared_datagram_counts": manifest.get("datagrams_observed"),
        "observation_entry_count": len(run.observations),
        "unassigned_observation_entry_count": sum(
            item.point not in POINTS for item in run.observations
        ),
        "datagram_entry_count": len(run.datagrams),
        "points": [_observed_point(run, name) for name in POINTS],
    }
    issue_positions = {index for index, issue in enumerate(run.issues) if issue.severity == "error"}
    issue_positions = set(sorted(issue_positions)[:MAX_REPORTED_ISSUES])
    for index in range(len(run.issues)):
        if len(issue_positions) >= MAX_REPORTED_ISSUES:
            break
        issue_positions.add(index)
    issues = {
        "total_count": len(run.issues),
        "included_count": len(issue_positions),
        "omitted_count": len(run.issues) - len(issue_positions),
        "items": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "file_name": issue.file_name,
                "line_number": issue.line_number,
                "message": issue.message[:MAX_ISSUE_MESSAGE_CHARACTERS],
                "message_omitted_characters": max(
                    0, len(issue.message) - MAX_ISSUE_MESSAGE_CHARACTERS
                ),
            }
            for index, issue in enumerate(run.issues)
            if index in issue_positions
        ],
    }
    sections = [
        "# UAV Debugger saved Experiment report",
        "This offline report distinguishes requested settings, recorded application actions "
        "and observations at each capture point. Opening evidence does not run an experiment.",
        "## Run provenance and status",
        _bounded_block("run_provenance", provenance, source_file="run.json"),
        _bounded_block("status", status, source_file="run.json"),
        "The manifest's declared outcome and the reader's evidence status are independent. "
        "Consistent evidence is not authentication or proof that every message was recorded. "
        "Missing evidence is not replaced by a zero count or a successful outcome.",
        "## Requested settings",
        _bounded_block("requested", run.requested, source_file="run.json"),
        "These settings describe the requested run, not proof that its gate was applied.",
        "## Recorded environment and topology",
        _bounded_block("metadata", metadata, source_file="run.json"),
        "Metadata is retained as declared. Executable and parameter references are evidence; "
        "this report does not execute files or read external paths.",
        "## Clocks and run phases",
        _bounded_block("clocks", clocks, source_file="run.json"),
        "Monotonic nanoseconds establish durations within this run. Elapsed time uses the "
        "run origin; measurement time can begin later, after startup. Host Unix microseconds "
        "identify capture observations. Payload device time remains separate. No exact "
        "alignment, receiver latency or relationship between separate run origins is inferred.",
        "## Applied forwarding intervals",
        _bounded_block("applied_actions", applied, source_file="actions.jsonl"),
        "Gate durations use validated disable/enable events and their monotonic timestamps, "
        "not the requested duration. An interval without an ending event has no established "
        "duration. JSONL line references are one-based.",
        "## Recorded action references",
        _bounded_block("action_trace", action_summary, source_file="actions.jsonl"),
        "References retain lifecycle events first, then earlier trace entries, in original "
        "file order. Only selected action fields are shown; the original files retain "
        "all fields. Invalid entries remain identified and do not establish gate intervals.",
        "## Observed evidence at both points",
        _bounded_block("observed_evidence", observed, source_file="observations.jsonl"),
        "Capture records, valid sidecar references and manifest counters are separate counts. "
        "Original record indices and byte offsets are zero-based. Opaque messages retain "
        "unverified definitions and checksums. Captures establish observation at their named "
        "point; a forwarding action does not alone establish receiver delivery.",
        "## Evidence issues",
        _bounded_block("issues", issues),
        "Counts describe parsed reader results; issues identify any stopped traversal "
        "or unreadable trace remainder. This report includes at most 100 "
        "gate intervals, 100 action references, 20 observation references per point and 100 "
        "issues; omission counts are explicit, and errors take priority over warnings. "
        "Issue messages are capped at 256 characters and source labels at 4,096 characters. "
        "Oversized declared errors and artifact metadata have individual omission notices "
        "so status and supplied file fingerprints remain available. "
        "Each JSON section is capped at 65,536 "
        "characters; oversized sections are replaced by an explicit omission notice. "
        "Original file fingerprints identify evidence retained outside this report.",
        "No physical link loss, autopilot response, failsafe behavior or cause is inferred "
        "from a requested interruption or an interval without recorded observations.",
    ]
    if point is not None:
        sections.extend(["## Selected capture analysis", _json_block({"capture_point": point})])
        capture = run.captures.get(point)
        if capture is None:
            sections.append("The selected capture is unavailable; no capture analysis is invented.")
        else:
            sections.append(
                build_markdown_report(
                    capture,
                    Selection() if selection is None else selection,
                    selected_indices=selected_indices,
                    attitude_plot_gap_us=attitude_plot_gap_us,
                ).rstrip()
            )
    return "\n\n".join(sections) + "\n"
