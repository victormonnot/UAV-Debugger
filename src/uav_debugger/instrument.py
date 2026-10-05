"""Serve bounded, stateless recording analysis on loopback."""

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass
from email.message import Message
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import uvicorn
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import InputTooLargeError, import_bytes
from .analysis import (
    Selection,
    observed_intervals,
    seconds_to_timestamp,
    select_records,
    timestamp_to_seconds,
)
from .catalog import (
    MAX_CATALOG_BYTES,
    MAX_CATALOG_CHILDREN,
    MAX_MANIFEST_BYTES,
    _key_parts,
    load_catalog_entry,
    scan_catalog,
)
from .charts import activity_chart, attitude_chart
from .importer import MAX_INPUT_BYTES
from .model import ImportResult, Record
from .report import _json_value, build_markdown_report
from .run_report import _bounded_value, build_run_markdown_report
from .saved_run import CAPTURE_POINTS, SavedRun, load_run_files
from .saved_run_ui import (
    MAX_UPLOAD_BYTES,
    MAX_UPLOAD_FILES,
    _seconds,
    run_activity_chart,
    uploaded_run_files,
)
from .telemetry import attitude_plot_summary, build_attitude_view

STATIC_ROOT = Path(__file__).with_name("instrument_static")
ISSUE_PAGE_SIZE = 100
RECORD_PAGE_SIZE = 100
RUN_PAGE_SIZE = 100
MAX_TIMELINE_INTERVALS = 100
MAX_RUN_ENVELOPE_BYTES = 1024 * 1024
MAX_PART_HEADER_BYTES = 8192
MAX_UPLOAD_PATH_BYTES = 4096
RUN_PARAMETERS = {"point", "run_sha256", "evidence_page", "gate_page", "trace", "trace_page"}
FILTER_PARAMETERS = {
    "source",
    "message_id",
    "start",
    "end",
    "gap",
    "issue_page",
    "record_page",
    "record_index",
    "format",
    "sha256",
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _integer_parameter(value: str, name: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{name} must be a nonnegative decimal integer.")
    return int(value)


def _query_parameters(
    request: Request, *, upload: bool = False, run: bool = False, catalog: bool = False
) -> dict[str, str]:
    allowed = FILTER_PARAMETERS | ({"name"} if upload else set())
    if run:
        allowed |= RUN_PARAMETERS
    if catalog:
        allowed |= {"key"}
    parameters = {}
    for name, value in request.query_params.multi_items():
        if name not in allowed:
            raise ValueError(f"Unsupported query parameter: {name}.")
        if name in parameters:
            raise ValueError(f"Query parameter {name} must occur only once.")
        if len(value) > (4096 if name == "key" else 1024 if name == "name" else 128):
            raise ValueError(f"Query parameter {name} is too long.")
        parameters[name] = value
    return parameters


def _applied_selection(result: ImportResult, parameters: dict[str, str]) -> tuple[Selection, int]:
    origin = result.records[0].timestamp_us if result.records else None
    low = min((record.timestamp_us for record in result.records), default=None)
    high = max((record.timestamp_us for record in result.records), default=None)
    source = None
    if "source" in parameters:
        parts = parameters["source"].split(":")
        if len(parts) != 2:
            raise ValueError("Source must be a system:component pair.")
        source = tuple(_integer_parameter(part, "Source identifier") for part in parts)
        if not any((record.system_id, record.component_id) == source for record in result.records):
            raise ValueError("Source is not present in this recording.")
    message_id = None
    if "message_id" in parameters:
        message_id = _integer_parameter(parameters["message_id"], "Message identifier")
        if not any(record.message_id == message_id for record in result.records):
            raise ValueError("Message type is not present in this recording.")
    gap_us = seconds_to_timestamp(parameters.get("gap", "1"), 0)
    if gap_us == 0:
        raise ValueError("Maximum line gap must be positive.")
    start_us, end_us = low, high
    if origin is not None:
        if "start" in parameters:
            start_us = seconds_to_timestamp(parameters["start"], origin)
        if "end" in parameters:
            end_us = seconds_to_timestamp(parameters["end"], origin)
    elif "start" in parameters or "end" in parameters:
        raise ValueError("An empty recording has no capture-time origin for time filters.")
    return (
        Selection(
            sources=(source,) if source is not None else None,
            message_ids=(message_id,) if message_id is not None else None,
            start_us=start_us,
            end_us=end_us,
        ),
        gap_us,
    )


def _page_parameter(parameters: dict[str, str], name: str, count: int, size: int) -> int:
    page = _integer_parameter(parameters.get(name, "0"), name.replace("_", " ").capitalize())
    last_page = max(0, (count - 1) // size)
    if page > last_page:
        raise ValueError(
            f"{name.replace('_', ' ').capitalize()} must be between 0 and {last_page}."
        )
    return page


def _record_page(
    records: tuple[Record, ...], parameters: dict[str, str]
) -> tuple[int, Record | None]:
    page = _page_parameter(parameters, "record_page", len(records), RECORD_PAGE_SIZE)
    if "record_index" not in parameters:
        return page, None
    index = _integer_parameter(parameters["record_index"], "Record index")
    for position, record in enumerate(records):
        if record.index == index:
            selected_page = position // RECORD_PAGE_SIZE
            if "record_page" in parameters and page != selected_page:
                raise ValueError("Record index must belong to the requested record page.")
            return selected_page, record
    raise ValueError("Record index must belong to the current filtered selection.")


def _record_row(record: Record, origin_us: int) -> dict[str, object]:
    return {
        "index": record.index,
        "time_s": timestamp_to_seconds(record.timestamp_us, origin_us),
        "timestamp_us": str(record.timestamp_us),
        "system_id": record.system_id,
        "component_id": record.component_id,
        "message_id": record.message_id,
        "message_name": record.message_name,
        "sequence": record.sequence,
        "offset": record.offset,
        "frame_offset": record.frame_offset,
        "end_offset": record.end_offset,
        "wire_version": record.wire_version,
        "checksum_status": record.checksum_status,
    }


def _record_details(result: ImportResult, record: Record, origin_us: int) -> dict[str, object]:
    return {
        **_record_row(record, origin_us),
        # Keep this serialized: browser JSON numbers cannot represent every MAVLink integer.
        "fields_json": json.dumps(
            _json_value(record.fields), ensure_ascii=True, indent=2, allow_nan=False
        )
        if record.fields is not None
        else None,
        "raw_frame_hex": record.raw_frame.hex(),
        "raw_record_hex": result.raw_bytes[record.offset : record.end_offset].hex(),
    }


def _recording_payload(
    result: ImportResult, parameters: dict[str, str] | None = None, *, synthetic: bool = False
) -> dict[str, object]:
    """Keep exact evidence strings separate from floating-point plot coordinates."""
    parameters = parameters or {}
    origin = result.records[0].timestamp_us if result.records else None
    last = result.records[-1].timestamp_us if result.records else None
    low = min((record.timestamp_us for record in result.records), default=None)
    high = max((record.timestamp_us for record in result.records), default=None)
    source_counts = Counter((record.system_id, record.component_id) for record in result.records)
    message_counts = Counter(record.message_id for record in result.records)
    message_names = {
        record.message_id: record.message_name or f"UNKNOWN_{record.message_id}"
        for record in result.records
    }
    selection, gap_us = _applied_selection(result, parameters)
    selected = select_records(result, selection)
    record_page, inspected = _record_page(selected, parameters)
    record_start = record_page * RECORD_PAGE_SIZE
    intervals = observed_intervals(result, selection)
    longest = max(
        (interval.delta_us for interval in intervals if interval.delta_us is not None), default=None
    )
    view = build_attitude_view(result, selection, max_gap_us=gap_us)
    summary = attitude_plot_summary(view)
    summary["max_gap_us"] = str(gap_us)
    issue_page = _page_parameter(parameters, "issue_page", len(result.issues), ISSUE_PAGE_SIZE)
    issue_start = issue_page * ISSUE_PAGE_SIZE
    return {
        "schema_version": 4,
        "recording": {
            "source_name": result.source_name,
            "synthetic": synthetic,
            "sha256": result.sha256,
            "size_bytes": len(result.raw_bytes),
            "profile": result.profile,
            "dialect": result.dialect,
            "decoder_version": result.decoder_version,
            "traversal": result.traversal,
            "consumed_bytes": result.consumed_bytes,
            "remaining_bytes": result.remaining_bytes,
            "record_count": len(result.records),
            "decoded_count": result.decoded_count,
            "opaque_count": result.opaque_count,
            "source_count": len(source_counts),
            "wire_versions": sorted({record.wire_version for record in result.records}),
            "capture_origin_us": str(origin) if origin is not None else None,
            "first_timestamp_us": str(origin) if origin is not None else None,
            "last_timestamp_us": str(last) if last is not None else None,
            "min_timestamp_us": str(low) if low is not None else None,
            "max_timestamp_us": str(high) if high is not None else None,
            "start_s": timestamp_to_seconds(low, origin) if origin is not None else None,
            "end_s": timestamp_to_seconds(high, origin) if origin is not None else None,
            "capture_span_s": timestamp_to_seconds(high, low) if low is not None else None,
        },
        "issues": [
            asdict(issue) for issue in result.issues[issue_start : issue_start + ISSUE_PAGE_SIZE]
        ],
        "issue_count": len(result.issues),
        "issue_counts": dict(Counter(issue.code for issue in result.issues)),
        "issue_page": issue_page,
        "issue_page_size": ISSUE_PAGE_SIZE,
        "inspector": _record_details(result, inspected, origin) if inspected is not None else None,
        "sources": [
            {"system_id": source[0], "component_id": source[1], "record_count": count}
            for source, count in sorted(source_counts.items())
        ],
        "message_types": [
            {"message_id": message_id, "name": message_names[message_id], "record_count": count}
            for message_id, count in sorted(message_counts.items())
        ],
        "selection": {
            "source": list(selection.sources[0]) if selection.sources is not None else None,
            "message_id": selection.message_ids[0] if selection.message_ids is not None else None,
            "start_s": timestamp_to_seconds(selection.start_us, origin)
            if origin is not None
            else None,
            "end_s": timestamp_to_seconds(selection.end_us, origin) if origin is not None else None,
            "start_us": str(selection.start_us) if selection.start_us is not None else None,
            "end_us": str(selection.end_us) if selection.end_us is not None else None,
            "max_gap_s": timestamp_to_seconds(gap_us, 0),
            "record_count": len(selected),
            "decoded_count": sum(record.fields is not None for record in selected),
            "opaque_count": sum(record.fields is None for record in selected),
            "longest_interval_us": str(longest) if longest is not None else None,
            "messages": {
                "page": record_page,
                "page_size": RECORD_PAGE_SIZE,
                "page_count": max(1, (len(selected) + RECORD_PAGE_SIZE - 1) // RECORD_PAGE_SIZE),
                "total_count": len(selected),
                "records": [
                    _record_row(record, origin)
                    for record in selected[record_start : record_start + RECORD_PAGE_SIZE]
                ],
            },
            "activity": {
                "status": "ready" if selected else "empty",
                "figure": json.loads(activity_chart(selected, origin_us=origin).to_json())
                if selected
                else None,
            },
            "attitude": {
                "summary": summary,
                "figure": json.loads(attitude_chart(view, origin_us=origin).to_json())
                if view.status == "ready"
                else None,
            },
        },
    }


def _analyze_response(
    data: bytes, source_name: str, parameters: dict[str, str], *, synthetic: bool = False
) -> Response:
    try:
        output_format = parameters.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise ValueError("Format must be json or markdown.")
        expected_sha256 = parameters.get("sha256")
        if expected_sha256 is not None and (
            len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("SHA-256 must contain 64 lowercase hexadecimal characters.")
        if output_format == "markdown" and expected_sha256 is None:
            raise ValueError("Report export requires the applied recording SHA-256.")
        result = import_bytes(data, source_name=source_name)
        if expected_sha256 is not None and expected_sha256 != result.sha256:
            return JSONResponse(
                {"error": "The recording no longer matches the applied SHA-256."}, status_code=409
            )
        if output_format == "markdown":
            selection, gap_us = _applied_selection(result, parameters)
            selected = select_records(result, selection)
            _, inspected = _record_page(selected, parameters)
            _page_parameter(parameters, "issue_page", len(result.issues), ISSUE_PAGE_SIZE)
            report = build_markdown_report(
                result,
                selection,
                selected_indices=(inspected.index,) if inspected is not None else (),
                attitude_plot_gap_us=gap_us if result.records else None,
            )
            return Response(
                report,
                media_type="text/markdown",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="uav-debugger-{result.sha256[:12]}.md"'
                    )
                },
            )
        return JSONResponse(_recording_payload(result, parameters, synthetic=synthetic))
    except InputTooLargeError as error:
        return JSONResponse({"error": str(error)}, status_code=413)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return (origin is None or origin == str(request.base_url).rstrip("/")) and request.headers.get(
        "sec-fetch-site", "same-origin"
    ) in {"same-origin", "none"}


@dataclass
class _UploadedEvidence:
    name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)

    def getvalue(self) -> bytes:
        return self.data


class _RunUpload:
    """Collect bounded multipart evidence in memory; never use temporary-file uploads."""

    def __init__(self, boundary: bytes):
        self.files: list[_UploadedEvidence] = []
        self.total_bytes = 0
        self.complete = False
        self.headers: dict[bytes, bytes] = {}
        self.field = bytearray()
        self.value = bytearray()
        self.data = bytearray()
        self.name = ""
        self.header_bytes = 0
        self.parser = MultipartParser(
            boundary,
            callbacks={
                name: getattr(self, name)
                for name in (
                    "on_part_begin",
                    "on_header_field",
                    "on_header_value",
                    "on_header_end",
                    "on_headers_finished",
                    "on_part_data",
                    "on_part_end",
                    "on_end",
                )
            },
            max_header_count=8,
            max_header_size=MAX_PART_HEADER_BYTES,
        )

    def on_part_begin(self) -> None:
        if len(self.files) >= MAX_UPLOAD_FILES:
            raise InputTooLargeError("A saved experiment accepts at most 64 uploaded files.")
        self.headers = {}
        self.field = bytearray()
        self.value = bytearray()
        self.data = bytearray()
        self.name = ""
        self.header_bytes = 0

    def _header(self, target: bytearray, data: bytes, start: int, end: int) -> None:
        self.header_bytes += end - start
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise ValueError("Multipart part headers exceed the 8 KiB limit.")
        target.extend(data[start:end])

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header(self.field, data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header(self.value, data, start, end)

    def on_header_end(self) -> None:
        field = bytes(self.field).lower()
        if field in self.headers:
            raise ValueError("Duplicate multipart part headers are not accepted.")
        self.headers[field] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def on_headers_finished(self) -> None:
        raw_disposition = self.headers.get(b"content-disposition", b"")
        # The parser normalizes legacy Windows paths; this profile must reject them unchanged.
        if b"\\" in raw_disposition:
            raise ValueError("Uploaded experiment paths must use relative forward-slash paths.")
        header = Message()
        header["Content-Disposition"] = raw_disposition.decode("latin-1")
        option_names = [name for name, _ in header.get_params([], header="content-disposition")[1:]]
        if len(set(option_names)) != len(option_names):
            raise ValueError("Duplicate multipart disposition parameters are not accepted.")
        disposition, options = parse_options_header(raw_disposition)
        if (
            disposition != b"form-data"
            or options.get(b"name") != b"files"
            or b"filename" not in options
        ):
            raise ValueError("Saved experiment parts must be files named files.")
        if b"content-transfer-encoding" in self.headers:
            raise ValueError("Encoded multipart file content is not accepted.")
        name = options[b"filename"]
        if len(name) > MAX_UPLOAD_PATH_BYTES:
            raise ValueError("Uploaded experiment paths exceed the 4096-byte limit.")
        self.name = name.decode("utf-8")
        if any(ord(character) < 32 for character in self.name):
            raise ValueError("Uploaded experiment paths cannot contain control characters.")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.total_bytes += end - start
        if self.total_bytes > MAX_UPLOAD_BYTES:
            raise InputTooLargeError("A saved experiment accepts at most 64 MiB in total.")
        self.data.extend(data[start:end])

    def on_part_end(self) -> None:
        self.files.append(_UploadedEvidence(self.name, bytes(self.data)))
        self.data.clear()

    def on_end(self) -> None:
        self.complete = True


def _exact_number(value: object) -> str | None:
    return str(value) if type(value) is int and 0 <= value < 2**64 else None


def _seconds_or_none(value: object) -> str | None:
    return _seconds(value) if _exact_number(value) is not None else None


def _bounded_json(value: object, source_file: str) -> str:
    return json.dumps(
        _json_value(_bounded_value(value, source_file=source_file, limit=65_536)),
        ensure_ascii=True,
        indent=2,
        allow_nan=False,
    )


def _run_page(parameters: dict[str, str], name: str, values: tuple) -> tuple[dict, tuple]:
    page = _page_parameter(parameters, name, len(values), RUN_PAGE_SIZE)
    start = page * RUN_PAGE_SIZE
    return {
        "page": page,
        "page_size": RUN_PAGE_SIZE,
        "page_count": max(1, (len(values) + RUN_PAGE_SIZE - 1) // RUN_PAGE_SIZE),
        "total_count": len(values),
    }, values[start : start + RUN_PAGE_SIZE]


def _trace_row(event) -> dict:
    action = event.data.get("action")
    return {
        "file_name": event.file_name,
        "line_number": event.line_number,
        "reference": f"{event.file_name}:{event.line_number}",
        "action": action if isinstance(action, str) and len(action) <= 256 else None,
        "point": event.data.get("point") if event.data.get("point") in CAPTURE_POINTS else None,
        "record_index": _exact_number(event.data.get("record_index")),
        "offset": _exact_number(event.data.get("offset")),
        "elapsed_s": _seconds_or_none(event.elapsed_ns),
        "monotonic_ns": _exact_number(event.monotonic_ns),
        "elapsed_ns": _exact_number(event.elapsed_ns),
        "unix_us": _exact_number(event.unix_us),
        "valid": event.valid,
        "data_json": _bounded_json(event.data, f"{event.file_name}:{event.line_number}"),
    }


def _run_payload(
    run: SavedRun, parameters: dict[str, str], *, point: str | None, ignored: tuple[str, ...]
) -> dict:
    gates, page_gates = _run_page(parameters, "gate_page", run.gate_intervals)
    gates["rows"] = [
        {
            "index": gates["page"] * RUN_PAGE_SIZE + index,
            "start_reference": f"actions.jsonl:{gate.start.line_number}",
            "end_reference": f"actions.jsonl:{gate.end.line_number}" if gate.end else None,
            "start_elapsed_s": _seconds_or_none(gate.start.elapsed_ns),
            "end_elapsed_s": _seconds_or_none(gate.end.elapsed_ns) if gate.end else None,
            "duration_s": _seconds_or_none(gate.duration_ns),
            "start_monotonic_ns": _exact_number(gate.start.monotonic_ns),
            "end_monotonic_ns": _exact_number(gate.end.monotonic_ns) if gate.end else None,
            "duration_ns": _exact_number(gate.duration_ns),
            "end_reason": gate.end.data.get("reason")
            if gate.end
            and isinstance(gate.end.data.get("reason"), str)
            and len(gate.end.data["reason"]) <= 256
            else None,
        }
        for index, gate in enumerate(page_gates)
    ]
    issues, page_issues = _run_page(parameters, "evidence_page", run.issues)
    issues["rows"] = [asdict(issue) for issue in page_issues]
    issues["counts"] = dict(Counter(issue.code for issue in run.issues))
    kind = parameters.get("trace", "actions")
    if kind not in {"actions", "observations"}:
        raise ValueError("Trace must be actions or observations.")
    trace, page_events = _run_page(
        parameters, "trace_page", run.actions if kind == "actions" else run.observations
    )
    trace.update(kind=kind, rows=[_trace_row(event) for event in page_events])
    timeline_status = "ready" if any(event.valid for event in run.observations) else "empty"
    if len(run.gate_intervals) > MAX_TIMELINE_INTERVALS:
        timeline_status = "too_many_intervals"
    elapsed = (
        run.measurement_monotonic_ns - run.origin_monotonic_ns
        if run.measurement_monotonic_ns is not None and run.origin_monotonic_ns is not None
        else None
    )
    captures = []
    for capture_point in CAPTURE_POINTS:
        capture = run.captures.get(capture_point)
        references = [event for event in run.observations if event.point == capture_point]
        captures.append(
            {
                "point": capture_point,
                "present": capture is not None,
                "record_count": len(capture.records) if capture else None,
                "traversal": capture.traversal if capture else None,
                "sha256": capture.sha256 if capture else None,
                "observation_count": len(references),
                "consistent_reference_count": sum(event.valid for event in references),
            }
        )
    return {
        "identity": run.identity,
        "source_name": run.source_name,
        "schema": run.schema,
        "declared_outcome": run.declared_outcome,
        "evidence_status": run.evidence_status,
        "selected_point": point,
        "requested_json": _bounded_json(run.requested, "run.json"),
        "provenance_json": _bounded_json(
            {
                key: run.manifest.get(key)
                for key in (
                    "schema",
                    "environment",
                    "topology",
                    "clocks",
                    "simulator",
                    "origin_monotonic_ns",
                    "measurement_start",
                    "start",
                    "end",
                    "error",
                    "artifacts",
                )
            },
            "run.json",
        ),
        "files": [
            {"name": name, "size_bytes": evidence.size_bytes, "sha256": evidence.sha256}
            for name, evidence in sorted(run.files.items())
        ],
        "ignored_files": list(ignored),
        "captures": captures,
        "origin_monotonic_ns": _exact_number(run.origin_monotonic_ns),
        "measurement_monotonic_ns": _exact_number(run.measurement_monotonic_ns),
        "measurement_elapsed_s": _seconds_or_none(elapsed),
        "timeline": {
            "status": timeline_status,
            "figure": json.loads(run_activity_chart(run).to_json())
            if timeline_status == "ready"
            else None,
            "interval_count": len(run.gate_intervals),
            "interval_limit": MAX_TIMELINE_INTERVALS,
        },
        "gates": gates,
        "issues": issues,
        "trace": trace,
    }


def _run_response(
    run: SavedRun, parameters: dict[str, str], *, ignored: tuple[str, ...] = ()
) -> Response:
    try:
        output_format = parameters.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise ValueError("Format must be json or markdown.")
        for field in ("sha256", "run_sha256"):
            expected = parameters.get(field)
            if expected is not None and (
                len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
            ):
                raise ValueError(f"{field} must contain 64 lowercase hexadecimal characters.")
        expected = parameters.get("run_sha256")
        if output_format == "markdown" and expected is None:
            raise ValueError("Run report export requires the applied run SHA-256.")
        if expected is not None and expected != run.identity:
            return JSONResponse(
                {
                    "error": "The saved run no longer matches the applied run SHA-256.",
                    "error_code": "run_changed",
                },
                status_code=409,
            )
        point = parameters.get("point")
        if point is not None and point not in CAPTURE_POINTS:
            raise ValueError("Observation point must be relay-input or receiver.")
        if point is not None and point not in run.captures:
            raise ValueError("The selected observation point has no available capture.")
        if point is None:
            point = next(
                (value for value in ("receiver", "relay-input") if value in run.captures), None
            )
        capture = run.captures.get(point)
        capture_parameters = {
            key: value for key, value in parameters.items() if key in FILTER_PARAMETERS
        }
        if capture is None:
            if set(capture_parameters) - {"format"}:
                raise ValueError("Capture filters and record details require an available capture.")
        elif parameters.get("sha256") is not None and parameters["sha256"] != capture.sha256:
            return JSONResponse(
                {"error": "The capture no longer matches the applied SHA-256."}, status_code=409
            )
        # Validate every requested page for reports too, without constructing a timeline.
        _run_page(parameters, "gate_page", run.gate_intervals)
        _run_page(parameters, "evidence_page", run.issues)
        kind = parameters.get("trace", "actions")
        if kind not in {"actions", "observations"}:
            raise ValueError("Trace must be actions or observations.")
        _run_page(parameters, "trace_page", run.actions if kind == "actions" else run.observations)
        if output_format == "markdown":
            selection, gap_us, inspected = None, None, None
            if capture is not None:
                selection, gap_us = _applied_selection(capture, capture_parameters)
                _, inspected = _record_page(select_records(capture, selection), capture_parameters)
                _page_parameter(
                    capture_parameters, "issue_page", len(capture.issues), ISSUE_PAGE_SIZE
                )
                if not capture.records:
                    gap_us = None
            report = build_run_markdown_report(
                run,
                point=point,
                selection=selection,
                selected_indices=(inspected.index,) if inspected else (),
                attitude_plot_gap_us=gap_us,
            )
            return Response(
                report,
                media_type="text/markdown",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="uav-debugger-run-{run.identity[:12]}.md"'
                    )
                },
            )
        payload = (
            _recording_payload(capture, capture_parameters)
            if capture is not None
            else {
                "schema_version": 4,
                "recording": None,
                "selection": None,
                "inspector": None,
                "issues": [],
                "issue_count": 0,
                "issue_counts": {},
                "issue_page": 0,
                "issue_page_size": ISSUE_PAGE_SIZE,
                "sources": [],
                "message_types": [],
            }
        )
        payload["run"] = _run_payload(run, parameters, point=point, ignored=ignored)
        return JSONResponse(payload)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


def _uploaded_run_response(
    uploaded: list[_UploadedEvidence], parameters: dict[str, str]
) -> Response:
    try:
        contents, name, ignored = uploaded_run_files(uploaded)
        run = load_run_files(contents, source_name=name)
        return _run_response(run, parameters, ignored=ignored)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)


def create_app(
    *, classic_port: int | None = None, experiment_root: Path = Path("local/experiments")
) -> Starlette:
    """Create a file-only workspace; no recording session or execution state is retained."""
    if classic_port is not None and (
        type(classic_port) is not int or not 1 <= classic_port <= 65535
    ):
        raise ValueError("Existing workspace port must be between 1 and 65535.")
    root = Path(os.path.abspath(experiment_root))

    def configuration(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": 4,
                "version": version("uav-debugger"),
                "classic_url": f"http://127.0.0.1:{classic_port}" if classic_port else None,
                "max_recording_bytes": MAX_INPUT_BYTES,
                "max_run_bytes": MAX_UPLOAD_BYTES,
                "max_run_files": MAX_UPLOAD_FILES,
                "experiment_root": str(root),
            }
        )

    def example(request: Request) -> Response:
        try:
            parameters = _query_parameters(request)
            data = files("uav_debugger").joinpath("data", "telemetry-gap.tlog").read_bytes()
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except OSError:
            return JSONResponse(
                {"error": "The bundled example could not be read."}, status_code=503
            )
        return _analyze_response(
            data, "telemetry-gap.tlog (synthetic example)", parameters, synthetic=True
        )

    async def analyze(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse(
                {"error": "Cross-origin uploads are not accepted."}, status_code=403
            )
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != ("application/octet-stream")
            or request.headers.get("content-encoding", "identity").lower() != "identity"
        ):
            return JSONResponse(
                {"error": "Send unencoded recording bytes as application/octet-stream."},
                status_code=415,
            )
        try:
            parameters = _query_parameters(request, upload=True)
            name = parameters.pop("name", "recording.tlog")
            if not name.strip() or any(ord(character) < 32 for character in name):
                raise ValueError(
                    "Recording name must be a nonempty label without control characters."
                )
            length = request.headers.get("content-length")
            if (
                length is not None
                and _integer_parameter(length, "Content-Length") > MAX_INPUT_BYTES
            ):
                raise InputTooLargeError(f"Input exceeds the {MAX_INPUT_BYTES}-byte size limit.")
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_INPUT_BYTES:
                    raise InputTooLargeError(
                        f"Input exceeds the {MAX_INPUT_BYTES}-byte size limit."
                    )
                data.extend(chunk)
        except InputTooLargeError as error:
            return JSONResponse({"error": str(error)}, status_code=413)
        except (ValueError, ClientDisconnect) as error:
            return JSONResponse(
                {"error": str(error) or "Recording upload was interrupted."}, status_code=400
            )
        return await run_in_threadpool(_analyze_response, bytes(data), name, parameters)

    async def analyze_run(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse(
                {"error": "Cross-origin uploads are not accepted."}, status_code=403
            )
        if request.headers.get("content-encoding", "identity").lower() != "identity":
            return JSONResponse(
                {"error": "Send unencoded multipart recording files."}, status_code=415
            )
        try:
            parameters = _query_parameters(request, run=True)
            content_type = request.headers.get("content-type", "")
            if len(content_type) > MAX_PART_HEADER_BYTES:
                raise ValueError("Multipart Content-Type header is too long.")
            media_type, options = parse_options_header(content_type)
            if media_type != b"multipart/form-data":
                return JSONResponse(
                    {"error": "Send saved experiment files as multipart/form-data."},
                    status_code=415,
                )
            boundary = options.get(b"boundary", b"")
            if not 1 <= len(boundary) <= 70 or any(byte < 32 or byte > 126 for byte in boundary):
                raise ValueError("A valid multipart boundary of at most 70 bytes is required.")
            wire_limit = MAX_UPLOAD_BYTES + MAX_RUN_ENVELOPE_BYTES
            length = request.headers.get("content-length")
            if length is not None and _integer_parameter(length, "Content-Length") > wire_limit:
                raise InputTooLargeError(
                    "Saved experiment multipart body exceeds the 65 MiB transport limit."
                )
            upload = _RunUpload(boundary)
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > wire_limit:
                    raise InputTooLargeError(
                        "Saved experiment multipart body exceeds the 65 MiB transport limit."
                    )
                upload.parser.write(chunk)
                if received - upload.total_bytes > MAX_RUN_ENVELOPE_BYTES:
                    raise InputTooLargeError(
                        "Saved experiment multipart envelope exceeds the 1 MiB limit."
                    )
            upload.parser.finalize()
            if not upload.complete:
                raise ValueError("Saved experiment multipart upload is incomplete.")
        except InputTooLargeError as error:
            return JSONResponse({"error": str(error)}, status_code=413)
        except (ValueError, ClientDisconnect) as error:
            return JSONResponse(
                {"error": str(error) or "Saved experiment upload was interrupted."}, status_code=400
            )
        return await run_in_threadpool(_uploaded_run_response, upload.files, parameters)

    def catalog(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse(
                {"error": "Cross-origin catalog access is not accepted."}, status_code=403
            )
        if request.query_params:
            return JSONResponse(
                {"error": "Catalog listing does not accept query parameters."}, status_code=400
            )
        result = scan_catalog(root)
        return JSONResponse(
            {
                "schema_version": 4,
                "root": str(result.root),
                "entries": [
                    {
                        "key": entry.key,
                        "scenario": entry.scenario,
                        "source": entry.source,
                        "outcome": entry.outcome,
                        "duration_s": entry.duration_s,
                        "start_unix_us": _exact_number(entry.start_unix_us),
                        "issue": entry.issue,
                        "openable": entry.openable,
                    }
                    for entry in result.entries
                ],
                "issues": list(result.issues),
                "truncated": result.truncated,
                "limits": {
                    "max_children": MAX_CATALOG_CHILDREN,
                    "max_manifest_bytes": MAX_MANIFEST_BYTES,
                    "max_total_manifest_bytes": MAX_CATALOG_BYTES,
                },
            }
        )

    def open_catalog(request: Request) -> Response:
        if not _same_origin(request):
            return JSONResponse(
                {"error": "Cross-origin catalog access is not accepted."}, status_code=403
            )
        try:
            parameters = _query_parameters(request, run=True, catalog=True)
            key = parameters.pop("key", "")
            _key_parts(key)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        try:
            run = load_catalog_entry(root, key)
        except (OSError, ValueError) as error:
            return JSONResponse(
                {"error": f"Cannot open saved evidence: {error}", "error_code": "run_unavailable"},
                status_code=409,
            )
        return _run_response(run, parameters)

    def index(request: Request) -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html", media_type="text/html")

    def plotly_bundle(request: Request) -> FileResponse:
        bundle = files("plotly").joinpath("package_data", "plotly.min.js")
        return FileResponse(str(bundle), media_type="text/javascript")

    def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/health", health),
            Route("/api/config", configuration),
            Route("/api/example", example),
            Route("/api/analyze", analyze, methods=["POST"]),
            Route("/api/run", analyze_run, methods=["POST"]),
            Route("/api/catalog", catalog),
            Route("/api/catalog/open", open_catalog),
            Route("/vendor/plotly.min.js", plotly_bundle),
            Mount("/assets", app=StaticFiles(directory=STATIC_ROOT, check_dir=False)),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])],
    )

    async def response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if request.url.path.startswith("/api/") or request.url.path == "/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=response_headers)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open the local UAV Debugger Instrument workspace."
    )
    parser.add_argument("--port", type=int, default=8765, help="Local UI port (default: 8765)")
    parser.add_argument(
        "--experiment-root",
        type=Path,
        default=Path("local/experiments"),
        help="Read-only local saved-experiment catalog root (default: local/experiments)",
    )
    parser.add_argument(
        "--classic-port",
        type=int,
        help="Link to an existing workspace started separately on this local port",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if args.classic_port is not None:
        if not 1 <= args.classic_port <= 65535:
            parser.error("classic-port must be between 1 and 65535")
        if args.port == args.classic_port:
            parser.error("Instrument and existing workspace ports must differ")
    uvicorn.run(
        create_app(classic_port=args.classic_port, experiment_root=args.experiment_root),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
