"""Saved evidence and catalog requests remain bounded, exact and execution-independent."""

import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from urllib.error import URLError
from urllib.parse import urlencode

import pytest
from test_instrument import _endpoint_request, _request
from test_run_report import finalize, fixture_files, summaries

from uav_debugger.analysis import Selection
from uav_debugger.instrument import _run_response, create_app, main
from uav_debugger.run_report import build_run_markdown_report
from uav_debugger.saved_run import GateInterval, RunIssue, TraceEvent, load_run_files

BOUNDARY = b"uav-debugger-test-boundary"


def multipart(contents, *, prefix="saved_run", field="files"):
    items = contents.items() if isinstance(contents, dict) else contents
    parts = []
    for name, raw in items:
        path = f"{prefix}/{name}" if prefix else name
        parts.extend(
            (
                b"--" + BOUNDARY + b"\r\n",
                f'Content-Disposition: form-data; name="{field}"; filename="{path}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                raw,
                b"\r\n",
            )
        )
    return b"".join((*parts, b"--" + BOUNDARY + b"--\r\n"))


def upload(base_url, contents, *, prefix="saved_run", headers=None, **parameters):
    return _request(
        base_url,
        "/api/run?" + urlencode(parameters),
        method="POST",
        headers={
            "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
            **(headers or {}),
        },
        data=multipart(contents, prefix=prefix),
    )


def write_run(directory, contents):
    for name, raw in contents.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def fingerprints(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.fixture(scope="module")
def run_server(tmp_path_factory):
    directory = tmp_path_factory.mktemp("instrument-runs")
    root = directory / "catalog"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with (directory / "server.log").open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uav_debugger.instrument",
                "--port",
                str(port),
                "--experiment-root",
                str(root),
            ],
            cwd=directory,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail((directory / "server.log").read_text())
                try:
                    if _request(url, "/health")[0] == 200:
                        break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.05)
            else:
                pytest.fail("Instrument run test server did not start")
            yield url, root
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_missing_catalog_root_is_reported_without_creating_it(run_server):
    url, root = run_server
    assert not root.exists()
    status, headers, body = _request(url, "/api/catalog")
    payload = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert payload["schema_version"] == 6
    assert payload["root"] == str(root)
    assert payload["entries"] == [] and payload["issues"]
    assert payload["truncated"] is False
    assert payload["limits"] == {
        "max_children": 200,
        "max_manifest_bytes": 1024 * 1024,
        "max_total_manifest_bytes": 8 * 1024 * 1024,
    }
    assert not root.exists()
    config = json.loads(_request(url, "/api/config")[2])
    assert config["experiment_root"] == str(root)
    assert config["max_run_bytes"] == 64 * 1024 * 1024 and config["max_run_files"] == 64


@pytest.mark.parametrize("version", [1, 2])
def test_uploaded_saved_run_preserves_settings_actions_observations_and_capture_identity(
    run_server, version
):
    url, _ = run_server
    files = fixture_files(version=version)
    original = dict(files)
    run = load_run_files(files, source_name="literal_run_name")
    status, headers, body = upload(url, files, prefix="literal_run_name")
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    payload = json.loads(body)
    summary = payload["run"]
    assert summary["identity"] == run.identity
    assert summary["source_name"] == "literal_run_name"
    assert summary["schema"] == f"uav-debugger-experiment-v{version}"
    assert summary["declared_outcome"] == "completed"
    assert summary["evidence_status"] == run.evidence_status == "consistent"
    assert json.loads(summary["requested_json"])["blackout_duration_s"] == 2
    assert summary["gates"]["rows"][0]["duration_ns"] == "2100000000"
    assert summary["gates"]["rows"][0]["duration_s"] == "2.100000000"
    assert summary["gates"]["rows"][0]["start_reference"].startswith("actions.jsonl:")
    assert summary["gates"]["rows"][0]["end_reference"].startswith("actions.jsonl:")
    assert summary["selected_point"] == "receiver"
    assert [capture["record_count"] for capture in summary["captures"]] == [3, 2]
    assert [capture["consistent_reference_count"] for capture in summary["captures"]] == [3, 2]
    assert summary["measurement_elapsed_s"] == ("0.000000000" if version == 1 else "1.000000000")
    assert summary["timeline"]["status"] == "ready"
    assert [sum(trace["y"]) for trace in summary["timeline"]["figure"]["data"]] == [3, 2]
    assert all(len(trace["y"]) <= 200 for trace in summary["timeline"]["figure"]["data"])
    assert payload["recording"]["sha256"] == run.captures["receiver"].sha256
    assert payload["selection"]["record_count"] == 2
    assert payload["inspector"] is None
    assert summary["ignored_files"] == []
    assert {row["name"]: row["sha256"] for row in summary["files"]} == {
        name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()
    }
    assert files == original


