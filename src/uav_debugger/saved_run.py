"""Read bounded saved Experiment evidence without importing execution modules."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .importer import MAX_INPUT_BYTES, import_bytes
from .model import ImportResult

CAPTURE_POINTS = ("relay-input", "receiver")
FILE_LIMITS = {
    "run.json": 1024 * 1024,
    "actions.jsonl": 16 * 1024 * 1024,
    "observations.jsonl": 16 * 1024 * 1024,
    "relay-input.tlog": MAX_INPUT_BYTES,
    "receiver.tlog": MAX_INPUT_BYTES,
    "datagrams.jsonl": 33 * 1024 * 1024,
    "simulator.log": 8 * 1024 * 1024,
    "simulator/profile.parm": 1024 * 1024,
}
KNOWN_FILES = tuple(FILE_LIMITS)
MAX_RUN_BYTES = 64 * 1024 * 1024
MAX_TRACE_EVENTS = 100_000
MAX_JSON_LINE_BYTES = 1024 * 1024
SCHEMAS = ("uav-debugger-experiment-v1", "uav-debugger-experiment-v2")
ACTIONS = {
    "producer_started",
    "producer_stopped",
    "sender_submitted",
    "relay_forwarded",
    "relay_dropped",
    "forwarding_disabled",
    "forwarding_enabled",
    "drain_finished",
    "simulator_started",
    "simulator_stopped",
    "measurement_started",
    "datagram_forwarded",
    "datagram_dropped",
    "sitl_startup_preamble",
}


@dataclass(frozen=True, slots=True)
class EvidenceFile:
    name: str
    raw_bytes: bytes
    sha256: str

    @property
    def size_bytes(self) -> int:
        return len(self.raw_bytes)


@dataclass(frozen=True, slots=True)
class RunIssue:
    code: str
    severity: Literal["warning", "error"]
    file_name: str
    line_number: int | None
    message: str


@dataclass(frozen=True, slots=True)
class TraceEvent:
    file_name: str
    line_number: int
    data: Mapping[str, object]
    valid: bool

    @property
    def monotonic_ns(self) -> int | None:
        return self.data.get("monotonic_ns")

    @property
    def elapsed_ns(self) -> int | None:
        return self.data.get("elapsed_ns")

    @property
    def unix_us(self) -> int | None:
        return self.data.get("unix_us")


@dataclass(frozen=True, slots=True)
class Observation(TraceEvent):
    @property
    def point(self) -> str | None:
        return self.data.get("point")

    @property
    def record_index(self) -> int | None:
        return self.data.get("record_index")


@dataclass(frozen=True, slots=True)
class Datagram(TraceEvent):
    raw_bytes: bytes | None

    @property
    def point(self) -> str | None:
        return self.data.get("point")

    @property
    def datagram_index(self) -> int | None:
        return self.data.get("datagram_index")


@dataclass(frozen=True, slots=True)
class GateInterval:
    start: TraceEvent
    end: TraceEvent | None

    @property
    def duration_ns(self) -> int | None:
        if self.end is None:
            return None
        return self.end.monotonic_ns - self.start.monotonic_ns


@dataclass(frozen=True, slots=True)
class SavedRun:
    source_name: str
    schema: str
    manifest: Mapping[str, object]
    files: Mapping[str, EvidenceFile]
    captures: Mapping[str, ImportResult]
    actions: tuple[TraceEvent, ...]
    observations: tuple[Observation, ...]
    datagrams: tuple[Datagram, ...]
    issues: tuple[RunIssue, ...]
    gate_intervals: tuple[GateInterval, ...]
    origin_monotonic_ns: int | None
    measurement_monotonic_ns: int | None

    @property
    def requested(self) -> Mapping[str, object]:
        value = self.manifest.get("requested")
        return value if isinstance(value, Mapping) else {}

    @property
    def declared_outcome(self) -> str:
        value = self.manifest.get("outcome")
        return value if isinstance(value, str) else "unknown"

    @property
    def evidence_status(self) -> str:
        if any(issue.severity == "error" for issue in self.issues):
            return "invalid"
        if self.issues or self.declared_outcome == "running":
            return "incomplete"
        return "consistent"

    @property
    def identity(self) -> str:
        digest = hashlib.sha256()
        for name, evidence in sorted(self.files.items()):
            digest.update(name.encode("utf-8") + b"\x00")
            digest.update(bytes.fromhex(evidence.sha256))
        return digest.hexdigest()


def _integer(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**64


def _key(point, number):
    return (point, number) if point in CAPTURE_POINTS and _integer(number) else None


def _frame_offsets(raw: bytes) -> set[int]:
    """Structural datagram boundaries only; capture import owns checksum validation."""
    offsets = set()
    offset = 0
    while offset < len(raw):
        magic = raw[offset]
        header = 6 if magic == 0xFE else 10
        if magic not in (0xFE, 0xFD) or len(raw) - offset < header:
            return set()
        if magic == 0xFD and raw[offset + 2] != 0:
            return set()
        end = offset + header + raw[offset + 1] + 2
        if end > len(raw):
            return set()
        offsets.add(offset)
        offset = end
    return offsets


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _json(raw: bytes) -> dict:
    def reject_constant(value: str):
        raise ValueError(f"Non-finite JSON number: {value}")

    try:
        value = json.loads(raw, object_pairs_hook=_object, parse_constant=reject_constant)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 24:
                raise ValueError("JSON nesting exceeds 24 levels")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
            elif isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Non-finite JSON number")
        return value
    except (UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError(f"Invalid bounded JSON: {type(error).__name__}") from error


class _Reader:
    def __init__(self, files: Mapping[str, bytes], source_name: str):
        self.source_name = source_name
        self.issues: list[RunIssue] = []
        self.files = {
            name: EvidenceFile(name, raw, hashlib.sha256(raw).hexdigest())
            for name, raw in files.items()
        }
        self.manifest = _json(files["run.json"])
        self.schema = self.manifest.get("schema")
        if self.schema not in SCHEMAS:
            raise ValueError(f"Unsupported saved Experiment schema: {self.schema!r}")
        self.untrusted_files: set[str] = set()
        self.origin = self.manifest.get("origin_monotonic_ns")
        if not _integer(self.origin):
            self.issue("invalid_origin", "run.json", "Missing or invalid monotonic run origin.")
            self.origin = None
        self.end = None
        self.measurement = self.origin if self.schema == SCHEMAS[0] else None
        self.validate_manifest()

    def issue(self, code, file_name, message, line=None, *, warning=False):
        self.issues.append(
            RunIssue(code, "warning" if warning else "error", file_name, line, message)
        )

    def stamp_valid(self, data: Mapping, file_name: str, line=None) -> bool:
        values = [data.get(key) for key in ("monotonic_ns", "elapsed_ns", "unix_us")]
        valid = all(_integer(value) for value in values) and self.origin is not None
        if valid:
            valid = values[0] - self.origin == values[1] and values[2] < 2**64
        if valid and self.end is not None:
            valid = values[0] <= self.end
        measurement = data.get("measurement_elapsed_ns")
        if measurement is not None:
            valid = (
                valid
                and _integer(measurement)
                and self.measurement is not None
                and values[0] - self.measurement == measurement
            )
        if not valid:
            self.issue(
                "invalid_clock", file_name, "Invalid clock values or origin arithmetic.", line
            )
        return valid

    def validate_manifest(self):
        manifest = self.manifest
        if manifest.get("outcome") not in ("running", "completed", "interrupted", "failed"):
            self.issue("invalid_outcome", "run.json", "Unknown or missing declared outcome.")
        if not isinstance(manifest.get("requested"), dict):
            self.issue("invalid_requested", "run.json", "Requested settings must be an object.")
        if manifest.get("capture_profile") != "qgc-timestamped-mavlink-v1":
            self.issue("invalid_profile", "run.json", "Unknown or missing capture profile.")
        for field in ("start", "end"):
            value = manifest.get(field)
            if value is None:
                self.issue(
                    "missing_clock", "run.json", f"No finalized {field} clock.", warning=True
                )
            elif not isinstance(value, dict) or not self.stamp_valid(value, "run.json"):
                if not isinstance(value, dict):
                    self.issue("invalid_clock", "run.json", f"{field} must be a clock object.")
            elif field == "end":
                self.end = value["monotonic_ns"]
        start = manifest.get("start")
        if self.end is not None and isinstance(start, dict) and _integer(start.get("monotonic_ns")):
            if self.end < start["monotonic_ns"]:
                self.issue(
                    "invalid_clock", "run.json", "End precedes start on the monotonic clock."
                )
        measurement = manifest.get("measurement_start")
        if self.schema == SCHEMAS[1] and measurement is not None:
            if isinstance(measurement, dict) and self.stamp_valid(measurement, "run.json"):
                self.measurement = measurement["monotonic_ns"]
            elif not isinstance(measurement, dict):
                self.issue("invalid_clock", "run.json", "measurement_start must be a clock object.")
        if self.schema == SCHEMAS[1] and self.measurement is None:
            self.issue(
                "missing_measurement", "run.json", "No validated measurement origin.", warning=True
            )
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, dict):
            self.issue("invalid_artifacts", "run.json", "Artifact fingerprints must be an object.")
            artifacts = {}
        expected = set(KNOWN_FILES[1:5])
        if self.schema == SCHEMAS[1]:
            expected.update(KNOWN_FILES[5:])
        for name in artifacts:
            if name not in KNOWN_FILES[1:]:
                self.issue("unknown_artifact", "run.json", f"Unsupported artifact name: {name!r}.")
        for name in expected | (set(self.files) - {"run.json"}):
            evidence = self.files.get(name)
            if evidence is None:
                self.issue(
                    "missing_file",
                    name,
                    "Artifact not supplied; no file was opened from manifest paths.",
                    warning=True,
                )
                continue
            metadata = artifacts.get(name)
            if metadata is None:
                self.issue(
                    "missing_fingerprint",
                    name,
                    "No finalized manifest fingerprint for this file.",
                    warning=True,
                )
            elif (
                not isinstance(metadata, dict)
                or not _integer(metadata.get("size_bytes"))
                or metadata.get("size_bytes") != evidence.size_bytes
                or metadata.get("sha256") != evidence.sha256
            ):
                self.issue(
                    "artifact_mismatch", name, "File size or SHA-256 does not match the manifest."
                )
                self.untrusted_files.add(name)

    def lines(self, name: str, event_class=TraceEvent) -> list[TraceEvent]:
        evidence = self.files.get(name)
        if evidence is None:
            return []
        events = []
        previous_ns = None
        # splitlines() keeps a partial final line; parse it only if it is complete JSON.
        for line, raw in enumerate(evidence.raw_bytes.splitlines(), 1):
            if line > MAX_TRACE_EVENTS or len(raw) > MAX_JSON_LINE_BYTES:
                self.issue(
                    "trace_limit",
                    name,
                    "Trace event count or line size limit reached; retained prefix.",
                    line,
                )
                break
            try:
                data = _json(raw)
            except ValueError as error:
                self.issue("invalid_jsonl", name, f"Trace stopped at invalid JSON: {error}", line)
                break
            valid = self.stamp_valid(data, name, line)
            stamp = data.get("monotonic_ns")
            if valid and previous_ns is not None and stamp < previous_ns:
                self.issue(
                    "clock_regression",
                    name,
                    "Monotonic trace time regresses; original order retained.",
                    line,
                )
                valid = False
            if valid:
                previous_ns = stamp
            valid = valid and name not in self.untrusted_files
            args = (name, line, MappingProxyType(data), valid)
            events.append(
                event_class(*args, None) if event_class is Datagram else event_class(*args)
            )
        return events

    def read_datagrams(self) -> tuple[Datagram, ...]:
        events = self.lines("datagrams.jsonl", Datagram)
        expected = dict.fromkeys(CAPTURE_POINTS, 0)
        seen = set()
        duplicate = set()
        for index, event in enumerate(events):
            data = event.data
            point, number = data.get("point"), data.get("datagram_index")
            raw_hex = data.get("raw_hex")
            raw = None
            valid = point in CAPTURE_POINTS and _integer(number)
            if valid:
                key = point, number
                if key in seen:
                    duplicate.add(key)
                seen.add(key)
                valid = number == expected[point]
                expected[point] += 1
            if isinstance(raw_hex, str) and len(raw_hex) <= 131070 and len(raw_hex) % 2 == 0:
                try:
                    raw = bytes.fromhex(raw_hex)
                    valid = valid and len(raw) * 2 == len(raw_hex)
                except ValueError:
                    valid = False
            else:
                valid = False
            if not valid:
                self.issue(
                    "invalid_datagram",
                    event.file_name,
                    "Invalid point, index or bounded datagram bytes.",
                    event.line_number,
                )
            events[index] = replace(event, valid=event.valid and valid, raw_bytes=raw)
        for index, event in enumerate(events):
            if _key(event.point, event.datagram_index) in duplicate:
                events[index] = replace(event, valid=False)
        return tuple(events)

    def read_observations(self, captures, datagrams) -> tuple[Observation, ...]:
        events = self.lines("observations.jsonl", Observation)
        by_datagram = {(item.point, item.datagram_index): item for item in datagrams if item.valid}
        boundaries = {key: _frame_offsets(item.raw_bytes) for key, item in by_datagram.items()}
        seen = set()
        duplicate = set()
        for index, event in enumerate(events):
            point, record_index = event.point, event.record_index
            data = event.data
            capture = captures.get(point) if isinstance(point, str) else None
            valid = (
                capture is not None
                and _integer(record_index)
                and record_index < len(capture.records)
            )
            if valid:
                key = point, record_index
                if key in seen:
                    duplicate.add(key)
                seen.add(key)
                record = capture.records[record_index]
                valid = (
                    type(data.get("offset")) is int
                    and data.get("offset") == record.offset
                    and type(data.get("frame_size_bytes")) is int
                    and data.get("frame_size_bytes") == len(record.raw_frame)
                    and data.get("frame_sha256") == hashlib.sha256(record.raw_frame).hexdigest()
                    and data.get("unix_us") == record.timestamp_us
                    and f"{point}.tlog" not in self.untrusted_files
                )
                if self.schema == SCHEMAS[1]:
                    number, offset = data.get("datagram_index"), data.get("datagram_offset")
                    datagram = by_datagram.get((point, number)) if _integer(number) else None
                    valid = valid and datagram is not None and _integer(offset)
                    if valid:
                        valid = (
                            offset in boundaries[(point, number)]
                            and datagram.raw_bytes[offset : offset + len(record.raw_frame)]
                            == record.raw_frame
                            and all(
                                data.get(key) == datagram.data.get(key)
                                for key in ("monotonic_ns", "elapsed_ns", "unix_us")
                            )
                        )
            if not valid:
                self.issue(
                    "invalid_observation",
                    event.file_name,
                    "Observation does not identify exact capture bytes, clocks "
                    "and datagram reference.",
                    event.line_number,
                )
            events[index] = replace(event, valid=event.valid and valid)
        for index, event in enumerate(events):
            key = _key(event.point, event.record_index)
            if key in duplicate:
                self.issue(
                    "duplicate_observation",
                    event.file_name,
                    "Duplicate capture record reference; neither reference is verified.",
                    event.line_number,
                )
                events[index] = replace(event, valid=False)
        for point, capture in captures.items():
            verified = {item.record_index for item in events if item.valid and item.point == point}
            missing = len(capture.records) - len(verified)
            if missing:
                self.issue(
                    "unreferenced_records",
                    f"{point}.tlog",
                    f"{missing} accepted capture records lack a validated observation reference.",
                    warning=True,
                )
        return tuple(events)

    def read_actions(self, observations, datagrams):
        events = self.lines("actions.jsonl")
        by_record = {(item.point, item.record_index): item for item in observations if item.valid}
        by_datagram = {(item.point, item.datagram_index): item for item in datagrams if item.valid}
        seen_decisions = set()
        duplicate_decisions = set()
        for index, event in enumerate(events):
            data = event.data
            action = data.get("action")
            if not isinstance(action, str):
                action = None
            valid = action in ACTIONS
            reference = None
            if action in {"relay_forwarded", "relay_dropped"}:
                number = data.get("record_index")
                key = ("relay-input", number) if _integer(number) else None
                reference = by_record.get(key)
                valid = valid and data.get("point") == "relay-input" and reference is not None
                if key is not None:
                    if key in seen_decisions:
                        valid = False
                        duplicate_decisions.add(key)
                    seen_decisions.add(key)
            elif action in {"datagram_forwarded", "datagram_dropped", "sitl_startup_preamble"}:
                number, point = data.get("datagram_index"), data.get("point")
                key = (point, number) if point in CAPTURE_POINTS and _integer(number) else None
                reference = by_datagram.get(key)
                valid = valid and reference is not None
                if action != "sitl_startup_preamble":
                    valid = valid and point == "relay-input"
                else:
                    valid = valid and reference is not None and reference.raw_bytes == b"0 "
            elif action == "forwarding_disabled":
                valid = valid and _integer(data.get("requested_elapsed_ns"))
            elif action == "forwarding_enabled":
                valid = valid and isinstance(data.get("reason"), str)
            elif action == "sender_submitted":
                valid = valid and _integer(data.get("sequence")) and data["sequence"] < 256
            if reference is not None and event.valid:
                valid = valid and event.monotonic_ns >= reference.monotonic_ns
            if not valid:
                self.issue(
                    "invalid_action",
                    event.file_name,
                    "Unsupported action or invalid action details/evidence reference.",
                    event.line_number,
                )
            events[index] = replace(event, valid=event.valid and valid)
        for index, event in enumerate(events):
            if event.data.get("action") in ("relay_forwarded", "relay_dropped"):
                key = _key(event.data.get("point"), event.data.get("record_index"))
                if key in duplicate_decisions:
                    self.issue(
                        "duplicate_action",
                        event.file_name,
                        "Multiple relay decisions refer to this record; neither is verified.",
                        event.line_number,
                    )
                    events[index] = replace(event, valid=False)
        return tuple(events)

    def gates(self, actions):
        intervals = []
        opened = None
        ambiguous = False
        for event in actions:
            action = event.data.get("action")
            if not event.valid or action not in ("forwarding_disabled", "forwarding_enabled"):
                continue
            if action == "forwarding_disabled" and opened is None and not ambiguous:
                opened = event
            elif action == "forwarding_enabled" and (opened is not None or ambiguous):
                if not ambiguous:
                    intervals.append(GateInterval(opened, event))
                opened = None
                ambiguous = False
            else:
                if action == "forwarding_disabled":
                    opened = None
                    ambiguous = True
                self.issue(
                    "gate_sequence",
                    event.file_name,
                    "Gate transition has no matching previous state.",
                    event.line_number,
                )
        if opened is not None:
            intervals.append(GateInterval(opened, None))
            self.issue(
                "open_gate_interval",
                opened.file_name,
                "No validated closing transition; actual gate end is unknown.",
                opened.line_number,
                warning=True,
            )
        return tuple(intervals)

    def validate_counts(self, captures, actions, datagrams):
        counters = self.manifest.get("counters")
        if not isinstance(counters, dict):
            self.issue("missing_counters", "run.json", "No final counters object.", warning=True)
            return
        values = {
            "relay_observed": len(captures["relay-input"].records)
            if "relay-input" in captures
            else None,
            "receiver_observed": len(captures["receiver"].records)
            if "receiver" in captures
            else None,
            "relay_forwarded": sum(
                item.valid and item.data.get("action") == "relay_forwarded" for item in actions
            ),
            "relay_dropped": sum(
                item.valid and item.data.get("action") == "relay_dropped" for item in actions
            ),
        }
        for key, observed in values.items():
            declared = counters.get(key)
            if observed is not None and (_integer(declared) is False or declared != observed):
                self.issue(
                    "counter_mismatch",
                    "run.json",
                    f"Declared {key} does not match validated supplied evidence.",
                )
        if self.schema == SCHEMAS[0]:
            submitted = sum(
                item.valid and item.data.get("action") == "sender_submitted" for item in actions
            )
            if (
                not _integer(counters.get("sender_submitted"))
                or counters["sender_submitted"] != submitted
            ):
                self.issue(
                    "counter_mismatch",
                    "run.json",
                    "Declared sender_submitted differs from validated actions.",
                )
            if not _integer(counters.get("missed_emission_slots")):
                self.issue(
                    "invalid_counter",
                    "run.json",
                    "missed_emission_slots must be a non-negative integer; "
                    "it has no independent observation oracle.",
                )
        elif any(
            counters.get(key) is not None for key in ("sender_submitted", "missed_emission_slots")
        ):
            self.issue(
                "invalid_counter",
                "run.json",
                "External simulator emission counts must remain unknown (null).",
            )
        declared_datagrams = self.manifest.get("datagrams_observed")
        if isinstance(declared_datagrams, dict):
            for point in CAPTURE_POINTS:
                count = sum(item.valid and item.point == point for item in datagrams)
                if (
                    not _integer(declared_datagrams.get(point))
                    or declared_datagrams[point] != count
                ):
                    self.issue(
                        "counter_mismatch",
                        "run.json",
                        f"Declared datagram count at {point} differs from validated evidence.",
                    )

    def read(self) -> SavedRun:
        captures = {}
        for point in CAPTURE_POINTS:
            name = f"{point}.tlog"
            if name in self.files:
                captured = import_bytes(self.files[name].raw_bytes, source_name=name)
                captures[point] = captured
                if captured.traversal == "stopped":
                    self.issue(
                        "capture_stopped",
                        name,
                        "Capture parsing stopped; accepted prefix remains available.",
                    )
        datagrams = self.read_datagrams()
        observations = self.read_observations(captures, datagrams)
        actions = self.read_actions(observations, datagrams)
        gates = self.gates(actions)
        if self.manifest.get("outcome") == "running":
            self.issue(
                "unfinalized_run",
                "run.json",
                "Manifest declares a running, unfinalized run.",
                warning=True,
            )
        else:
            self.validate_counts(captures, actions, datagrams)
        return SavedRun(
            self.source_name,
            self.schema,
            MappingProxyType(self.manifest),
            MappingProxyType(self.files),
            MappingProxyType(captures),
            actions,
            observations,
            datagrams,
            tuple(self.issues),
            gates,
            self.origin,
            self.measurement,
        )


def load_run_files(
    files: Mapping[str, bytes], *, source_name: str = "saved experiment"
) -> SavedRun:
    """Read canonical artifact names and immutable bytes; never interpret a path in JSON.

    Unsupported schemas, malformed manifests and input-size violations raise
    ValueError. Damaged evidence retains usable prefixes with explicit issues.
    Fingerprints prove consistency of supplied bytes, not source authenticity.
    """
    if "run.json" not in files:
        raise ValueError("A saved Experiment requires run.json")
    if len(files) > len(KNOWN_FILES):
        raise ValueError("Too many saved Experiment files")
    total = 0
    for name, raw in files.items():
        if name not in FILE_LIMITS:
            raise ValueError(f"Unsupported saved Experiment file name: {name!r}")
        if not isinstance(raw, bytes):
            raise TypeError("Saved Experiment inputs must be immutable bytes")
        if len(raw) > FILE_LIMITS[name]:
            raise ValueError(f"Saved Experiment file exceeds size limit: {name}")
        total += len(raw)
    if total > MAX_RUN_BYTES:
        raise ValueError("Saved Experiment exceeds the 64 MiB total size limit")
    return _Reader(files, source_name).read()


def load_run_directory(path: Path) -> SavedRun:
    """Read only the fixed known files, rejecting symlinks and non-regular files."""
    path = Path(path)
    files = {}
    total = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    root = os.open(path, flags | os.O_DIRECTORY)
    try:
        for name, limit in FILE_LIMITS.items():
            directory = root
            owned_directory = None
            try:
                basename = name
                if "/" in name:
                    dirname, basename = name.split("/")
                    owned_directory = os.open(dirname, flags | os.O_DIRECTORY, dir_fd=root)
                    directory = owned_directory
                descriptor = os.open(basename, flags, dir_fd=directory)
            except FileNotFoundError:
                continue
            finally:
                if owned_directory is not None:
                    os.close(owned_directory)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"Saved Experiment artifact is not a regular file: {name}")
                if metadata.st_size > limit:
                    raise ValueError(f"Saved Experiment file exceeds size limit: {name}")
                if total + metadata.st_size > MAX_RUN_BYTES:
                    raise ValueError("Saved Experiment exceeds the 64 MiB total size limit")
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    raw = stream.read(min(limit, MAX_RUN_BYTES - total) + 1)
                if len(raw) > limit:
                    raise ValueError(f"Saved Experiment file exceeds size limit: {name}")
                total += len(raw)
                if total > MAX_RUN_BYTES:
                    raise ValueError("Saved Experiment exceeds the 64 MiB total size limit")
                files[name] = raw
            finally:
                os.close(descriptor)
    finally:
        os.close(root)
    return load_run_files(files, source_name=path.name)
