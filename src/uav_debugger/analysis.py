"""Exact source/time selection and bounded observations over imported evidence."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .model import ImportResult, Record

MAX_TIMESTAMP_US = (1 << 64) - 1


def _unsigned_integer(value: object, maximum: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}.")


@dataclass(frozen=True, slots=True)
class Selection:
    """Inclusive filters; None selects all values and an empty tuple selects none."""

    sources: tuple[tuple[int, int], ...] | None = None
    message_ids: tuple[int, ...] | None = None
    start_us: int | None = None
    end_us: int | None = None

    def __post_init__(self) -> None:
        if self.sources is not None:
            if not isinstance(self.sources, tuple):
                raise ValueError("sources must be a tuple of (system_id, component_id) tuples.")
            for source in self.sources:
                if not isinstance(source, tuple) or len(source) != 2:
                    raise ValueError("Each source must be a (system_id, component_id) tuple.")
                _unsigned_integer(source[0], 255, "system_id")
                _unsigned_integer(source[1], 255, "component_id")
        if self.message_ids is not None:
            if not isinstance(self.message_ids, tuple):
                raise ValueError("message_ids must be a tuple of message identifiers.")
            for message_id in self.message_ids:
                _unsigned_integer(message_id, 0xFFFFFF, "message_id")
        if self.start_us is not None:
            _unsigned_integer(self.start_us, MAX_TIMESTAMP_US, "start_us")
        if self.end_us is not None:
            _unsigned_integer(self.end_us, MAX_TIMESTAMP_US, "end_us")
        if self.start_us is not None and self.end_us is not None and self.start_us > self.end_us:
            raise ValueError("start_us must not exceed end_us.")


def _matches(record: Record, selection: Selection) -> bool:
    return (
        (selection.sources is None or (record.system_id, record.component_id) in selection.sources)
        and (selection.message_ids is None or record.message_id in selection.message_ids)
        and (selection.start_us is None or record.timestamp_us >= selection.start_us)
        and (selection.end_us is None or record.timestamp_us <= selection.end_us)
    )


def select_records(result: ImportResult, selection: Selection) -> tuple[Record, ...]:
    """Filter with exact integer comparisons, retaining the original file order."""
    return tuple(record for record in result.records if _matches(record, selection))


def seconds_to_timestamp(text: str, origin_us: int) -> int:
    """Add exact decimal seconds to an integer origin without rounding.

    Decimal's tuple representation avoids arithmetic context rounding and large
    exponentiation. Nonzero fractions smaller than one microsecond are rejected;
    additional decimal places containing only zero are allowed.
    """
    _unsigned_integer(origin_us, MAX_TIMESTAMP_US, "origin_us")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Seconds must be a finite decimal number.")
    try:
        seconds = Decimal(text.strip())
    except InvalidOperation as error:
        raise ValueError("Seconds must be a finite decimal number.") from error
    if not seconds.is_finite():
        raise ValueError("Seconds must be a finite decimal number.")
    sign, digits, exponent = seconds.as_tuple()
    if not any(digits):
        return origin_us

    power = exponent + 6
    if power < 0:
        fractional_places = -power
        if fractional_places >= len(digits) or any(digits[-fractional_places:]):
            raise ValueError("Seconds must resolve to an exact whole number of microseconds.")
        digits = digits[:-fractional_places]
        power = 0
    # An in-range origin and result cannot differ by more than 20 decimal digits.
    if len(digits) + power > 20:
        raise ValueError("The resulting timestamp is outside the unsigned 64-bit range.")
    magnitude = 0
    for digit in digits:
        magnitude = magnitude * 10 + digit
    delta_us = magnitude * (10**power)
    timestamp_us = origin_us + (-delta_us if sign else delta_us)
    _unsigned_integer(timestamp_us, MAX_TIMESTAMP_US, "timestamp_us")
    return timestamp_us


def timestamp_to_seconds(timestamp_us: int, origin_us: int) -> str:
    """Format the exact signed seconds offset, without float conversion."""
    _unsigned_integer(timestamp_us, MAX_TIMESTAMP_US, "timestamp_us")
    _unsigned_integer(origin_us, MAX_TIMESTAMP_US, "origin_us")
    difference = timestamp_us - origin_us
    seconds, microseconds = divmod(abs(difference), 1_000_000)
    fraction = f".{microseconds:06d}".rstrip("0") if microseconds else ""
    return f"{'-' if difference < 0 else ''}{seconds}{fraction}"


@dataclass(frozen=True, slots=True)
class ObservationInterval:
    """A same-source/type observation pair; None means no interval is established."""

    previous_index: int
    index: int
    system_id: int
    component_id: int
    message_id: int
    delta_us: int | None


def observed_intervals(
    result: ImportResult, selection: Selection
) -> tuple[ObservationInterval, ...]:
    """Find successive selected observations per source/type in file order.

    Every original record participates in clock-regression detection, even when
    filters hide it. A pair crossing a regression, or containing an opaque or
    checksum-unverified endpoint, has no established interval. A repeated clock
    value within one segment yields zero. These intervals are not packet losses.
    """
    previous: dict[tuple[int, int, int], tuple[Record, int]] = {}
    intervals = []
    clock_segment = 0
    last_timestamp = None
    for record in result.records:
        if last_timestamp is not None and record.timestamp_us < last_timestamp:
            clock_segment += 1
        last_timestamp = record.timestamp_us
        if not _matches(record, selection):
            continue
        key = (record.system_id, record.component_id, record.message_id)
        if key in previous:
            earlier, earlier_segment = previous[key]
            established = (
                earlier_segment == clock_segment
                and earlier.fields is not None
                and record.fields is not None
                and earlier.checksum_status == "valid"
                and record.checksum_status == "valid"
            )
            intervals.append(
                ObservationInterval(
                    previous_index=earlier.index,
                    index=record.index,
                    system_id=record.system_id,
                    component_id=record.component_id,
                    message_id=record.message_id,
                    delta_us=record.timestamp_us - earlier.timestamp_us if established else None,
                )
            )
        previous[key] = (record, clock_segment)
    return tuple(intervals)