def test_actual_synthetic_run_opens_without_mutating_original_evidence(run_server, tmp_path):
    from uav_debugger.experiment import ExperimentConfig, run_experiment

    directory = tmp_path / "actual"
    run_experiment(directory, ExperimentConfig(duration_s=0.15))
    original = fingerprints(directory)
    files = {path.name: path.read_bytes() for path in directory.iterdir()}
    status, _, body = upload(run_server[0], files)
    assert status == 200
    payload = json.loads(body)
    assert payload["run"]["declared_outcome"] == "completed"
    assert payload["run"]["evidence_status"] == "consistent"
    assert payload["recording"]["record_count"] > 0
    assert fingerprints(directory) == original


def test_capture_selection_and_report_share_applied_filters_without_changing_run_timeline(
    run_server,
):
    files = fixture_files(version=2)
    run = load_run_files(files, source_name="saved_run")
    baseline = json.loads(upload(run_server[0], files)[2])
    parameters = {
        "point": "relay-input",
        "message_id": "30",
        "record_index": "2",
        "gap": "2",
        "run_sha256": run.identity,
    }
    payload = json.loads(upload(run_server[0], files, **parameters)[2])
    assert payload["run"]["selected_point"] == "relay-input"
    assert payload["run"]["timeline"] == baseline["run"]["timeline"]
    assert payload["selection"]["record_count"] == 2
    assert payload["inspector"]["index"] == 2
    assert (
        payload["inspector"]["raw_frame_hex"]
        == run.captures["relay-input"].records[2].raw_frame.hex()
    )
    status, headers, body = upload(run_server[0], files, **parameters, format="markdown")
    capture = run.captures["relay-input"]
    assert status == 200
    assert (
        headers["Content-Disposition"]
        == f'attachment; filename="uav-debugger-run-{run.identity[:12]}.md"'
    )
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert body.decode() == build_run_markdown_report(
        run,
        point="relay-input",
        selection=Selection(
            message_ids=(30,),
            start_us=capture.records[0].timestamp_us,
            end_us=capture.records[-1].timestamp_us,
        ),
        selected_indices=(2,),
        attitude_plot_gap_us=2_000_000,
    )
    changed_point = json.loads(
        upload(run_server[0], files, point="receiver", run_sha256=run.identity)[2]
    )
    assert changed_point["inspector"] is None
    assert changed_point["selection"]["message_id"] is None
    assert changed_point["selection"]["record_count"] == 2


def test_missing_captures_still_produce_run_summary_and_report_without_fictitious_recording(
    run_server,
):
    files = {"run.json": fixture_files()["run.json"]}
    run = load_run_files(files, source_name="saved_run")
    status, _, body = upload(run_server[0], files)
    assert status == 200
    payload = json.loads(body)
    assert payload["recording"] is payload["selection"] is payload["inspector"] is None
    assert payload["sources"] == payload["message_types"] == payload["issues"] == []
    assert payload["run"]["selected_point"] is None
    assert payload["run"]["evidence_status"] != "consistent"
    assert all(
        row["record_count"] is None and not row["present"] for row in payload["run"]["captures"]
    )
    assert payload["run"]["timeline"]["status"] == "empty"
    status, _, report = upload(run_server[0], files, run_sha256=run.identity, format="markdown")
    assert status == 200
    exported = summaries(report.decode())
    assert exported["run_provenance"]["bundle_sha256"] == run.identity
    assert exported["run_provenance"]["selected_point"] is None
    assert exported["status"]["reader_issue_count"] == len(run.issues)
    assert b"Selected capture analysis" not in report
    assert _run_response(
        run, {"run_sha256": run.identity, "format": "markdown"}
    ).body.decode() == build_run_markdown_report(run)
    assert upload(run_server[0], files, point="receiver")[0] == 400
    assert upload(run_server[0], files, start="0")[0] == 400


