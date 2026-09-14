"""Strict, file-only reader for one QGroundControl-style recording profile."""

import hashlib
import os
import stat
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType

from pymavlink.dialects.v10 import common as common_v1
from pymavlink.dialects.v20 import common as common_v2
from pymavlink.generator.mavcrc import x25crc

from .model import ImportIssue, ImportResult, Record

PROFILE = "qgc-timestamped-mavlink-v1"
DIALECT = "common"
MAX_INPUT_BYTES = 10 * 1024 * 1024


class InputTooLargeError(ValueError):
    """Input exceeds the explicit in-memory importer limit."""


class _RecordError(Exception):
    def __init__(self, code: str, offset: int, message: str):
        self.code = code
        self.offset = offset
        self.message = message
        super().__init__(message)


def _check_size(size: int, max_bytes: int) -> None:
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer.")
    if size > max_bytes:
        raise InputTooLargeError(f"Input exceeds the {max_bytes}-byte size limit.")


def import_file(path: str | os.PathLike[str], *, max_bytes: int = MAX_INPUT_BYTES) -> ImportResult:
    """Read a regular local file without modifying it or interpreting transport URLs.

    File access failures raise OSError; oversized inputs raise InputTooLargeError.
    Content errors instead return inspectable evidence with an explicit outcome.
    """
    source = Path(path)
    info = source.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Input must be a regular local file.")
    _check_size(info.st_size, max_bytes)
    with source.open("rb") as stream:
        opened_info = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened_info.st_mode):
            raise ValueError("Input must be a regular local file.")
        _check_size(opened_info.st_size, max_bytes)
        data = stream.read(max_bytes + 1)
    return import_bytes(data, source_name=source.name, max_bytes=max_bytes)


def _read_record(data: bytes, offset: int, index: int, decoder: common_v2.MAVLink) -> Record:
    if len(data) - offset < 8:
        raise _RecordError("incomplete_timestamp", offset, "Incomplete 8-byte capture timestamp.")
    timestamp_us = int.from_bytes(data[offset : offset + 8], "big")
    start = offset + 8
    if start == len(data):
        raise _RecordError("incomplete_frame", start, "Capture timestamp has no MAVLink frame.")
    magic = data[start]
    if magic not in (0xFE, 0xFD):
        raise _RecordError(
            "invalid_magic", start, "Expected a MAVLink 1 or MAVLink 2 frame marker."
        )
    wire_version = 1 if magic == 0xFE else 2
    header_length = 6 if wire_version == 1 else 10
    if len(data) - start < header_length:
        raise _RecordError("incomplete_frame", start, "Incomplete MAVLink frame header.")
    payload_length = data[start + 1]
    if wire_version == 2:
        flags = data[start + 2]
        if flags & ~1:
            raise _RecordError(
                "unsupported_flags", start + 2, "Unsupported MAVLink 2 incompatibility flags."
            )
        if flags & 1:
            raise _RecordError(
                "unsupported_signature",
                start + 2,
                "Signed MAVLink frames are outside this profile.",
            )
        sequence, system_id, component_id = data[start + 4 : start + 7]
        message_id = int.from_bytes(data[start + 7 : start + 10], "little")
    else:
        sequence, system_id, component_id, message_id = data[start + 2 : start + 6]

    end = start + header_length + payload_length + 2
    if end > len(data):
        raise _RecordError("incomplete_frame", start, "Incomplete MAVLink payload or checksum.")
    frame = data[start:end]
    definitions = common_v1.mavlink_map if wire_version == 1 else common_v2.mavlink_map
    definition = definitions.get(message_id)
    if payload_length == 0:
        raise _RecordError("invalid_length", start + 1, "A MAVLink payload must contain a byte.")
    fields = None
    name = None
    checksum_status = "unverified"
    if definition is not None:
        expected_length = definition.unpacker.size
        if (wire_version == 1 and payload_length != expected_length) or (
            wire_version == 2 and payload_length > expected_length
        ):
            raise _RecordError(
                "invalid_length",
                start + 1,
                "Payload length is outside the selected message definition.",
            )
        # pymavlink's decoder can skip CRC via MAV_IGNORE_CRC. Always validate
        # explicitly here, without changing process-wide library configuration.
        crc = x25crc(frame[1:-2])
        crc.accumulate(bytes([definition.crc_extra]))
        if int.from_bytes(frame[-2:], "little") != crc.crc:
            raise _RecordError(
                "checksum_mismatch", end - 2, "Frame checksum does not match the common definition."
            )
        try:
            message = decoder.decode(bytearray(frame))
        except common_v2.MAVError as error:
            raise _RecordError(
                "decode_error", start, f"MAVLink decoding failed: {error}"
            ) from error
        name = message.get_type()
        # The v2 decoder also pads extension fields on v1 input. Exclude fields
        # absent from the v1 definition rather than inventing recorded values.
        # Arrays become tuples so callers cannot mutate decoded evidence in place.
        fields = MappingProxyType(
            {
                key: tuple(value) if isinstance(value, list) else value
                for key, value in message.to_dict().items()
                if key in definition.fieldnames
            }
        )
        checksum_status = "valid"
    return Record(
        index=index,
        offset=offset,
        timestamp_us=timestamp_us,
        wire_version=wire_version,
        sequence=sequence,
        system_id=system_id,
        component_id=component_id,
        message_id=message_id,
        message_name=name,
        fields=fields,
        checksum_status=checksum_status,
        raw_frame=frame,
    )


def import_bytes(
    data: bytes, *, source_name: str = "<bytes>", max_bytes: int = MAX_INPUT_BYTES
) -> ImportResult:
    """Inspect immutable input bytes using the explicit timestamped-log profile.

    Timestamps are unsigned Unix microseconds under this profile's host-logging
    convention, with no claim about clock accuracy, resolution or synchronization.
    Unknown IDs remain opaque; framing errors stop at the affected record.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be immutable bytes.")
    _check_size(len(data), max_bytes)
    records: list[Record] = []
    issues: list[ImportIssue] = []
    offset = 0
    traversal = "complete" if data else "empty"
    decoder = common_v2.MAVLink(None)
    if not data:
        issues.append(ImportIssue("empty_input", "error", 0, "Input contains no records.", 0))
    while offset < len(data):
        try:
            record = _read_record(data, offset, len(records), decoder)
        except _RecordError as error:
            issues.append(
                ImportIssue(error.code, "error", error.offset, error.message, len(records))
            )
            traversal = "stopped"
            break
        if record.fields is None:
            issues.append(
                ImportIssue(
                    "unknown_message",
                    "warning",
                    record.frame_offset,
                    "Message definition is unavailable; "
                    "fields, checksum and framing are unverified.",
                    record.index,
                )
            )
        if records and record.timestamp_us <= records[-1].timestamp_us:
            repeated = record.timestamp_us == records[-1].timestamp_us
            issues.append(
                ImportIssue(
                    "timestamp_repeated" if repeated else "timestamp_regression",
                    "warning",
                    offset,
                    "Capture timestamp repeats the previous record."
                    if repeated
                    else "Capture timestamp precedes the previous record; file order is preserved.",
                    record.index,
                )
            )
        records.append(record)
        offset = record.end_offset
    return ImportResult(
        source_name=source_name,
        raw_bytes=data,
        sha256=hashlib.sha256(data).hexdigest(),
        profile=PROFILE,
        dialect=DIALECT,
        decoder_version=version("pymavlink"),
        records=tuple(records),
        issues=tuple(issues),
        traversal=traversal,
        consumed_bytes=offset,
    )
