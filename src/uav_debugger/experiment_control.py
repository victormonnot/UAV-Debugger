"""Own one optional local Experiment worker without importing presentation code.

Process state and controller clocks are separate from the runner's evidence.
Opening Analyze files does not import or construct this controller.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

from .experiment import ExperimentConfig

STOP_GRACE_SECONDS = 5.0
RUN_MARGIN_SECONDS = 8.0
DIAGNOSTIC_BYTES = 8192
ACTIVE_STATES = frozenset({"starting", "running", "stopping"})
_controllers: weakref.WeakSet[ExperimentController] = weakref.WeakSet()


@dataclass(frozen=True)
class ControlClock:
    monotonic_ns: int
    unix_us: int


def _clock() -> ControlClock:
    return ControlClock(time.monotonic_ns(), time.time_ns() // 1000)


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    output: Path
    control_file: Path
    requested: Mapping[str, object]
    sitl_binary: Path | None
    state: str
    created: ControlClock
    stop_requested: ControlClock | None = None
    stop_reason: str | None = None
    finished: ControlClock | None = None
    returncode: int | None = None
    declared_outcome: str | None = None
    error: str | None = None
    diagnostics: str = ""
    forced_termination: bool = False

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


@dataclass
class _Worker:
    snapshot: RunSnapshot
    process: subprocess.Popen | None = None
    deadline_ns: int = 0
    stop_deadline_ns: int | None = None
    diagnostic_tail: bytes = b""
    done: threading.Event | None = None


def _command(output: Path, config: ExperimentConfig) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "uav_debugger.experiment_worker",
        "--output",
        str(output),
        "--scenario",
        config.scenario,
        "--duration",
        str(config.duration_s),
    ]
    if config.blackout_at_s is not None:
        command.extend(("--blackout-at", str(config.blackout_at_s)))
    if config.sitl_binary is not None:
        command.extend(
            (
                "--sitl-binary",
                str(config.sitl_binary),
                "--startup-timeout",
                str(config.startup_timeout_s),
                "--isolated",
            )
        )
        # Namespace PID 1 exiting removes every namespace process, including
        # the simulator's separate session. unshare --fork itself does not
        # forward SIGTERM; stdin EOF is the cooperative shutdown mechanism.
        command = [
            "unshare",
            "--user",
            "--map-root-user",
            "--net",
            "--pid",
            "--fork",
            "--kill-child=SIGKILL",
            *command,
        ]
    return command


def _serialize(snapshot: RunSnapshot) -> dict:
    def stamp(value):
        return (
            None
            if value is None
            else {"monotonic_ns": value.monotonic_ns, "unix_us": value.unix_us}
        )

    return {
        "schema": "uav-debugger-experiment-control-v1",
        "run_id": snapshot.run_id,
        "output": str(snapshot.output),
        "requested": dict(snapshot.requested),
        "sitl_binary": str(snapshot.sitl_binary) if snapshot.sitl_binary else None,
        "state": snapshot.state,
        "created": stamp(snapshot.created),
        "stop_requested": stamp(snapshot.stop_requested),
        "stop_reason": snapshot.stop_reason,
        "finished": stamp(snapshot.finished),
        "returncode": snapshot.returncode,
        "declared_outcome": snapshot.declared_outcome,
        "error": snapshot.error,
        "diagnostics": snapshot.diagnostics,
        "forced_termination": snapshot.forced_termination,
        "clocks": {
            "monotonic_ns": "Controller host monotonic clock; deadlines and request ordering.",
            "unix_us": "Controller wall clock, sampled separately; may repeat or regress.",
            "limits": "Controller requests are not runner actions or telemetry observations.",
        },
    }


class ExperimentController:
    """Serialize explicitly requested launches and retain a bounded in-memory history.

    Each new container holds control.json and a fresh evidence/ directory. History
    eviction never deletes files. Browser disconnect is not server shutdown: the
    worker remains bounded and visible to other local tabs on the same server.
    """

    def __init__(self, output_root: Path = Path("local/experiments"), *, history_limit: int = 20):
        if (
            isinstance(history_limit, bool)
            or not isinstance(history_limit, int)
            or not 1 <= history_limit <= 100
        ):
            raise ValueError("history_limit must be an integer between 1 and 100")
        self.output_root = Path(output_root).resolve()
        self.history_limit = history_limit
        self._lock = threading.RLock()
        self._workers: dict[str, _Worker] = {}
        self._active: str | None = None
        self._closed = False
        _controllers.add(self)

    def _persist(self, worker: _Worker, *, required: bool = False) -> None:
        snapshot = worker.snapshot
        temporary = snapshot.control_file.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(_serialize(snapshot), indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(snapshot.control_file)
        except OSError as error:
            if required:
                raise
            worker.snapshot = replace(snapshot, error=f"Cannot save controller metadata: {error}")

    def start(self, config: ExperimentConfig) -> RunSnapshot:
        """Validate first; launch only one fresh owned worker, never a shell command."""
        config.validate()
        if config.sitl_binary is not None:
            from .sitl import validate_binary

            identity = validate_binary(config.sitl_binary)
            config = replace(config, sitl_binary=Path(identity["path"]))
            if not shutil.which("unshare") or not shutil.which("ip"):
                raise ValueError("SITL requires the local unshare and ip commands")
        with self._lock:
            if self._closed:
                raise RuntimeError("Experiment controller is closed")
            if self._active is not None:
                raise RuntimeError("An experiment is already active on this server")
            self.output_root.mkdir(parents=True, exist_ok=True)
            container = Path(tempfile.mkdtemp(prefix="run-", dir=self.output_root))
            snapshot = RunSnapshot(
                run_id=container.name,
                output=container / "evidence",
                control_file=container / "control.json",
                requested=MappingProxyType(config.requested()),
                sitl_binary=config.sitl_binary,
                state="starting",
                created=_clock(),
            )
            worker = _Worker(snapshot, done=threading.Event())
            self._persist(worker, required=True)
            self._workers[snapshot.run_id] = worker
            while len(self._workers) > self.history_limit:
                del self._workers[next(iter(self._workers))]
            try:
                worker.process = subprocess.Popen(
                    _command(snapshot.output, config),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as error:
                worker.snapshot = replace(
                    snapshot,
                    state="failed",
                    finished=_clock(),
                    error=f"Cannot launch worker: {error}",
                )
                worker.done.set()
                self._persist(worker)
                return worker.snapshot
            worker.snapshot = replace(snapshot, state="running")
            allowance = config.duration_s + RUN_MARGIN_SECONDS
            if config.sitl_binary is not None:
                allowance += config.startup_timeout_s
            worker.deadline_ns = snapshot.created.monotonic_ns + round(allowance * 1e9)
            self._active = snapshot.run_id
            self._persist(worker)
            threading.Thread(target=self._monitor, args=(worker,), daemon=True).start()
            return worker.snapshot

    def snapshot(self, run_id: str) -> RunSnapshot:
        with self._lock:
            return self._workers[run_id].snapshot

    def history(self) -> tuple[RunSnapshot, ...]:
        with self._lock:
            return tuple(worker.snapshot for worker in reversed(self._workers.values()))

    def _request_stop(self, worker: _Worker, reason: str) -> None:
        if not worker.snapshot.active or worker.snapshot.stop_requested is not None:
            return
        stamp = _clock()
        worker.snapshot = replace(
            worker.snapshot, state="stopping", stop_requested=stamp, stop_reason=reason
        )
        worker.stop_deadline_ns = stamp.monotonic_ns + round(STOP_GRACE_SECONDS * 1e9)
        if worker.process is not None and worker.process.stdin is not None:
            worker.process.stdin.close()
        self._persist(worker)

    def stop(self, run_id: str) -> RunSnapshot:
        """Request runner cleanup; repeated requests preserve the original request clock."""
        with self._lock:
            worker = self._workers[run_id]
            self._request_stop(worker, "user")
            return worker.snapshot

    def _read_diagnostics(self, worker: _Worker) -> None:
        assert worker.process is not None and worker.process.stderr is not None
        with worker.process.stderr as stream:
            while block := stream.read(4096):
                with self._lock:
                    worker.diagnostic_tail = (worker.diagnostic_tail + block)[-DIAGNOSTIC_BYTES:]

    def _monitor(self, worker: _Worker) -> None:
        process = worker.process
        assert process is not None
        reader = threading.Thread(target=self._read_diagnostics, args=(worker,), daemon=True)
        reader.start()
        while process.poll() is None:
            with self._lock:
                now = time.monotonic_ns()
                if now >= worker.deadline_ns and worker.snapshot.stop_requested is None:
                    self._request_stop(worker, "watchdog")
                    worker.snapshot = replace(
                        worker.snapshot, error="Worker exceeded its bounded execution allowance"
                    )
                if (
                    worker.stop_deadline_ns is not None
                    and now >= worker.stop_deadline_ns
                    and not worker.snapshot.forced_termination
                ):
                    worker.snapshot = replace(
                        worker.snapshot,
                        forced_termination=True,
                        error=(
                            "Worker did not stop within the cleanup allowance; "
                            "forced termination requested"
                        ),
                    )
                    # Target only the session created for this owned launch. PID
                    # namespace teardown also removes the separately grouped SITL.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self._persist(worker)
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                continue
        process.wait()
        if process.stdin is not None:
            process.stdin.close()
        reader.join(timeout=1)
        outcome, runner_error = self._final_outcome(worker.snapshot.output)
        with self._lock:
            snapshot = worker.snapshot
            failed = (
                process.returncode != 0
                or outcome not in {"completed", "interrupted"}
                or snapshot.error is not None
            )
            worker.snapshot = replace(
                snapshot,
                state="failed" if failed else "finished",
                finished=_clock(),
                returncode=process.returncode,
                declared_outcome=outcome,
                diagnostics=worker.diagnostic_tail.decode("utf-8", errors="replace"),
                error=snapshot.error
                or runner_error
                or (
                    f"Worker exited with status {process.returncode}"
                    if process.returncode
                    else None
                ),
            )
            self._active = None
            self._persist(worker)
            assert worker.done is not None
            worker.done.set()

    @staticmethod
    def _final_outcome(output: Path) -> tuple[str | None, str | None]:
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            directory = os.open(output, flags | os.O_DIRECTORY)
            try:
                descriptor = os.open("run.json", flags, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValueError("Runner manifest must be a regular file")
                    if info.st_size > 1024 * 1024:
                        raise ValueError("Runner manifest exceeds the size limit")
                    raw = stream.read(1024 * 1024 + 1)
            finally:
                os.close(directory)
            if len(raw) > 1024 * 1024:
                raise ValueError("Runner manifest exceeds the size limit")
            manifest = json.loads(raw)
            outcome = manifest.get("outcome") if isinstance(manifest, dict) else None
            if not isinstance(outcome, str) or outcome not in {
                "running",
                "completed",
                "interrupted",
                "failed",
            }:
                raise ValueError("Runner manifest has no recognized declared outcome")
            error = manifest.get("error")
            if outcome == "running":
                error = "Worker exited without a finalized runner outcome"
            return outcome, str(error)[:DIAGNOSTIC_BYTES] if error is not None else None
        except (OSError, ValueError, RecursionError) as error:
            return None, f"Final runner outcome unavailable: {error}"

    def close(self) -> None:
        """Request owned-worker cleanup and wait a bounded time for its reaping."""
        with self._lock:
            self._closed = True
            worker = self._workers.get(self._active) if self._active else None
            if worker is not None:
                self._request_stop(worker, "controller_shutdown")
        if worker is not None and worker.done is not None:
            worker.done.wait(timeout=STOP_GRACE_SECONDS + 3)


def shutdown_all() -> None:
    """Close controllers already constructed by an explicit Experiment workflow."""
    for controller in list(_controllers):
        controller.close()


atexit.register(shutdown_all)
