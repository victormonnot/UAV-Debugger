"""Instrument serves original bundled evidence through a bounded local interface."""

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
from urllib.request import Request, urlopen

import pytest

from uav_debugger import import_bytes
from uav_debugger.instrument import _recording_payload, create_app, main

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"
MANIFEST = FIXTURE.with_name("telemetry-gap.expected.json")


def _request(base_url, path, *, method="GET", headers=None):
    request = Request(base_url + path, method=method, headers=headers or {})
    try:
        response = urlopen(request, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, response.headers, response.read()


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
    assert payload["schema_version"] == 1
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
    assert recording["capture_span_s"] == "6"
    assert len(payload["issues"]) == 5
    assert {issue["code"] for issue in payload["issues"]} == {"timestamp_repeated"}
    assert {issue["severity"] for issue in payload["issues"]} == {"warning"}

    for source in payload["sources"]:
        source_id = source["system_id"], source["component_id"]
        expected_records = [
            record
            for record in expected["records"]
            if (record["system_id"], record["component_id"]) == source_id
        ]
        assert source["record_count"] == len(expected_records)
        attitude = source["attitude"]
        summary = attitude["summary"]
        assert summary["status"] == "ready"
        assert summary["source"] == list(source_id)
        assert summary["unit"] == "rad"
        assert summary["max_gap_us"] == 1_000_000
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
    payload = _recording_payload(result)
    assert payload["recording"]["capture_origin_us"] == str(origin)
    assert payload["recording"]["last_timestamp_us"] == str(origin + 6_000_000)
    assert payload["recording"]["capture_span_s"] == "6"
    for source in payload["sources"]:
        for trace in source["attitude"]["figure"]["data"]:
            for reference in trace["customdata"]:
                if reference is not None:
                    record = result.records[reference[0]]
                    assert reference[1] == str(record.timestamp_us)
                    assert data[record.frame_offset : record.end_offset] == record.raw_frame
    json.dumps(payload, allow_nan=False)


def test_example_is_stateless_and_query_parameters_cannot_select_files(instrument_server):
    first = _request(instrument_server, "/api/example")[2]
    second = _request(instrument_server, "/api/example?path=/etc/passwd&source=other")[2]
    assert first == second
    assert _request(instrument_server, "/api/example", method="POST")[0] == 405
    assert _request(instrument_server, "/api/upload", method="POST")[0] == 404
    assert _request(instrument_server, "/api/experiment", method="POST")[0] == 404


def test_missing_bundled_example_returns_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setattr("uav_debugger.instrument.files", lambda package: tmp_path)
    application = create_app()
    example = next(route for route in application.routes if route.path == "/api/example")
    response = example.endpoint(None)
    assert response.status_code == 503
    assert json.loads(response.body) == {"error": "The bundled example could not be read."}


def test_config_health_assets_and_response_headers(instrument_server):
    status, headers, body = _request(instrument_server, "/api/config")
    assert status == 200
    assert json.loads(body) == {
        "schema_version": 1,
        "version": version("uav-debugger"),
        "classic_url": None,
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


def test_example_route_never_imports_execution_or_starts_commands(tmp_path):
    script = """
import importlib.abc
import json
import subprocess
import sys

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
payload = json.loads(route.endpoint(None).body)
assert payload['recording']['record_count'] == 12
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
