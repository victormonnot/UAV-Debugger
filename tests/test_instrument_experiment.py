"""Explicit HTTP actions retain the existing owned-worker and evidence boundaries."""

import asyncio
import inspect
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_instrument import _endpoint_request, _request
from test_instrument_runs import run_server as run_server

from uav_debugger.instrument import create_app, main
from uav_debugger.instrument_experiment import ACTION_HEADER
from uav_debugger.saved_run import load_run_directory

BASELINE = {"source": "synthetic", "scenario": "baseline", "duration_s": 0.2}


@pytest.fixture
def app(tmp_path):
    application = create_app(experiment_root=tmp_path / "runs")
    yield application
    application.state.experiments.close()


def invoke(app, path, value=None, *, raw=None, headers=None, parameters=None, chunk_size=31):
    data = raw if raw is not None else json.dumps(value).encode() if value is not None else b""
    chunks = iter(data[index : index + chunk_size] for index in range(0, len(data), chunk_size))

    async def receive():
        chunk = next(chunks, b"")
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

    request = _endpoint_request(
        path,
        parameters=parameters,
        headers={
            "Content-Type": "application/json",
            ACTION_HEADER: app.state.experiments.action_token,
            **(headers or {}),
        },
        receive=receive,
    )
    route = next(route for route in app.routes if route.path == path)
    response = route.endpoint(request)
    if inspect.isawaitable(response):
        response = asyncio.run(response)
    return response.status_code, json.loads(response.body)


def start(app, **settings):
    return invoke(app, "/api/experiment/start", {**BASELINE, **settings})


def status(app):
    return invoke(app, "/api/experiment/status")[1]["experiment"]


