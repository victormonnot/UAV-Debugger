"""Serve the bounded Instrument workspace and its bundled recording on loopback."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import import_bytes
from .analysis import Selection, timestamp_to_seconds
from .charts import attitude_chart
from .model import ImportResult
from .telemetry import attitude_plot_summary, build_attitude_view

STATIC_ROOT = Path(__file__).with_name("instrument_static")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def _recording_payload(result: ImportResult) -> dict[str, object]:
    """Keep exact evidence strings separate from floating-point plot coordinates."""
    origin = result.records[0].timestamp_us if result.records else None
    last = result.records[-1].timestamp_us if result.records else None
    source_counts = Counter((record.system_id, record.component_id) for record in result.records)
    sources = []
    for source, count in sorted(source_counts.items()):
        view = build_attitude_view(result, Selection(sources=(source,)))
        figure = (
            json.loads(attitude_chart(view, origin_us=origin).to_json())
            if view.status == "ready"
            else None
        )
        sources.append(
            {
                "system_id": source[0],
                "component_id": source[1],
                "record_count": count,
                "attitude": {"summary": attitude_plot_summary(view), "figure": figure},
            }
        )
    return {
        "schema_version": 1,
        "recording": {
            "source_name": result.source_name,
            "synthetic": True,
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
            "capture_origin_us": str(origin) if origin is not None else None,
            "first_timestamp_us": str(origin) if origin is not None else None,
            "last_timestamp_us": str(last) if last is not None else None,
            "capture_span_s": timestamp_to_seconds(last, origin) if origin is not None else None,
        },
        "issues": [asdict(issue) for issue in result.issues],
        "sources": sources,
    }


def create_app(*, classic_port: int | None = None) -> Starlette:
    """Create a read-only workspace; no recording session or execution state is shared."""
    if classic_port is not None and (
        type(classic_port) is not int or not 1 <= classic_port <= 65535
    ):
        raise ValueError("Existing workspace port must be between 1 and 65535.")

    def configuration(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "schema_version": 1,
                "version": version("uav-debugger"),
                "classic_url": f"http://127.0.0.1:{classic_port}" if classic_port else None,
            }
        )

    def example(request: Request) -> JSONResponse:
        try:
            data = files("uav_debugger").joinpath("data", "telemetry-gap.tlog").read_bytes()
        except OSError:
            return JSONResponse(
                {"error": "The bundled example could not be read."}, status_code=503
            )
        result = import_bytes(data, source_name="telemetry-gap.tlog (synthetic example)")
        return JSONResponse(_recording_payload(result))

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
