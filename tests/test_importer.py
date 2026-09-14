"""Behavioral checks using fixed expectations and independently encoded frames.

The frame helpers use the documented wire layout and a bitwise CRC-16/MCRF4XX,
without importing pymavlink or the production importer's encoding helpers.
See https://mavlink.io/en/guide/serialization.html.
"""

import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from uav_debugger import InputTooLargeError, import_bytes, import_file

FIXTURES = Path(__file__).parent / "fixtures"
EPOCH_US = 1_700_000_000_000_000
HEARTBEAT_FIELDS = {
    "custom_mode": 0x12345678,
    "type": 2,
    "autopilot": 3,
    "base_mode": 129,
    "system_status": 4,
    "mavlink_version": 3,
}
HEARTBEAT_PAYLOAD = struct.pack("<IBBBBB", 0x12345678, 2, 3, 129, 4, 3)


def _crc(data: bytes) -> bytes:
    remainder = 0xFFFF
    for value in data:
        remainder ^= value
        for _ in range(8):
            remainder = (remainder >> 1) ^ (0x8408 if remainder & 1 else 0)
    return remainder.to_bytes(2, "little")


def _frame(
    version=1,
    *,
    payload=HEARTBEAT_PAYLOAD,
    message_id=0,
    crc_extra=50,
    sequence=7,
    system_id=1,
    component_id=1,
    incompat_flags=0,
    compat_flags=0,
):
    if version == 1:
        header = bytes((len(payload), sequence, system_id, component_id, message_id))
        magic = b"\xfe"
    else:
        header = bytes(
            (len(payload), incompat_flags, compat_flags, sequence, system_id, component_id)
        ) + message_id.to_bytes(3, "little")
        magic = b"\xfd"
    checksum = _crc(header + payload + bytes((crc_extra,)))
    return magic + header + payload + checksum


def _record(frame, timestamp_us=EPOCH_US):
    return timestamp_us.to_bytes(8, "big") + frame


def _assert_stopped(result, data, prefix, code):
    assert result.traversal == "stopped"
    assert result.raw_bytes == data
    assert result.consumed_bytes == len(prefix)
    assert result.remaining_bytes == len(data) - len(prefix)
    assert len(result.records) == (1 if prefix else 0)
    errors = [issue for issue in result.issues if issue.severity == "error"]
    assert len(errors) == 1
    issue = errors[0]
    assert issue.code == code
    assert issue.record_index == len(result.records)
    assert len(prefix) <= issue.offset <= len(data)
    assert issue.message
    if prefix:
        assert result.records[0].raw_frame == prefix[8:]


def test_synthetic_recording_matches_independent_manifest():
    path = FIXTURES / "telemetry-gap.tlog"
    manifest = json.loads((FIXTURES / "telemetry-gap.expected.json").read_text())
    original = path.read_bytes()

    result = import_file(path)

    assert path.read_bytes() == original
    assert result.raw_bytes == original
    assert result.sha256 == manifest["sha256"] == hashlib.sha256(original).hexdigest()
    assert len(original) == manifest["size_bytes"]
    assert result.profile == manifest["profile"]
    assert result.dialect == manifest["dialect"] == "common"
    assert result.decoder_version
    assert result.traversal == "complete"
    assert result.consumed_bytes == len(original)
    assert result.remaining_bytes == 0
    assert result.decoded_count == manifest["record_count"]
    assert result.opaque_count == 0
    assert [(issue.code, issue.severity, issue.record_index) for issue in result.issues] == [
        ("timestamp_repeated", "warning", index) for index in (1, 3, 6, 9, 11)
    ]
    assert isinstance(result.records, tuple)
    assert len(result.records) == len(manifest["records"])

    for record, expected in zip(result.records, manifest["records"], strict=True):
        for attribute in (
            "index",
            "offset",
            "frame_offset",
            "timestamp_us",
            "wire_version",
            "sequence",
            "system_id",
            "component_id",
            "message_id",
            "message_name",
        ):
            assert getattr(record, attribute) == expected[attribute]
        assert record.raw_frame.hex() == expected["frame_hex"]
        assert len(record.raw_frame) == expected["frame_length"]
        assert record.end_offset == record.frame_offset + expected["frame_length"]
        assert original[record.frame_offset : record.end_offset] == record.raw_frame
        assert record.checksum_status == "valid"
        for field, value in expected["fields"].items():
            assert record.fields[field] == (
                pytest.approx(value) if isinstance(value, float) else value
            )

    source_one_attitude = [
        record.timestamp_us - EPOCH_US
        for record in result.records
        if record.system_id == 1 and record.message_id == 30
    ]
    assert source_one_attitude == [1_000_000, 5_000_000]
    source_two_attitude = [
        record.timestamp_us - EPOCH_US
        for record in result.records
        if record.system_id == 2 and record.message_id == 30
    ]
    assert source_two_attitude == [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]