def test_single_capture_fallback_and_corrupt_opaque_capture_evidence_remain_explicit(run_server):
    files = fixture_files(version=2, opaque=True)
    files.pop("receiver.tlog")
    payload = json.loads(upload(run_server[0], files, record_index="2")[2])
    assert payload["run"]["selected_point"] == "relay-input"
    assert payload["run"]["captures"][1]["record_count"] is None
    assert payload["recording"]["opaque_count"] == 1
    assert payload["inspector"]["fields_json"] is None
    assert payload["inspector"]["checksum_status"] == "unverified"
    files["relay-input.tlog"] = files["relay-input.tlog"][:-1]
    payload = json.loads(upload(run_server[0], files)[2])
    assert payload["recording"]["traversal"] == "stopped"
    assert payload["run"]["evidence_status"] == "invalid"
    assert payload["run"]["captures"][0]["consistent_reference_count"] == 0


def test_large_monotonic_clocks_remain_exact_and_separate_from_capture_unix_time(run_server):
    files = fixture_files(version=2)
    original = load_run_files(files)
    shift = 2**53 + 1
    manifest = json.loads(files["run.json"])
    manifest["origin_monotonic_ns"] += shift
    manifest["requested"]["reference_integer"] = 2**64 - 1
    for field in ("start", "end", "measurement_start"):
        manifest[field]["monotonic_ns"] += shift
    for name in ("actions.jsonl", "observations.jsonl", "datagrams.jsonl"):
        events = [json.loads(line) for line in files[name].splitlines()]
        for event in events:
            event["monotonic_ns"] += shift
        files[name] = b"".join((json.dumps(event) + "\n").encode() for event in events)
    files = finalize(files, manifest)
    payload = json.loads(upload(run_server[0], files, trace="observations")[2])
    summary = payload["run"]
    assert summary["evidence_status"] == "consistent"
    assert summary["origin_monotonic_ns"] == str(original.origin_monotonic_ns + shift)
    assert summary["measurement_monotonic_ns"] == str(original.measurement_monotonic_ns + shift)
    assert summary["measurement_elapsed_s"] == "1.000000000"
    assert summary["trace"]["rows"][0]["monotonic_ns"] == str(
        original.observations[0].monotonic_ns + shift
    )
    assert summary["trace"]["rows"][0]["unix_us"] == str(original.observations[0].unix_us)
    assert payload["recording"]["capture_origin_us"] == str(
        original.captures["receiver"].records[0].timestamp_us
    )
    assert f'"reference_integer": {2**64 - 1}' in summary["requested_json"]
    assert files["receiver.tlog"] == original.captures["receiver"].raw_bytes


@pytest.mark.parametrize(
    "parameters",
    [
        {"point": "sender"},
        {"point": ""},
        {"run_sha256": "A" * 64},
        {"run_sha256": "0"},
        {"trace": "datagrams"},
        {"trace_page": "1"},
        {"evidence_page": "1"},
        {"gate_page": "1"},
        {"gate_page": "-1"},
        {"record_index": "999"},
        {"format": "markdown"},
        {"format": "html"},
        {"name": "ignored"},
        {"path": "/etc/passwd"},
        {"key": "arbitrary"},
    ],
)
def test_run_parameters_reject_unsupported_or_stale_selection(run_server, parameters):
    assert upload(run_server[0], fixture_files(), **parameters)[0] == 400


