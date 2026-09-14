# Timestamped MAVLink importer

The first Analyze component reads one local recording and exposes its source
bytes, decoded records and import issues through a Python API. A small CLI prints
a JSON import summary. The [Analyze interface](analyze.md) adds interactive
filtering, an activity plot, record inspection and Markdown report export using
this importer. Experiment execution remains unimplemented.

## Install and run

Use Python 3.12 on Linux and
[uv](https://docs.astral.sh/uv/getting-started/installation/). From the repository
root:

```sh
uv sync --locked
uv run --locked python -m uav_debugger tests/fixtures/telemetry-gap.tlog
```

The equivalent console entry point is:

```sh
uv run --locked uav-debugger tests/fixtures/telemetry-gap.tlog
```

Dependencies are pinned in `uv.lock`; `--locked` rejects an out-of-date lock.
Installation or environment synchronization can need network access. The
importer itself reads local bytes and opens no network or serial connection.

The synthetic example contains 409 bytes, 12 records, two source identities,
7 `ATTITUDE` messages and 5 `HEARTBEAT` messages. Its five
`timestamp_repeated` warnings are expected. See the
[fixture documentation](../tests/fixtures/README.md) for exact expected values,
the artificial clock and provenance. Synthetic verification does not establish
interoperability with an actual QGroundControl release or recording.

## Input profile

The profile identifier is **`qgc-timestamped-mavlink-v1`**. The final `v1`
versions this importer profile; it accepts both MAVLink wire versions 1 and 2.
Each record consists of:

1. An unsigned 64-bit, big-endian Unix-epoch timestamp in microseconds.
2. One unsigned MAVLink 1 or MAVLink 2 frame.

The selected definitions are the `common` dialect supplied by
**`pymavlink==2.4.49`**. Known messages are decoded only after their frame checksum
has been validated against these definitions. The recording extension is not a
format detector, and no other container is attempted automatically.

| Record condition | Import behavior |
| --- | --- |
| Known message and valid checksum | Retain the raw frame, decode fields and mark the checksum `valid`. |
| Unknown message ID | Retain a structurally bounded opaque record with no decoded fields, checksum `unverified` and an `unknown_message` warning. Advance by the declared length. |
| Invalid marker or length, failed known-message checksum, incomplete timestamp or frame | Stop at the affected record and return the accepted prefix, an error and the unprocessed remainder. |
| Signed MAVLink 2 frame or unsupported incompatibility flag | Stop with an unsupported-feature error. No signature verification is attempted. |
| Repeated or decreasing logging timestamp | Preserve file order and the exact integer timestamp; add a warning. |
| Empty input | Return traversal `empty`, zero records and an `empty_input` issue. |

Advancing over an opaque record does not validate its fields, checksum or actual
framing. A complete traversal can therefore contain undecoded information.
There is no heuristic search for the next possible frame after an error.
Checksum validation does not authenticate a sender, and an unsigned recording
does not establish whether the original traffic was signed.

MAVLink 2 payloads may omit trailing zero bytes; decoded values follow the
protocol's zero-padding convention while the raw frame preserves the shorter
payload. MAVLink 2 extension fields are absent from MAVLink 1 records and are
omitted from their decoded field mappings.

This profile excludes MAVProxy's timestamp/link-bit convention, raw MAVLink
streams, ULog, DataFlash, custom dialects and signed frames. Format references
and the broader scope are documented in the [first milestone](first-milestone.md).

## Clock and evidence conventions

The outer timestamp is interpreted as the logger's host wall clock under the
selected profile. Microsecond units do not establish microsecond resolution,
accuracy or synchronization with a vehicle. No timestamp correction, sorting or
clock alignment is performed. Device clocks such as `time_boot_ms` remain
separate decoded fields.

The MAVLink checksum does not cover the outer timestamp. Repeated or decreasing
timestamps are observations about the recording and must be considered before
calculating time intervals. A gap in recorded messages establishes neither
physical packet loss nor its cause.

The importer retains the original input bytes and their SHA-256. File indices
and byte offsets are zero-based. For each accepted record:

- `offset` points to the outer timestamp.
- `frame_offset` points eight bytes later, to the MAVLink frame marker.
- `end_offset` is the exclusive end of the frame.
- `timestamp_us` is the original timestamp integer.
- `raw_frame` is the original frame, including its checksum.
- `system_id`, `component_id`, `message_id`, `sequence` and `wire_version`
  retain frame identity.
- `message_name` and `fields` are absent (`None`) for opaque records.

`consumed_bytes` ends after the last accepted record. If traversal stops, the
remainder starts at the affected record's timestamp; an issue's `offset` can
point later within that record to the specific affected field. Reading all bytes
does not prove that the producer recorded an entire session.

## Python API

```python
from uav_debugger import import_bytes, import_file

result = import_file("tests/fixtures/telemetry-gap.tlog")
print(result.traversal, result.decoded_count, result.opaque_count)

first = result.records[0]
print(first.timestamp_us, first.system_id, first.component_id, first.fields)
assert first.raw_frame == result.raw_bytes[first.frame_offset : first.end_offset]

for issue in result.issues:
    print(issue.code, issue.record_index, issue.offset, issue.message)

same_bytes = import_bytes(result.raw_bytes, source_name="memory.tlog")
assert same_bytes.sha256 == result.sha256
```

`import_file(path, *, max_bytes=10 * 1024 * 1024)` accepts a regular local file
and leaves it unchanged. `import_bytes(data, *, source_name="<bytes>",
max_bytes=10 * 1024 * 1024)` accepts immutable `bytes`. The source name from a file
is its basename; for a bytes import it is the caller's label.

Both return `ImportResult`, containing `source_name`, `raw_bytes`, `sha256`,
`profile`, `dialect`, `decoder_version`, `records`, `issues`, `traversal` and
`consumed_bytes`, with `remaining_bytes`, `decoded_count` and `opaque_count`
properties. Records and issues are immutable data objects; decoded fields are
read-only mappings, with arrays exposed as tuples.

File-access failures raise `OSError`; non-regular files and invalid size-limit
arguments raise `ValueError`. Oversized input raises `InputTooLargeError`, a
`ValueError` subclass exported by `uav_debugger`. Malformed recording content
returns an explicit import outcome instead of discarding the accepted prefix.

## JSON summary and exit status

The CLI writes one JSON object to stdout for an inspected input, including
damaged or empty files. It exposes:

| Fields | Meaning |
| --- | --- |
| `profile`, `dialect`, `decoder_version` | Decoding configuration. |
| `source_name`, `sha256`, `size_bytes` | Identity and size of the entire input, including any remainder. |
| `traversal`, `consumed_bytes`, `remaining_bytes` | File traversal independently of decoding coverage. |
| `decoded_count`, `opaque_count` | Accepted records with and without decoded fields. |
| `sources` | Counts grouped by declared `system_id` and `component_id`. |
| `message_counts` | Counts by decoded message name; opaque IDs use `UNKNOWN_<decimal_id>`. |
| `issues` | Each issue's `code`, `severity`, `offset`, `record_index` and English `message`. |
| `clock` | Host logging interpretation and explicit timing/observation limits. |

Counts include only accepted records; source and message counts include opaque
records using their unverified frame headers. Raw bytes and individual decoded
payloads are available through the API, not included in the summary.

| Exit code | Meaning |
| --- | --- |
| `0` | Traversal is complete and all accepted records were decoded; clock warnings may still be present. |
| `1` | Traversal stopped, the input was empty, or opaque records remain. Inspect the JSON outcome and issues. |
| `2` | Invalid CLI usage, file access failure, non-regular input or size-limit rejection. An explanatory error is written to stderr without a JSON summary. |

## Capacity and verification

The default cap is **10 MiB (10,485,760 bytes)**. A larger regular file is
rejected before parsing. Reads are bounded by the cap plus one byte so that a
file growing during the read is also rejected. The API permits an explicit
positive `max_bytes` override; capacities above the default are not established
by the supplied verification. The CLI does not expose an override.

Input bytes, records and decoded fields remain in memory. The cap bounds input
bytes rather than total process memory; record density and message types affect
memory use. One Linux x86_64 / Python 3.12.3 measurement imported 10,485,750 bytes
containing 419,430 valid `HEARTBEAT` records with increasing timestamps in 3.343
seconds, with a peak process RSS of 289.2 MiB including construction of the
input. Traversal was complete with no issues. This single synthetic measurement
does not establish equivalent capacity or speed for every message mix.

A fresh environment installation with `uv sync --locked` and the importer/CLI
tests passed on that runtime. The fixture CLI also completed with exit code `0`
inside an isolated Linux network namespace. Browser workflow checks are recorded
separately in the [Analyze guide](analyze.md); real producer interoperability
remains unverified.

Run the delivered checks from the repository root:

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/generate_fixture.py --check
```

See the [architecture](architecture.md) and [first milestone](first-milestone.md)
for the boundary between imported evidence and the interactive Analyze
application.
