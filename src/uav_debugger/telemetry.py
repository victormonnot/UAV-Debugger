"""Bounded ATTITUDE observations without clock alignment or angle transformations."""

import math
from dataclasses import dataclass
from typing import Literal

from .analysis import Selection, _matches
from .model import ImportResult, Record

ATTITUDE_FIELDS = ("roll", "pitch", "yaw")
ATTITUDE_POINT_LIMIT = 5000


@dataclass(frozen=True, slots=True)
class AttitudeSample:
    record: Record
    segment: int
    roll: float | None
    pitch: float | None
    yaw: float | None


@dataclass(frozen=True, slots=True)
class AttitudeView:
    status: Literal["ready", "empty", "multiple_sources", "too_many"]
    sources: tuple[tuple[int, int], ...]
    samples: tuple[AttitudeSample, ...]
    record_count: int
    max_gap_us: int
    point_limit: int = ATTITUDE_POINT_LIMIT

    @property
    def source(self) -> tuple[int, int] | None:
        return self.sources[0] if len(self.sources) == 1 else None


def _finite_field(record: Record, name: str) -> float | None:
    if (
        record.checksum_status != "valid"
        or record.message_name != "ATTITUDE"
        or record.fields is None
    ):
        return None
    value = record.fields.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def build_attitude_view(
    result: ImportResult, selection: Selection, *, max_gap_us: int = 1_000_000
) -> AttitudeView:
    """Retain one source's selected ATTITUDE records, or an explicit display limit.

    All original records participate in capture-clock regression detection, even
    when hidden by the selection. At most 5,000 records are extracted for display;
    exceeding the limit requires a narrower selection rather than decimation.
    """
    if isinstance(max_gap_us, bool) or not isinstance(max_gap_us, int) or max_gap_us <= 0:
        raise ValueError("Maximum line gap must be a positive integer number of microseconds.")

    sources = set()
    count = 0
    for record in result.records:
        if record.message_id == 30 and _matches(record, selection):
            sources.add((record.system_id, record.component_id))
            count += 1
    ordered_sources = tuple(sorted(sources))
    if not count:
        status = "empty"
    elif len(sources) != 1:
        status = "multiple_sources"
    elif count > ATTITUDE_POINT_LIMIT:
        status = "too_many"
    else:
        status = "ready"
    if status != "ready":
        return AttitudeView(status, ordered_sources, (), count, max_gap_us)

    samples = []
    segment = 0
    previous_timestamp = None
    for record in result.records:
        if previous_timestamp is not None and record.timestamp_us < previous_timestamp:
            segment += 1
        previous_timestamp = record.timestamp_us
        if record.message_id == 30 and _matches(record, selection):
            samples.append(
                AttitudeSample(
                    record=record,
                    segment=segment,
                    roll=_finite_field(record, "roll"),
                    pitch=_finite_field(record, "pitch"),
                    yaw=_finite_field(record, "yaw"),
                )
            )
    return AttitudeView(status, ordered_sources, tuple(samples), count, max_gap_us)


def attitude_plot_summary(view: AttitudeView) -> dict[str, object]:
    """Describe actual display coverage and settings using JSON-compatible data."""
    return {
        "status": view.status,
        "sources": [list(source) for source in view.sources],
        "source": list(view.source) if view.source is not None else None,
        "record_count": view.record_count,
        "plotted_record_count": sum(
            any(getattr(sample, field) is not None for field in ATTITUDE_FIELDS)
            for sample in view.samples
        ),
        "point_limit": view.point_limit,
        "max_gap_us": view.max_gap_us,
        "unit": "rad",
        "fields": list(ATTITUDE_FIELDS),
        "invalid_value_counts": {
            field: sum(getattr(sample, field) is None for sample in view.samples)
            for field in ATTITUDE_FIELDS
        }
        if view.status == "ready"
        else None,
    }