@pytest.mark.parametrize("format", ["json", "markdown"])
def test_run_guard_checks_manifest_changes_even_when_capture_bytes_are_unchanged(
    run_server, format
):
    files = fixture_files()
    run = load_run_files(files)
    parameters = {
        "run_sha256": run.identity,
        "sha256": run.captures["receiver"].sha256,
        "format": format,
    }
    assert upload(run_server[0], files, **parameters)[0] == 200
    altered = dict(files)
    manifest = json.loads(altered["run.json"])
    manifest["environment"]["label"] = "changed"
    altered["run.json"] = json.dumps(manifest).encode()
    status, headers, body = upload(run_server[0], altered, **parameters)
    assert status == 409 and json.loads(body)["error_code"] == "run_changed"
    assert "Content-Disposition" not in headers
    assert altered["receiver.tlog"] == files["receiver.tlog"]
    assert upload(run_server[0], files, **parameters)[0] == 200


def test_excluded_uploaded_files_are_visible_but_not_part_of_fixed_evidence_identity(run_server):
    files = fixture_files(version=2)
    fingerprint = load_run_files(files).identity
    files["simulator/working.bin"] = b"private unrelated state"
    payload = json.loads(upload(run_server[0], files)[2])
    assert payload["run"]["identity"] == fingerprint
    assert payload["run"]["ignored_files"] == ["simulator/working.bin"]
    assert "simulator/working.bin" not in {row["name"] for row in payload["run"]["files"]}


@pytest.mark.parametrize(
    "names",
    [
        ["one/run.json", "two/run.json"],
        ["one/run.json", "two/receiver.tlog"],
        ["one/run.json", "one/run.json"],
        ["one/run.json", "one/../receiver.tlog"],
        ["/run.json"],
        ["nested/one/run.json"],
        ["one\\run.json"],
        ["C:\\fakepath\\run.json"],
        ["\\\\server\\run.json"],
        ["receiver.tlog"],
    ],
)
def test_unsafe_or_ambiguous_directory_uploads_are_rejected(run_server, names):
    assert upload(run_server[0], [(name, b"{}") for name in names], prefix="")[0] == 400


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Content-Type": "application/octet-stream"}, 415),
        ({"Content-Encoding": "gzip"}, 415),
        ({"Origin": "https://other.invalid"}, 403),
        ({"Origin": "null"}, 403),
        ({"Sec-Fetch-Site": "same-site"}, 403),
        ({"Content-Type": "multipart/form-data"}, 400),
        ({"Content-Type": "multipart/form-data; boundary=" + "x" * 71}, 400),
    ],
)
def test_run_upload_accepts_only_bounded_same_origin_multipart(run_server, headers, status):
    assert upload(run_server[0], fixture_files(), headers=headers)[0] == status
    assert _request(run_server[0], "/api/run", method="OPTIONS")[0] == 405
    assert _request(run_server[0], "/api/run")[0] == 405


def call_upload(body, *, headers=None, chunk_size=31, parameters=None):
    chunks = iter(body[index : index + chunk_size] for index in range(0, len(body), chunk_size))

    async def receive():
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    request = _endpoint_request(
        "/api/run",
        parameters=parameters,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
            **(headers or {}),
        },
        receive=receive,
    )
    route = next(route for route in create_app().routes if route.path == "/api/run")
    return asyncio.run(route.endpoint(request))


def test_multipart_chunk_boundaries_preserve_original_bytes_and_reject_truncation():
    files = fixture_files(version=2)
    body = multipart(files)
    response = call_upload(body, chunk_size=1)
    assert response.status_code == 200
    assert json.loads(response.body)["run"]["identity"] == load_run_files(files).identity
    assert call_upload(body[:-5]).status_code == 400
    assert call_upload(multipart(files, field="other")).status_code == 400
    assert (
        call_upload(
            body.replace(
                b"Content-Type: application/octet-stream", b"Content-Transfer-Encoding: base64", 1
            )
        ).status_code
        == 400
    )
    assert (
        call_upload(body.replace(b'name="files";', b'name="other"; name="files";', 1)).status_code
        == 400
    )
    assert (
        call_upload(
            body.replace(
                b"Content-Type: application/octet-stream",
                b"Content-Type: application/octet-stream\r\nContent-Type: application/octet-stream",
                1,
            )
        ).status_code
        == 400
    )


