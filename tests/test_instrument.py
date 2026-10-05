"""Instrument imports bounded uploads with exact filters and no retained evidence."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from starlette.requests import Request as StarletteRequest

from uav_debugger import import_bytes
from uav_debugger.importer import MAX_INPUT_BYTES
from uav_debugger.instrument import _recording_payload, create_app, main

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
    assert payload["schema_version"] == 2
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
        "schema_version": 2,
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
    assert sum(payload["selection"]["activity"]["figure"]["data"][0]["y"]) == 5001
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
async def receive():
    return {'type': 'http.request', 'body': data, 'more_body': False}
request = Request({
    'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/api/analyze',
    'root_path': '', 'server': ('127.0.0.1', 8765), 'query_string': b'name=../recording.tlog',
    'headers': [(b'host', b'127.0.0.1:8765'), (b'content-type', b'application/octet-stream')],
}, receive=receive)
route = next(route for route in app.routes if route.path == '/api/analyze')
response = asyncio.run(route.endpoint(request))
assert response.status_code == 200
assert json.loads(response.body)['recording']['record_count'] == 12
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
