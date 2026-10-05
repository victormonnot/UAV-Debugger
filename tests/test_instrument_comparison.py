"""Instrument compares saved evidence without shared state, execution or invented clocks."""

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from urllib.parse import urlencode

import pytest
from test_comparison import finalize, fixture_files
from test_instrument import _endpoint_request, _request
from test_instrument_runs import BOUNDARY, fingerprints, write_run
from test_instrument_runs import run_server as run_server

from uav_debugger.comparison import ComparisonGate, compare_runs
from uav_debugger.comparison_report import build_comparison_markdown_report
from uav_debugger.instrument import create_app
from uav_debugger.instrument_comparison import _payload, comparison_response
from uav_debugger.saved_run import RunIssue, load_run_files

ROLES = ("baseline", "blackout")


def pair_files(**kwargs):
    return {role: fixture_files(role, **kwargs) for role in ROLES}


def multipart_pair(roles, *, prefixes=None):
    parts = []
    for role, contents in roles.items():
        items = contents.items() if isinstance(contents, dict) else contents
        for name, raw in items:
            prefix = (prefixes or {}).get(role, f"saved_{role}")
            path = f"{prefix}/{name}" if prefix else name
            parts.extend(
                (
                    b"--" + BOUNDARY + b"\r\n",
                    (
                        f'Content-Disposition: form-data; name="{role}"; filename="{path}"\r\n'
                    ).encode(),
                    b"Content-Type: application/octet-stream\r\n\r\n",
                    raw,
                    b"\r\n",
                )
            )
    return b"".join((*parts, b"--" + BOUNDARY + b"--\r\n"))


def compare(url, roles, *, headers=None, prefixes=None, **parameters):
    return _request(
        url,
        "/api/comparison?" + urlencode(parameters),
        method="POST",
        headers={
            "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
            **(headers or {}),
        },
        data=multipart_pair(roles, prefixes=prefixes),
    )


def loaded(roles):
    return {
        role: load_run_files(contents, source_name=f"saved_{role}")
        for role, contents in roles.items()
    }


def identities(runs):
    return {f"{role}_sha256": run.identity for role, run in runs.items()}


@pytest.mark.parametrize("version", [1, 2])
def test_uploaded_pair_uses_existing_comparator_metrics_and_original_references(
    run_server, version
):
    files = pair_files(version=version)
    runs = loaded(files)
    expected = compare_runs(runs["baseline"], runs["blackout"])
    status, headers, body = compare(run_server[0], files, **identities(runs))
    assert status == 200 and headers["Cache-Control"] == "no-store"
    payload = json.loads(body)
    assert payload["schema_version"] == 5
    result = payload["comparison"]
    assert result["comparable"] is expected.comparable is True
    assert result["reasons"]["rows"] == []
    assert result["available_selections"] == [{"source": [1, 1], "message_types": ["ATTITUDE"]}]
    assert result["selection"] == {
        "source": [1, 1],
        "message_type": "ATTITUDE",
        "start_ns": "0",
        "end_ns": "3000000000",
        "duration_ns": "3000000000",
        "start_s": "0.000000000",
        "end_s": "3.000000000",
        "duration_s": "3.000000000",
        "point": "receiver",
        "bounds": "half-open [start, end)",
    }
    assert result["metrics"]["available"] is True
    assert result["metrics"]["delta_direction"] == "blackout minus baseline"
    for row in result["metrics"]["points"]:
        point = row["point"]
        for role in ROLES:
            actual, metric = row[role], expected.metrics[role][point]
            assert actual["count"] == metric.count
            assert actual["rate_hz"] == metric.rate_hz
            assert actual["capture_sha256"] == runs[role].captures[point].sha256
            assert actual["longest_interval"]["duration_ns"] == str(
                metric.longest_interval.duration_ns
            )
            references = [
                actual["first"],
                actual["last"],
                actual["longest_interval"]["previous"],
                actual["longest_interval"]["current"],
            ]
            originals = [
                metric.first,
                metric.last,
                metric.longest_interval.previous,
                metric.longest_interval.current,
            ]
            for reference, original in zip(references, originals, strict=True):
                record = original.record
                assert reference["capture_timestamp_us"] == str(record.timestamp_us)
                assert reference["measurement_ns"] == str(original.measurement_ns)
                assert reference["monotonic_ns"] == str(original.observation.monotonic_ns)
                assert reference["unix_us"] == str(original.observation.unix_us)
                assert reference["line_number"] == original.observation.line_number
                assert reference["record_index"] == record.index
                assert (
                    reference["offset"],
                    reference["frame_offset"],
                    reference["end_offset"],
                ) == (record.offset, record.frame_offset, record.end_offset)
                assert reference["frame_sha256"] == hashlib.sha256(record.raw_frame).hexdigest()
        assert row["delta"]["count"] == row["blackout"]["count"] - row["baseline"]["count"]
        assert row["delta"]["rate_hz"] == row["blackout"]["rate_hz"] - row["baseline"]["rate_hz"]
    assert result["activity"]["status"] == "ready"
    assert [sum(trace["y"]) for trace in result["activity"]["figure"]["data"]] == [6, 3]
    assert all(len(trace["y"]) <= 200 for trace in result["activity"]["figure"]["data"])
    assert result["gates"]["baseline"]["rows"] == []
    gate = result["gates"]["blackout"]["rows"][0]
    assert gate["start_ns"] == "200000000" and gate["duration_ns"] == "2000000000"
    assert gate["start_line"] == expected.gates["blackout"][0].start_line


