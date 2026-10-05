"""Bounded browser payloads for validated, file-only run comparisons."""

import json

from starlette.responses import JSONResponse, Response

from .comparison import ROLES, available_selections, compare_runs
from .comparison_report import (
    _metrics,
    _run_issues,
    _run_provenance,
    build_comparison_markdown_report,
)
from .comparison_ui import comparison_activity_chart, window_nanoseconds
from .report import _json_value
from .run_report import _bounded_value
from .saved_run import CAPTURE_POINTS, SavedRun
from .saved_run_ui import _seconds

SUMMARY_LIMIT = 100


def _json_text(value: object, *, limit: int = 65_536) -> str:
    return json.dumps(
        _json_value(_bounded_value(value, source_file="run.json", limit=limit)),
        ensure_ascii=True,
        indent=2,
        allow_nan=False,
    )


def _clock(value: int | None) -> str | None:
    return str(value) if value is not None else None


def _clock_strings(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _clock(item) if key.endswith(("_ns", "_us")) else _clock_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_clock_strings(item) for item in value]
    return value


def _excerpt(rows: list[dict], total: int) -> dict:
    return {
        "total_count": total,
        "included_count": len(rows),
        "omitted_count": total - len(rows),
        "rows": rows,
    }


def _run_summary(run: SavedRun, ignored: tuple[str, ...]) -> dict:
    issues = _run_issues(run)
    issues["rows"] = issues.pop("items")
    return {
        "identity": run.identity,
        "source_name": run.source_name[:4096],
        "source_name_omitted_characters": max(0, len(run.source_name) - 4096),
        "schema": run.schema,
        "declared_outcome": run.declared_outcome[:1024],
        "declared_outcome_omitted_characters": max(0, len(run.declared_outcome) - 1024),
        "evidence_status": run.evidence_status,
        "origin_monotonic_ns": _clock(run.origin_monotonic_ns),
        "measurement_monotonic_ns": _clock(run.measurement_monotonic_ns),
        "requested_json": _json_text(run.requested),
        "provenance_json": _json_text(
            {
                **_run_provenance(run),
                "clocks": run.manifest.get("clocks"),
                "start": run.manifest.get("start"),
                "measurement_start": run.manifest.get("measurement_start"),
                "end": run.manifest.get("end"),
            }
        ),
        "files": [
            {"name": name, "size_bytes": evidence.size_bytes, "sha256": evidence.sha256}
            for name, evidence in sorted(run.files.items())
        ],
        "ignored_files": list(ignored),
        "issues": issues,
    }


def _payload(comparison, available: dict, point: str, ignored: dict[str, tuple[str, ...]]) -> dict:
    metrics = _metrics(comparison)
    for metric_point in metrics["points"]:
        for role in ROLES:
            metric = metric_point[role]
            if metric is None:
                continue
            metric.pop("reference_excerpt_section")
            references = [metric["first"], metric["last"]]
            if metric["longest_interval"] is not None:
                references.extend(
                    (metric["longest_interval"]["previous"], metric["longest_interval"]["current"])
                )
            capture = getattr(comparison, role).captures[metric_point["point"]]
            for reference in references:
                if reference is not None:
                    record = capture.records[reference["record_index"]]
                    reference.update(frame_offset=record.frame_offset, end_offset=record.end_offset)
    gates = {}
    for role in ROLES:
        intervals = comparison.gates[role]
        gates[role] = _excerpt(
            [
                {
                    "start_ns": _clock(gate.start_ns),
                    "end_ns": _clock(gate.end_ns),
                    "duration_ns": _clock(gate.duration_ns),
                    "start_s": _seconds(gate.start_ns),
                    "end_s": _seconds(gate.end_ns) if gate.end_ns is not None else None,
                    "duration_s": _seconds(gate.duration_ns)
                    if gate.duration_ns is not None
                    else None,
                    "start_line": gate.start_line,
                    "end_line": gate.end_line,
                }
                for gate in intervals[:SUMMARY_LIMIT]
            ],
            len(intervals),
        )
    return {
        "schema_version": 5,
        "comparison": {
            "runs": {
                role: _run_summary(getattr(comparison, role), ignored.get(role, ()))
                for role in ROLES
            },
            "available_selections": [
                {"source": list(source), "message_types": list(types)}
                for source, types in available.items()
            ],
            "selection": {
                "source": list(comparison.source),
                "message_type": comparison.message_type,
                "start_ns": _clock(comparison.start_ns),
                "end_ns": _clock(comparison.end_ns),
                "duration_ns": _clock(comparison.duration_ns),
                "start_s": _seconds(comparison.start_ns),
                "end_s": _seconds(comparison.end_ns) if comparison.end_ns is not None else None,
                "duration_s": _seconds(comparison.duration_ns)
                if comparison.duration_ns is not None
                else None,
                "point": point,
                "bounds": "half-open [start, end)",
            },
            "comparable": comparison.comparable,
            "reasons": _excerpt(
                [
                    {
                        "code": issue.code,
                        "run_role": issue.run_role,
                        "message": issue.message[:256],
                        "message_omitted_characters": max(0, len(issue.message) - 256),
                    }
                    for issue in comparison.issues[:SUMMARY_LIMIT]
                ],
                len(comparison.issues),
            ),
            "differences": _excerpt(
                [
                    {
                        "field": item.field[:4096],
                        "field_omitted_characters": max(0, len(item.field) - 4096),
                        "blocking": item.blocking,
                        "baseline_json": _json_text(item.baseline, limit=4096),
                        "blackout_json": _json_text(item.blackout, limit=4096),
                    }
                    for item in comparison.differences[:SUMMARY_LIMIT]
                ],
                len(comparison.differences),
            ),
            "gates": gates,
            "metrics": _clock_strings(metrics),
            "activity": {
                "status": "ready" if comparison.comparable else "blocked",
                "figure": json.loads(comparison_activity_chart(comparison, point).to_json())
                if comparison.comparable
                else None,
            },
        },
    }


