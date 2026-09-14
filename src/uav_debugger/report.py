"""Render a local Markdown evidence summary from an explicit analysis selection."""

import json
import math
import re
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

from .analysis import Selection, observed_intervals, select_records
from .model import ImportResult, Record

MAX_REPORTED_ISSUES = 100


def _json_value(value: object) -> object:
    """Keep field values inspectable, including byte arrays and nonfinite floats."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite_float": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_block(value: object) -> str:
    # External labels and text only appear as data. JSON escapes controls, and
    # HTML characters are escaped without changing the parsed JSON values.
    content = json.dumps(_json_value(value), ensure_ascii=True, indent=2, allow_nan=False)
    content = content.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")
    fence_length = max((len(run) + 1 for run in re.findall(r"`+", content)), default=3)
    fence = "`" * max(3, fence_length)
    return f"{fence}json\n{content}\n{fence}"


def _application_version() -> str:
    try:
        return version("uav-debugger")
    except PackageNotFoundError:
        return "unknown (package metadata unavailable)"


def _record_details(record: Record) -> dict[str, object]:
    return {
        "index": record.index,
        "offset": record.offset,
        "frame_offset": record.frame_offset,
        "end_offset": record.end_offset,
        "timestamp_us": record.timestamp_us,
        "wire_version": record.wire_version,
        "system_id": record.system_id,
        "component_id": record.component_id,
        "message_id": record.message_id,
        "message_name": record.message_name,
        "sequence": record.sequence,
        "checksum_status": record.checksum_status,
        "fields": record.fields,
        "raw_frame_hex": record.raw_frame.hex(),
    }


def build_markdown_report(
    result: ImportResult,
    selection: Selection,
    *,
    selected_indices: tuple[int, ...] = (),
) -> str:
    """Export complete counts/issues and only explicitly requested record details.

    Detail indices must be distinct indices in the current filtered selection.
    The issue list is capped with an explicit omitted count; input and filtered
    record counts always describe their entire respective sets.
    """
    if not isinstance(selected_indices, tuple) or any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in selected_indices
    ):
        raise ValueError("selected_indices must be a tuple of nonnegative record indices.")
    if len(set(selected_indices)) != len(selected_indices):
        raise ValueError("selected_indices must not contain duplicates.")
    filtered = select_records(result, selection)
    requested = set(selected_indices)
    details = tuple(record for record in filtered if record.index in requested)
    if len(details) != len(requested):
        raise ValueError("Every detail record index must belong to the current selection.")
    # A long warning list must not hide why a partial import stopped. The
    # importer currently emits at most one error; reserve its place in the cap,
    # fill the rest with the earliest issues, then restore original issue order.
    issue_positions = {
        index for index, issue in enumerate(result.issues) if issue.severity == "error"
    }
    issue_positions = set(sorted(issue_positions)[:MAX_REPORTED_ISSUES])
    for index in range(len(result.issues)):
        if len(issue_positions) == MAX_REPORTED_ISSUES:
            break
        issue_positions.add(index)
    issues = tuple(result.issues[index] for index in sorted(issue_positions))
    omitted_issues = len(result.issues) - len(issues)
    filtered_decoded = sum(record.fields is not None for record in filtered)
    intervals = observed_intervals(result, selection)
    established_count = 0
    longest = None
    for interval in intervals:
        if interval.delta_us is not None:
            established_count += 1
            if longest is None or interval.delta_us > longest.delta_us:
                longest = interval
    interval_summary = {
        "total_count": len(intervals),
        "established_count": established_count,
        "unavailable_count": len(intervals) - established_count,
        "longest": (
            {
                "delta_us": longest.delta_us,
                "previous_index": longest.previous_index,
                "index": longest.index,
                "system_id": longest.system_id,
                "component_id": longest.component_id,
                "message_id": longest.message_id,
            }
            if longest is not None
            else None
        ),
    }
    interval_description = (
        f"Successive selected pairs of the same source and message type: {len(intervals)} total, "
        f"{established_count} established, {len(intervals) - established_count} unavailable."
    )
    if longest is not None:
        interval_description += (
            f" The longest established interval is {longest.delta_us} microseconds, "
            f"from original record {longest.previous_index} to record {longest.index} "
            f"(source {longest.system_id} / {longest.component_id}, "
            f"message ID {longest.message_id})."
        )
    else:
        interval_description += " No longest established interval is available."
    source = {
        "application": {"name": "UAV Debugger", "version": _application_version()},
        "source_name": result.source_name,
        "sha256": result.sha256,
        "size_bytes": len(result.raw_bytes),
        "profile": result.profile,
        "dialect": result.dialect,
        "decoder_version": result.decoder_version,
    }
    filters = {
        "sources": selection.sources,
        "message_ids": selection.message_ids,
        "start_us": selection.start_us,
        "end_us": selection.end_us,
        "bounds": "inclusive",
        "relative_origin_us": result.records[0].timestamp_us if result.records else None,
    }
    coverage = {
        "traversal": result.traversal,
        "consumed_bytes": result.consumed_bytes,
        "remaining_bytes": result.remaining_bytes,
        "imported_record_count": len(result.records),
        "imported_decoded_count": result.decoded_count,
        "imported_opaque_count": result.opaque_count,
        "filtered_record_count": len(filtered),
        "filtered_decoded_count": filtered_decoded,
        "filtered_opaque_count": len(filtered) - filtered_decoded,
        "explicit_detail_record_count": len(details),
        "import_issue_count": len(result.issues),
        "reported_issue_count": len(issues),
        "omitted_issue_count": omitted_issues,
        "observation_intervals": interval_summary,
    }
    issue_data = [
        {
            "severity": issue.severity,
            "code": issue.code,
            "record_index": issue.record_index,
            "offset": issue.offset,
            "message": issue.message,
        }
        for issue in issues
    ]
    sections = [
        "# UAV Debugger — Analyze report",
        "## Input provenance",
        _json_block(source),
        "## Selection",
        "Time bounds are inclusive original capture timestamps in integer microseconds. "
        "For source and message filters, null means all values and [] means no values. "
        "The relative display origin is the first imported record's timestamp.",
        _json_block(filters),
        "## Coverage",
        _json_block(coverage),
        "Counts cover the complete imported prefix and the complete filtered selection. "
        "Only explicitly selected records appear in the detail section; this report is "
        "an evidence summary, not a recording archive.",
        "## Observation interval summary",
        interval_description,
        "The coverage data retains the interval's two original record references even when "
        "only one endpoint was selected for detailed export. This is an observation interval, "
        "not a physical packet-loss measurement. Equal maxima use the first pair in file order.",
        "## Clock and observation limits",
        "The outer timestamp is the logger's host wall clock, interpreted as unsigned "
        "Unix-epoch microseconds under the declared input profile. Microsecond units "
        "do not establish resolution, clock accuracy or synchronization with the vehicle. "
        "Device timestamps remain separate fields; no clock alignment is inferred.",
        "File order and original integer timestamps are preserved. Repeated and decreasing "
        "timestamps remain visible in the import issues, including records outside the "
        "selection. Observation intervals crossing an original clock regression, or with "
        "opaque endpoints, are not established. An interval without observations does not "
        "establish physical packet loss, vehicle inactivity, sensor latency or causation.",
        "A MAVLink checksum covers its frame, not the outer recording timestamp, and does "
        "not authenticate the sender. Opaque records have unverified definitions and "
        "checksums; advancing by their declared lengths does not verify their framing. "
        "Unsigned frames in a log do not establish whether the original traffic was signed.",
        "Complete file traversal does not prove that the producer recorded an entire "
        "session. If traversal stopped, the remaining bytes are unprocessed and their "
        "observations are unavailable.",
        "## Import issues",
        "These issues describe the whole imported input, including evidence outside the filters.",
        f"Showing {len(issues)} of {len(result.issues)} import issues; "
        f"{omitted_issues} omitted from this report (limit {MAX_REPORTED_ISSUES}).",
        "Error causes take priority within this limit; the remaining places contain the "
        "earliest issues. Displayed issues retain their original order.",
        _json_block(issue_data),
        "## Explicit record details",
        "Indices and byte offsets are zero-based. offset includes the 8-byte timestamp; "
        "frame_offset starts at the frame marker and end_offset is exclusive. "
        "Raw frames include their original checksum bytes. Byte arrays and nonfinite "
        "field values use explicit tagged JSON representations.",
    ]
    if not details:
        sections.append("No record details were explicitly selected.")
    for record in details:
        sections.extend((f"### Record {record.index}", _json_block(_record_details(record))))
    return "\n\n".join(sections) + "\n"