def test_half_open_window_keeps_exact_nanoseconds_legitimate_zeroes_and_unavailable_intervals(
    run_server,
):
    files = pair_files()
    result = json.loads(compare(run_server[0], files, start="0.1", end="0.250000001")[2])[
        "comparison"
    ]
    assert result["selection"]["start_ns"] == "100000000"
    assert result["selection"]["end_ns"] == "250000001"
    points = {row["point"]: row for row in result["metrics"]["points"]}
    assert points["relay-input"]["baseline"]["count"] == 2
    assert points["receiver"]["baseline"]["count"] == 2
    assert points["receiver"]["blackout"]["count"] == 1
    assert points["receiver"]["blackout"]["longest_interval"] is None
    assert points["receiver"]["delta"]["longest_interval_ns"] is None
    result = json.loads(compare(run_server[0], files, start="0.1", end="0.25")[2])["comparison"]
    assert all(row[role]["count"] == 1 for row in result["metrics"]["points"] for role in ROLES)
    result = json.loads(compare(run_server[0], files, start="0.100000001", end="0.2")[2])[
        "comparison"
    ]
    assert result["comparable"] is True
    assert all(
        row[role]["count"] == row[role]["rate_hz"] == 0
        for row in result["metrics"]["points"]
        for role in ROLES
    )
    assert all(
        row[role]["first"] is row[role]["last"] is row[role]["longest_interval"] is None
        for row in result["metrics"]["points"]
        for role in ROLES
    )
    assert result["gates"]["blackout"]["rows"][0]["start_ns"] == "200000000"


def test_plot_point_changes_only_the_activity_not_metrics_window_or_report(run_server):
    files = pair_files(version=2)
    runs = loaded(files)
    receiver = json.loads(compare(run_server[0], files)[2])["comparison"]
    relay = json.loads(compare(run_server[0], files, point="relay-input")[2])["comparison"]
    assert relay["metrics"] == receiver["metrics"]
    assert relay["gates"] == receiver["gates"]
    assert [sum(trace["y"]) for trace in relay["activity"]["figure"]["data"]] == [6, 6]
    reports = []
    for point in ("relay-input", "receiver"):
        status, headers, body = compare(
            run_server[0], files, point=point, format="markdown", **identities(runs)
        )
        assert status == 200 and headers["Content-Type"] == "text/markdown; charset=utf-8"
        filename = (
            f"uav-debugger-comparison-{runs['baseline'].identity[:8]}-"
            f"{runs['blackout'].identity[:8]}.md"
        )
        assert headers["Content-Disposition"] == f'attachment; filename="{filename}"'
        reports.append(body)
    assert (
        reports[0]
        == reports[1]
        == build_comparison_markdown_report(
            compare_runs(runs["baseline"], runs["blackout"])
        ).encode()
    )