def test_streaming_caps_count_excluded_bytes_and_do_not_trust_declared_content_length(monkeypatch):
    files = {"run.json": fixture_files()["run.json"]}
    limit = len(files["run.json"]) + 32
    monkeypatch.setattr("uav_debugger.instrument.MAX_UPLOAD_BYTES", limit)
    files["excluded.bin"] = bytes(32)
    assert call_upload(multipart(files), headers={"Content-Length": "1"}).status_code == 200
    files["excluded.bin"] += b"x"
    assert call_upload(multipart(files), headers={"Content-Length": "1"}).status_code == 413


def test_multipart_count_headers_path_and_envelope_are_bounded(monkeypatch):
    files = [("run.json", fixture_files()["run.json"])]
    files.extend((f"excluded-{index}", b"") for index in range(63))
    assert call_upload(multipart(files)).status_code == 200
    assert call_upload(multipart([*files, ("extra", b"")])).status_code == 413
    assert call_upload(multipart([("x" * 4097, b"{}")], prefix="")).status_code == 400
    oversized = multipart(files[:1]).replace(
        b"Content-Type: application/octet-stream", b"X-Long: " + b"a" * 8200, 1
    )
    assert call_upload(oversized).status_code == 400
    monkeypatch.setattr("uav_debugger.instrument.MAX_RUN_ENVELOPE_BYTES", 32)
    assert call_upload(multipart(files[:1])).status_code == 413


def test_oversized_declared_run_body_and_disconnect_do_not_reach_reader():
    async def forbidden():
        pytest.fail("Declared oversized upload must be rejected before reading")

    route = next(route for route in create_app().routes if route.path == "/api/run")
    headers = {
        "Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode(),
        "Content-Length": str(65 * 1024 * 1024 + 1),
    }
    request = _endpoint_request("/api/run", headers=headers, receive=forbidden)
    assert asyncio.run(route.endpoint(request)).status_code == 413

    async def disconnected():
        return {"type": "http.disconnect"}

    headers.pop("Content-Length")
    request = _endpoint_request("/api/run", headers=headers, receive=disconnected)
    assert asyncio.run(route.endpoint(request)).status_code == 400


def test_run_summary_pages_all_evidence_and_bounds_timeline_shapes_without_dropping_counts():
    run = load_run_files(fixture_files())
    actions = tuple(
        TraceEvent(
            "actions.jsonl",
            index + 1,
            {
                "action": "forwarding_disabled",
                "elapsed_ns": index,
                "monotonic_ns": index + 10,
                "unix_us": 2**64 - 1,
            },
            True,
        )
        for index in range(205)
    )
    issues = tuple(
        RunIssue("constructed", "warning", "run.json", None, f"Issue {index}")
        for index in range(205)
    )
    run = replace(
        run,
        actions=actions,
        issues=issues,
        gate_intervals=tuple(GateInterval(event, None) for event in actions),
    )
    response = _run_response(run, {"trace_page": "2", "evidence_page": "2", "gate_page": "2"})
    assert response.status_code == 200
    summary = json.loads(response.body)["run"]
    for key in ("gates", "trace", "issues"):
        assert summary[key]["page"] == 2 and summary[key]["page_count"] == 3
        assert summary[key]["total_count"] == 205 and len(summary[key]["rows"]) == 5
    assert summary["timeline"] == {
        "status": "too_many_intervals",
        "figure": None,
        "interval_count": 205,
        "interval_limit": 100,
    }
    assert summary["gates"]["rows"][0]["duration_ns"] is None
    assert summary["gates"]["rows"][0]["end_reference"] is None
    assert summary["trace"]["rows"][0]["unix_us"] == str(2**64 - 1)
    assert summary["trace"]["rows"][0]["elapsed_ns"] == "200"
    assert summary["trace"]["rows"][0]["reference"] == "actions.jsonl:201"
    assert _run_response(run, {"trace_page": "3"}).status_code == 400