def comparison_response(
    runs: dict[str, SavedRun],
    parameters: dict[str, str],
    *,
    ignored: dict[str, tuple[str, ...]] | None = None,
) -> Response:
    """Keep comparison eligibility in the existing domain and exact clocks in text."""
    try:
        output_format = parameters.get("format", "json")
        if output_format not in {"json", "markdown"}:
            raise ValueError("Format must be json or markdown.")
        if output_format == "markdown" and any(
            f"{role}_sha256" not in parameters for role in ROLES
        ):
            raise ValueError("Comparison reports require both applied run SHA-256 values.")
        for role in ROLES:
            if role not in runs:
                return JSONResponse(
                    {"error": f"A {role} saved experiment is required.", "run_role": role},
                    status_code=400,
                )
            expected = parameters.get(f"{role}_sha256")
            if expected is not None and (
                len(expected) != 64
                or any(character not in "0123456789abcdef" for character in expected)
            ):
                raise ValueError(f"{role}_sha256 must contain 64 lowercase hexadecimal characters.")
            if expected is not None and expected != runs[role].identity:
                return JSONResponse(
                    {
                        "error": f"The {role} run no longer matches its applied SHA-256.",
                        "error_code": "run_changed",
                        "run_role": role,
                    },
                    status_code=409,
                )
        point = parameters.get("point", "receiver")
        if point not in CAPTURE_POINTS:
            raise ValueError("Comparison point must be relay-input or receiver.")
        available = available_selections(runs["baseline"], runs["blackout"])
        if "source" in parameters:
            parts = parameters["source"].split(":")
            if len(parts) != 2 or any(not item.isascii() or not item.isdecimal() for item in parts):
                raise ValueError("Source must be a system:component pair of decimal integers.")
            source = tuple(int(item) for item in parts)
        else:
            source = (1, 1) if (1, 1) in available else next(iter(available), (1, 1))
        types = available.get(source, ())
        message_type = parameters.get(
            "message_type", "ATTITUDE" if "ATTITUDE" in types else next(iter(types), "ATTITUDE")
        )
        comparison = compare_runs(
            runs["baseline"],
            runs["blackout"],
            source=source,
            message_type=message_type,
            start_ns=window_nanoseconds(parameters.get("start", "0")),
            end_ns=window_nanoseconds(parameters["end"]) if "end" in parameters else None,
        )
        if output_format == "markdown":
            filename = (
                f"uav-debugger-comparison-{runs['baseline'].identity[:8]}-"
                f"{runs['blackout'].identity[:8]}.md"
            )
            return Response(
                build_comparison_markdown_report(comparison),
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        return JSONResponse(_payload(comparison, available, point, ignored or {}))
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)