@pytest.mark.parametrize("version", [1, 2])
def test_manual_heartbeat_decodes_without_losing_header_or_raw_bytes(version):
    frame = _frame(version, sequence=255, system_id=42, component_id=17)
    data = _record(frame)

    result = import_bytes(data, source_name="manual-recording.bin")

    assert result.source_name == "manual-recording.bin"
    assert result.traversal == "complete"
    assert result.issues == ()
    assert result.decoded_count == 1
    assert result.opaque_count == 0
    record = result.records[0]
    assert (record.index, record.offset, record.frame_offset, record.end_offset) == (
        0,
        0,
        8,
        len(data),
    )
    assert record.timestamp_us == EPOCH_US
    assert record.wire_version == version
    assert (record.sequence, record.system_id, record.component_id) == (255, 42, 17)
    assert (record.message_id, record.message_name) == (0, "HEARTBEAT")
    assert record.checksum_status == "valid"
    assert record.raw_frame == frame
    for field, expected in HEARTBEAT_FIELDS.items():
        assert record.fields[field] == expected


def test_mixed_wire_versions_and_components_keep_file_order():
    frames = [
        _frame(1, system_id=1, component_id=1, sequence=255),
        _frame(2, system_id=1, component_id=42, sequence=0),
        _frame(1, system_id=2, component_id=1, sequence=255),
    ]
    chunks = [_record(frame, EPOCH_US + index) for index, frame in enumerate(frames)]
    result = import_bytes(b"".join(chunks))

    assert result.traversal == "complete"
    assert result.issues == ()
    assert [(r.system_id, r.component_id) for r in result.records] == [(1, 1), (1, 42), (2, 1)]
    assert [r.wire_version for r in result.records] == [1, 2, 1]
    assert [r.sequence for r in result.records] == [255, 0, 255]
    assert [r.offset for r in result.records] == [0, len(chunks[0]), sum(map(len, chunks[:2]))]


@pytest.mark.parametrize("payload", [b"\x01", struct.pack("<I6f", 1, 0, 0, 0, 0, 0, 0)])
def test_mavlink2_zero_extension_preserves_the_short_original_frame(payload):
    frame = _frame(2, message_id=30, crc_extra=39, payload=payload)
    result = import_bytes(_record(frame))

    assert result.traversal == "complete"
    assert result.issues == ()
    record = result.records[0]
    assert record.raw_frame == frame
    assert record.message_name == "ATTITUDE"
    assert record.timestamp_us == EPOCH_US
    assert record.fields["time_boot_ms"] == 1
    for field in ("roll", "pitch", "yaw", "rollspeed", "pitchspeed", "yawspeed"):
        assert record.fields[field] == 0.0


def test_mavlink1_uses_base_length_when_message_has_mavlink2_extensions():
    # GPS_RAW_INT has a 30-byte base payload; later extension fields are omitted in v1.
    frame = _frame(1, message_id=24, crc_extra=24, payload=bytes(30))
    result = import_bytes(_record(frame))

    assert result.traversal == "complete"
    assert result.decoded_count == 1
    assert result.records[0].message_name == "GPS_RAW_INT"
    assert result.records[0].checksum_status == "valid"
    assert result.records[0].fields == {
        "time_usec": 0,
        "fix_type": 0,
        "lat": 0,
        "lon": 0,
        "alt": 0,
        "eph": 0,
        "epv": 0,
        "vel": 0,
        "cog": 0,
        "satellites_visible": 0,
    }


def test_mavlink2_truncated_gps_payload_decodes_defined_extensions_as_zero():
    frame = _frame(2, message_id=24, crc_extra=24, payload=b"\x00")
    result = import_bytes(_record(frame))

    assert result.traversal == "complete"
    assert result.records[0].raw_frame == frame
    assert result.records[0].fields == {
        "time_usec": 0,
        "fix_type": 0,
        "lat": 0,
        "lon": 0,
        "alt": 0,
        "eph": 0,
        "epv": 0,
        "vel": 0,
        "cog": 0,
        "satellites_visible": 0,
        "alt_ellipsoid": 0,
        "h_acc": 0,
        "v_acc": 0,
        "vel_acc": 0,
        "hdg_acc": 0,
        "yaw": 0,
    }


