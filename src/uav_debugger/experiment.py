"""Bounded synthetic or pinned ArduCopter SITL experiments on local loopback."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import selectors
import signal
import socket
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import BinaryIO, TextIO

from pymavlink.dialects.v20 import common

from .importer import MAX_INPUT_BYTES, import_bytes

RATE_HZ = 20
PERIOD_NS = 1_000_000_000 // RATE_HZ
BLACKOUT_NS = 2_000_000_000
DRAIN_NS = 200_000_000


@dataclass(frozen=True)
class ExperimentConfig:
    scenario: str = "baseline"
    duration_s: float = 6.0
    blackout_at_s: float | None = None
    sitl_binary: Path | None = None
    startup_timeout_s: float = 15.0

    def validate(self) -> None:
        if not math.isfinite(self.startup_timeout_s) or not 0.1 <= self.startup_timeout_s <= 60:
            raise ValueError("startup-timeout must be finite and between 0.1 and 60 seconds")
        if self.scenario not in {"baseline", "blackout"}:
            raise ValueError("scenario must be baseline or blackout")
        if not math.isfinite(self.duration_s) or not 0.1 <= self.duration_s <= 60:
            raise ValueError("duration must be finite and between 0.1 and 60 seconds")
        if self.scenario == "baseline" and self.blackout_at_s is not None:
            raise ValueError("blackout-at is only valid for the blackout scenario")
        if self.scenario == "blackout":
            at = self.blackout_at
            # Compare integer scheduling values, including decimal boundary inputs.
            if (
                not math.isfinite(at)
                or at < 0.1
                or round(at * 1e9) + BLACKOUT_NS + 100_000_000 > round(self.duration_s * 1e9)
            ):
                raise ValueError(
                    "blackout requires at least 0.1 seconds before and after its 2s window"
                )

    @property
    def blackout_at(self) -> float:
        return 2.0 if self.blackout_at_s is None else self.blackout_at_s

    def requested(self) -> dict:
        self.validate()
        return {
            "scenario": self.scenario,
            "duration_s": self.duration_s,
            "source": "arducopter-sitl" if self.sitl_binary else "synthetic",
            "startup_timeout_s": self.startup_timeout_s if self.sitl_binary else None,
            "rate_hz": None if self.sitl_binary else RATE_HZ,
            "blackout_at_s": self.blackout_at if self.scenario == "blackout" else None,
            "blackout_duration_s": 2.0 if self.scenario == "blackout" else None,
            "drain_timeout_s": DRAIN_NS / 1e9,
            "transport": "UDP IPv4 loopback only",
            "message": "autopilot telemetry" if self.sitl_binary else "ATTITUDE",
            "wire_version": "unsigned 1/2" if self.sitl_binary else 2,
            "dialect": "common",
            "system_id": 1,
            "component_id": 1,
        }


def _stamp(origin_ns: int) -> dict[str, int]:
    monotonic_ns = time.monotonic_ns()
    return {
        "monotonic_ns": monotonic_ns,
        "elapsed_ns": monotonic_ns - origin_ns,
        "unix_us": time.time_ns() // 1000,
    }


def _json_line(stream: TextIO, value: dict) -> None:
    stream.write(json.dumps(value, allow_nan=False) + "\n")
    stream.flush()


def _save_manifest(output: Path, manifest: dict) -> None:
    temporary = output / "run.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(output / "run.json")


def _split_datagram(data: bytes) -> list[tuple[int, bytes]]:
    """Split complete unsigned MAVLink frames; reject ambiguous or unsupported input.

    No cross-datagram reassembly or resynchronization is attempted. Original
    datagrams are retained separately before this validation runs.
    """
    frames = []
    offset = 0
    while offset < len(data):
        magic = data[offset]
        if magic not in (0xFE, 0xFD):
            raise ValueError(f"Invalid MAVLink marker at datagram byte {offset}")
        header_size = 6 if magic == 0xFE else 10
        if len(data) - offset < header_size:
            raise ValueError(f"Incomplete MAVLink header at datagram byte {offset}")
        if magic == 0xFD and data[offset + 2] != 0:
            raise ValueError("Signed frames and incompatibility flags are unsupported")
        end = offset + header_size + data[offset + 1] + 2
        if end > len(data):
            raise ValueError(f"Incomplete MAVLink frame at datagram byte {offset}")
        frames.append((offset, data[offset:end]))
        offset = end
    if not frames:
        raise ValueError("Empty MAVLink datagram")
    # Reuse the same checksum/definition boundary as Analyze without changing it.
    validation = import_bytes(b"".join(bytes(8) + frame for _, frame in frames))
    if validation.traversal != "complete":
        issue = next(issue for issue in validation.issues if issue.severity == "error")
        raise ValueError(f"Invalid MAVLink datagram: {issue.code}: {issue.message}")
    return frames


class _Capture:
    def __init__(self, point: str, stream: BinaryIO, observations: TextIO):
        self.point = point
        self.stream = stream
        self.observations = observations
        self.count = 0

    def write(self, frame: bytes, stamp: dict[str, int]) -> int:
        index = self.count
        offset = self.stream.tell()
        record = stamp["unix_us"].to_bytes(8, "big") + frame
        if offset + len(record) > MAX_INPUT_BYTES:
            raise ValueError("Capture reached the Analyze input size limit")
        if self.stream.write(record) != len(record):
            raise OSError("Incomplete capture write")
        self.count += 1
        _json_line(
            self.observations,
            {
                "point": self.point,
                "record_index": index,
                "offset": offset,
                "frame_size_bytes": len(frame),
                "frame_sha256": hashlib.sha256(frame).hexdigest(),
                **stamp,
            },
        )
        return index


class _Runner:
    def __init__(self, manifest: dict, stop: threading.Event):
        self.manifest = manifest
        self.stop = stop
        self.origin_ns = time.monotonic_ns()
        self.manifest["origin_monotonic_ns"] = self.origin_ns
        self.actions: TextIO
        self.captures: dict[str, _Capture] = {}
        self.gate_closed = False
        self.gate_started_ns: int | None = None
        self.producing = False
        self.simulator = None
        self.datagrams: TextIO | None = None
        self.datagram_counts = {"relay-input": 0, "receiver": 0}
        self.preamble_counts = {"relay-input": 0, "receiver": 0}
        self.readiness_messages: set[str] = set()
        self.measurement_ns: int | None = None

    def action(self, action: str, **details) -> dict:
        event = {"action": action, **_stamp(self.origin_ns), **details}
        if self.measurement_ns is not None:
            event["measurement_elapsed_ns"] = event["monotonic_ns"] - self.measurement_ns
        _json_line(self.actions, event)
        return event

    def open_gate(self, reason: str) -> None:
        if self.gate_closed:
            self.gate_closed = False
            self.action("forwarding_enabled", reason=reason)

    def update_gate(self, now: int, config: ExperimentConfig) -> None:
        if config.scenario != "blackout" or self.measurement_ns is None:
            return
        if self.gate_started_ns is None:
            if now - self.measurement_ns >= round(config.blackout_at * 1e9):
                self.gate_closed = True
                applied = self.action(
                    "forwarding_disabled",
                    requested_elapsed_ns=self.measurement_ns
                    - self.origin_ns
                    + round(config.blackout_at * 1e9),
                )
                self.gate_started_ns = applied["monotonic_ns"]
        elif self.gate_closed and now >= self.gate_started_ns + BLACKOUT_NS:
            self.open_gate("blackout_elapsed")

    def stop_producer(self, reason: str) -> None:
        if self.producing:
            self.producing = False
            if self.simulator is not None:
                stopped = self.simulator.stop()
                self.manifest["simulator"]["shutdown"] = stopped
                self.action("simulator_stopped", **stopped)
            self.action("producer_stopped", reason=reason)

    def execute(self, output: Path, config: ExperimentConfig) -> None:
        with ExitStack() as stack:
            self.actions = stack.enter_context(
                (output / "actions.jsonl").open("x", encoding="utf-8")
            )
            observations = stack.enter_context(
                (output / "observations.jsonl").open("x", encoding="utf-8")
            )
            for point in ("relay-input", "receiver"):
                stream = stack.enter_context((output / f"{point}.tlog").open("xb", buffering=0))
                self.captures[point] = _Capture(point, stream, observations)
            if config.sitl_binary is not None:
                self.datagrams = stack.enter_context(
                    (output / "datagrams.jsonl").open("x", encoding="utf-8")
                )
            endpoints = {}
            names = ("relay-input", "relay-output", "receiver")
            if config.sitl_binary is None:
                names = ("sender", *names)
            for name in names:
                sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                sock.bind(("127.0.0.1", 0))
                sock.setblocking(False)
                endpoints[name] = sock
            # Connected UDP limits receipt to the peer in this run. No address comes from input.
            pairs = [("relay-output", "receiver")]
            if config.sitl_binary is None:
                pairs.append(("sender", "relay-input"))
            for a, b in pairs:
                endpoints[a].connect(endpoints[b].getsockname())
                endpoints[b].connect(endpoints[a].getsockname())
            self.manifest["topology"] = {
                name: {"host": sock.getsockname()[0], "port": sock.getsockname()[1]}
                for name, sock in endpoints.items()
            }
            selector = stack.enter_context(selectors.DefaultSelector())
            for point in ("relay-input", "receiver"):
                selector.register(endpoints[point], selectors.EVENT_READ, point)
            self.origin_ns = time.monotonic_ns()
            self.manifest["origin_monotonic_ns"] = self.origin_ns
            self.manifest["start"] = _stamp(self.origin_ns)
            _save_manifest(output, self.manifest)
            try:
                if config.sitl_binary is not None:
                    from .sitl import SITLProcess

                    self.simulator = SITLProcess(
                        config.sitl_binary, output, endpoints["relay-input"].getsockname()[1]
                    )
                    self.manifest["simulator"].update(self.simulator.metadata)
                    self.simulator.start()
                    self.manifest["simulator"].update(self.simulator.metadata)
                    self.producing = True
                    self.action("simulator_started", pid=self.simulator.process.pid)
                    _save_manifest(output, self.manifest)
                self.loop(config, endpoints, selector)
            finally:
                # ExitStack closes every resource even if writing these last actions fails.
                self.stop_producer("shutdown")
                self.open_gate("shutdown")

    def observe(self, point: str, data: bytes, stamp: dict) -> list[int]:
        if self.datagrams is None:
            return [self.captures[point].write(data, stamp)]
        datagram_index = self.datagram_counts[point]
        # Retain even a rejected datagram, including its actual receipt time.
        if self.datagrams.tell() > 32 * 1024 * 1024:
            raise ValueError("Datagram evidence reached the 32 MiB size limit")
        _json_line(
            self.datagrams,
            {"point": point, "datagram_index": datagram_index, "raw_hex": data.hex(), **stamp},
        )
        self.datagram_counts[point] += 1
        # The pinned firmware sends the SiK bootloader-exit bytes three times
        # when initializing its MAVLink UART. Retain and forward those exact
        # datagrams without inventing MAVLink records or scanning past text.
        from .sitl import STARTUP_PREAMBLE, STARTUP_PREAMBLE_COUNT

        if (
            data == STARTUP_PREAMBLE
            and self.captures[point].count == 0
            and self.preamble_counts[point] < STARTUP_PREAMBLE_COUNT
        ):
            self.preamble_counts[point] += 1
            self.action("sitl_startup_preamble", point=point, datagram_index=datagram_index)
            return []
        frames = _split_datagram(data)
        indices = []
        for offset, frame in frames:
            indices.append(
                self.captures[point].write(
                    frame, {**stamp, "datagram_index": datagram_index, "datagram_offset": offset}
                )
            )
            if point == "relay-input" and self.measurement_ns is None:
                parsed = import_bytes(stamp["unix_us"].to_bytes(8, "big") + frame).records[0]
                if (parsed.system_id, parsed.component_id) == (1, 1) and parsed.message_name:
                    self.readiness_messages.add(parsed.message_name)
        return indices

    def loop(self, config: ExperimentConfig, endpoints: dict, selector) -> None:
        counters = self.manifest["counters"]
        encoder = common.MAVLink(None, srcSystem=1, srcComponent=1)
        if config.sitl_binary is None:
            self.measurement_ns = self.origin_ns
            self.manifest["measurement_start"] = self.manifest["start"]
        startup_end_ns = self.origin_ns + round(config.startup_timeout_s * 1e9)
        end_ns = (
            None
            if self.measurement_ns is None
            else self.measurement_ns + round(config.duration_s * 1e9)
        )
        next_send_ns = self.origin_ns
        drain_end_ns = None
        self.producing = True
        self.action("producer_started")
        while True:
            now = time.monotonic_ns()
            if drain_end_ns is None:
                if self.simulator is not None and self.simulator.process.poll() is not None:
                    raise RuntimeError(
                        "Simulator exited before run completion: "
                        f"{self.simulator.process.returncode}"
                    )
                if self.measurement_ns is None and not self.stop.is_set():
                    if now >= startup_end_ns:
                        raise RuntimeError(
                            "Simulator readiness timeout: "
                            "HEARTBEAT and ATTITUDE required from source 1/1"
                        )
                    if {"HEARTBEAT", "ATTITUDE"} <= self.readiness_messages:
                        self.manifest["measurement_start"] = _stamp(self.origin_ns)
                        self.measurement_ns = self.manifest["measurement_start"]["monotonic_ns"]
                        end_ns = self.measurement_ns + round(config.duration_s * 1e9)
                        self.action(
                            "measurement_started", readiness_messages=["HEARTBEAT", "ATTITUDE"]
                        )
                if not self.stop.is_set():
                    self.update_gate(now, config)
                if self.stop.is_set() or (end_ns is not None and now >= end_ns):
                    if self.stop.is_set():
                        self.manifest["outcome"] = "interrupted"
                    elif config.scenario == "blackout" and (
                        self.gate_started_ns is None or self.gate_closed
                    ):
                        raise RuntimeError(
                            "Run duration ended before the full blackout was applied"
                        )
                    self.stop_producer("interrupted" if self.stop.is_set() else "duration_elapsed")
                    self.open_gate("shutdown")
                    drain_end_ns = time.monotonic_ns() + DRAIN_NS
                elif config.sitl_binary is None and now >= next_send_ns:
                    counters["missed_emission_slots"] += (now - next_send_ns) // PERIOD_NS
                    elapsed_s = (now - self.origin_ns) / 1e9
                    message = encoder.attitude_encode(
                        (now - self.origin_ns) // 1_000_000,
                        0.3 * math.sin(elapsed_s),
                        0.2 * math.cos(elapsed_s),
                        0.1 * math.sin(elapsed_s / 2),
                        0.3 * math.cos(elapsed_s),
                        -0.2 * math.sin(elapsed_s),
                        0.05 * math.cos(elapsed_s / 2),
                    )
                    frame = message.pack(encoder)
                    if endpoints["sender"].send(frame) != len(frame):
                        raise OSError("Incomplete sender datagram submission")
                    counters["sender_submitted"] += 1
                    self.action("sender_submitted", sequence=encoder.seq)
                    encoder.seq = (encoder.seq + 1) % 256
                    # Skip missed slots instead of making up observations or bursting a backlog.
                    next_send_ns = (
                        self.origin_ns + ((now - self.origin_ns) // PERIOD_NS + 1) * PERIOD_NS
                    )
            if drain_end_ns is not None and now >= drain_end_ns:
                self.action("drain_finished", reason="deadline_elapsed")
                return
            deadline = drain_end_ns if drain_end_ns is not None else (end_ns or startup_end_ns)
            if config.sitl_binary is None and drain_end_ns is None:
                deadline = min(deadline, next_send_ns)
            if (
                drain_end_ns is None
                and config.scenario == "blackout"
                and self.measurement_ns is not None
            ):
                gate_deadline = (
                    self.measurement_ns + round(config.blackout_at * 1e9)
                    if self.gate_started_ns is None
                    else self.gate_started_ns + BLACKOUT_NS
                )
                if self.gate_started_ns is None or self.gate_closed:
                    deadline = min(deadline, gate_deadline)
            # Polling bounds signal-stop latency without threads or a signal wakeup socket.
            timeout = max(0, min(0.05, (deadline - time.monotonic_ns()) / 1e9))
            for key, _ in selector.select(timeout):
                point = key.data
                try:
                    if (
                        point == "relay-input"
                        and config.sitl_binary is not None
                        and "sender" not in self.manifest["topology"]
                    ):
                        frame, peer = key.fileobj.recvfrom(65535)
                        key.fileobj.connect(peer)
                        self.manifest["topology"]["sender"] = {"host": peer[0], "port": peer[1]}
                    else:
                        frame = key.fileobj.recv(65535)
                except BlockingIOError:
                    continue
                stamp = _stamp(self.origin_ns)  # Sample receipt, never the scheduled send time.
                indices = self.observe(point, frame, stamp)
                if point == "receiver":
                    continue
                if drain_end_ns is None:
                    self.update_gate(time.monotonic_ns(), config)
                if self.gate_closed:
                    counters["relay_dropped"] += len(indices)
                    if self.datagrams is not None:
                        self.action(
                            "datagram_dropped",
                            point=point,
                            datagram_index=self.datagram_counts[point] - 1,
                        )
                    for index in indices:
                        self.action("relay_dropped", point=point, record_index=index)
                else:
                    if endpoints["relay-output"].send(frame) != len(frame):
                        raise OSError("Incomplete relay datagram submission")
                    counters["relay_forwarded"] += len(indices)
                    if self.datagrams is not None:
                        self.action(
                            "datagram_forwarded",
                            point=point,
                            datagram_index=self.datagram_counts[point] - 1,
                        )
                    for index in indices:
                        self.action("relay_forwarded", point=point, record_index=index)


def run_experiment(
    output: Path, config: ExperimentConfig, *, stop: threading.Event | None = None
) -> dict:
    """Create a new run directory, retain partial evidence on failure and close all sockets."""
    requested = config.requested()  # Validate before creating files or opening transports.
    simulator_identity = None
    if config.sitl_binary is not None:
        from .sitl import validate_binary

        simulator_identity = validate_binary(config.sitl_binary)
    manifest = {
        "schema": "uav-debugger-experiment-v2"
        if config.sitl_binary
        else "uav-debugger-experiment-v1",
        "simulator": simulator_identity,
        "measurement_start": None,
        "requested": requested,
        "outcome": "running",
        "error": None,
        "environment": {
            "uav_debugger": version("uav-debugger"),
            "pymavlink": version("pymavlink"),
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "clocks": {
            "monotonic_ns": "Host monotonic clock; scheduling and elapsed durations only.",
            "elapsed_ns": "monotonic_ns minus origin_monotonic_ns for this run.",
            "measurement_elapsed_ns": "Event monotonic_ns minus measurement_start.monotonic_ns.",
            "unix_us": "Host time.time_ns() // 1000, sampled just after monotonic_ns; may regress.",
            "capture": "Unix microseconds sampled immediately after socket receive.",
            "time_boot_ms": "Synthetic sender monotonic elapsed milliseconds since run origin.",
            "limits": "Paired clock samples are sequential, not atomic; no alignment inferred.",
        },
        "capture_profile": "qgc-timestamped-mavlink-v1",
        "topology": {},
        "start": None,
        "end": None,
        "counters": {
            "sender_submitted": 0,
            "relay_observed": 0,
            "relay_forwarded": 0,
            "relay_dropped": 0,
            "receiver_observed": 0,
            "missed_emission_slots": 0,
        },
        "artifacts": {},
    }
    if config.sitl_binary is not None:
        manifest["counters"]["sender_submitted"] = None
        manifest["counters"]["missed_emission_slots"] = None
        manifest["clocks"]["time_boot_ms"] = (
            "Autopilot simulated boot clock; not aligned to host observation or run clocks."
        )
    output = Path(output)
    runner = _Runner(manifest, stop if stop is not None else threading.Event())
    output.mkdir(parents=True, exist_ok=False)
    _save_manifest(output, manifest)
    try:
        runner.execute(output, config)
        if manifest["outcome"] == "running":
            manifest["outcome"] = "interrupted" if runner.stop.is_set() else "completed"
    except Exception as error:
        manifest["outcome"] = "failed"
        manifest["error"] = f"{type(error).__name__}: {error}"
    manifest["end"] = _stamp(runner.origin_ns)
    if config.sitl_binary is not None:
        manifest["datagrams_observed"] = runner.datagram_counts
    for point, capture in runner.captures.items():
        key = "relay_observed" if point == "relay-input" else "receiver_observed"
        manifest["counters"][key] = capture.count
    try:
        for name in (
            "relay-input.tlog",
            "receiver.tlog",
            "actions.jsonl",
            "observations.jsonl",
            "datagrams.jsonl",
            "simulator.log",
            "simulator/profile.parm",
        ):
            path = output / name
            if path.is_file():
                content = path.read_bytes()
                manifest["artifacts"][name] = {
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
    except OSError as error:
        manifest["outcome"] = "failed"
        manifest["error"] = f"Artifact finalization failed: {error}"
    _save_manifest(output, manifest)  # A failed final write must also fail the command.
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a bounded loopback MAVLink experiment with a synthetic or SITL source."
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="New run directory (must not exist)"
    )
    parser.add_argument("--scenario", choices=("baseline", "blackout"), default="baseline")
    parser.add_argument(
        "--duration", type=float, default=6.0, help="Production duration, 0.1–60s (default: 6)"
    )
    parser.add_argument(
        "--blackout-at", type=float, help="Requested blackout start in seconds (default: 2)"
    )
    parser.add_argument(
        "--sitl-binary",
        type=Path,
        help="Use the pinned local ArduCopter SITL executable instead of the synthetic sender",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=15.0,
        help="SITL readiness timeout, 0.1–60s (default: 15)",
    )
    args = parser.parse_args(argv)
    config = ExperimentConfig(
        args.scenario, args.duration, args.blackout_at, args.sitl_binary, args.startup_timeout
    )
    try:
        config.validate()
    except ValueError as error:
        parser.error(str(error))
    stop = threading.Event()
    received_signal = None

    def request_stop(signum, _frame):
        nonlocal received_signal
        if received_signal is None:
            received_signal = signum
        stop.set()

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        manifest = run_experiment(args.output, config, stop=stop)
    except FileExistsError as error:
        print(f"Cannot create experiment output: {error}", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"Cannot configure experiment: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"Cannot save experiment evidence: {error}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps({"output": str(args.output), **manifest}, indent=2))
    if manifest["outcome"] == "failed":
        print(f"Experiment failed: {manifest['error']}", file=sys.stderr)
        return 1
    if manifest["outcome"] == "interrupted" and received_signal is not None:
        return 128 + received_signal
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