def terminal(app, identifier, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = next(row for row in status(app)["history"] if row["run_id"] == identifier)
        if not row["active"]:
            return row
        time.sleep(0.025)
    pytest.fail("Owned worker did not finish within the test allowance")


def test_idle_status_and_config_do_not_create_output_or_restore_history(app):
    workspace = app.state.experiments
    result = status(app)
    assert result["history"] == [] and result["active_run_id"] is None
    assert result["history_limit"] == 20 and len(result["action_token"]) >= 32
    assert result["configuration"] == {
        "experiment_root": str(workspace.root),
        "sitl_configured": False,
        "sitl_binary": None,
    }
    assert workspace._controller is None and not workspace.root.exists()
    another = create_app(experiment_root=workspace.root)
    assert status(another)["action_token"] != result["action_token"]
    assert invoke(app, "/api/experiment/open", parameters={"run_id": "run-missing"})[0] == 404
    assert workspace._controller is None and not workspace.root.exists()


@pytest.mark.parametrize(
    "settings",
    [
        {"duration_s": True},
        {"duration_s": "1"},
        {"duration_s": None},
        {"duration_s": []},
        {"duration_s": -1},
        {"duration_s": 0},
        {"duration_s": 61},
        {"duration_s": 10**300},
        {"duration_s": float("nan")},
        {"duration_s": float("inf")},
        {"source": "physical"},
        {"source": []},
        {"scenario": {}},
        {"scenario": "unknown"},
        {"blackout_at_s": 0.1},
        {"blackout_duration_s": 0.1},
        {"startup_timeout_s": False},
        {"startup_timeout_s": 0},
        {"startup_timeout_s": None},
        {"scenario": "blackout", "duration_s": 0.2},
        {"scenario": "blackout", "blackout_at_s": 0.0},
        {"scenario": "blackout", "blackout_duration_s": "0.1"},
        {"sitl_binary": "/client/path"},
        {"output": "/client/path"},
        {"source": "arducopter-sitl"},
    ],
)
def test_invalid_settings_never_create_controller_or_output(app, settings):
    code, body = start(app, **settings)
    assert code == 400 and body["error"]
    assert app.state.experiments._controller is None
    assert not app.state.experiments.root.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"null",
        b"1",
        b"false",
        b"{}",
        b"{",
        b'{"source":"synthetic","scenario":"baseline","duration_s":1,"duration_s":2}',
        b'{"source":"synthetic","scenario":"baseline","duration_s":NaN}',
        b'{"source":"synthetic","scenario":"baseline","duration_s":1e999}',
        b'{"source":"synthetic","scenario":"baseline","duration_s":1,"x":{"a":1,"a":2}}',
        b'{"source":"synthetic","scenario":"baseline","duration_s":1}\xff',
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_malformed_or_ambiguous_json_is_rejected_without_files(app, raw):
    assert invoke(app, "/api/experiment/start", raw=raw)[0] == 400
    assert app.state.experiments._controller is None
    assert not app.state.experiments.root.exists()


@pytest.mark.parametrize(
    "headers,code",
    [
        ({ACTION_HEADER: ""}, 403),
        ({ACTION_HEADER: "different"}, 403),
        ({ACTION_HEADER: "\u00e9"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://remote.invalid"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
        ({"Content-Encoding": "gzip"}, 415),
        ({"Content-Length": "4097"}, 413),
        ({"Content-Length": "-1"}, 400),
        ({"Content-Length": "x"}, 400),
    ],
)
def test_action_transport_guards_precede_any_execution(app, headers, code):
    assert invoke(app, "/api/experiment/start", BASELINE, headers=headers)[0] == code
    assert invoke(app, "/api/experiment/stop", {"run_id": "run-old"}, headers=headers)[0] == code
    assert app.state.experiments._controller is None
    assert not app.state.experiments.root.exists()


def test_actual_streaming_limit_and_query_guards(app):
    assert (
        invoke(app, "/api/experiment/start", raw=b" " * 4097, headers={"Content-Length": "1"})[0]
        == 413
    )
    assert invoke(app, "/api/experiment/start", BASELINE, parameters={"duration_s": "1"})[0] == 400
    assert invoke(app, "/api/experiment/status", parameters={"run_id": "run-old"})[0] == 400
    assert invoke(app, "/api/experiment/open", parameters={"key": "arbitrary/evidence"})[0] == 400
    assert not app.state.experiments.root.exists()


def test_real_baseline_snapshots_exact_clocks_and_validated_catalog_handoff(app):
    code, result = start(app)
    assert code == 200 and result["schema_version"] == 6
    identifier = result["run_id"]
    row = terminal(app, identifier)
    assert row["state"] == "finished" and row["declared_outcome"] == "completed"
    assert row["evidence_status"] == "not_validated" and row["can_open"] is True
    assert row["stop_requested"] is None and row["returncode"] == 0
    assert int(row["elapsed_ns"]) == int(row["finished"]["monotonic_ns"]) - int(
        row["created"]["monotonic_ns"]
    )
    code, opened = invoke(app, "/api/experiment/open", parameters={"run_id": identifier})
    assert code == 200 and opened["schema_version"] == 6
    assert opened["catalog_key"] == f"{identifier}/evidence"
    saved = load_run_directory(Path(row["output"]))
    assert opened["run"]["identity"] == saved.identity
    assert opened["run"]["evidence_status"] == "consistent"
    assert opened["recording"]["sha256"] == saved.captures["receiver"].sha256
    assert status(app)["history"][0]["evidence_status"] == "not_validated"
    code, catalog = invoke(
        app,
        "/api/catalog/open",
        parameters={"key": opened["catalog_key"], "run_sha256": saved.identity},
    )
    assert code == 200 and catalog["run"]["identity"] == saved.identity
    restarted = create_app(experiment_root=app.state.experiments.root)
    assert status(restarted)["history"] == []
    assert invoke(restarted, "/api/experiment/open", parameters={"run_id": identifier})[0] == 404
    assert invoke(restarted, "/api/catalog")[1]["entries"][0]["key"] == opened["catalog_key"]
    assert restarted.state.experiments._controller is None
    manifest = Path(row["output"]) / "run.json"
    changed = json.loads(manifest.read_bytes())
    changed["label"] = "Modified after handoff without changing captures"
    manifest.write_text(json.dumps(changed))
    code, error = invoke(
        app,
        "/api/catalog/open",
        parameters={"key": opened["catalog_key"], "run_sha256": saved.identity},
    )
    assert code == 409 and error["error_code"] == "run_changed"


def test_real_blackout_interruption_retains_requested_applied_and_observed_evidence(app):
    code, result = start(
        app, scenario="blackout", duration_s=4, blackout_at_s=0.1, blackout_duration_s=3
    )
    assert code == 200
    identifier = result["run_id"]
    output = Path(result["experiment"]["history"][0]["output"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        actions = output / "actions.jsonl"
        if actions.exists() and b'"action": "forwarding_disabled"' in actions.read_bytes():
            break
        time.sleep(0.02)
    else:
        pytest.fail("Worker never applied its configured forwarding interruption")
    code, stopped = invoke(app, "/api/experiment/stop", {"run_id": identifier})
    assert code == 200 and stopped["run_id"] == identifier
    request_clock = stopped["experiment"]["history"][0]["stop_requested"]
    assert request_clock is not None
    assert (
        invoke(app, "/api/experiment/stop", {"run_id": identifier})[1]["experiment"]["history"][0][
            "stop_requested"
        ]
        == request_clock
    )
    row = terminal(app, identifier)
    assert row["declared_outcome"] == "interrupted" and row["state"] == "finished"
    assert row["stop_reason"] == "user" and not row["forced_termination"]
    saved = load_run_directory(output)
    assert saved.requested["blackout_duration_s"] == 3
    assert len(saved.gate_intervals) == 1
    assert 0 <= saved.gate_intervals[0].duration_ns < 3_000_000_000
    assert saved.captures["relay-input"].records and saved.captures["receiver"].records
    assert invoke(app, "/api/experiment/open", parameters={"run_id": identifier})[0] == 200


def test_concurrent_starts_share_one_controller_and_stale_stop_never_targets_latest(app):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(app, duration_s=3), range(2)))
    assert sorted(code for code, _ in results) == [200, 409]
    first = next(body["run_id"] for code, body in results if code == 200)
    assert len(list(app.state.experiments.root.iterdir())) == 1
    assert invoke(app, "/api/experiment/open", parameters={"run_id": first})[0] == 409
    assert invoke(app, "/api/experiment/stop", {"run_id": first})[0] == 200
    terminal(app, first)
    code, second = start(app, duration_s=3)
    assert code == 200
    second_id = second["run_id"]
    code, stale = invoke(app, "/api/experiment/stop", {"run_id": first})
    assert code == 200 and stale["run_id"] == first
    assert stale["experiment"]["active_run_id"] == second_id
    assert stale["experiment"]["history"][0]["stop_requested"] is None
    assert invoke(app, "/api/experiment/stop", {"run_id": "run-unknown"})[0] == 404
    assert status(app)["active_run_id"] == second_id


def test_real_completed_pair_handoffs_feed_existing_comparison_without_relaunch(app):
    parameters = {}
    for role in ("baseline", "blackout"):
        settings = {"scenario": role, "duration_s": 0.5}
        if role == "blackout":
            settings.update(blackout_at_s=0.1, blackout_duration_s=0.1)
        code, started = start(app, **settings)
        assert code == 200
        identifier = started["run_id"]
        assert terminal(app, identifier)["declared_outcome"] == "completed"
        code, opened = invoke(app, "/api/experiment/open", parameters={"run_id": identifier})
        assert code == 200
        parameters[f"{role}_key"] = opened["catalog_key"]
        parameters[f"{role}_sha256"] = opened["run"]["identity"]
    code, compared = invoke(
        app,
        "/api/comparison",
        raw=b"--comparison-boundary--\r\n",
        headers={"Content-Type": "multipart/form-data; boundary=comparison-boundary"},
        parameters=parameters,
    )
    assert code == 200 and compared["comparison"]["comparable"] is True
    assert compared["comparison"]["metrics"]["available"] is True
    assert compared["comparison"]["gates"]["blackout"]["total_count"] == 1
    assert len(status(app)["history"]) == 2 and status(app)["active_run_id"] is None


@pytest.mark.parametrize(
    "values", [{}, {"run_id": None}, {"run_id": "../x"}, {"run_id": "run-a", "all": True}]
)
def test_stop_cannot_infer_an_active_run_or_accept_arbitrary_paths(app, values):
    assert invoke(app, "/api/experiment/stop", values)[0] == 400
    assert app.state.experiments._controller is None


def test_terminal_handoff_rechecks_pinned_files_and_rejects_unfinalized_or_symlinked_evidence(
    app, tmp_path
):
    _, result = start(app)
    identifier = result["run_id"]
    row = terminal(app, identifier)
    output = Path(row["output"])
    manifest = output / "run.json"
    original = manifest.read_bytes()
    contents = json.loads(original)
    contents["outcome"] = "running"
    manifest.write_text(json.dumps(contents))
    code, body = invoke(app, "/api/experiment/open", parameters={"run_id": identifier})
    assert code == 409 and body["error_code"] == "run_unavailable"
    manifest.write_bytes(original)
    replacement = tmp_path / "elsewhere"
    output.rename(replacement)
    output.symlink_to(replacement, target_is_directory=True)
    assert invoke(app, "/api/experiment/open", parameters={"run_id": identifier})[0] == 409


def test_lifespan_shutdown_closes_owned_worker_but_idle_shutdown_creates_nothing(tmp_path):
    async def run():
        idle = create_app(experiment_root=tmp_path / "idle")
        async with idle.router.lifespan_context(idle):
            assert idle.state.experiments._controller is None
        assert not (tmp_path / "idle").exists()
        app = create_app(experiment_root=tmp_path / "active")
        async with app.router.lifespan_context(app):
            result = await asyncio.to_thread(
                app.state.experiments.start, {**BASELINE, "duration_s": 4}
            )
            identifier = result["run_id"]
            await asyncio.sleep(0.2)
        row = app.state.experiments.status()["experiment"]["history"][0]
        assert row["run_id"] == identifier and not row["active"]
        assert row["stop_reason"] == "controller_shutdown"
        assert row["declared_outcome"] == "interrupted"
        assert app.state.experiments._controller._workers[identifier].process.poll() is not None

    asyncio.run(run())


def test_snapshot_json_preserves_large_control_clocks_and_diagnostics(app):
    from uav_debugger.experiment_control import ControlClock, RunSnapshot

    root = app.state.experiments.root
    stamp = 2**63 + 51
    snapshot = RunSnapshot(
        "run-literal_name",
        root / "run-literal_name/evidence",
        root / "run-literal_name/control.json",
        BASELINE,
        None,
        "failed",
        ControlClock(stamp, 2**53 + 19),
        finished=ControlClock(stamp + 11, 2**53 + 17),
        declared_outcome="failed",
        diagnostics="x" * 8192,
        error="Retained worker failure",
        forced_termination=True,
    )
    snapshots = tuple(
        replace(snapshot, run_id=f"run-{index}", output=root / f"run-{index}/evidence")
        for index in range(20)
    )
    app.state.experiments._controller = SimpleNamespace(
        history=lambda: snapshots, output_root=root, close=lambda: None
    )
    rows = status(app)["history"]
    assert len(rows) == 20 and rows[0]["elapsed_ns"] == "11"
    assert rows[0]["created"]["monotonic_ns"] == str(stamp)
    assert rows[0]["finished"]["unix_us"] == str(2**53 + 17)
    assert len(rows[0]["diagnostics"]) == 8192 and rows[0]["forced_termination"] is True


def test_failed_process_launch_remains_distinct_from_unavailable_saved_evidence(app, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Deliberate worker launch failure")

    monkeypatch.setattr("uav_debugger.experiment_control.subprocess.Popen", fail)
    code, result = start(app)
    assert code == 200
    row = result["experiment"]["history"][0]
    assert row["state"] == "failed" and row["declared_outcome"] is None
    assert row["evidence_status"] == "not_validated"
    assert row["finished"] is not None and "Deliberate" in row["error"]
    assert app.state.experiments._controller.history_limit == 20
    assert invoke(app, "/api/experiment/open", parameters={"run_id": row["run_id"]})[0] == 409


@pytest.mark.parametrize("nested", [False, True])
def test_start_refuses_symlink_root_or_ancestor_without_writing_outside_catalog(tmp_path, nested):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    app = create_app(experiment_root=link / "new-root" if nested else link)
    assert status(app)["history"] == []
    assert start(app)[0] == 400
    assert app.state.experiments._controller is None
    assert list(target.iterdir()) == []


def test_abrupt_application_exit_preserves_worker_owner_pipe_cleanup(tmp_path):
    program = r"""
import os, pathlib, sys, time
from uav_debugger.instrument import create_app
app=create_app(experiment_root=pathlib.Path(sys.argv[1]))
response=app.state.experiments.start({'source':'synthetic','scenario':'baseline','duration_s':10})
output=pathlib.Path(response['experiment']['history'][0]['output'])
deadline=time.monotonic()+5
while time.monotonic()<deadline:
    actions=output/'actions.jsonl'
    if actions.exists() and '"producer_started"' in actions.read_text():
        print(str(output),flush=True)
        os._exit(0)
    time.sleep(0.01)
raise RuntimeError('Worker did not produce start evidence')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "runs")],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    output = Path(result.stdout.strip())
    manifest = output / "run.json"
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if json.loads(manifest.read_bytes())["outcome"] == "interrupted":
            break
        time.sleep(0.025)
    saved = load_run_directory(output)
    assert saved.declared_outcome == "interrupted" and saved.evidence_status == "consistent"
    assert json.loads((output.parent / "control.json").read_bytes())["state"] == "running"


def test_server_sitl_configuration_is_inert_until_start_and_cannot_be_overridden(
    app, tmp_path, monkeypatch
):
    missing = tmp_path / "missing-binary"
    native = create_app(experiment_root=tmp_path / "native", sitl_binary=missing)
    try:
        config = status(native)["configuration"]
        assert config["sitl_configured"] is True and config["sitl_binary"] == str(missing)
        assert native.state.experiments._controller is None
        assert start(native, source="arducopter-sitl")[0] == 400
        assert not (tmp_path / "native").exists()
        assert start(native, sitl_binary="/client/path")[0] == 400
    finally:
        native.state.experiments.close()
    captured = []
    monkeypatch.setattr(
        "uav_debugger.instrument.uvicorn.run", lambda app, **kwargs: captured.append(app)
    )
    assert main(["--sitl-binary", str(missing), "--experiment-root", str(tmp_path / "cli")]) == 0
    assert captured[0].state.experiments.sitl_binary == missing
    assert captured[0].state.experiments._controller is None
    assert not (tmp_path / "cli").exists()


def test_http_routes_keep_origin_host_method_and_action_token_guards(run_server):
    url, root = run_server
    code, headers, body = _request(url, "/api/experiment/status")
    assert code == 200 and headers["Cache-Control"] == "no-store"
    token = json.loads(body)["experiment"]["action_token"]
    for path in ("/api/experiment/start", "/api/experiment/stop"):
        assert _request(url, path)[0] == 405
        assert _request(url, path, method="OPTIONS")[0] == 405
        assert (
            _request(
                url,
                path,
                method="POST",
                headers={"Content-Type": "application/json"},
                data=json.dumps(BASELINE).encode(),
            )[0]
            == 403
        )
    assert (
        _request(url, "/api/experiment/status", headers={"Origin": "https://foreign.invalid"})[0]
        == 403
    )
    assert _request(url, "/api/experiment/status", headers={"Host": "foreign.invalid"})[0] == 400
    code, _, body = _request(
        url,
        "/api/experiment/start",
        method="POST",
        headers={"Content-Type": "application/json", ACTION_HEADER: token},
        data=json.dumps(BASELINE).encode(),
    )
    assert code == 200 and json.loads(body)["run_id"].startswith("run-")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        payload = json.loads(_request(url, "/api/experiment/status")[2])["experiment"]
        if payload["active_run_id"] is None:
            break
        time.sleep(0.05)
    assert payload["history"][0]["declared_outcome"] == "completed"


def test_read_only_routes_and_idle_lifespan_do_not_import_execution_or_create_root(tmp_path):
    script = r"""
import asyncio, importlib.abc, inspect, json, sys
from pathlib import Path
from starlette.requests import Request
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'uav_debugger.experiment','uav_debugger.experiment_control',
                        'uav_debugger.experiment_worker','uav_debugger.sitl','streamlit'}:
            raise AssertionError('Read-only route imported execution: '+fullname)
sys.meta_path.insert(0,Guard())
from uav_debugger.instrument import create_app
root=Path(sys.argv[1]); app=create_app(experiment_root=root)
async def check():
    async with app.router.lifespan_context(app):
        for path in ('/api/config','/api/example','/api/catalog','/api/experiment/status'):
            request=Request({'type':'http','method':'GET','scheme':'http','server':('127.0.0.1',8765),
                'path':path,'root_path':'','query_string':b'','headers':[(b'host',b'127.0.0.1:8765')]})
            route=next(route for route in app.routes if route.path==path)
            response=route.endpoint(request)
            if inspect.isawaitable(response): response=await response
            assert response.status_code==200
        assert app.state.experiments._controller is None
    assert not root.exists()
asyncio.run(check())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "absent")],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr.decode()