def test_unknown_message_with_arbitrary_checksum_remains_opaque_then_parsing_continues():
    unknown = _frame(2, message_id=0xFFFFFF, payload=b"\xfe\xfd\x00", crc_extra=0)
    unknown = unknown[:-2] + b"\x00\x00"
    first = _record(unknown)
    second = _record(_frame(), EPOCH_US + 1)

    result = import_bytes(first + second)

    assert result.traversal == "complete"
    assert result.consumed_bytes == len(first + second)
    assert (result.decoded_count, result.opaque_count) == (1, 1)
    opaque = result.records[0]
    assert opaque.message_id == 0xFFFFFF
    assert opaque.message_name is None
    assert opaque.fields is None
    assert opaque.checksum_status == "unverified"
    assert opaque.raw_frame == unknown
    assert opaque.end_offset == len(first)
    assert result.records[1].offset == len(first)
    assert result.records[1].message_name == "HEARTBEAT"
    assert [(issue.code, issue.severity, issue.record_index) for issue in result.issues] == [
        ("unknown_message", "warning", 0)
    ]


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("corruption", ["payload", "sequence", "checksum"])
def test_checksum_error_stops_at_corrupt_record_without_resynchronizing(version, corruption):
    prefix = _record(_frame())
    frame = bytearray(_frame(version))
    index = {
        "payload": 6 if version == 1 else 10,
        "sequence": 2 if version == 1 else 4,
        "checksum": len(frame) - 1,
    }[corruption]
    frame[index] ^= 0x01
    damaged = _record(bytes(frame), EPOCH_US + 1)
    later_valid_record = _record(_frame(), EPOCH_US + 2)
    data = prefix + damaged + later_valid_record

    result = import_bytes(data)

    _assert_stopped(result, data, prefix, "checksum_mismatch")
    assert result.records[0].end_offset == len(prefix)


@pytest.mark.parametrize("version", [1, 2])
def test_process_environment_cannot_disable_required_checksum_validation(version):
    frame = _frame(version)
    corrupt_frame = frame[:-1] + bytes((frame[-1] ^ 1,))
    data = _record(corrupt_frame)
    environment = os.environ.copy()
    environment["MAV_IGNORE_CRC"] = "1"
    check = """
import sys
from uav_debugger import import_bytes
result = import_bytes(bytes.fromhex(sys.argv[1]))
assert result.traversal == 'stopped', result
assert result.records == (), result
assert result.consumed_bytes == 0, result
assert any(issue.code == 'checksum_mismatch' for issue in result.issues), result
"""

    completed = subprocess.run(
        [sys.executable, "-c", check, data.hex()],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "version, payload",
    [
        (1, HEARTBEAT_PAYLOAD[:-1]),
        (1, HEARTBEAT_PAYLOAD + b"\x00"),
        (2, b""),
        (2, HEARTBEAT_PAYLOAD + b"\x00"),
    ],
)
def test_known_message_payload_length_is_validated_even_with_matching_crc(version, payload):
    prefix = _record(_frame())
    data = prefix + _record(_frame(version, payload=payload), EPOCH_US + 1)

    _assert_stopped(import_bytes(data), data, prefix, "invalid_length")


@pytest.mark.parametrize(
    "incompat_flags, expected_code",
    [
        (0x01, "unsupported_signature"),
        (0x02, "unsupported_flags"),
        (0x80, "unsupported_flags"),
    ],
)
def test_unsupported_features_preserve_prefix_and_unprocessed_bytes(incompat_flags, expected_code):
    prefix = _record(_frame())
    frame = _frame(2, incompat_flags=incompat_flags)
    if incompat_flags == 1:
        frame += bytes(13)
    data = prefix + _record(frame, EPOCH_US + 1) + _record(_frame(), EPOCH_US + 2)

    _assert_stopped(import_bytes(data), data, prefix, expected_code)


def test_unknown_compatibility_flags_do_not_block_decoding():
    frame = _frame(2, compat_flags=0xA5)
    result = import_bytes(_record(frame))

    assert result.traversal == "complete"
    assert result.records[0].checksum_status == "valid"
    assert result.records[0].raw_frame == frame
    assert result.records[0].raw_frame[3] == 0xA5


@pytest.mark.parametrize(
    "version, cutoff",
    [(version, cutoff) for version in (1, 2) for cutoff in range(1, len(_record(_frame(version))))],
)
def test_truncation_at_every_record_byte_preserves_only_the_complete_prefix(version, cutoff):
    prefix = _record(_frame())
    second = _record(_frame(version), EPOCH_US + 1)
    data = prefix + second[:cutoff]
    code = "incomplete_timestamp" if cutoff < 8 else "incomplete_frame"

    _assert_stopped(import_bytes(data), data, prefix, code)


def test_unknown_frame_truncation_does_not_create_an_opaque_record():
    frame = _frame(2, message_id=0xFFFFFF, payload=b"abc", crc_extra=0)
    data = _record(frame)[:-1]

    _assert_stopped(import_bytes(data), data, b"", "incomplete_frame")


