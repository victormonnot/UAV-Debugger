"""Bounded comparison of two saved runs on explicit measurement-relative windows.

This module only consumes validated file evidence. It neither imports execution
modules nor matches frames or synchronizes clocks between runs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .model import Record
from .saved_run import CAPTURE_POINTS, Observation, SavedRun

ROLES = ("baseline", "blackout")
MAX_MEASUREMENT_NS = 60_000_000_000
SITL_PROFILE = "arducopter-4.6.3-linux-x86_64-v1"
_OPTIONAL_DIFFERENCES = {
    "scenario",
    "duration_s",
    "blackout_at_s",
    "blackout_duration_s",
    "startup_timeout_s",
}


@dataclass(frozen=True, slots=True)
class ComparisonIssue:
    code: str
    message: str
    run_role: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigurationDifference:
    field: str
    baseline: object
    blackout: object
    blocking: bool


@dataclass(frozen=True, slots=True)
class ComparedObservation:
    observation: Observation
    record: Record
    measurement_ns: int


@dataclass(frozen=True, slots=True)
class ObservationInterval:
    previous: ComparedObservation
    current: ComparedObservation
    duration_ns: int


@dataclass(frozen=True, slots=True)
class PointMetrics:
    count: int
    rate_hz: float
    observations: tuple[ComparedObservation, ...]
    longest_interval: ObservationInterval | None

    @property
    def first(self) -> ComparedObservation | None:
        return self.observations[0] if self.observations else None

    @property
    def last(self) -> ComparedObservation | None:
        return self.observations[-1] if self.observations else None


@dataclass(frozen=True, slots=True)
class ComparisonGate:
    start_ns: int
    end_ns: int | None
    start_line: int
    end_line: int | None

    @property
    def duration_ns(self) -> int | None:
        return None if self.end_ns is None else self.end_ns - self.start_ns


@dataclass(frozen=True, slots=True)
class RunComparison:
    baseline: SavedRun
    blackout: SavedRun
    source: tuple[int, int]
    message_type: str
    start_ns: int
    end_ns: int | None
    issues: tuple[ComparisonIssue, ...]
    differences: tuple[ConfigurationDifference, ...]
    metrics: Mapping[str, Mapping[str, PointMetrics]]
    gates: Mapping[str, tuple[ComparisonGate, ...]]

    @property
    def comparable(self) -> bool:
        return not self.issues

    @property
    def duration_ns(self) -> int | None:
        return (
            None
            if self.end_ns is None or self.end_ns <= self.start_ns
            else self.end_ns - self.start_ns
        )


def _source_kind(run: SavedRun) -> object:
    fallback = "synthetic" if run.schema == "uav-debugger-experiment-v1" else None
    return run.requested.get("source", fallback)


def _duration_ns(value: object) -> int | None:
    if type(value) not in (int, float) or not 0.1 <= value <= 60 or not math.isfinite(value):
        return None
    return round(value * 1_000_000_000)


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _normalized_argv(value: object, defaults_path: object) -> tuple[str, ...] | None:
    """Remove only changing run locations and the assigned loopback telemetry port."""
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        return None
    if (
        not isinstance(defaults_path, str)
        or not defaults_path.endswith("/profile.parm")
        or "," in defaults_path
        or value.count("--defaults") != 1
        or value.count("--serial1") != 1
    ):
        return None
    result = []
    index = 1  # The declared executable identity is checked separately.
    while index < len(value):
        item = value[index]
        if item in ("--defaults", "--serial1"):
            if index + 1 >= len(value):
                return None
            argument = value[index + 1]
            if item == "--defaults" and argument != defaults_path:
                return None
            if item == "--serial1":
                prefix = "udpclient:127.0.0.1:"
                port = argument.removeprefix(prefix)
                if not argument.startswith(prefix) or not port.isascii() or not port.isdigit():
                    return None
                if len(port) > 5 or not 1 <= int(port) <= 65535:
                    return None
                result.extend((item, prefix + "<assigned-port>"))
            index += 2
        else:
            result.append(item)
            index += 1
    if "--serial1" not in result:
        return None
    return tuple(result)


def _settings(run: SavedRun, role: str, issues: list[ComparisonIssue]) -> dict[str, object]:
    settings = {f"requested.{key}": value for key, value in run.requested.items()}
    kind = _source_kind(run)
    settings["requested.source"] = kind
    # These were absent from the original synthetic v1 schema.
    settings.setdefault("requested.startup_timeout_s", None)
    settings["capture_profile"] = run.manifest.get("capture_profile")
    environment = run.manifest.get("environment")
    if isinstance(environment, Mapping):
        settings.update({f"environment.{key}": value for key, value in environment.items()})
    expected = "synthetic" if run.schema == "uav-debugger-experiment-v1" else "arducopter-sitl"
    if kind != expected:
        issues.append(
            ComparisonIssue("source_profile", "Unsupported source for this saved-run schema.", role)
        )
    if kind != "arducopter-sitl":
        return settings
    simulator = run.manifest.get("simulator")
    simulator = simulator if isinstance(simulator, Mapping) else {}
    binary = simulator.get("binary")
    binary = binary if isinstance(binary, Mapping) else {}
    if binary.get("profile") != SITL_PROFILE or not _digest(binary.get("sha256")):
        issues.append(
            ComparisonIssue(
                "simulator_identity",
                "A supported declared SITL profile and executable SHA-256 are required.",
                role,
            )
        )
    for key in ("profile", "sha256", "version", "git_commit", "size_bytes"):
        settings[f"simulator.binary.{key}"] = binary.get(key)
    parameters = run.files.get("simulator/profile.parm")
    if parameters is None or simulator.get("defaults_sha256") != parameters.sha256:
        issues.append(
            ComparisonIssue(
                "simulator_parameters",
                "Requested simulator parameters lack a matching saved artifact fingerprint.",
                role,
            )
        )
    settings["simulator.parameters_sha256"] = None if parameters is None else parameters.sha256
    arguments = simulator.get("argv")
    argv = _normalized_argv(arguments, simulator.get("defaults_path"))
    if argv is not None and binary.get("path") is not None and arguments[0] != binary["path"]:
        argv = None
    if argv is None:
        issues.append(
            ComparisonIssue(
                "simulator_arguments",
                "The declared simulator arguments do not identify the supported loopback profile.",
                role,
            )
        )
    settings["simulator.arguments"] = argv
    return settings


def _differences(baseline: dict, blackout: dict) -> tuple[ConfigurationDifference, ...]:
    differences = []
    for field in sorted(baseline.keys() | blackout.keys()):
        before, after = baseline.get(field), blackout.get(field)
        if before == after:
            continue
        blocking = not (
            field.startswith("environment.")
            or field in {f"requested.{key}" for key in _OPTIONAL_DIFFERENCES}
        )
        differences.append(ConfigurationDifference(field, before, after, blocking))
    return tuple(differences)


def available_selections(
    baseline: SavedRun, blackout: SavedRun
) -> Mapping[tuple[int, int], tuple[str, ...]]:
    """Common source/type labels with valid relay-input observation references."""
    sets = []
    for run in (baseline, blackout):
        values = set()
        capture = run.captures.get("relay-input")
        if capture is not None:
            for event in run.observations:
                if event.valid and event.point == "relay-input":
                    record = capture.records[event.record_index]
                    label = record.message_name or f"UNKNOWN_{record.message_id}"
                    values.add(((record.system_id, record.component_id), label))
        sets.append(values)
    common = sets[0] & sets[1]
    return MappingProxyType(
        {
            source: tuple(sorted(label for found, label in common if found == source))
            for source in sorted({source for source, _ in common})
        }
    )


def _project_gates(run: SavedRun) -> tuple[ComparisonGate, ...]:
    origin = run.measurement_monotonic_ns
    if origin is None:
        return ()
    return tuple(
        ComparisonGate(
            gate.start.monotonic_ns - origin,
            None if gate.end is None else gate.end.monotonic_ns - origin,
            gate.start.line_number,
            None if gate.end is None else gate.end.line_number,
        )
        for gate in run.gate_intervals
    )


def _eligibility(
    run: SavedRun,
    role: str,
    start_ns: int,
    end_ns: int | None,
    duration_ns: int | None,
    issues: list[ComparisonIssue],
) -> None:
    def reject(code: str, message: str):
        issues.append(ComparisonIssue(code, message, role))

    if run.declared_outcome != "completed":
        reject("run_not_completed", "Comparison requires a run declared completed.")
    if run.evidence_status != "consistent":
        reject("run_evidence", "Comparison requires consistent supplied run evidence.")
    if run.requested.get("scenario") != role:
        reject("scenario", f"The {role} input must declare the {role} scenario.")
    if duration_ns is None:
        reject("duration", "A bounded numeric requested measurement duration is required.")
    if run.measurement_monotonic_ns is None or run.origin_monotonic_ns is None:
        reject("measurement_origin", "A validated measurement origin is required.")
    if any(point not in run.captures for point in CAPTURE_POINTS):
        reject("capture_unavailable", "Both saved capture points are required.")
    starts = [
        event
        for event in run.actions
        if event.valid and event.data.get("action") == "producer_started"
    ]
    stops = [
        event
        for event in run.actions
        if event.valid and event.data.get("action") == "producer_stopped"
    ]
    if len(starts) != 1 or len(stops) != 1:
        reject("production_evidence", "Exactly one validated producer start and stop are required.")
    elif starts[0].monotonic_ns > stops[0].monotonic_ns:
        reject("production_evidence", "Production stop precedes its start.")
    else:
        started = starts[0].monotonic_ns
        evidence_times = [event.monotonic_ns for event in run.observations if event.valid]
        evidence_times.extend(
            event.monotonic_ns
            for event in run.actions
            if event.valid and event.data.get("action") == "sender_submitted"
        )
        if evidence_times and started > min(evidence_times):
            reject(
                "production_evidence",
                "Recorded observations or submissions precede producer start.",
            )
        measurement = run.measurement_monotonic_ns
        if run.schema == "uav-debugger-experiment-v2" and measurement is not None:
            if started > measurement:
                reject("production_evidence", "SITL producer start follows its measurement origin.")
        if (
            measurement is not None
            and end_ns is not None
            and (stops[0].monotonic_ns - measurement < end_ns or started - measurement >= end_ns)
        ):
            reject(
                "production_coverage",
                "Recorded production does not reach or cover the comparison window's end.",
            )
    if (
        end_ns is not None
        and duration_ns is not None
        and (end_ns > duration_ns or start_ns >= duration_ns)
    ):
        reject(
            "window_coverage",
            "The comparison window exceeds the requested measurement phase; "
            "startup and drain are excluded.",
        )
    if role == "baseline" and run.gate_intervals:
        reject("baseline_gate", "The baseline contains an applied forwarding interruption.")
    if role == "blackout":
        gates = run.gate_intervals
        requested_at = _duration_ns(run.requested.get("blackout_at_s"))
        requested_length = run.requested.get("blackout_duration_s")
        if (
            requested_at is None
            or type(requested_length) not in (int, float)
            or requested_length != 2
            or duration_ns is None
            or requested_at + 2_100_000_000 > duration_ns
        ):
            reject(
                "blackout_request",
                "The supported blackout requests a two-second gate with time before and after it.",
            )
        if len(gates) != 1 or gates[0].end is None or gates[0].duration_ns < 2_000_000_000:
            reject(
                "blackout_gate",
                "The blackout requires one actual, closed interruption "
                "lasting at least two seconds.",
            )
        elif run.measurement_monotonic_ns is not None:
            start = gates[0].start.monotonic_ns - run.measurement_monotonic_ns
            end = gates[0].end.monotonic_ns - run.measurement_monotonic_ns
            if requested_at is not None and start < requested_at:
                reject(
                    "blackout_gate", "The applied gate precedes its requested measurement deadline."
                )
            if duration_ns is not None and end > duration_ns:
                reject(
                    "blackout_gate",
                    "The applied gate extends beyond the requested measurement phase.",
                )


def _metrics(
    run: SavedRun,
    point: str,
    source: tuple[int, int],
    message_type: str,
    start_ns: int,
    end_ns: int,
) -> PointMetrics:
    capture = run.captures[point]
    origin = run.measurement_monotonic_ns
    observations = []
    for event in run.observations:
        if not event.valid or event.point != point:
            continue
        record = capture.records[event.record_index]
        label = record.message_name or f"UNKNOWN_{record.message_id}"
        elapsed = event.monotonic_ns - origin
        if (
            (record.system_id, record.component_id) == source
            and label == message_type
            and start_ns <= elapsed < end_ns
        ):
            observations.append(ComparedObservation(event, record, elapsed))
    longest = None
    for previous, current in zip(observations, observations[1:], strict=False):
        interval = ObservationInterval(
            previous, current, current.measurement_ns - previous.measurement_ns
        )
        if longest is None or interval.duration_ns > longest.duration_ns:
            longest = interval
    return PointMetrics(
        len(observations),
        len(observations) * 1_000_000_000 / (end_ns - start_ns),
        tuple(observations),
        longest,
    )


def compare_runs(
    baseline: SavedRun,
    blackout: SavedRun,
    *,
    source: tuple[int, int] = (1, 1),
    message_type: str = "ATTITUDE",
    start_ns: int = 0,
    end_ns: int | None = None,
) -> RunComparison:
    """Compare one baseline/blackout pair or retain explicit reasons it is blocked.

    Endpoints are exact integer nanoseconds, with start included and end excluded.
    The default end is the shorter requested measurement duration. Profiles and
    production evidence must support that entire window. No aggregate metric is
    returned for incompatible, incomplete or invalid evidence. A legitimate zero
    count is possible inside a covered window; an unavailable capture is not zero.
    """
    if (
        not isinstance(source, tuple)
        or len(source) != 2
        or any(type(value) is not int or not 0 <= value <= 255 for value in source)
    ):
        raise ValueError("source must contain two integer MAVLink identities from 0 to 255")
    if not isinstance(message_type, str) or not message_type or len(message_type) > 256:
        raise ValueError("message_type must be a nonempty label of at most 256 characters")
    if type(start_ns) is not int or not 0 <= start_ns < MAX_MEASUREMENT_NS:
        raise ValueError(
            "start_ns must be integer measurement nanoseconds from 0 to below 60 seconds"
        )
    if end_ns is not None and (
        type(end_ns) is not int or not start_ns < end_ns <= MAX_MEASUREMENT_NS
    ):
        raise ValueError("end_ns must be an integer after start_ns and at most 60 seconds")
    issues = []
    runs = dict(zip(ROLES, (baseline, blackout), strict=True))
    durations = {role: _duration_ns(run.requested.get("duration_s")) for role, run in runs.items()}
    if end_ns is None and all(value is not None for value in durations.values()):
        end_ns = min(durations.values())
    if end_ns is not None and end_ns <= start_ns:
        issues.append(
            ComparisonIssue(
                "empty_window", "The common measurement window does not extend beyond its start."
            )
        )
    settings = {role: _settings(run, role, issues) for role, run in runs.items()}
    differences = _differences(settings["baseline"], settings["blackout"])
    for difference in differences:
        if difference.blocking:
            issues.append(
                ComparisonIssue(
                    "configuration_difference", f"Incompatible setting: {difference.field}."
                )
            )
    for role, run in runs.items():
        _eligibility(run, role, start_ns, end_ns, durations[role], issues)
    if message_type not in available_selections(baseline, blackout).get(source, ()):
        issues.append(
            ComparisonIssue(
                "selection_unavailable",
                "The selected source and message type lack valid relay-input "
                "observations in both runs.",
            )
        )
    metrics = {}
    if not issues:
        metrics = {
            role: MappingProxyType(
                {
                    point: _metrics(run, point, source, message_type, start_ns, end_ns)
                    for point in CAPTURE_POINTS
                }
            )
            for role, run in runs.items()
        }
    return RunComparison(
        baseline,
        blackout,
        source,
        message_type,
        start_ns,
        end_ns,
        tuple(issues),
        differences,
        MappingProxyType(metrics),
        MappingProxyType({role: _project_gates(run) for role, run in runs.items()}),
    )
