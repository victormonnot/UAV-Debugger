# First milestone: offline Analyze

**Status: offline Analyze workflow implemented in the development version.**
The file importer, synthetic fixture, exact source/type/time filters, activity
plot, record inspector and Markdown evidence report are available. See the
[Analyze guide](analyze.md) and [importer guide](importer.md) for usage and the
actual verification boundary. Producer interoperability and distribution
licensing remain open release decisions. Experiment is a separate later delivery.

## User outcome: investigate a gap in recorded telemetry

An engineer opens a saved MAVLink recording, selects a source and message type,
locates an interval without observations and inspects the messages before and
after it. They export a report identifying the recording, selection and evidence
limits. The result works without ARGOS, a vehicle, simulator, experiment runner
or external network access after installation.

An interval without recorded messages does not establish physical packet loss,
vehicle inactivity or the cause of the gap. This distinction applies to the
timeline, derived statistics and report.

## Implemented user workflow

1. **Open one recording.** Choose a local file using the documented input profile.
   Review its name, byte size, SHA-256, import outcome and limitations.
2. **Review the recording.** See source identities, message types, counts and
   the observed time range, with the clock convention displayed.
3. **Select an interval.** Filter by system/component, message type and explicit
   time bounds. A message-activity plot and table show the same selection.
4. **Inspect evidence.** Select a record to see decoded fields, its original
   timestamp, file order, byte offset and raw frame. Show intervals between
   consecutive selected messages where the capture clock permits calculation.
5. **Export a report.** Download Markdown containing the input fingerprint,
   decoder/profile versions, filters, counts, selected record references and
   import/time limitations. The report is a derived result, not a session archive.

The interface exposes Analyze. Video, cross-session comparison,
multi-file alignment, additional formats and active Experiment controls are
outside v0.1; they remain possible subsequent deliveries.

## Implemented input profile

The importer accepts a **QGroundControl-style timestamped MAVLink telemetry log**: repeated
records containing an unsigned 8-byte big-endian Unix-epoch timestamp in
microseconds followed by one unsigned MAVLink 1 or MAVLink 2 frame. Decode using
the `common` dialect included in `pymavlink` 2.4.49. The profile identifier is
`qgc-timestamped-mavlink-v1`; its suffix versions the container contract, not
the MAVLink wire protocol.

