"""Explicit controller ownership without importing execution on read-only paths."""

import json
import math
import secrets
import threading
import time
from pathlib import Path

from starlette.requests import ClientDisconnect, Request

from .catalog import _root_directory
from .saved_run_ui import _seconds

ACTION_HEADER = "X-UAV-Debugger-Action"
MAX_ACTION_BYTES = 4096
HISTORY_LIMIT = 20


class ExperimentRequestError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate JSON object keys are not accepted.")
        result[name] = value
    return result


async def action_payload(request: Request, token: str, *, same_origin: bool) -> dict:
    """Reject non-explicit actions before reading or importing execution code."""
    supplied = request.headers.get(ACTION_HEADER, "")
    if not same_origin or not supplied.isascii() or not secrets.compare_digest(supplied, token):
        raise ExperimentRequestError("A same-origin Experiment action token is required.", 403)
    if request.query_params:
        raise ExperimentRequestError("Experiment actions do not accept query parameters.")
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
        or request.headers.get("content-encoding", "identity").lower() != "identity"
    ):
        raise ExperimentRequestError("Send unencoded application/json action data.", 415)
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 20:
            raise ExperimentRequestError("Content-Length must be a nonnegative decimal integer.")
        if int(length) > MAX_ACTION_BYTES:
            raise ExperimentRequestError("Experiment action data exceeds 4096 bytes.", 413)
    data = bytearray()
    try:
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_ACTION_BYTES:
                raise ExperimentRequestError("Experiment action data exceeds 4096 bytes.", 413)
            data.extend(chunk)

        def invalid_constant(value):
            raise ValueError(f"JSON constant {value} is not accepted.")

        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_object, parse_constant=invalid_constant
        )
    except ExperimentRequestError:
        raise
    except (ValueError, RecursionError, ClientDisconnect) as error:
        raise ExperimentRequestError(f"Invalid Experiment action data: {error}") from error
    if not isinstance(value, dict):
        raise ExperimentRequestError("Experiment action data must be a JSON object.")
    return value


def _clock(value) -> dict | None:
    if value is None:
        return None
    return {"monotonic_ns": str(value.monotonic_ns), "unix_us": str(value.unix_us)}


