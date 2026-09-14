"""Exercise the installed module's local-file command-line workflow."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"


def run_cli(*arguments: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "uav_debugger", *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )


def test_fixture_summary_preserves_input_and_identifies_evidence():
    original = FIXTURE.read_bytes()

    process = run_cli(FIXTURE)

    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    summary = json.loads(process.stdout)
    assert summary["source_name"] == FIXTURE.name
    assert summary["sha256"] == hashlib.sha256(original).hexdigest()
    assert summary["size_bytes"] == len(original)
    assert summary["consumed_bytes"] == len(original)
    assert summary["remaining_bytes"] == 0
    assert summary["traversal"] == "complete"
    assert summary["decoded_count"] == 12
    assert summary["opaque_count"] == 0
    assert summary["sources"] == [
        {"system_id": 1, "component_id": 1, "count": 4},
        {"system_id": 2, "component_id": 1, "count": 8},
    ]
    assert summary["message_counts"] == {"ATTITUDE": 7, "HEARTBEAT": 5}
    assert summary["profile"]
    assert summary["dialect"] == "common"
    assert summary["decoder_version"]
    assert "Host logging wall clock" in summary["clock"]["interpretation"]
    assert "packet loss" in summary["clock"]["limitations"]
    assert "records" not in summary
    assert "raw_bytes" not in summary
    assert FIXTURE.read_bytes() == original


def test_truncated_recording_reports_partial_import(tmp_path: Path):
    damaged = tmp_path / "truncated.tlog"
    damaged.write_bytes(FIXTURE.read_bytes()[:-1])

    process = run_cli(damaged)

    assert process.returncode == 1, process.stderr
    assert process.stderr == ""
    summary = json.loads(process.stdout)
    assert summary["traversal"] == "stopped"
    assert summary["decoded_count"] > 0
    assert summary["remaining_bytes"] > 0
    assert summary["size_bytes"] == damaged.stat().st_size
    assert summary["issues"]
    issue = summary["issues"][-1]
    assert set(issue) == {"code", "severity", "offset", "record_index", "message"}
    assert issue["offset"] >= summary["consumed_bytes"]
    assert issue["message"]


def test_empty_recording_reports_explicit_outcome(tmp_path: Path):
    empty = tmp_path / "empty.tlog"
    empty.write_bytes(b"")

    process = run_cli(empty)

    assert process.returncode == 1, process.stderr
    assert process.stderr == ""
    summary = json.loads(process.stdout)
    assert summary["traversal"] == "empty"
    assert summary["size_bytes"] == 0
    assert summary["decoded_count"] == summary["opaque_count"] == 0
    assert summary["sources"] == []
    assert summary["message_counts"] == {}


def test_opaque_message_is_distinct_from_complete_decoding(tmp_path: Path):
    opaque = tmp_path / "opaque.tlog"
    # A structurally bounded, unsigned MAVLink 2 frame for unknown ID 0xffffff.
    # Its checksum is deliberately unverified: the dialect has no definition.
    frame = bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00")
    opaque.write_bytes((1_700_000_000_000_000).to_bytes(8, "big") + frame)

    process = run_cli(opaque)

    assert process.returncode == 1, process.stderr
    assert process.stderr == ""
    summary = json.loads(process.stdout)
    assert summary["traversal"] == "complete"
    assert summary["remaining_bytes"] == 0
    assert summary["decoded_count"] == 0
    assert summary["opaque_count"] == 1
    assert summary["message_counts"] == {"UNKNOWN_16777215": 1}
    assert summary["issues"]


def test_repeated_logging_time_keeps_successful_decoding(tmp_path: Path):
    repeated = tmp_path / "repeated.tlog"
    # The documented first golden record is an 8-byte timestamp + 17-byte frame.
    first_record = FIXTURE.read_bytes()[:25]
    repeated.write_bytes(first_record * 2)

    process = run_cli(repeated)

    assert process.returncode == 0, process.stderr
    assert process.stderr == ""
    summary = json.loads(process.stdout)
    assert summary["traversal"] == "complete"
    assert summary["decoded_count"] == 2
    assert summary["issues"]


def test_missing_recording_has_useful_error_without_traceback(tmp_path: Path):
    missing = tmp_path / "missing.tlog"

    process = run_cli(missing)

    assert process.returncode == 2
    assert process.stdout == ""
    assert "Cannot import recording:" in process.stderr
    assert "missing.tlog" in process.stderr
    assert "Traceback" not in process.stderr


def test_oversized_recording_is_rejected_without_import(tmp_path: Path):
    oversized = tmp_path / "oversized.tlog"
    with oversized.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)

    process = run_cli(oversized)

    assert process.returncode == 2
    assert process.stdout == ""
    assert "Cannot import recording:" in process.stderr
    assert "Traceback" not in process.stderr


def test_directory_is_rejected_without_traceback(tmp_path: Path):
    process = run_cli(tmp_path)

    assert process.returncode == 2
    assert process.stdout == ""
    assert "Cannot import recording:" in process.stderr
    assert "Traceback" not in process.stderr


def test_missing_argument_displays_usage():
    process = run_cli()

    assert process.returncode == 2
    assert process.stdout == ""
    assert "usage:" in process.stderr
    assert "path" in process.stderr
    assert "Traceback" not in process.stderr