The profile describes expected bytes; a `.tlog` extension does not identify the
producer or prove compatibility. QGroundControl's logger writes its host wall
clock in this layout. The inspected implementation obtains milliseconds and
multiplies by 1,000, so microsecond units do not imply microsecond resolution.
Clock accuracy and synchronization with the vehicle remain unknown.
See the [QGroundControl logger source](https://github.com/mavlink/qgroundcontrol/blob/master/src/Comms/MAVLinkProtocol.cc).

Keep the original integer timestamp and record order. Device timestamps inside
messages remain separate fields. Repeated or decreasing capture timestamps are
visible, and calculations requiring monotonic time must identify affected ranges.
The MAVLink checksum does not cover the outer recording timestamp.

| Input condition | Implemented behavior |
| --- | --- |
| Known message, valid frame checksum | Decode with the pinned `common` definitions and retain its raw bytes. |
| Message ID absent from the selected definitions | Preserve a structurally bounded record as opaque; show that fields and checksum are unverified. Advancing by its declared length is not proof of valid framing. |
| Invalid framing, failed checksum on a known message or truncated record | Stop at the first affected record; retain the decoded prefix and identify the stopping offset, reason and unprocessed remainder. No heuristic recovery in v0.1. |
| Signed frame or unsupported incompatibility flag | Report an unsupported feature, preserve the input and stop with any preceding records available. Signature verification is outside v0.1. |
| Empty file or unsupported input | Show an explicit outcome; do not fabricate a session or silently try another container. |

File traversal, message decoding coverage and issues are separate report fields.
Reaching the end of a file does not prove that the producer recorded an entire
session. A checksum validates a known frame against its definitions; it does not
authenticate the sender. QGroundControl's inspected logger removes signatures,
so an unsigned log also does not establish whether the original traffic was
signed. See [MAVLink serialization](https://mavlink.io/en/guide/serialization.html)
and [signing and logging](https://mavlink.io/en/guide/message_signing.html#logging).

MAVProxy's logging convention encodes link information in the timestamp's low
bits; it is not part of this initial profile. Raw MAVLink streams, onboard logs
such as ULog/DataFlash and custom dialects also have no v0.1 support.
See the [MAVProxy logger source](https://github.com/ArduPilot/MAVProxy/blob/master/MAVProxy/modules/mavproxy_link.py).

## Implementation boundary

The implementation uses Python, `pymavlink`, Streamlit and Plotly in one local application, with
in-memory session data and Markdown export. The
[architecture proposal](architecture.md#recommended-starting-stack) explains the
responsibilities and tradeoffs. Target Linux with Python 3.12 for initial
verification; other platforms would require their own checks.

The first deliverable now includes a deterministic synthetic recording, its
independent expected-observation manifest, a file-only importer and behavioral
tests. A command-line JSON summary makes the importer directly inspectable.
The interactive view and report exercise that same importer. A fixture
matching the documented layout alone does not establish interoperability with
every QGroundControl release: any producer compatibility claim must identify
the actual producer/version and recording tested.

The [synthetic example](../tests/fixtures/README.md) contains two system/component
identities, known `HEARTBEAT` and `ATTITUDE` values, and a documented interval
without messages from one source while the other remains present. Generation,
the artificial clock and expected values are documented. Distribution licensing
remains a release decision; the fixture supplies no evidence of an actual
aircraft response.

## Full v0.1 acceptance criteria

Importer and command-line checks are recorded in the [importer guide](importer.md).
Interactive workflow checks and measured capacity are recorded in the
[Analyze guide](analyze.md); these are bounded synthetic checks, not a guarantee
for all producers, message mixes or hardware.

- After a fresh documented installation, the complete open/filter/inspect/export
  workflow runs with external networking disabled and loopback available.
- The fixture's two sources remain distinct; filtering source, type and time
  returns the expected records and reveals the prescribed observation gap.
- Decoded values, byte references and chronology agree with the independent
  manifest. At least one golden frame is cross-checked independently of the
  codec used to generate and parse the fixture.
- Unknown IDs, bad checksums, incomplete timestamps/frames, signed frames and
  unsupported flags produce the documented outcomes with traceable offsets.
- Repeated and decreasing timestamps remain visible. No report asserts measured
  sensor latency, physical packet loss or causation from these observations.
- Original recordings remain byte-for-byte unchanged. Reports identify their
  inputs and are saved separately from them.
- Loading, filtering and exporting cause no vehicle connection, simulator
  startup or MAVLink transmission. The local browser connection is only for UI.
- Publish and enforce an input-size limit established by a measured fixture on
  the target environment; report import time, memory use and filter behavior.
  Files beyond that limit receive an explicit outcome, not silent truncation.
- Installation, importer tests and the actual browser workflow are checked for
  the delivered version. Synthetic importer verification does not establish
  compatibility with an untested producer.

## Candidate follow-up: one controlled experiment

After a usable analyzer exists, a separate delivery could forward a known local
MAVLink stream through a bounded experiment path, introduce one specified
one-direction interruption, record applied actions and endpoint observations,
and inspect the resulting session in Analyze. Nominal and perturbed runs should
share the same configuration except for the studied change.

That delivery needs its own target topology, scenario semantics, recording
format, stop behavior and verification criteria. A stream experiment would
establish behavior on that configured path; autopilot response would require an
additional actual simulator/bench integration and its own evidence.

See [project scope](project-scope.md) and [architecture direction](architecture.md)
for the product boundaries and shared analysis design.