def test_independent_large_monotonic_origins_startup_and_regressing_capture_clock_are_not_aligned(
    run_server,
):
    files = {
        "baseline": fixture_files(
            "baseline",
            version=2,
            origin_ns=2**63 + 1,
            startup_ns=1_000_000_001,
            wall_regresses=True,
        ),
        "blackout": fixture_files(
            "blackout", version=2, origin_ns=2**63 + 2**53 + 3, startup_ns=2_000_000_002
        ),
    }
    runs = loaded(files)
    result = json.loads(compare(run_server[0], files, **identities(runs))[2])["comparison"]
    assert result["comparable"] is True
    assert result["runs"]["baseline"]["measurement_monotonic_ns"] == str(
        runs["baseline"].measurement_monotonic_ns
    )
    assert result["runs"]["blackout"]["measurement_monotonic_ns"] == str(
        runs["blackout"].measurement_monotonic_ns
    )
    row = result["metrics"]["points"][0]
    assert row["baseline"]["count"] == row["blackout"]["count"] == 6
    assert (
        row["baseline"]["first"]["measurement_ns"]
        == row["blackout"]["first"]["measurement_ns"]
        == "0"
    )
    assert row["baseline"]["first"]["monotonic_ns"] != row["blackout"]["first"]["monotonic_ns"]
    assert (
        row["baseline"]["first"]["capture_timestamp_us"]
        != row["blackout"]["first"]["capture_timestamp_us"]
    )
    assert row["baseline"]["first"]["record_index"] == 1  # Startup observation remains outside.
    assert row["baseline"]["last"]["record_index"] == 6  # End and drain remain outside.
    assert result["gates"]["blackout"]["rows"][0]["start_ns"] == "200000000"


@pytest.mark.parametrize(
    "condition,reason",
    [
        ("interrupted", "run_not_completed"),
        ("missing_capture", "capture_unavailable"),
        ("wrong_role", "scenario"),
        ("incompatible", "configuration_difference"),
        ("missing_duration", "blackout_request"),
        ("unclosed_gate", "blackout_gate"),
        ("outside_window", "window_coverage"),
        ("unavailable_selection", "selection_unavailable"),
    ],
)
def test_blocked_results_retain_reasons_and_reports_without_aggregate_metrics(
    run_server, condition, reason
):
    files = pair_files()
    manifest = json.loads(files["blackout"]["run.json"])
    params = {}
    if condition == "interrupted":
        manifest["outcome"] = "interrupted"
    elif condition == "missing_capture":
        files["blackout"].pop("receiver.tlog")
    elif condition == "wrong_role":
        manifest["requested"]["scenario"] = "baseline"
    elif condition == "incompatible":
        manifest["requested"]["rate_hz"] = 30
    elif condition == "missing_duration":
        manifest["requested"].pop("blackout_duration_s")
    elif condition == "unclosed_gate":
        events = [json.loads(line) for line in files["blackout"]["actions.jsonl"].splitlines()]
        files["blackout"]["actions.jsonl"] = b"".join(
            (json.dumps(event) + "\n").encode()
            for event in events
            if event["action"] != "forwarding_enabled"
        )
    elif condition == "outside_window":
        params["end"] = "4"
    elif condition == "unavailable_selection":
        params.update(source="2:1", message_type="HEARTBEAT")
    files["blackout"] = finalize(files["blackout"], manifest)
    runs = loaded(files)
    status, _, body = compare(run_server[0], files, **params, **identities(runs))
    assert status == 200
    result = json.loads(body)["comparison"]
    assert result["comparable"] is result["metrics"]["available"] is False
    assert reason in {row["code"] for row in result["reasons"]["rows"]}
    assert all(
        row["baseline"] is row["blackout"] is row["delta"] is None
        for row in result["metrics"]["points"]
    )
    assert result["activity"] == {"status": "blocked", "figure": None}
    status, _, report = compare(
        run_server[0], files, **params, **identities(runs), format="markdown"
    )
    assert status == 200 and reason.encode() in report
    assert b'"comparable": false' in report and b'"available": false' in report