def test_invalid_trace_values_are_null_and_large_metadata_has_explicit_omission():
    run = load_run_files(fixture_files())
    malformed = TraceEvent(
        "actions.jsonl",
        1,
        {
            "action": {},
            "point": [],
            "record_index": True,
            "offset": "3",
            "elapsed_ns": {},
            "monotonic_ns": "10",
            "unix_us": -1,
        },
        False,
    )
    manifest = {**run.manifest, "requested": {"large": "a" * 70000}}
    run = replace(run, manifest=manifest, actions=(malformed,))
    summary = json.loads(_run_response(run, {}).body)["run"]
    row = summary["trace"]["rows"][0]
    assert all(
        row[key] is None
        for key in (
            "action",
            "point",
            "record_index",
            "offset",
            "elapsed_s",
            "monotonic_ns",
            "elapsed_ns",
            "unix_us",
        )
    )
    assert row["valid"] is False
    assert json.loads(row["data_json"])["elapsed_ns"] == {}
    omitted = json.loads(summary["requested_json"])
    assert omitted["content_omitted"] is True and omitted["source_file"] == "run.json"
    assert omitted["limit_characters"] == 65536


def test_catalog_lists_declared_metadata_then_validates_each_open_and_reread(run_server):
    url, root = run_server
    files = fixture_files(version=2)
    write_run(root / "literal_cli_run", files)
    write_run(root / "literal_browser_run" / "evidence", files)
    (root / "literal_browser_run" / "control.json").write_text('{"state":"running"}')
    original = fingerprints(root)
    payload = json.loads(_request(url, "/api/catalog")[2])
    assert [entry["key"] for entry in payload["entries"]] == [
        "literal_browser_run/evidence",
        "literal_cli_run",
    ]
    assert all(entry["openable"] for entry in payload["entries"])
    for entry in payload["entries"]:
        path = "/api/catalog/open?" + urlencode({"key": entry["key"]})
        status, _, body = _request(url, path)
        assert status == 200
        opened = json.loads(body)
        assert opened["run"]["source_name"] == entry["key"]
        assert opened["run"]["evidence_status"] == "consistent"
        assert opened["run"]["identity"] == load_run_files(files).identity
        assert "control.json" not in {item["name"] for item in opened["run"]["files"]}
    assert fingerprints(root) == original
    identity = load_run_files(files).identity
    path = root / "literal_cli_run" / "run.json"
    manifest = json.loads(path.read_bytes())
    manifest["environment"]["change"] = True
    path.write_bytes(json.dumps(manifest).encode())
    request = {"key": "literal_cli_run", "run_sha256": identity, "format": "markdown"}
    status, _, body = _request(url, "/api/catalog/open?" + urlencode(request))
    assert status == 409 and json.loads(body)["error_code"] == "run_changed"
    path.unlink()
    status, _, body = _request(url, "/api/catalog/open?" + urlencode(request))
    assert status == 409 and json.loads(body)["error_code"] == "run_unavailable"
    assert any(
        not row["openable"] for row in json.loads(_request(url, "/api/catalog")[2])["entries"]
    )
    shutil.rmtree(root)


def test_running_upload_is_inspectable_but_catalog_does_not_claim_process_liveness(run_server):
    url, root = run_server
    files = fixture_files()
    manifest = json.loads(files["run.json"])
    manifest["outcome"] = "running"
    files["run.json"] = json.dumps(manifest).encode()
    payload = json.loads(upload(url, files)[2])
    assert payload["run"]["declared_outcome"] == "running"
    assert payload["run"]["evidence_status"] == "incomplete"
    write_run(root / "unfinalized", files)
    entry = json.loads(_request(url, "/api/catalog")[2])["entries"][0]
    assert not entry["openable"] and "does not establish an active process" in entry["issue"]
    status, _, body = _request(url, "/api/catalog/open?key=unfinalized")
    assert status == 409 and json.loads(body)["error_code"] == "run_unavailable"
    shutil.rmtree(root)


@pytest.mark.parametrize(
    "key", ["", "/etc", "../run", "a/../run", "a/nested", "a/evidence/deep", "a\\run", "a\x00run"]
)
def test_catalog_keys_never_select_arbitrary_server_paths(run_server, key):
    assert _request(run_server[0], "/api/catalog/open?" + urlencode({"key": key}))[0] == 400


