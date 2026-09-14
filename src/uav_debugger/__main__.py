"""Inspect one saved telemetry recording and print its import summary."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from uav_debugger import InputTooLargeError, import_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m uav_debugger",
        description="Inspect one local QGroundControl-style telemetry log offline.",
    )
    parser.add_argument("path", type=Path, help="Path to the saved telemetry recording")
    args = parser.parse_args(argv)

    try:
        result = import_file(args.path)
    except (InputTooLargeError, OSError, ValueError) as error:
        print(f"Cannot import recording: {error}", file=sys.stderr)
        return 2

    source_counts = Counter((record.system_id, record.component_id) for record in result.records)
    message_counts = Counter(
        record.message_name or f"UNKNOWN_{record.message_id}" for record in result.records
    )
    summary = {
        "profile": result.profile,
        "dialect": result.dialect,
        "decoder_version": result.decoder_version,
        "source_name": result.source_name,
        "sha256": result.sha256,
        "size_bytes": len(result.raw_bytes),
        "traversal": result.traversal,
        "consumed_bytes": result.consumed_bytes,
        "remaining_bytes": result.remaining_bytes,
        "decoded_count": result.decoded_count,
        "opaque_count": result.opaque_count,
        "sources": [
            {"system_id": system_id, "component_id": component_id, "count": count}
            for (system_id, component_id), count in sorted(source_counts.items())
        ],
        "message_counts": dict(sorted(message_counts.items())),
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "offset": issue.offset,
                "record_index": issue.record_index,
                "message": issue.message,
            }
            for issue in result.issues
        ],
        "clock": {
            "source": "Outer recording timestamp",
            "encoding": "Unsigned 64-bit big-endian Unix-epoch microseconds",
            "interpretation": "Host logging wall clock under the selected input profile",
            "limitations": (
                "Units do not establish resolution, accuracy or synchronization. "
                "The frame checksum does not cover the outer timestamp. "
                "Device timestamps remain separate decoded message fields. "
                "A gap in recorded observations does not establish packet loss or cause."
            ),
        },
    }
    print(json.dumps(summary, indent=2))
    return 0 if result.traversal == "complete" and result.opaque_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
