"""Serve bounded, stateless recording analysis on loopback."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import uvicorn
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
from .charts import activity_chart, attitude_chart
from .importer import MAX_INPUT_BYTES
from .model import ImportResult, Record
from .report import _json_value, build_markdown_report
from .telemetry import attitude_plot_summary, build_attitude_view

STATIC_ROOT = Path(__file__).with_name("instrument_static")
ISSUE_PAGE_SIZE = 100
RECORD_PAGE_SIZE = 100
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


def _query_parameters(request: Request, *, upload: bool = False) -> dict[str, str]:
    allowed = FILTER_PARAMETERS | ({"name"} if upload else set())
    parameters = {}
    for name, value in request.query_params.multi_items():
        if name not in allowed:
            raise ValueError(f"Unsupported query parameter: {name}.")
        if name in parameters:
            raise ValueError(f"Query parameter {name} must occur only once.")
        if len(value) > (1024 if name == "name" else 128):
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
        "schema_version": 3,
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


def create_app(*, classic_port: int | None = None) -> Starlette:
    """Create a file-only workspace; no recording session or execution state is retained."""
    if classic_port is not None and (
        type(classic_port) is not int or not 1 <= classic_port <= 65535
    ):
        raise ValueError("Existing workspace port must be between 1 and 65535.")

    def configuration(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": 3,
                "version": version("uav-debugger"),
                "classic_url": f"http://127.0.0.1:{classic_port}" if classic_port else None,
                "max_recording_bytes": MAX_INPUT_BYTES,
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
        origin = request.headers.get("origin")
        if (
            origin is not None and origin != str(request.base_url).rstrip("/")
        ) or request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
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
        create_app(classic_port=args.classic_port),
        host="127.0.0.1",
        port=args.port,
        proxy_headers=False,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