@pytest.mark.parametrize(
    "params",
    [
        {"source": "1"},
        {"source": "-1:1"},
        {"source": "256:1"},
        {"source": "1.0:1"},
        {"message_type": ""},
        {"message_type": "x" * 257},
        {"point": "sender"},
        {"start": "NaN"},
        {"end": "inf"},
        {"start": "-1"},
        {"end": "61"},
        {"start": "60"},
        {"start": "0.0000000001"},
        {"end": "0.0000000001"},
        {"start": "2", "end": "1"},
        {"start": "1", "end": "1"},
        {"start": "1e1000"},
        {"format": "html"},
        {"record_index": "0"},
        {"baseline_sha256": "A" * 64},
        {"blackout_sha256": "short"},
        {"format": "markdown"},
        {"format": "markdown", "baseline_sha256": "a" * 64},
    ],
)
def test_bad_parameters_are_errors_not_new_applied_results(run_server, params):
    assert compare(run_server[0], pair_files(), **params)[0] == 400


def test_absent_observations_use_classic_default_selection_without_inventing_metrics(run_server):
    files = pair_files()
    for role in ROLES:
        files[role].pop("observations.jsonl")
        files[role] = finalize(files[role], json.loads(files[role]["run.json"]))
    status, _, body = compare(run_server[0], files)
    assert status == 200
    result = json.loads(body)["comparison"]
    assert result["available_selections"] == []
    assert result["selection"]["source"] == [1, 1]
    assert result["selection"]["message_type"] == "ATTITUDE"
    assert result["comparable"] is result["metrics"]["available"] is False
    assert result["activity"]["figure"] is None


@pytest.mark.parametrize("role", ROLES)
def test_every_supplied_run_identity_is_checked_even_when_capture_fingerprints_match(
    run_server, role
):
    files = pair_files()
    runs = loaded(files)
    manifest = json.loads(files[role]["run.json"])
    manifest["environment"]["new_label"] = "modified"
    files[role]["run.json"] = json.dumps(manifest).encode()
    for format in ("json", "markdown"):
        status, headers, body = compare(run_server[0], files, format=format, **identities(runs))
        assert status == 409 and "Content-Disposition" not in headers
        assert json.loads(body)["error_code"] == "run_changed"
        assert json.loads(body)["run_role"] == role
    assert files[role]["receiver.tlog"] == runs[role].captures["receiver"].raw_bytes


@pytest.mark.parametrize("catalog_roles", [("baseline",), ("blackout",), ROLES])
def test_uploaded_catalog_and_mixed_pairs_use_pinned_fresh_evidence(run_server, catalog_roles):
    url, root = run_server
    files = pair_files(version=2)
    params = {}
    uploaded = dict(files)
    for role in catalog_roles:
        key = f"literal_{role}_key/evidence" if role == "blackout" else f"literal_{role}_key"
        write_run(root / key, files[role])
        params[f"{role}_key"] = key
        uploaded.pop(role)
    original = fingerprints(root)
    status, _, body = compare(url, uploaded, **params, **identities(loaded(files)))
    assert status == 200 and json.loads(body)["comparison"]["comparable"] is True
    for role in catalog_roles:
        assert json.loads(body)["comparison"]["runs"][role]["source_name"] == params[f"{role}_key"]
    assert (
        compare(url, uploaded, **params, **identities(loaded(files)), format="markdown")[0] == 200
    )
    assert fingerprints(root) == original
    role = catalog_roles[0]
    (root / params[f"{role}_key"] / "run.json").unlink()
    status, _, body = compare(url, uploaded, **params, **identities(loaded(files)))
    assert status == 409 and json.loads(body)["error_code"] == "run_unavailable"
    assert json.loads(body)["run_role"] == role
    shutil.rmtree(root)


