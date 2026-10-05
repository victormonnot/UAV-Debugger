"""Instrument imports bounded uploads with exact filters and no retained evidence."""

import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import replace
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from pymavlink.dialects.v20 import common
from starlette.requests import Request as StarletteRequest

from uav_debugger import import_bytes
from uav_debugger.analysis import Selection
from uav_debugger.importer import MAX_INPUT_BYTES
from uav_debugger.instrument import _analyze_response, _recording_payload, create_app, main
from uav_debugger.report import build_markdown_report

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"
MANIFEST = FIXTURE.with_name("telemetry-gap.expected.json")


def _request(base_url, path, *, method="GET", headers=None, data=None):
    request = Request(base_url + path, method=method, headers=headers or {}, data=data)
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


def _upload(base_url, data, *, name="recording.tlog", **filters):
    return _request(
        base_url,
        "/api/analyze?" + urlencode({"name": name, **filters}),
        method="POST",
        headers={"Content-Type": "application/octet-stream"},
        data=data,
    )


def _endpoint_request(path, *, parameters=None, headers=None, receive=None):
    return StarletteRequest(
        {
            "type": "http",
            "method": "POST" if path == "/api/analyze" else "GET",
            "path": path,
            "root_path": "",
            "scheme": "http",
            "server": ("127.0.0.1", 8765),
            "query_string": urlencode(parameters or {}).encode(),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in {"host": "127.0.0.1:8765", **(headers or {})}.items()
            ],
        },
        receive=receive,
    )


@pytest.fixture(scope="module")
def instrument_server(tmp_path_factory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    directory = tmp_path_factory.mktemp("instrument-server")
    log_path = directory / "server.log"
    base_url = f"http://127.0.0.1:{port}"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uav_debugger.instrument", "--port", str(port)],
            cwd=directory,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Instrument exited:\n{log_path.read_text(errors='replace')}")
                try:
                    if _request(base_url, "/health")[0] == 200:
                        break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.05)
            else:
                pytest.fail(f"Instrument did not start:\n{log_path.read_text(errors='replace')}")
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_example_api_preserves_fixture_identity_and_actual_observations(instrument_server):
    original = FIXTURE.read_bytes()
    expected = json.loads(MANIFEST.read_text())
    status, headers, body = _request(instrument_server, "/api/example")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert headers["Cache-Control"] == "no-store"
    payload = json.loads(body)
    assert payload["schema_version"] == 3
    recording = payload["recording"]
    assert recording["synthetic"] is True
    assert recording["source_name"] == "telemetry-gap.tlog (synthetic example)"
    assert recording["sha256"] == expected["sha256"] == hashlib.sha256(original).hexdigest()
    assert recording["size_bytes"] == recording["consumed_bytes"] == expected["size_bytes"]
    assert recording["record_count"] == recording["decoded_count"] == expected["record_count"]
    assert recording["opaque_count"] == recording["remaining_bytes"] == 0
    assert recording["traversal"] == "complete"
    assert recording["source_count"] == 2
    assert recording["profile"] == expected["profile"]
    assert recording["dialect"] == expected["dialect"]
    assert recording["decoder_version"] == version("pymavlink")
    assert recording["capture_origin_us"] == recording["first_timestamp_us"] == "1700000000000000"
    assert recording["last_timestamp_us"] == "1700000006000000"
    assert recording["min_timestamp_us"] == recording["first_timestamp_us"]
    assert recording["max_timestamp_us"] == recording["last_timestamp_us"]
    assert (recording["start_s"], recording["end_s"]) == ("0", "6")
    assert recording["wire_versions"] == [1, 2]
    assert recording["capture_span_s"] == "6"
    assert len(payload["issues"]) == payload["issue_count"] == 5
    assert payload["issue_counts"] == {"timestamp_repeated": 5}
    assert (payload["issue_page"], payload["issue_page_size"]) == (0, 100)
    assert {issue["code"] for issue in payload["issues"]} == {"timestamp_repeated"}
    assert {issue["severity"] for issue in payload["issues"]} == {"warning"}
    assert payload["message_types"] == [
        {"message_id": 0, "name": "HEARTBEAT", "record_count": 5},
        {"message_id": 30, "name": "ATTITUDE", "record_count": 7},
    ]
    selection = payload["selection"]
    assert selection["source"] is None and selection["message_id"] is None
    assert selection["record_count"] == 12
    assert selection["attitude"]["summary"]["status"] == "multiple_sources"
    assert selection["attitude"]["figure"] is None
    assert sum(selection["activity"]["figure"]["data"][0]["y"]) == 12

    for source in payload["sources"]:
        source_id = source["system_id"], source["component_id"]
        expected_records = [
            record
            for record in expected["records"]
            if (record["system_id"], record["component_id"]) == source_id
        ]
        assert source["record_count"] == len(expected_records)
        source_payload = json.loads(
            _request(instrument_server, f"/api/example?source={source_id[0]}:{source_id[1]}")[2]
        )
        attitude = source_payload["selection"]["attitude"]
        summary = attitude["summary"]
        assert summary["status"] == "ready"
        assert summary["source"] == list(source_id)
        assert summary["unit"] == "rad"
        assert summary["max_gap_us"] == "1000000"
        expected_attitude = [record for record in expected_records if record["message_id"] == 30]
        assert summary["record_count"] == summary["plotted_record_count"] == len(expected_attitude)
        for trace, field in zip(attitude["figure"]["data"], ("roll", "pitch", "yaw"), strict=True):
            assert [value for value in trace["y"] if value is not None] == [
                record["fields"][field] for record in expected_attitude
            ]
            references = [item for item in trace["customdata"] if item is not None]
            assert [item[0] for item in references] == [item["index"] for item in expected_attitude]
            for reference, record in zip(references, expected_attitude, strict=True):
                assert reference[1] == str(record["timestamp_us"])
                assert reference[4:] == [
                    record["offset"],
                    record["frame_offset"],
                    record["frame_offset"] + record["frame_length"],
                ]
            assert trace["connectgaps"] is False
            if source_id == (1, 1):
                assert trace["x"] == [1.0, None, 5.0]
    assert FIXTURE.read_bytes() == original
    assert files("uav_debugger").joinpath("data", "telemetry-gap.tlog").read_bytes() == original