def test_invalid_magic_does_not_search_ahead_for_a_valid_frame():
    prefix = _record(_frame())
    data = prefix + _record(b"\x00" + _frame(), EPOCH_US + 1)

    result = import_bytes(data)

    _assert_stopped(result, data, prefix, "invalid_magic")
    assert result.issues[-1].offset == len(prefix) + 8


def test_empty_input_has_no_invented_records():
    result = import_bytes(b"")

    assert result.traversal == "empty"
    assert result.raw_bytes == b""
    assert result.sha256 == hashlib.sha256(b"").hexdigest()
    assert result.records == ()
    assert result.consumed_bytes == result.remaining_bytes == 0
    assert result.decoded_count == result.opaque_count == 0
    assert [issue.code for issue in result.issues] == ["empty_input"]


def test_capture_time_regressions_and_repetitions_are_reported_without_reordering():
    timestamps = [EPOCH_US + 20, EPOCH_US + 20, EPOCH_US + 10, EPOCH_US + 30]
    chunks = [_record(_frame(sequence=index), ts) for index, ts in enumerate(timestamps)]
    data = b"".join(chunks)

    result = import_bytes(data)

    assert result.traversal == "complete"
    assert [record.timestamp_us for record in result.records] == timestamps
    assert [record.sequence for record in result.records] == [0, 1, 2, 3]
    assert [record.raw_frame for record in result.records] == [chunk[8:] for chunk in chunks]
    assert [
        (issue.code, issue.severity, issue.record_index, issue.offset) for issue in result.issues
    ] == [
        ("timestamp_repeated", "warning", 1, len(chunks[0])),
        ("timestamp_regression", "warning", 2, len(chunks[0]) + len(chunks[1])),
    ]


@pytest.mark.parametrize("timestamp_us", [0, 2**53 + 1, 2**64 - 1])
def test_capture_timestamp_retains_exact_unsigned_integer_without_datetime_roundtrip(timestamp_us):
    frame = _frame()
    result = import_bytes(_record(frame, timestamp_us))

    assert result.traversal == "complete"
    assert type(result.records[0].timestamp_us) is int
    assert result.records[0].timestamp_us == timestamp_us
    assert result.records[0].checksum_status == "valid"
    assert result.records[0].raw_frame == frame


def test_capture_timestamp_is_not_protected_by_the_frame_checksum():
    frame = _frame()
    original = import_bytes(_record(frame, EPOCH_US))
    changed = import_bytes(_record(frame, EPOCH_US + 1))

    assert original.sha256 != changed.sha256
    assert original.records[0].raw_frame == changed.records[0].raw_frame
    assert original.records[0].checksum_status == changed.records[0].checksum_status == "valid"
    assert changed.records[0].timestamp_us == original.records[0].timestamp_us + 1


def test_size_limit_is_inclusive_and_applies_before_parsing():
    data = _record(_frame())

    assert import_bytes(data, max_bytes=len(data)).traversal == "complete"
    with pytest.raises(InputTooLargeError):
        import_bytes(data, max_bytes=len(data) - 1)
    with pytest.raises(InputTooLargeError):
        import_bytes(b"not a telemetry recording", max_bytes=1)


def test_import_file_preserves_input_and_creates_no_outputs(tmp_path):
    path = tmp_path / "recording.bin"
    original = _record(_frame())
    path.write_bytes(original)

    result = import_file(path, max_bytes=len(original))

    assert result.raw_bytes == path.read_bytes() == original
    assert result.sha256 == hashlib.sha256(original).hexdigest()
    assert list(tmp_path.iterdir()) == [path]
    with pytest.raises(InputTooLargeError):
        import_file(path, max_bytes=len(original) - 1)
    assert path.read_bytes() == original


def test_import_file_rejects_non_regular_input(tmp_path):
    with pytest.raises(ValueError, match="regular"):
        import_file(tmp_path)


def test_source_names_and_file_paths_cannot_select_a_network_transport(monkeypatch, tmp_path):
    def forbid_socket(*args, **kwargs):
        pytest.fail("Offline import attempted to create a network socket")

    monkeypatch.setattr(socket, "socket", forbid_socket)
    monkeypatch.setattr(socket, "create_connection", forbid_socket)
    monkeypatch.chdir(tmp_path)
    source_name = "udpin:127.0.0.1:14550"
    path = Path(source_name)
    data = _record(_frame())
    path.write_bytes(data)

    assert import_bytes(data, source_name=source_name).source_name == source_name
    assert import_file(source_name).raw_bytes == data
    with pytest.raises(FileNotFoundError):
        import_file("udpout:127.0.0.1:14551")
    assert path.read_bytes() == data