def test_catalog_mutation_to_running_symlink_or_changed_manifest_is_rejected(run_server, tmp_path):
    url, root = run_server
    files = pair_files()
    write_run(root / "base", files["baseline"])
    params = {"baseline_key": "base", **identities(loaded(files))}
    manifest = json.loads(files["baseline"]["run.json"])
    manifest["environment"]["mutation"] = True
    (root / "base" / "run.json").write_bytes(json.dumps(manifest).encode())
    status, _, body = compare(url, {"blackout": files["blackout"]}, **params)
    assert status == 409 and json.loads(body)["error_code"] == "run_changed"
    manifest["outcome"] = "running"
    (root / "base" / "run.json").write_bytes(json.dumps(manifest).encode())
    assert compare(url, {"blackout": files["blackout"]}, **params)[0] == 409
    shutil.rmtree(root / "base")
    write_run(tmp_path / "external", files["baseline"])
    (root / "base").symlink_to(tmp_path / "external", target_is_directory=True)
    status, _, body = compare(url, {"blackout": files["blackout"]}, **params)
    assert status == 409 and json.loads(body)["error_code"] == "run_unavailable"
    shutil.rmtree(root)


@pytest.mark.parametrize(
    "roles,params",
    [
        ({}, {}),
        ({"baseline": {}}, {}),
        ({"baseline": {}}, {"blackout_key": "../outside"}),
        ({}, {"baseline_key": "/etc", "blackout_key": "run"}),
        ({}, {"baseline_key": "run/deep/path", "blackout_key": "run"}),
        ({}, {"baseline_key": "run\\path", "blackout_key": "run"}),
    ],
)
def test_missing_or_unsafe_role_inputs_cannot_become_phantom_comparisons(run_server, roles, params):
    assert compare(run_server[0], roles, **params)[0] == 400


def test_same_role_cannot_mix_upload_and_catalog_and_unexpected_fields_are_rejected(run_server):
    files = pair_files()
    assert compare(run_server[0], files, baseline_key="other")[0] == 400
    assert (
        compare(run_server[0], {"files": files["baseline"], "blackout": files["blackout"]})[0]
        == 400
    )
    duplicate = list(files["baseline"].items()) + [("run.json", files["baseline"]["run.json"])]
    assert compare(run_server[0], {**files, "baseline": duplicate})[0] == 400
    body = multipart_pair(files)
    path = "/api/comparison?point=receiver&point=relay-input"
    assert (
        _request(
            run_server[0],
            path,
            method="POST",
            headers={"Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode()},
            data=body,
        )[0]
        == 400
    )


def call_comparison(body, *, params=None, headers=None, chunk_size=31):
    chunks = iter(body[index : index + chunk_size] for index in range(0, len(body), chunk_size))

    async def receive():
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    request = _endpoint_request(
        "/api/comparison",
        parameters=params,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
            **(headers or {}),
        },
        receive=receive,
    )
    route = next(route for route in create_app().routes if route.path == "/api/comparison")
    return asyncio.run(route.endpoint(request))


def test_streaming_per_role_limits_include_excluded_files_without_pooled_budget(monkeypatch):
    files = {role: {"run.json": fixture_files(role)["run.json"]} for role in ROLES}
    limit = max(len(contents["run.json"]) for contents in files.values()) + 32
    monkeypatch.setattr("uav_debugger.instrument.MAX_UPLOAD_BYTES", limit)
    for role in ROLES:
        files[role]["excluded.bin"] = bytes(limit - len(files[role]["run.json"]))
    response = call_comparison(multipart_pair(files), headers={"Content-Length": "1"})
    assert response.status_code == 200
    result = json.loads(response.body)["comparison"]
    assert all(result["runs"][role]["ignored_files"] == ["excluded.bin"] for role in ROLES)
    files["baseline"]["excluded.bin"] += b"x"
    files["blackout"]["excluded.bin"] = b""
    assert call_comparison(multipart_pair(files)).status_code == 413