def test_large_capture_timestamps_remain_exact_strings():
    imported = import_bytes(FIXTURE.read_bytes())
    origin = (1 << 64) - 10_000_000
    data = b"".join(
        (origin + record.timestamp_us - imported.records[0].timestamp_us).to_bytes(8, "big")
        + record.raw_frame
        for record in imported.records
    )
    result = import_bytes(data)
    payload = _recording_payload(result, {"source": "1:1"})
    assert payload["recording"]["capture_origin_us"] == str(origin)
    assert payload["recording"]["last_timestamp_us"] == str(origin + 6_000_000)
    assert payload["recording"]["capture_span_s"] == "6"
    for trace in payload["selection"]["attitude"]["figure"]["data"]:
        for reference in trace["customdata"]:
            if reference is not None:
                record = result.records[reference[0]]
                assert reference[1] == str(record.timestamp_us)
                assert data[record.frame_offset : record.end_offset] == record.raw_frame
    json.dumps(payload, allow_nan=False)


def test_example_is_stateless_and_query_parameters_cannot_select_files(instrument_server):
    first = _request(instrument_server, "/api/example")[2]
    assert _request(instrument_server, "/api/example?path=/etc/passwd")[0] == 400
    _request(instrument_server, "/api/example?source=1:1&start=2&end=4")
    assert first == _request(instrument_server, "/api/example")[2]
    assert _request(instrument_server, "/api/example", method="POST")[0] == 405
    assert _request(instrument_server, "/api/upload", method="POST")[0] == 404
    assert _request(instrument_server, "/api/experiment", method="POST")[0] == 404
    assert _request(instrument_server, "/api/analyze")[0] == 405


def test_missing_bundled_example_returns_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setattr("uav_debugger.instrument.files", lambda package: tmp_path)
    application = create_app()
    example = next(route for route in application.routes if route.path == "/api/example")
    response = example.endpoint(_endpoint_request("/api/example"))
    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "The bundled example could not be read."}


