"""Datagram boundaries, simulator readiness and ownership on the local test path."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from uav_debugger import experiment, import_file
from uav_debugger.experiment import ExperimentConfig, run_experiment

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "telemetry-gap.expected.json").read_text()
)
HEARTBEAT_V1 = bytes.fromhex(FIXTURE["records"][0]["frame_hex"])
HEARTBEAT_V2 = bytes.fromhex(FIXTURE["records"][1]["frame_hex"])
ATTITUDE_V2 = bytes.fromhex(FIXTURE["records"][2]["frame_hex"])
ATTITUDE_V1 = bytes.fromhex(FIXTURE["records"][3]["frame_hex"])
# The payload deliberately includes both frame markers. The unknown definition
# remains opaque with an unverified checksum under the existing import profile.
OPAQUE_V2 = bytes.fromhex("fd030000ff0101ffffff fefd01 0000")


def test_datagram_split_preserves_mixed_versions_and_exact_byte_offsets():
    frames = [HEARTBEAT_V1, ATTITUDE_V2, HEARTBEAT_V2, ATTITUDE_V1]
    datagram = b"".join(frames)
    actual = experiment._split_datagram(datagram)

    expected = []
    offset = 0
    for frame in frames:
        expected.append((offset, frame))
        offset += len(frame)
    assert actual == expected
    assert b"".join(frame for _, frame in actual) == datagram


def test_datagram_split_retains_unknown_frame_without_scanning_payload_markers():
    datagram = HEARTBEAT_V1 + OPAQUE_V2 + ATTITUDE_V2
    assert experiment._split_datagram(datagram) == [
        (0, HEARTBEAT_V1),
        (len(HEARTBEAT_V1), OPAQUE_V2),
        (len(HEARTBEAT_V1) + len(OPAQUE_V2), ATTITUDE_V2),
    ]


@pytest.mark.parametrize(
    "datagram",
    [
        b"",
        b"noise" + HEARTBEAT_V1,
        HEARTBEAT_V1 + b"noise" + ATTITUDE_V2,
        HEARTBEAT_V1 + b"\xfd",
        HEARTBEAT_V1 + ATTITUDE_V2[:8],
        HEARTBEAT_V1 + ATTITUDE_V2[:-1],
        OPAQUE_V2[:-1],
        HEARTBEAT_V1 + ATTITUDE_V2 + b"\x00",
        HEARTBEAT_V1[:-1] + bytes([HEARTBEAT_V1[-1] ^ 1]),
        ATTITUDE_V2[:-1] + bytes([ATTITUDE_V2[-1] ^ 1]),
        HEARTBEAT_V1 + ATTITUDE_V2[:-1] + bytes([ATTITUDE_V2[-1] ^ 1]) + HEARTBEAT_V2,
    ],
    ids=[
        "empty",
        "leading-noise",
        "interleaved-noise",
        "incomplete-marker",
        "incomplete-header",
        "incomplete-checksum",
        "incomplete-opaque",
        "trailing-noise",
        "v1-invalid-crc",
        "v2-invalid-crc",
        "corrupt-between-valid-frames",
    ],
)
def test_datagram_split_rejects_damage_without_resynchronizing(datagram):
    with pytest.raises(ValueError):
        experiment._split_datagram(datagram)


@pytest.mark.parametrize("flags", [1, 2, 3, 128])
def test_datagram_split_rejects_signed_or_unsupported_frames(flags):
    frame = bytearray(ATTITUDE_V2)
    frame[2] = flags
    if flags & 1:
        frame.extend(bytes(13))

    with pytest.raises(ValueError):
        experiment._split_datagram(HEARTBEAT_V1 + bytes(frame))


def test_datagram_split_does_not_reassemble_across_udp_messages():
    cut = len(ATTITUDE_V2) // 2
    for fragment in (ATTITUDE_V2[:cut], ATTITUDE_V2[cut:]):
        with pytest.raises(ValueError):
            experiment._split_datagram(fragment)

    assert experiment._split_datagram(ATTITUDE_V2) == [(0, ATTITUDE_V2)]


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def fake_simulator(tmp_path, monkeypatch):
    """Run a local UDP test child, never an autopilot, under a test-only hash pin."""
    from uav_debugger import sitl

    children = []
    original_popen = subprocess.Popen

    def track_child(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        command = args[0] if args else kwargs["args"]
        if command[0] == str(tmp_path / "fake-sitl"):
            children.append(child)
        return child

    monkeypatch.setattr(sitl.subprocess, "Popen", track_child)
    # The fake child below can only send loopback UDP. Real SITL tests retain
    # this guard because the pinned binary has other network backends.
    monkeypatch.setattr(sitl, "require_loopback_only", lambda: None)

    def make(mode="stream"):
        path = tmp_path / "fake-sitl"
        path.write_text(
            f"""#!{sys.executable}