class ExperimentWorkspace:
    """Allocate a single existing controller only for an explicit validated Start."""

    def __init__(self, root: Path, sitl_binary: Path | None = None):
        self.root = root
        self.sitl_binary = sitl_binary
        self.action_token = secrets.token_urlsafe(32)
        self._controller = None
        self._lock = threading.RLock()
        self._closed = False

    def configuration(self) -> dict:
        return {
            "experiment_root": str(self.root),
            "sitl_configured": self.sitl_binary is not None,
            "sitl_binary": str(self.sitl_binary) if self.sitl_binary else None,
        }

    def _validate_root(self) -> None:
        # New roots are allowed, but existing ancestors must follow the catalog's
        # no-symlink rule before the controller resolves its output directory.
        for candidate in (self.root, *self.root.parents):
            try:
                with _root_directory(candidate):
                    return
            except FileNotFoundError:
                continue
        raise ValueError("No accessible ancestor for the Experiment output root.")

    @staticmethod
    def _key(snapshot, controller) -> str:
        try:
            key = snapshot.output.relative_to(controller.output_root).as_posix()
        except ValueError as error:
            raise ExperimentRequestError(
                "Run evidence is outside the configured root.", 409
            ) from error
        if key != f"{snapshot.run_id}/evidence":
            raise ExperimentRequestError("Run evidence has an unexpected location.", 409)
        return key

    def status(self, *, run_id: str | None = None) -> dict:
        with self._lock:
            history = self._controller.history() if self._controller is not None else ()
            now_ns, unix_us = time.monotonic_ns(), time.time_ns() // 1000
            rows = []
            for snapshot in history:
                end = snapshot.finished.monotonic_ns if snapshot.finished else now_ns
                elapsed = max(0, end - snapshot.created.monotonic_ns)
                rows.append(
                    {
                        "run_id": snapshot.run_id,
                        "catalog_key": self._key(snapshot, self._controller),
                        "output": str(snapshot.output),
                        "control_file": str(snapshot.control_file),
                        "requested": dict(snapshot.requested),
                        "state": snapshot.state,
                        "active": snapshot.active,
                        "created": _clock(snapshot.created),
                        "stop_requested": _clock(snapshot.stop_requested),
                        "finished": _clock(snapshot.finished),
                        "stop_reason": snapshot.stop_reason,
                        "returncode": snapshot.returncode,
                        "declared_outcome": snapshot.declared_outcome,
                        "error": snapshot.error,
                        "diagnostics": snapshot.diagnostics,
                        "forced_termination": snapshot.forced_termination,
                        "elapsed_ns": str(elapsed),
                        "elapsed_s": _seconds(elapsed),
                        "evidence_status": "not_validated",
                        "can_open": not snapshot.active,
                    }
                )
            result = {
                "schema_version": 6,
                "experiment": {
                    "action_token": self.action_token,
                    "configuration": self.configuration(),
                    "history_limit": HISTORY_LIMIT,
                    "active_run_id": next((item.run_id for item in history if item.active), None),
                    "history": rows,
                    "server_clock": {"monotonic_ns": str(now_ns), "unix_us": str(unix_us)},
                },
            }
            if run_id is not None:
                result["run_id"] = run_id
            return result

    def start(self, values: dict) -> dict:
        allowed = {
            "source",
            "scenario",
            "duration_s",
            "blackout_at_s",
            "blackout_duration_s",
            "startup_timeout_s",
        }
        if set(values) - allowed or not {"source", "scenario", "duration_s"} <= set(values):
            raise ExperimentRequestError(
                "Start requires source, scenario and duration_s only with supported settings."
            )
        if values["source"] not in ("synthetic", "arducopter-sitl"):
            raise ExperimentRequestError("Source must be synthetic or arducopter-sitl.")
        if values["scenario"] not in ("baseline", "blackout"):
            raise ExperimentRequestError("Scenario must be baseline or blackout.")
        for name in allowed - {"source", "scenario"}:
            if name not in values:
                continue
            value = values[name]
            if value is None and name in {"blackout_at_s", "blackout_duration_s"}:
                continue
            if (
                type(value) not in (int, float)
                or not -60 <= value <= 60
                or not math.isfinite(value)
            ):
                raise ExperimentRequestError(
                    f"{name} must be a finite JSON number in the supported range."
                )
        native = values["source"] == "arducopter-sitl"
        if native and self.sitl_binary is None:
            raise ExperimentRequestError("The server has no configured SITL executable.")
        from .experiment import ExperimentConfig

        config = ExperimentConfig(
            scenario=values["scenario"],
            duration_s=values["duration_s"],
            blackout_at_s=values.get("blackout_at_s"),
            blackout_duration_s=values.get("blackout_duration_s"),
            startup_timeout_s=values.get("startup_timeout_s", 15.0),
            sitl_binary=self.sitl_binary if native else None,
        )
        try:
            config.validate()
            with self._lock:
                if self._closed:
                    raise ExperimentRequestError("Experiment workspace is shutting down.", 409)
                self._validate_root()
                if self._controller is None:
                    from .experiment_control import ExperimentController

                    self._controller = ExperimentController(self.root, history_limit=HISTORY_LIMIT)
                snapshot = self._controller.start(config)
                return self.status(run_id=snapshot.run_id)
        except ExperimentRequestError:
            raise
        except RuntimeError as error:
            raise ExperimentRequestError(str(error), 409, "experiment_active") from error
        except (OSError, ValueError) as error:
            raise ExperimentRequestError(str(error)) from error

    @staticmethod
    def run_identifier(value: object) -> str:
        if (
            not isinstance(value, str)
            or not value.startswith("run-")
            or not 5 <= len(value) <= 128
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in value
            )
        ):
            raise ExperimentRequestError("A valid controller run_id is required.")
        return value

    def _snapshot(self, run_id: str):
        try:
            if self._controller is None:
                raise KeyError(run_id)
            return self._controller.snapshot(run_id)
        except KeyError as error:
            raise ExperimentRequestError(
                "The run is not in this server's controller history.", 404
            ) from error

    def stop(self, values: dict) -> dict:
        if set(values) != {"run_id"}:
            raise ExperimentRequestError("Stop requires exactly one run_id.")
        identifier = self.run_identifier(values["run_id"])
        with self._lock:
            self._snapshot(identifier)
            self._controller.stop(identifier)
            return self.status(run_id=identifier)

    def open_key(self, identifier: str) -> str:
        identifier = self.run_identifier(identifier)
        with self._lock:
            snapshot = self._snapshot(identifier)
            if snapshot.active:
                raise ExperimentRequestError(
                    "Wait for the worker to finish before opening its evidence.",
                    409,
                    "experiment_active",
                )
            return self._key(snapshot, self._controller)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._controller is not None:
                self._controller.close()