def test_catalog_never_reads_capture_files_during_listing_and_refuses_symlinks_and_fifos(
    run_server, tmp_path
):
    url, root = run_server
    write_run(root / "fifo", {"run.json": fixture_files()["run.json"]})
    os.mkfifo(root / "fifo" / "receiver.tlog")
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    payload = json.loads(_request(url, "/api/catalog")[2])
    entries = {entry["key"]: entry for entry in payload["entries"]}
    assert entries["fifo"]["openable"] is True
    assert entries["linked"]["openable"] is False
    for key in ("fifo", "linked"):
        status, _, body = _request(url, "/api/catalog/open?key=" + key)
        assert status == 409 and json.loads(body)["error_code"] == "run_unavailable"
    shutil.rmtree(root)


def test_catalog_rejects_unknown_duplicate_and_cross_origin_requests(run_server):
    url, _ = run_server
    assert _request(url, "/api/catalog?path=/etc")[0] == 400
    assert _request(url, "/api/catalog/open?key=a&key=b")[0] == 400
    assert _request(url, "/api/catalog/open?key=a&point=receiver&point=relay-input")[0] == 400
    for path in ("/api/catalog", "/api/catalog/open?key=a"):
        assert _request(url, path, headers={"Origin": "https://other.invalid"})[0] == 403
        assert _request(url, path, method="POST")[0] == 405


def test_launcher_keeps_catalog_root_absolute_without_creating_or_resolving_it(
    monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(
        "uav_debugger.instrument.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    missing = tmp_path / "absent" / ".." / "runs"
    assert main(["--experiment-root", str(missing)]) == 0
    (app,), _ = calls[0]
    route = next(route for route in app.routes if route.path == "/api/config")
    config = json.loads(route.endpoint(None).body)
    assert config["experiment_root"] == str(tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_run_routes_never_import_execution_or_streamlit_start_commands_or_write_uploads(tmp_path):
    files = fixture_files(version=2)
    root = tmp_path / "catalog"
    write_run(root / "saved", files)
    original = fingerprints(tmp_path)
    body = multipart(files)
    script = r"""
import asyncio, builtins, importlib.abc, json, os, subprocess, sys, tempfile
from pathlib import Path
from starlette.requests import Request
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'uav_debugger.experiment', 'uav_debugger.experiment_control',
                        'uav_debugger.experiment_worker', 'uav_debugger.sitl', 'streamlit'}:
            raise AssertionError('Forbidden import ' + fullname)
sys.meta_path.insert(0, Guard())
def forbidden(*args, **kwargs):
    raise AssertionError('Analysis attempted execution or temporary-file storage')
subprocess.Popen = tempfile.TemporaryFile = tempfile.SpooledTemporaryFile = forbidden
from uav_debugger.instrument import create_app
app = create_app(experiment_root=Path(sys.argv[1]))
body = sys.stdin.buffer.read()
def request(path, query=b''):
    async def receive():
        return {'type':'http.request','body':body,'more_body':False}
    return Request({'type':'http','method':'POST' if path=='/api/run' else 'GET',
        'scheme':'http','server':('127.0.0.1',8765),'path':path,'root_path':'',
        'query_string':query,'headers':[(b'host',b'127.0.0.1:8765'),
        (b'content-type',b'multipart/form-data; boundary=uav-debugger-test-boundary')]},
        receive=receive)
routes = {route.path:route for route in app.routes}
listing = routes['/api/catalog'].endpoint(request('/api/catalog'))
assert json.loads(listing.body)['entries'][0]['openable']
opened = routes['/api/catalog/open'].endpoint(request('/api/catalog/open',b'key=saved'))
identity = json.loads(opened.body)['run']['identity'].encode()
report = routes['/api/catalog/open'].endpoint(request('/api/catalog/open',
    b'key=saved&format=markdown&record_index=0&run_sha256='+identity))
assert report.status_code==200 and b'Explicit record details' in report.body
uploaded = asyncio.run(routes['/api/run'].endpoint(request('/api/run')))
assert json.loads(uploaded.body)['run']['identity'].encode()==identity
report = asyncio.run(routes['/api/run'].endpoint(request('/api/run',
    b'format=markdown&record_index=0&run_sha256='+identity)))
assert report.status_code==200 and b'Explicit record details' in report.body
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