import signal
import socket
import sys
import time

MODE = {mode!r}
HEARTBEAT = {HEARTBEAT_V1!r}
ATTITUDE = {ATTITUDE_V2!r}
WRONG_SOURCE = {HEARTBEAT_V2 + ATTITUDE_V1!r}
OPAQUE = {OPAQUE_V2!r}
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
if MODE == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if MODE == "exit":
    sys.exit(23)
destination = next(value for value in sys.argv if value.startswith("udpclient:"))
protocol, host, port = destination.split(":")
assert protocol == "udpclient" and host == "127.0.0.1"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("127.0.0.1", 0))
sock.connect((host, int(port)))
if MODE in ("preambles", "four-preambles"):
    for _ in range(3 if MODE == "preambles" else 4):
        sock.send(b"0 ")
        time.sleep(0.01)
if MODE in ("stream", "preambles", "ignore-term"):
    sock.send(HEARTBEAT)
    time.sleep(0.15)
if MODE == "late-preamble":
    sock.send(HEARTBEAT)
    time.sleep(0.02)
    sock.send(b"0 ")
while True:
    if MODE in ("stream", "preambles", "ignore-term"):
        sock.send(HEARTBEAT + ATTITUDE + OPAQUE)
    elif MODE == "wrong-source":
        sock.send(WRONG_SOURCE)
    elif MODE == "heartbeat-only":
        sock.send(HEARTBEAT)
    elif MODE == "malformed":
        sock.send(HEARTBEAT + ATTITUDE[:-1])
    time.sleep(0.03)