def test_config_health_assets_and_response_headers(instrument_server):
    status, headers, body = _request(instrument_server, "/api/config")
    assert status == 200
    assert json.loads(body) == {
        "schema_version": 3,
        "version": version("uav-debugger"),
        "classic_url": None,
        "max_recording_bytes": MAX_INPUT_BYTES,
    }
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["X-Frame-Options"] == "DENY"
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert "connect-src 'self'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers
    assert json.loads(_request(instrument_server, "/health")[2]) == {"status": "ok"}
    status, headers, body = _request(instrument_server, "/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b"UAV Debugger" in body
    status, headers, body = _request(instrument_server, "/vendor/plotly.min.js")
    assert status == 200
    assert headers["Content-Type"].startswith("text/javascript")
    assert body == files("plotly").joinpath("package_data", "plotly.min.js").read_bytes()
    assert _request(instrument_server, "/assets/nonexistent.txt")[0] == 404
    assert _request(instrument_server, "/assets/../instrument.py")[0] == 404
    assert _request(instrument_server, "/assets/%2e%2e/instrument.py")[0] == 404
    assert _request(instrument_server, "/vendor/nonexistent.js")[0] == 404
    assert _request(instrument_server, "/api/config", headers={"Host": "remote.invalid"})[0] == 400
    assert _request(instrument_server, "/api/config", headers={"Host": "localhost"})[0] == 200


def test_upload_and_example_share_the_same_import_and_filter_path(instrument_server):
    original = FIXTURE.read_bytes()
    query = {"source": "1:1", "message_id": "30", "start": "1", "end": "5"}
    status, headers, body = _upload(instrument_server, original, name="chosen.tlog", **query)
    assert status == 200
    assert headers["Cache-Control"] == "no-store"
    uploaded = json.loads(body)
    example = json.loads(_request(instrument_server, "/api/example?" + urlencode(query))[2])
    assert uploaded["recording"]["source_name"] == "chosen.tlog"
    assert uploaded["recording"]["synthetic"] is False
    example["recording"]["source_name"] = "chosen.tlog"
    example["recording"]["synthetic"] = False
    assert uploaded == example
    selection = uploaded["selection"]
    assert selection["source"] == [1, 1]
    assert selection["message_id"] == 30
    assert selection["record_count"] == selection["decoded_count"] == 2
    assert selection["opaque_count"] == 0
    assert selection["start_us"] == "1700000001000000"
    assert selection["end_us"] == "1700000005000000"
    assert selection["longest_interval_us"] == "4000000"
    assert sum(selection["activity"]["figure"]["data"][0]["y"]) == 2
    assert len(selection["activity"]["figure"]["data"][0]["y"]) <= 200
    assert selection["attitude"]["figure"]["data"][0]["x"] == [1.0, None, 5.0]
    connected = json.loads(_upload(instrument_server, original, gap="4", **query)[2])
    assert connected["selection"]["attitude"]["figure"]["data"][0]["x"] == [1.0, 5.0]
    assert connected["selection"]["max_gap_s"] == "4"
    assert connected["selection"]["record_count"] == 2
    assert FIXTURE.read_bytes() == original


@pytest.mark.parametrize(
    "data,traversal,count,code",
    [
        (b"", "empty", 0, "empty_input"),
        (b"not a supported recording", "stopped", 0, "invalid_magic"),
        (FIXTURE.read_bytes()[:-1], "stopped", 11, "incomplete_frame"),
    ],
)
def test_empty_and_damaged_inputs_are_inspectable_outcomes(
    instrument_server, data, traversal, count, code
):
    status, _, body = _upload(instrument_server, data)
    assert status == 200
    payload = json.loads(body)
    recording = payload["recording"]
    assert recording["traversal"] == traversal
    assert recording["record_count"] == payload["selection"]["record_count"] == count
    assert recording["sha256"] == hashlib.sha256(data).hexdigest()
    assert recording["consumed_bytes"] + recording["remaining_bytes"] == len(data)
    assert code in payload["issue_counts"]
    if not count:
        assert recording["capture_origin_us"] is None
        assert recording["min_timestamp_us"] is None
        assert payload["selection"]["start_us"] is None
        assert payload["selection"]["activity"] == {"status": "empty", "figure": None}
        assert payload["selection"]["attitude"]["summary"]["status"] == "empty"


def test_empty_selection_does_not_change_import_provenance(instrument_server):
    payload = json.loads(
        _upload(
            instrument_server,
            FIXTURE.read_bytes(),
            source="1:1",
            message_id="30",
            start="2",
            end="4",
        )[2]
    )
    assert payload["recording"]["record_count"] == 12
    assert payload["issue_count"] == 5
    selection = payload["selection"]
    assert selection["record_count"] == 0
    assert selection["activity"] == {"status": "empty", "figure": None}
    assert selection["attitude"]["summary"]["status"] == "empty"
    assert selection["attitude"]["figure"] is None
    assert selection["longest_interval_us"] is None


@pytest.mark.parametrize(
    "query",
    [
        "path=/etc/passwd",
        "source=1:1&source=2:1",
        "source=1",
        "source=1:2",
        "source=-1:1",
        "source=256:1",
        "message_id=999",
        "message_id=30.0",
        "message_id=1_0",
        "start=5&end=1",
        "start=NaN",
        "end=Infinity",
        "start=",
        "start=0.0000001",
        "end=1e10000",
        "gap=0",
        "gap=-1",
        "gap=nan",
        "gap=0.0000001",
        "issue_page=-1",
        "issue_page=1",
        "issue_page=0.0",
        "name=not-allowed.tlog",
        "start=" + "1" * 129,
    ],
)
def test_invalid_example_filters_are_explicit_errors_without_shared_state(instrument_server, query):
    status, _, body = _request(instrument_server, "/api/example?" + query)
    assert status == 400
    assert isinstance(json.loads(body)["error"], str)
    assert (
        json.loads(_request(instrument_server, "/api/example")[2])["selection"]["record_count"]
        == 12
    )


def test_upload_filters_and_names_are_validated(instrument_server):
    for parameters in [
        {"name": ""},
        {"name": "\x00file.tlog"},
        {"name": "x" * 1025},
        {"name": "capture.tlog", "source": "999:1"},
        {"name": "capture.tlog", "start": "0.0000001"},
    ]:
        assert _upload(instrument_server, FIXTURE.read_bytes(), **parameters)[0] == 400
    assert _upload(instrument_server, b"", start="0")[0] == 400
    literal = "../../<capture_1>.tlog"
    status, _, body = _upload(instrument_server, FIXTURE.read_bytes(), name=literal)
    assert status == 200
    assert json.loads(body)["recording"]["source_name"] == literal
    assert (
        json.loads(body)["recording"]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json"}, 415),
        ({"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"}, 415),
        ({"Content-Type": "application/octet-stream", "Origin": "https://other.invalid"}, 403),
        ({"Content-Type": "application/octet-stream", "Origin": "null"}, 403),
        ({"Content-Type": "application/octet-stream", "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "application/octet-stream", "Sec-Fetch-Site": "same-site"}, 403),
    ],
)
def test_upload_rejects_cross_origin_and_nonbinary_requests(instrument_server, headers, status):
    actual, response_headers, body = _request(
        instrument_server, "/api/analyze", method="POST", headers=headers, data=FIXTURE.read_bytes()
    )
    assert actual == status
    assert json.loads(body)["error"]
    assert "Access-Control-Allow-Origin" not in response_headers
    assert response_headers["Cache-Control"] == "no-store"


def test_same_origin_upload_is_accepted_and_preflight_is_not_enabled(instrument_server):
    status, _, _ = _request(
        instrument_server,
        "/api/analyze",
        method="POST",
        data=FIXTURE.read_bytes(),
        headers={
            "Content-Type": "application/octet-stream",
            "Origin": instrument_server,
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert status == 200
    assert _request(instrument_server, "/api/analyze", method="OPTIONS")[0] == 405


@pytest.mark.parametrize("declared_length", [None, "1"])
def test_upload_stream_limit_does_not_trust_content_length(monkeypatch, declared_length):
    monkeypatch.setattr("uav_debugger.instrument.MAX_INPUT_BYTES", 32)
    chunks_read = []

    async def receive():
        chunks_read.append(1)
        return {"type": "http.request", "body": bytes(16), "more_body": True}

    headers = {"Content-Type": "application/octet-stream"}
    if declared_length is not None:
        headers["Content-Length"] = declared_length
    request = _endpoint_request("/api/analyze", headers=headers, receive=receive)
    route = next(route for route in create_app().routes if route.path == "/api/analyze")
    response = asyncio.run(route.endpoint(request))
    assert response.status_code == 413
    assert json.loads(response.body) == {"error": "Input exceeds the 32-byte size limit."}
    assert len(chunks_read) == 3


def test_declared_oversize_is_rejected_without_reading_or_importing():
    async def receive():
        pytest.fail("An oversized declared body must not be read")

    route = next(route for route in create_app().routes if route.path == "/api/analyze")
    response = asyncio.run(
        route.endpoint(
            _endpoint_request(
                "/api/analyze",
                receive=receive,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(MAX_INPUT_BYTES + 1),
                },
            )
        )
    )
    assert response.status_code == 413


def test_exact_stream_limit_is_allowed_and_processing_runs_off_event_loop(monkeypatch):
    import threading

    from uav_debugger.instrument import _analyze_response

    monkeypatch.setattr("uav_debugger.instrument.MAX_INPUT_BYTES", 32)
    caller = threading.get_ident()
    worker_threads = []

    def checked_response(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        return _analyze_response(*args, **kwargs)

    async def receive():
        return {"type": "http.request", "body": bytes(32), "more_body": False}

    monkeypatch.setattr("uav_debugger.instrument._analyze_response", checked_response)
    route = next(route for route in create_app().routes if route.path == "/api/analyze")
    response = asyncio.run(
        route.endpoint(
            _endpoint_request(
                "/api/analyze",
                headers={"Content-Type": "application/octet-stream"},
                receive=receive,
            )
        )
    )
    assert response.status_code == 200
    assert json.loads(response.body)["recording"]["size_bytes"] == 32
    assert len(worker_threads) == 1 and worker_threads[0] != caller


def test_disconnected_upload_has_an_explicit_error():
    async def receive():
        return {"type": "http.disconnect"}

    route = next(route for route in create_app().routes if route.path == "/api/analyze")
    response = asyncio.run(
        route.endpoint(
            _endpoint_request(
                "/api/analyze",
                headers={"Content-Type": "application/octet-stream"},
                receive=receive,
            )
        )
    )
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "Recording upload was interrupted."}


def test_upload_requests_do_not_share_recordings_filters_or_identity(instrument_server):
    first = _upload(instrument_server, FIXTURE.read_bytes(), source="1:1", name="same.tlog")[2]
    replacement = json.loads(
        _upload(instrument_server, FIXTURE.read_bytes()[:25], name="same.tlog")[2]
    )
    assert replacement["recording"]["record_count"] == 1
    assert replacement["selection"]["source"] is None
    assert replacement["selection"]["start_s"] == replacement["selection"]["end_s"] == "0"
    assert (
        first == _upload(instrument_server, FIXTURE.read_bytes(), source="1:1", name="same.tlog")[2]
    )


def test_hidden_clock_regression_and_original_order_survive_api_filters(instrument_server):
    records = import_bytes(FIXTURE.read_bytes()).records
    data = b"".join(
        timestamp.to_bytes(8, "big") + records[index].raw_frame
        for index, timestamp in [(2, 100), (1, 90), (2, 110), (2, 120)]
    )
    payload = json.loads(_upload(instrument_server, data, source="1:1", message_id="30")[2])
    recording = payload["recording"]
    assert recording["capture_origin_us"] == "100"
    assert recording["min_timestamp_us"] == "90"
    assert recording["start_s"] == "-0.00001"
    assert recording["capture_span_s"] == "0.00003"
    assert payload["selection"]["longest_interval_us"] == "10"
    trace = payload["selection"]["attitude"]["figure"]["data"][0]
    assert trace["y"] == [0.25, None, 0.25, 0.25]
    assert [ref[0] for ref in trace["customdata"] if ref is not None] == [0, 2, 3]
    assert [row["index"] for row in payload["selection"]["messages"]["records"]] == [0, 2, 3]
    assert payload["issue_counts"] == {"timestamp_regression": 1}


def test_microsecond_filters_at_uint64_limit_and_large_gap_remain_exact(instrument_server):
    frame = import_bytes(FIXTURE.read_bytes()).records[2].raw_frame
    origin = (1 << 64) - 3
    data = b"".join((origin + delta).to_bytes(8, "big") + frame for delta in range(3))
    payload = json.loads(
        _upload(instrument_server, data, start="0.000001", end="0.000001", gap="9007199254.740993")[
            2
        ]
    )
    selection = payload["selection"]
    assert selection["record_count"] == 1
    assert selection["start_us"] == selection["end_us"] == str(origin + 1)
    assert selection["max_gap_s"] == "9007199254.740993"
    assert selection["attitude"]["summary"]["max_gap_us"] == "9007199254740993"
    reference = selection["attitude"]["figure"]["data"][0]["customdata"][0]
    assert reference[:3] == [1, str(origin + 1), "0.000001"]
    assert selection["activity"]["figure"]["data"][0]["customdata"] == [["0.000001", "0.000001"]]


def test_unknown_messages_remain_selectable_but_their_intervals_are_unverified(instrument_server):
    frame = bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00")
    data = b"".join(timestamp.to_bytes(8, "big") + frame for timestamp in (10, 20))
    payload = json.loads(_upload(instrument_server, data, message_id="16777215")[2])
    assert payload["recording"]["traversal"] == "complete"
    assert payload["recording"]["opaque_count"] == 2
    assert payload["message_types"] == [
        {"message_id": 0xFFFFFF, "name": "UNKNOWN_16777215", "record_count": 2}
    ]
    assert payload["selection"]["opaque_count"] == 2
    assert payload["selection"]["longest_interval_us"] is None
    assert payload["selection"]["attitude"]["summary"]["status"] == "empty"
    assert sum(payload["selection"]["activity"]["figure"]["data"][0]["y"]) == 2


def test_attitude_limit_is_explicit_and_can_be_resolved_by_time_filters(instrument_server):
    frame = import_bytes(FIXTURE.read_bytes()).records[2].raw_frame
    data = b"".join(index.to_bytes(8, "big") + frame for index in range(5001))
    payload = json.loads(_upload(instrument_server, data)[2])
    assert payload["selection"]["record_count"] == 5001
    attitude = payload["selection"]["attitude"]
    assert attitude["summary"]["status"] == "too_many"
    assert attitude["summary"]["point_limit"] == 5000
    assert attitude["figure"] is None
    assert payload["selection"]["messages"]["total_count"] == 5001
    assert payload["selection"]["messages"]["page_count"] == 51
    assert sum(payload["selection"]["activity"]["figure"]["data"][0]["y"]) == 5001
    last = json.loads(_upload(instrument_server, data, record_index="5000")[2])
    assert last["selection"]["messages"]["page"] == 50
    assert last["inspector"]["index"] == 5000
    assert last["inspector"]["raw_frame_hex"] == frame.hex()
    assert last["selection"]["attitude"]["summary"]["status"] == "too_many"
    narrowed = json.loads(_upload(instrument_server, data, end="0.004999")[2])
    assert narrowed["selection"]["attitude"]["summary"]["status"] == "ready"
    assert narrowed["selection"]["attitude"]["summary"]["record_count"] == 5000


def test_issue_pages_cover_original_full_recording_without_dropping_issues(instrument_server):
    frame = import_bytes(FIXTURE.read_bytes()).records[0].raw_frame
    data = (bytes(8) + frame) * 202
    first = json.loads(_upload(instrument_server, data)[2])
    second = json.loads(_upload(instrument_server, data, issue_page="1")[2])
    third = json.loads(_upload(instrument_server, data, issue_page="2")[2])
    issues = first["issues"] + second["issues"] + third["issues"]
    assert [len(page["issues"]) for page in (first, second, third)] == [100, 100, 1]
    assert [issue["record_index"] for issue in issues] == list(range(1, 202))
    for page, payload in enumerate((first, second, third)):
        assert payload["issue_count"] == 201
        assert payload["issue_counts"] == {"timestamp_repeated": 201}
        assert payload["issue_page"] == page
        assert payload["issue_page_size"] == 100
        assert payload["selection"]["longest_interval_us"] == "0"
        assert payload["selection"]["record_count"] == 202
    assert _upload(instrument_server, data, issue_page="3")[0] == 400


def _report_blocks(body):
    return [
        json.loads(match.group(2))
        for match in re.finditer(r"(?ms)^(`{3,})json\n(.*?)^\1[ \t]*$", body.decode())
    ]


def test_messages_and_explicit_inspector_preserve_original_byte_and_clock_references(
    instrument_server,
):
    original = FIXTURE.read_bytes()
    result = import_bytes(original)
    parameters = {"source": "1:1", "message_id": "30", "start": "1", "end": "5"}
    payload = json.loads(_upload(instrument_server, original, **parameters)[2])
    messages = payload["selection"]["messages"]
    assert messages["page"] == 0
    assert messages["page_count"] == 1
    assert messages["page_size"] == 100
    assert messages["total_count"] == 2
    assert payload["inspector"] is None
    assert [record["index"] for record in messages["records"]] == [2, 8]
    for row in messages["records"]:
        record = result.records[row["index"]]
        assert row == {
            "index": record.index,
            "time_s": "1" if record.index == 2 else "5",
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
    inspected = json.loads(_upload(instrument_server, original, **parameters, record_index="8")[2])[
        "inspector"
    ]
    record = result.records[8]
    assert {key: inspected[key] for key in messages["records"][1]} == messages["records"][1]
    assert json.loads(inspected["fields_json"]) == dict(record.fields)
    assert inspected["raw_frame_hex"] == record.raw_frame.hex()
    assert (
        bytes.fromhex(inspected["raw_frame_hex"])
        == original[record.frame_offset : record.end_offset]
    )
    assert bytes.fromhex(inspected["raw_record_hex"]) == original[record.offset : record.end_offset]
    assert inspected["raw_record_hex"][:16] == record.timestamp_us.to_bytes(8, "big").hex()
    assert FIXTURE.read_bytes() == original


def test_message_paging_uses_filtered_file_order_and_explicit_index_reveals_its_page(
    instrument_server,
):
    records = import_bytes(FIXTURE.read_bytes()).records
    data = b"".join(
        index.to_bytes(8, "big") + records[0 if index % 2 else 1].raw_frame for index in range(405)
    )
    pages = [
        json.loads(_upload(instrument_server, data, source="1:1", record_page=str(page))[2])
        for page in range(3)
    ]
    assert [len(page["selection"]["messages"]["records"]) for page in pages] == [100, 100, 2]
    assert [
        row["index"] for page in pages for row in page["selection"]["messages"]["records"]
    ] == list(range(1, 405, 2))
    for page_number, payload in enumerate(pages):
        assert payload["selection"]["record_count"] == 202
        assert payload["selection"]["messages"]["total_count"] == 202
        assert payload["selection"]["messages"]["page_count"] == 3
        assert payload["selection"]["messages"]["page"] == page_number
        assert payload["inspector"] is None
    focused = json.loads(_upload(instrument_server, data, source="1:1", record_index="401")[2])
    assert focused["selection"]["messages"]["page"] == 2
    assert focused["inspector"]["index"] == 401
    explicit = json.loads(
        _upload(instrument_server, data, source="1:1", record_index="401", record_page="2")[2]
    )
    assert explicit == focused
    assert _upload(instrument_server, data, source="1:1", record_page="3")[0] == 400
    assert (
        _upload(instrument_server, data, source="1:1", record_page="1", record_index="401")[0]
        == 400
    )


@pytest.mark.parametrize(
    "parameters",
    [
        {"record_index": "-1"},
        {"record_index": "1.0"},
        {"record_index": "1e0"},
        {"record_index": "12"},
        {"record_index": "0", "message_id": "30"},
        {"record_index": "8", "end": "2"},
        {"record_index": "2", "source": "2:1"},
        {"record_page": "-1"},
        {"record_page": "1"},
        {"record_page": "0.0"},
        {"record_page": "1e0"},
        {"format": "html"},
        {"sha256": ""},
        {"sha256": "0" * 63},
        {"sha256": "A" * 64},
        {"sha256": "g" * 64},
    ],
)
def test_inspector_and_report_parameters_are_strictly_validated(instrument_server, parameters):
    assert _request(instrument_server, "/api/example?" + urlencode(parameters))[0] == 400
    if "format" not in parameters and "sha256" not in parameters:
        report_parameters = {
            "format": "markdown",
            "sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            **parameters,
        }
        assert _request(instrument_server, "/api/example?" + urlencode(report_parameters))[0] == 400


@pytest.mark.parametrize("parameter", ["record_index", "record_page", "format", "sha256"])
def test_inspection_query_parameters_cannot_be_repeated(instrument_server, parameter):
    assert _request(instrument_server, f"/api/example?{parameter}=0&{parameter}=1")[0] == 400


def test_empty_message_table_and_partial_import_never_invent_a_selected_record(instrument_server):
    empty = json.loads(_upload(instrument_server, b"")[2])
    assert empty["selection"]["messages"] == {
        "page": 0,
        "page_size": 100,
        "page_count": 1,
        "total_count": 0,
        "records": [],
    }
    assert empty["inspector"] is None
    assert _upload(instrument_server, b"", record_index="0")[0] == 400
    assert _upload(instrument_server, b"", record_page="1")[0] == 400
    prefix = FIXTURE.read_bytes()[:-1]
    partial = json.loads(_upload(instrument_server, prefix, record_index="10")[2])
    assert partial["recording"]["traversal"] == "stopped"
    assert partial["selection"]["messages"]["total_count"] == 11
    assert partial["inspector"]["index"] == 10
    assert _upload(instrument_server, prefix, record_index="11")[0] == 400
    narrowed = json.loads(_upload(instrument_server, prefix, start="2", end="2.5")[2])
    assert narrowed["inspector"] is None


def test_opaque_inspector_retains_raw_bytes_without_claiming_fields_or_verified_checksum(
    instrument_server,
):
    frame = bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00")
    data = (123456789).to_bytes(8, "big") + frame
    payload = json.loads(_upload(instrument_server, data, record_index="0")[2])
    detail = payload["inspector"]
    assert detail["index"] == 0
    assert detail["message_id"] == 0xFFFFFF
    assert detail["message_name"] is None
    assert detail["checksum_status"] == "unverified"
    assert detail["fields_json"] is None
    assert detail["raw_frame_hex"] == frame.hex()
    assert detail["raw_record_hex"] == data.hex()
    status, _, body = _upload(
        instrument_server,
        data,
        record_index="0",
        format="markdown",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert status == 200
    exported = _report_blocks(body)[-1]
    assert exported["fields"] is None
    assert exported["checksum_status"] == "unverified"
    assert exported["raw_frame_hex"] == frame.hex()


def test_inspector_preserves_uint64_capture_and_device_clocks_as_distinct_exact_text(
    instrument_server,
):
    encoder = common.MAVLink(None, srcSystem=3, srcComponent=4)
    device_time = (1 << 64) - 1
    capture_time = device_time - 102
    frame = encoder.gps_raw_int_encode(device_time, 3, 0, 0, 0, 1, 1, 0, 0, 10).pack(encoder)
    data = capture_time.to_bytes(8, "big") + frame
    payload = json.loads(_upload(instrument_server, data, record_index="0")[2])
    detail = payload["inspector"]
    assert detail["timestamp_us"] == str(capture_time)
    assert detail["time_s"] == "0"
    assert isinstance(detail["fields_json"], str)
    assert f'"time_usec": {device_time}' in detail["fields_json"]
    assert json.loads(detail["fields_json"])["time_usec"] == device_time
    assert detail["raw_record_hex"] == data.hex()
    assert payload["selection"]["messages"]["records"][0]["timestamp_us"] == str(capture_time)
    status, _, body = _upload(
        instrument_server,
        data,
        record_index="0",
        format="markdown",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert status == 200
    exported = _report_blocks(body)[-1]
    assert exported["timestamp_us"] == capture_time
    assert exported["fields"]["time_usec"] == device_time


def test_inspector_uses_report_tags_for_nonfinite_values_bytes_and_literal_strings():
    original = import_bytes(FIXTURE.read_bytes())
    record = replace(
        original.records[0],
        fields={
            "device_clock": (1 << 64) - 1,
            "bytes": b"\x00\xff",
            "array": (1, float("nan"), float("inf"), float("-inf")),
            "literal": "nan",
            "label": "</script><script>unsafe()</script>",
        },
    )
    payload = _recording_payload(replace(original, records=(record,)), {"record_index": "0"})
    fields_text = payload["inspector"]["fields_json"]
    assert json.loads(fields_text) == {
        "device_clock": (1 << 64) - 1,
        "bytes": {"bytes_hex": "00ff"},
        "array": [
            1,
            {"non_finite_float": "nan"},
            {"non_finite_float": "inf"},
            {"non_finite_float": "-inf"},
        ],
        "literal": "nan",
        "label": "</script><script>unsafe()</script>",
    }
    json.dumps(payload, allow_nan=False)


def test_real_nonfinite_attitude_can_be_inspected_even_when_plot_value_is_unavailable(
    instrument_server,
):
    encoder = common.MAVLink(None)
    frame = encoder.attitude_encode(123, float("nan"), 0.5, 1.0, 0, 0, 0).pack(encoder)
    data = (100).to_bytes(8, "big") + frame
    payload = json.loads(_upload(instrument_server, data, record_index="0")[2])
    detail = payload["inspector"]
    assert json.loads(detail["fields_json"])["roll"] == {"non_finite_float": "nan"}
    assert detail["raw_record_hex"] == data.hex()
    assert payload["selection"]["attitude"]["summary"]["invalid_value_counts"]["roll"] == 1


@pytest.mark.parametrize("output_format", ["json", "markdown"])
def test_fingerprint_guard_rejects_replaced_bytes_before_returning_stale_details(
    instrument_server, output_format
):
    data = FIXTURE.read_bytes()
    expected = hashlib.sha256(data).hexdigest()
    parameters = {"sha256": expected, "record_index": "0", "format": output_format}
    assert _upload(instrument_server, data, **parameters)[0] == 200
    status, headers, body = _upload(instrument_server, data[:-1], **parameters)
    assert status == 409
    assert headers["Content-Type"] == "application/json"
    assert "applied SHA-256" in json.loads(body)["error"]
    assert "Content-Disposition" not in headers
    assert _upload(instrument_server, data, **parameters)[0] == 200
    assert (
        _request(
            instrument_server, "/api/example?" + urlencode({**parameters, "sha256": "0" * 64})
        )[0]
        == 409
    )


def test_markdown_download_matches_domain_report_and_only_explicitly_requested_details(
    instrument_server,
):
    data = FIXTURE.read_bytes()
    fingerprint = hashlib.sha256(data).hexdigest()
    parameters = {
        "source": "1:1",
        "message_id": "30",
        "start": "1",
        "end": "5",
        "gap": "1.000001",
        "record_index": "8",
        "format": "markdown",
        "sha256": fingerprint,
    }
    status, headers, body = _request(instrument_server, "/api/example?" + urlencode(parameters))
    assert status == 200
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert (
        headers["Content-Disposition"]
        == f'attachment; filename="uav-debugger-{fingerprint[:12]}.md"'
    )
    assert headers["Cache-Control"] == "no-store"
    result = import_bytes(data, source_name="telemetry-gap.tlog (synthetic example)")
    selection = Selection(
        sources=((1, 1),), message_ids=(30,), start_us=1700000001000000, end_us=1700000005000000
    )
    assert body.decode() == build_markdown_report(
        result, selection, selected_indices=(8,), attitude_plot_gap_us=1_000_001
    )
    provenance, filters, coverage, _, detail = _report_blocks(body)
    assert provenance["sha256"] == fingerprint
    assert filters["start_us"] == 1700000001000000
    assert coverage["filtered_record_count"] == 2
    assert coverage["imported_record_count"] == 12
    assert coverage["explicit_detail_record_count"] == 1
    assert coverage["attitude_plot"]["max_gap_us"] == 1_000_001
    assert detail["index"] == 8
    assert detail["raw_frame_hex"] == result.records[8].raw_frame.hex()
    without_detail = {key: value for key, value in parameters.items() if key != "record_index"}
    blocks = _report_blocks(
        _request(instrument_server, "/api/example?" + urlencode(without_detail))[2]
    )
    assert len(blocks) == 4
    assert blocks[2]["explicit_detail_record_count"] == 0
    assert _request(instrument_server, "/api/example?format=markdown")[0] == 400
    assert _upload(instrument_server, data, format="markdown")[0] == 400


def test_report_counts_cover_full_selection_not_current_pages_and_preserve_stop_error(
    instrument_server,
):
    frame = import_bytes(FIXTURE.read_bytes()).records[0].raw_frame
    data = (bytes(8) + frame) * 205 + b"truncated"
    status, _, body = _upload(
        instrument_server,
        data,
        record_page="2",
        record_index="204",
        issue_page="1",
        format="markdown",
        sha256=hashlib.sha256(data).hexdigest(),
    )
    assert status == 200
    _, _, coverage, issues, detail = _report_blocks(body)
    assert coverage["imported_record_count"] == coverage["filtered_record_count"] == 205
    assert coverage["explicit_detail_record_count"] == 1
    assert coverage["import_issue_count"] == 205
    assert coverage["reported_issue_count"] == 100
    assert coverage["omitted_issue_count"] == 105
    assert coverage["traversal"] == "stopped"
    assert coverage["remaining_bytes"] == len(b"truncated")
    assert issues[-1]["severity"] == "error"
    assert detail["index"] == 204


@pytest.mark.parametrize("data", [b"", b"invalid recording", FIXTURE.read_bytes()[:-1]])
def test_reports_remain_available_for_empty_and_partial_imports(instrument_server, data):
    status, _, body = _upload(
        instrument_server, data, format="markdown", sha256=hashlib.sha256(data).hexdigest()
    )
    assert status == 200
    provenance, _, coverage, issues = _report_blocks(body)
    result = import_bytes(data)
    assert provenance["sha256"] == hashlib.sha256(data).hexdigest()
    assert coverage["traversal"] == result.traversal
    assert coverage["remaining_bytes"] == result.remaining_bytes
    assert coverage["explicit_detail_record_count"] == 0
    assert coverage["filtered_record_count"] == len(result.records)
    assert len(issues) == len(result.issues)
    assert ("attitude_plot" in coverage) is bool(result.records)


def test_report_uses_shared_validation_without_rendering_chart_payloads(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Export should not construct browser chart payloads")

    monkeypatch.setattr("uav_debugger.instrument._recording_payload", forbidden)
    data = FIXTURE.read_bytes()
    name = '../../<script>alert("label")</script>`capture`.tlog'
    parameters = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": "markdown",
        "record_index": "0",
    }
    response = _analyze_response(data, name, parameters)
    assert response.status_code == 200
    assert "script" not in response.headers["Content-Disposition"]
    assert _report_blocks(response.body)[0]["source_name"] == name
    assert b"<script>" not in response.body
    assert FIXTURE.read_bytes() == data


@pytest.mark.parametrize("classic_port", [0, -1, 65536, True, "8501"])
def test_app_rejects_invalid_existing_workspace_port(classic_port):
    with pytest.raises(ValueError, match="port must be between"):
        create_app(classic_port=classic_port)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--port", "0"],
        ["--port", "65536"],
        ["--classic-port", "0"],
        ["--classic-port", "65536"],
        ["--port", "8501", "--classic-port", "8501"],
    ],
)
def test_launcher_rejects_invalid_ports_before_start(monkeypatch, arguments):
    def unexpected_start(*args, **kwargs):
        pytest.fail("Invalid launcher arguments started a server")

    monkeypatch.setattr("uav_debugger.instrument.uvicorn.run", unexpected_start)
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_launcher_binds_loopback_and_passes_only_explicit_handoff(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "uav_debugger.instrument.uvicorn.run", lambda *a, **kw: calls.append((a, kw))
    )
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    assert main(["--port", "8766", "--classic-port", "8502"]) == 0
    (application,), settings = calls[0]
    assert settings["host"] == "127.0.0.1"
    assert settings["port"] == 8766
    assert settings["proxy_headers"] is False
    configuration = next(route for route in application.routes if route.path == "/api/config")
    assert json.loads(configuration.endpoint(None).body)["classic_url"] == "http://127.0.0.1:8502"


def test_launcher_reports_an_occupied_port_without_leaving_a_process(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = subprocess.run(
            [sys.executable, "-m", "uav_debugger.instrument", "--port", str(port)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    assert result.returncode != 0
    assert "address already in use" in result.stderr.lower()


def test_analysis_routes_never_import_execution_start_commands_or_write_files(tmp_path):
    script = """
import asyncio
import hashlib
import importlib.abc
import json
import subprocess
import sys
from importlib.resources import files
from starlette.requests import Request

class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {
            'uav_debugger.experiment', 'uav_debugger.experiment_control',
            'uav_debugger.experiment_worker', 'uav_debugger.sitl', 'streamlit',
        }:
            raise AssertionError('Unexpected import: ' + fullname)

sys.meta_path.insert(0, Guard())
def forbidden(*args, **kwargs):
    raise AssertionError('The file-only workspace attempted to start a command')
subprocess.Popen = forbidden
from uav_debugger.instrument import create_app
app = create_app()
route = next(route for route in app.routes if route.path == '/api/example')
request = Request({'type': 'http', 'query_string': b''})
payload = json.loads(route.endpoint(request).body)
assert payload['recording']['record_count'] == 12
data = files('uav_debugger').joinpath('data', 'telemetry-gap.tlog').read_bytes()
query = b'record_index=8&source=1:1&message_id=30&sha256='
query += hashlib.sha256(data).hexdigest().encode()
request = Request({'type': 'http', 'query_string': query})
assert json.loads(route.endpoint(request).body)['inspector']['index'] == 8
request = Request({'type': 'http', 'query_string': query + b'&format=markdown'})
assert b'### Record 8' in route.endpoint(request).body
async def receive():
    return {'type': 'http.request', 'body': data, 'more_body': False}
request = Request({
    'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/api/analyze',
    'root_path': '', 'server': ('127.0.0.1', 8765),
    'query_string': b'name=../recording.tlog&' + query,
    'headers': [(b'host', b'127.0.0.1:8765'), (b'content-type', b'application/octet-stream')],
}, receive=receive)
route = next(route for route in app.routes if route.path == '/api/analyze')
response = asyncio.run(route.endpoint(request))
assert response.status_code == 200
assert json.loads(response.body)['recording']['record_count'] == 12
assert json.loads(response.body)['inspector']['index'] == 8
request = Request({
    **request.scope, 'query_string': request.scope['query_string'] + b'&format=markdown',
}, receive=receive)
response = asyncio.run(route.endpoint(request))
assert response.status_code == 200
assert b'### Record 8' in response.body
assert files('uav_debugger').joinpath('data', 'telemetry-gap.tlog').read_bytes() == data
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())