def test_count_limit_is_per_role_even_when_aggregate_count_is_in_range():
    files = {
        role: [
            ("run.json", fixture_files(role)["run.json"]),
            *((f"ignored-{index}", b"") for index in range(63)),
        ]
        for role in ROLES
    }
    assert call_comparison(multipart_pair(files)).status_code == 200
    files["baseline"].append(("extra", b""))
    files["blackout"] = files["blackout"][:1]
    assert call_comparison(multipart_pair(files)).status_code == 413


def test_comparison_envelope_limit_is_separate_from_file_bytes(monkeypatch):
    files = pair_files()
    body = multipart_pair(files)
    envelope = len(body) - sum(len(raw) for contents in files.values() for raw in contents.values())
    monkeypatch.setattr("uav_debugger.instrument.MAX_RUN_ENVELOPE_BYTES", (envelope + 1) // 2)
    assert call_comparison(body, chunk_size=len(body)).status_code == 200
    monkeypatch.setattr("uav_debugger.instrument.MAX_RUN_ENVELOPE_BYTES", (envelope - 1) // 2)
    assert call_comparison(body, chunk_size=len(body)).status_code == 413


def test_fragmented_transport_and_empty_catalog_form_are_completed_without_disk(tmp_path):
    files = pair_files(version=2)
    response = call_comparison(multipart_pair(files), chunk_size=1)
    assert response.status_code == 200
    assert (
        json.loads(response.body)["comparison"]["runs"]["baseline"]["identity"]
        == load_run_files(files["baseline"]).identity
    )
    assert call_comparison(multipart_pair(files)[:-5]).status_code == 400


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "https://other.invalid"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Encoding": "gzip"}, 415),
        ({"Content-Type": "application/octet-stream"}, 415),
        ({"Content-Type": "multipart/form-data"}, 400),
    ],
)
def test_comparison_rejects_cross_origin_encoded_and_nonmultipart_requests(
    run_server, headers, status
):
    assert compare(run_server[0], pair_files(), headers=headers)[0] == status
    assert _request(run_server[0], "/api/comparison")[0] == 405
    assert _request(run_server[0], "/api/comparison", method="OPTIONS")[0] == 405


def test_comparison_declared_wire_limit_and_disconnect_reject_before_loading():
    async def forbidden():
        pytest.fail("An oversized declared comparison body must not be read")

    route = next(route for route in create_app().routes if route.path == "/api/comparison")
    headers = {
        "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
        "Content-Length": str(130 * 1024 * 1024 + 1),
    }
    request = _endpoint_request("/api/comparison", headers=headers, receive=forbidden)
    assert asyncio.run(route.endpoint(request)).status_code == 413

    async def disconnected():
        return {"type": "http.disconnect"}

    headers.pop("Content-Length")
    request = _endpoint_request("/api/comparison", headers=headers, receive=disconnected)
    assert asyncio.run(route.endpoint(request)).status_code == 400


def test_summary_bounds_are_explicit_and_large_setting_integers_remain_literal():
    runs = loaded(pair_files())
    for role in ROLES:
        run = runs[role]
        requested = {**run.requested, "exact_integer": 2**64 - 1}
        if role == "blackout":
            requested.update({f"difference_{index}": index for index in range(105)})
        manifest = {**run.manifest, "requested": requested}
        runs[role] = replace(
            run,
            manifest=manifest,
            issues=tuple(
                RunIssue("constructed", "warning", "run.json", None, "x" * 300) for _ in range(105)
            ),
        )
    response = comparison_response(runs, {})
    assert response.status_code == 200
    result = json.loads(response.body)["comparison"]
    assert result["differences"]["included_count"] == result["reasons"]["included_count"] == 100
    assert result["differences"]["omitted_count"] > 0 and result["reasons"]["omitted_count"] > 0
    for role in ROLES:
        summary = result["runs"][role]
        assert summary["issues"]["total_count"] == 105
        assert (
            summary["issues"]["included_count"] == 100 and summary["issues"]["omitted_count"] == 5
        )
        assert summary["issues"]["rows"][0]["message_omitted_characters"] == 44
        assert f'"exact_integer": {2**64 - 1}' in summary["requested_json"]