""",
            encoding="utf-8",
        )
        path.chmod(0o700)
        monkeypatch.setattr(sitl, "BINARY_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
        return path

    yield make, children

    # An assertion failure must not leave the test's own child running.
    for child in children:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_sitl_readiness_keeps_warmup_and_preserves_datagram_and_capture_references(
    tmp_path, fake_simulator
):
    make, children = fake_simulator
    output = tmp_path / "run"
    result = run_experiment(
        output, ExperimentConfig(sitl_binary=make(), duration_s=0.25, startup_timeout_s=2)
    )

    assert result["outcome"] == "completed", result["error"]
    assert result["schema"] == "uav-debugger-experiment-v2"
    assert len(children) == 1 and children[0].poll() is not None
    measurement = result["measurement_start"]
    assert measurement["monotonic_ns"] > result["start"]["monotonic_ns"]
    assert result["end"]["monotonic_ns"] - measurement["monotonic_ns"] >= 250_000_000
    before = import_file(output / "relay-input.tlog")
    after = import_file(output / "receiver.tlog")
    assert before.traversal == after.traversal == "complete"
    assert before.opaque_count == after.opaque_count > 0
    assert [record.raw_frame for record in before.records] == [
        record.raw_frame for record in after.records
    ]
    datagrams = read_lines(output / "datagrams.jsonl")
    observations = read_lines(output / "observations.jsonl")
    assert any(item["monotonic_ns"] < measurement["monotonic_ns"] for item in observations)
    assert any(item["monotonic_ns"] > measurement["monotonic_ns"] for item in observations)
    for point, captured in (("relay-input", before), ("receiver", after)):
        at_point = [item for item in datagrams if item["point"] == point]
        assert [item["datagram_index"] for item in at_point] == list(range(len(at_point)))
        for observed in (item for item in observations if item["point"] == point):
            raw_datagram = at_point[observed["datagram_index"]]
            raw = bytes.fromhex(raw_datagram["raw_hex"])
            record = captured.records[observed["record_index"]]
            offset = observed["datagram_offset"]
            assert raw[offset : offset + len(record.raw_frame)] == record.raw_frame
            assert raw_datagram["unix_us"] == observed["unix_us"] == record.timestamp_us
            assert raw_datagram["monotonic_ns"] == observed["monotonic_ns"]
        assert any(
            len(experiment._split_datagram(bytes.fromhex(item["raw_hex"]))) == 3
            for item in at_point
        )
    assert result["counters"]["sender_submitted"] is None
    for name, expected in result["artifacts"].items():
        raw = (output / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"]
        assert len(raw) == expected["size_bytes"]


@pytest.mark.parametrize("mode", ["silent", "heartbeat-only", "wrong-source"])
def test_sitl_startup_timeout_requires_both_messages_from_source_one(
    tmp_path, fake_simulator, mode
):
    make, children = fake_simulator
    output = tmp_path / "timeout"
    started = time.monotonic()
    result = run_experiment(
        output,
        ExperimentConfig(sitl_binary=make(mode), duration_s=0.1, startup_timeout_s=0.25),
    )

    assert time.monotonic() - started < 5
    assert result["outcome"] == "failed"
    assert "readiness timeout" in result["error"]
    assert result["measurement_start"] is None
    assert len(children) == 1 and children[0].poll() is not None
    before = import_file(output / "relay-input.tlog")
    assert before.traversal == ("empty" if mode == "silent" else "complete")
    assert json.loads((output / "run.json").read_text())["outcome"] == "failed"


def test_sitl_early_exit_fails_and_records_exit_instead_of_timing_out(tmp_path, fake_simulator):
    make, children = fake_simulator
    result = run_experiment(
        tmp_path / "early-exit",
        ExperimentConfig(sitl_binary=make("exit"), duration_s=0.1, startup_timeout_s=5),
    )

    assert result["outcome"] == "failed"
    assert "Simulator exited" in result["error"]
    assert "23" in result["error"]
    assert children[0].poll() == 23
    assert result["measurement_start"] is None


def test_sitl_rejected_datagram_is_retained_without_partial_frame_capture(tmp_path, fake_simulator):
    make, children = fake_simulator
    output = tmp_path / "malformed"
    result = run_experiment(
        output,
        ExperimentConfig(sitl_binary=make("malformed"), duration_s=0.1, startup_timeout_s=2),
    )

    assert result["outcome"] == "failed"
    assert children[0].poll() is not None
    datagrams = read_lines(output / "datagrams.jsonl")
    assert len(datagrams) == 1
    assert datagrams[0]["point"] == "relay-input"
    assert bytes.fromhex(datagrams[0]["raw_hex"]) == HEARTBEAT_V1 + ATTITUDE_V2[:-1]
    assert import_file(output / "relay-input.tlog").traversal == "empty"
    assert import_file(output / "receiver.tlog").traversal == "empty"
    assert read_lines(output / "observations.jsonl") == []


def test_sitl_stop_during_startup_closes_owned_child_only(tmp_path, fake_simulator):
    make, children = fake_simulator
    # This child is visible to the test but is never passed to the runner.
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]) as unrelated:
        stop = threading.Event()
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        try:
            result = run_experiment(
                tmp_path / "interrupted",
                ExperimentConfig(sitl_binary=make("silent"), startup_timeout_s=5),
                stop=stop,
            )
            assert result["outcome"] == "interrupted"
            assert result["measurement_start"] is None
            assert len(children) == 1 and children[0].poll() is not None
            assert unrelated.poll() is None
        finally:
            timer.cancel()
            timer.join(timeout=1)
            unrelated.terminate()
            unrelated.wait(timeout=5)


@pytest.mark.parametrize("mode", ["preambles", "four-preambles"])
def test_sitl_startup_preambles_are_bounded_and_retained(tmp_path, fake_simulator, mode):
    make, children = fake_simulator
    output = tmp_path / mode
    result = run_experiment(
        output,
        ExperimentConfig(sitl_binary=make(mode), duration_s=0.1, startup_timeout_s=2),
    )

    assert children[0].poll() is not None
    datagrams = read_lines(output / "datagrams.jsonl")
    relay_datagrams = [item for item in datagrams if item["point"] == "relay-input"]
    preambles = [item for item in relay_datagrams if bytes.fromhex(item["raw_hex"]) == b"0 "]
    assert len(preambles) == (3 if mode == "preambles" else 4)
    if mode == "preambles":
        assert result["outcome"] == "completed", result["error"]
        captured = import_file(output / "relay-input.tlog")
        assert captured.traversal == "complete"
        assert all(record.raw_frame != b"0 " for record in captured.records)
        actions = read_lines(output / "actions.jsonl")
        assert any(item["action"] == "sitl_startup_preamble" for item in actions)
    else:
        assert result["outcome"] == "failed"
        assert result["measurement_start"] is None
        assert import_file(output / "relay-input.tlog").traversal == "empty"


def test_sitl_startup_preamble_is_rejected_after_first_frame(tmp_path, fake_simulator):
    make, children = fake_simulator
    output = tmp_path / "late-preamble"
    result = run_experiment(
        output,
        ExperimentConfig(sitl_binary=make("late-preamble"), duration_s=0.1, startup_timeout_s=2),
    )

    assert result["outcome"] == "failed"
    assert children[0].poll() is not None
    relay_datagrams = [
        item for item in read_lines(output / "datagrams.jsonl") if item["point"] == "relay-input"
    ]
    assert [bytes.fromhex(item["raw_hex"]) for item in relay_datagrams] == [HEARTBEAT_V1, b"0 "]
    captured = import_file(output / "relay-input.tlog")
    assert captured.traversal == "complete"
    assert [record.raw_frame for record in captured.records] == [HEARTBEAT_V1]


def test_sitl_blackout_starts_after_readiness_and_drops_whole_datagrams(tmp_path, fake_simulator):
    make, children = fake_simulator
    output = tmp_path / "blackout"
    result = run_experiment(
        output,
        ExperimentConfig(
            scenario="blackout",
            duration_s=2.4,
            blackout_at_s=0.2,
            sitl_binary=make(),
            startup_timeout_s=2,
        ),
    )

    assert result["outcome"] == "completed", result["error"]
    assert children[0].poll() is not None
    actions = read_lines(output / "actions.jsonl")
    disabled = next(item for item in actions if item["action"] == "forwarding_disabled")
    enabled = next(item for item in actions if item["action"] == "forwarding_enabled")
    measurement = result["measurement_start"]["monotonic_ns"]
    assert disabled["monotonic_ns"] > measurement
    assert disabled["measurement_elapsed_ns"] >= 200_000_000
    assert enabled["monotonic_ns"] - disabled["monotonic_ns"] >= 2_000_000_000
    observations = {
        item["record_index"]: item
        for item in read_lines(output / "observations.jsonl")
        if item["point"] == "relay-input"
    }
    decisions = {}
    frame_actions = [
        item for item in actions if item["action"] in {"relay_dropped", "relay_forwarded"}
    ]
    for item in frame_actions:
        datagram_index = observations[item["record_index"]]["datagram_index"]
        decisions.setdefault(datagram_index, set()).add(item["action"])
    assert all(len(choices) == 1 for choices in decisions.values())
    assert {frozenset(choices) for choices in decisions.values()} == {
        frozenset({"relay_dropped"}),
        frozenset({"relay_forwarded"}),
    }
    before = import_file(output / "relay-input.tlog")
    after = import_file(output / "receiver.tlog")
    assert sorted(item["record_index"] for item in frame_actions) == list(
        range(len(before.records))
    )
    assert [record.raw_frame for record in after.records] == [
        before.records[item["record_index"]].raw_frame
        for item in frame_actions
        if item["action"] == "relay_forwarded"
    ]
    forwards = [item for item in frame_actions if item["action"] == "relay_forwarded"]
    assert any(item["monotonic_ns"] < disabled["monotonic_ns"] for item in forwards)
    assert any(item["monotonic_ns"] >= enabled["monotonic_ns"] for item in forwards)


@pytest.mark.parametrize("timeout", [0, 0.099, 60.01, float("nan"), float("inf")])
def test_sitl_invalid_startup_timeout_creates_no_run(tmp_path, timeout):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run_experiment(output, ExperimentConfig(startup_timeout_s=timeout))
    assert not output.exists()


def test_sitl_unknown_binary_is_rejected_before_execution_or_output(tmp_path):
    path = tmp_path / "unverified"
    sentinel = tmp_path / "executed"
    path.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    )
    path.chmod(0o700)
    output = tmp_path / "run"

    with pytest.raises(ValueError):
        run_experiment(output, ExperimentConfig(sitl_binary=path))

    assert not sentinel.exists()
    assert not output.exists()


def test_sitl_guard_rejects_non_loopback_interfaces(monkeypatch):
    from uav_debugger import sitl

    monkeypatch.setattr(sitl.socket, "if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    with pytest.raises(RuntimeError, match="only loopback"):
        sitl.require_loopback_only()


def test_sitl_guard_requires_loopback_to_be_enabled(monkeypatch):
    import struct

    from uav_debugger import sitl

    monkeypatch.setattr(sitl.socket, "if_nameindex", lambda: [(1, "lo")])
    monkeypatch.setattr(sitl.fcntl, "ioctl", lambda *_: struct.pack("16sH14s", b"lo", 8, b""))
    with pytest.raises(RuntimeError, match="only loopback"):
        sitl.require_loopback_only()


def test_sitl_readiness_cannot_start_measurement_after_deadline():
    import io

    from uav_debugger.experiment import _Runner

    runner = _Runner({"counters": {}}, threading.Event())
    runner.actions = io.StringIO()
    runner.origin_ns = time.monotonic_ns() - 200_000_000
    runner.readiness_messages = {"HEARTBEAT", "ATTITUDE"}
    with pytest.raises(RuntimeError, match="readiness timeout"):
        runner.loop(ExperimentConfig(sitl_binary=Path("unused"), startup_timeout_s=0.1), {}, None)
    assert runner.measurement_ns is None


def test_sitl_stop_escalates_and_reaps_an_unresponsive_owned_child(tmp_path, fake_simulator):
    make, children = fake_simulator
    # Telemetry readiness proves the child's ignore handler is installed before
    # normal duration expiry requests termination; no timer guesses startup time.
    result = run_experiment(
        tmp_path / "unresponsive",
        ExperimentConfig(sitl_binary=make("ignore-term"), duration_s=0.1, startup_timeout_s=2),
    )
    assert result["outcome"] == "completed"
    assert result["simulator"]["shutdown"] == {
        "returncode": -signal.SIGKILL,
        "escalated_to_kill": True,
    }
    assert len(children) == 1 and children[0].poll() == -signal.SIGKILL


@pytest.fixture(scope="module", params=["baseline", "blackout"])
def real_sitl_run(tmp_path_factory, request):
    if not os.environ.get("UAV_DEBUGGER_SITL_BINARY"):
        pytest.skip("Set UAV_DEBUGGER_SITL_BINARY inside a loopback-only network namespace")
    scenario = request.param
    output = tmp_path_factory.mktemp("real-sitl") / scenario
    result = run_experiment(
        output,
        ExperimentConfig(
            scenario=scenario,
            duration_s=6,
            sitl_binary=Path(os.environ["UAV_DEBUGGER_SITL_BINARY"]),
        ),
    )
    return output, result


def test_opt_in_pinned_sitl_produces_analyzable_captures(real_sitl_run):
    output, result = real_sitl_run

    assert result["outcome"] == "completed", result["error"]
    assert result["measurement_start"] is not None
    for point in ("relay-input", "receiver"):
        captured = import_file(output / f"{point}.tlog")
        assert captured.traversal == "complete"
        assert {"HEARTBEAT", "ATTITUDE"} <= {
            record.message_name
            for record in captured.records
            if (record.system_id, record.component_id) == (1, 1)
        }
        assert captured.opaque_count > 0  # The profile retains ArduPilot-specific messages.
        assert all(
            record.fields["base_mode"] & 128 == 0
            for record in captured.records
            if record.message_name == "HEARTBEAT"
        )
    before = import_file(output / "relay-input.tlog")
    after = import_file(output / "receiver.tlog")
    actions = read_lines(output / "actions.jsonl")
    forwarded = [item["record_index"] for item in actions if item["action"] == "relay_forwarded"]
    assert [record.raw_frame for record in after.records] == [
        before.records[index].raw_frame for index in forwarded
    ]
    assert result["simulator"]["pid"] > 0
    assert result["simulator"]["shutdown"]["returncode"] is not None
    if result["requested"]["scenario"] == "blackout":
        assert result["counters"]["relay_dropped"] > 0
        disabled = next(item for item in actions if item["action"] == "forwarding_disabled")
        enabled = next(item for item in actions if item["action"] == "forwarding_enabled")
        assert disabled["measurement_elapsed_ns"] >= 2_000_000_000
        assert enabled["monotonic_ns"] - disabled["monotonic_ns"] >= 2_000_000_000
    else:
        assert result["counters"]["relay_dropped"] == 0


@pytest.mark.browser
def test_opt_in_sitl_captures_open_independently_and_export_evidence(
    real_sitl_run, analyze_page, tmp_path
):
    from test_analyze_browser import (
        apply_changed_filters,
        attitude_plot,
        choose,
        download_blocks,
        metric,
        upload,
    )

    output, result = real_sitl_run
    assert result["outcome"] == "completed", result["error"]
    page, expect = analyze_page
    for point in ("relay-input", "receiver"):
        capture = output / f"{point}.tlog"
        original = capture.read_bytes()
        imported = import_file(capture)
        selected = [
            record
            for record in imported.records
            if (record.system_id, record.component_id, record.message_id) == (1, 1, 30)
        ]
        assert len(selected) > 1
        upload(page, original, name=capture.name)
        expect(page.locator("code").filter(has_text=imported.sha256)).to_have_count(1)
        expect(metric(page, "Imported records")).to_have_text(str(len(imported.records)))
        choose(page, "Source", "1 / 1")
        choose(page, "Message type", "ATTITUDE")
        apply_changed_filters(page)
        expect(metric(page, "Selected records")).to_have_text(str(len(selected)))
        expect(attitude_plot(page)).to_be_visible()
        inspected = selected[1]
        choose(page, "Record", f"#{inspected.index} · ATTITUDE · 1 / 1")
        expect(
            page.get_by_role("heading", name=f"Record #{inspected.index}", exact=True)
        ).to_be_visible()
        report = tmp_path / f"{result['requested']['scenario']}-{point}-report.md"
        provenance, selection, coverage, _issues, detail = download_blocks(page, report)
        assert provenance["sha256"] == imported.sha256
        assert provenance["source_name"] == capture.name
        assert selection["sources"] == [[1, 1]]
        assert selection["message_ids"] == [30]
        assert coverage["filtered_record_count"] == len(selected)
        assert detail["index"] == inspected.index
        assert detail["raw_frame_hex"] == inspected.raw_frame.hex()
        assert detail["fields"] == dict(inspected.fields)
        assert capture.read_bytes() == original


@pytest.mark.browser
def test_opt_in_saved_sitl_directory_preserves_startup_and_capture_evidence(
    real_sitl_run, analyze_page, tmp_path
):
    from test_saved_run_browser import (
        apply_changed_filters,
        await_saved_capture,
        capture_report_blocks,
        choose,
        download_blocks,
        metric,
        run_report_block,
        timeline_data,
        upload_directory,
    )

    output, result = real_sitl_run
    assert result["outcome"] == "completed", result["error"]
    original = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    page, expect = analyze_page
    upload_directory(page, output)
    before = await_saved_capture(page, expect, output)
    # Native working files are uploaded without extension errors, then explicitly
    # excluded from the supported evidence set by the offline reader adapter.
    expect(page.locator('[data-testid="stFileChip"][aria-invalid="true"]')).to_have_count(0)
    ignored = [name for name in original if name.endswith((".bin", ".DAT"))]
    assert ignored
    excluded_files = page.get_by_text(
        f"Other files excluded from analysis ({len(ignored)})", exact=True
    )
    expect(excluded_files).to_be_visible()
    excluded_files.click()
    for name in ignored:
        expect(page.get_by_text(name, exact=True)).to_be_visible()
    expect(metric(page, "Evidence status")).to_have_text("consistent")
    choose(page, "Observation point", "Receiver")
    after = await_saved_capture(page, expect, output, "receiver")
    choose(page, "Source", "1 / 1")
    choose(page, "Message type", "ATTITUDE")
    apply_changed_filters(page)
    selected = [
        record
        for record in after.records
        if (record.system_id, record.component_id, record.message_id) == (1, 1, 30)
    ]
    expect(metric(page, "Selected records")).to_have_text(str(len(selected)))
    inspected = selected[1]
    choose(page, "Record", f"#{inspected.index} · ATTITUDE · 1 / 1")
    report = tmp_path / f"{result['requested']['scenario']}-saved-sitl-report.md"
    blocks = download_blocks(page, report)
    provenance, selection, detail = capture_report_blocks(blocks, "receiver.tlog")
    assert provenance["sha256"] == after.sha256
    assert selection["sources"] == [[1, 1]]
    assert selection["message_ids"] == [30]
    assert detail["raw_frame_hex"] == inspected.raw_frame.hex()
    run_provenance = run_report_block(blocks, "run_provenance")
    assert run_provenance["schema"] == "uav-debugger-experiment-v2"
    assert run_provenance["selected_point"] == "receiver"
    assert "datagrams.jsonl" in run_provenance["files"]
    assert "simulator/profile.parm" in run_provenance["files"]
    assert not any(name.endswith((".bin", ".DAT")) for name in run_provenance["files"])
    clocks = run_report_block(blocks, "clocks")
    assert clocks["measurement_monotonic_ns"] == result["measurement_start"]["monotonic_ns"]
    assert clocks["origin_monotonic_ns"] == result["origin_monotonic_ns"]
    assert clocks["startup_duration_ns"] > 0
    evidence = run_report_block(blocks, "observed_evidence")
    for point in evidence["points"]:
        assert point["datagram_trace_present"] is True
        assert point["valid_datagram_count"] > 0
        assert point["capture"]["opaque_count"] > 0
        assert point["references"]
        assert all("datagram_index" in reference for reference in point["references"])
        assert all("datagram_offset" in reference for reference in point["references"])
    timeline = timeline_data(page)
    assert {trace["name"]: sum(trace["y"]) for trace in timeline["traces"]} == {
        "Relay input": len(before.records),
        "Receiver": len(after.records),
    }
    assert any(
        shape["type"] == "rect"
        and shape["x0"] == 0
        and shape["x1"] == clocks["startup_duration_ns"] / 1e9
        for shape in timeline["shapes"]
    )
    assert {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    } == original