def test_report_path_skips_browser_chart_construction(monkeypatch):
    runs = loaded(pair_files())

    def forbidden(*args, **kwargs):
        pytest.fail("Report export must not create a browser chart")

    monkeypatch.setattr("uav_debugger.instrument_comparison._payload", forbidden)
    response = comparison_response(runs, {"format": "markdown", **identities(runs)})
    assert response.status_code == 200
    assert response.body.decode() == build_comparison_markdown_report(
        compare_runs(runs["baseline"], runs["blackout"])
    )


def test_gate_excerpt_keeps_signed_measurement_clocks_and_explicit_omissions():
    runs = loaded(pair_files())
    comparison = compare_runs(runs["baseline"], runs["blackout"])
    comparison = replace(
        comparison,
        gates={
            "baseline": (),
            "blackout": tuple(ComparisonGate(-10, 10, index, index + 1) for index in range(105)),
        },
    )
    result = _payload(comparison, {}, "receiver", {})["comparison"]
    gates = result["gates"]["blackout"]
    assert gates["total_count"] == 105
    assert gates["included_count"] == 100 and gates["omitted_count"] == 5
    assert gates["rows"][0]["start_ns"] == "-10"
    assert gates["rows"][0]["start_s"] == "-0.000000010"
    assert gates["rows"][0]["duration_ns"] == "20"


def test_comparison_routes_do_not_import_execution_or_streamlit_or_write_uploads(tmp_path):
    files = pair_files(version=2)
    root = tmp_path / "catalog"
    write_run(root / "base", files["baseline"])
    original = fingerprints(tmp_path)
    body = multipart_pair({"blackout": files["blackout"]})
    script = r"""
import asyncio, importlib.abc, json, subprocess, sys, tempfile
from pathlib import Path
from starlette.requests import Request
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'uav_debugger.experiment','uav_debugger.experiment_control',
                        'uav_debugger.experiment_worker','uav_debugger.sitl','streamlit'}:
            raise AssertionError('Forbidden import '+fullname)
sys.meta_path.insert(0,Guard())
def forbidden(*args,**kwargs):
    raise AssertionError('Comparison attempted execution or temporary upload storage')
subprocess.Popen=tempfile.TemporaryFile=tempfile.SpooledTemporaryFile=forbidden
from uav_debugger.instrument import create_app
app=create_app(experiment_root=Path(sys.argv[1]))
route=next(route for route in app.routes if route.path=='/api/comparison')
body=sys.stdin.buffer.read()
def request(query):
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':'POST','scheme':'http',
        'server':('127.0.0.1',8765),'path':'/api/comparison','root_path':'',
        'query_string':query,'headers':[(b'host',b'127.0.0.1:8765'),
        (b'content-type',b'multipart/form-data; boundary=uav-debugger-test-boundary')]},
        receive=receive)
result=asyncio.run(route.endpoint(request(b'baseline_key=base')))
assert result.status_code==200
comparison=json.loads(result.body)['comparison']
assert comparison['comparable']
query=b'baseline_key=base&format=markdown'
for role in ('baseline','blackout'):
    query+=b'&'+role.encode()+b'_sha256='+comparison['runs'][role]['identity'].encode()
report=asyncio.run(route.endpoint(request(query)))
assert report.status_code==200 and b'comparison report' in report.body
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        input=body,
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode()
    assert fingerprints(tmp_path) == original
