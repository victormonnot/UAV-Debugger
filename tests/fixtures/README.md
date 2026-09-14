# Synthetic telemetry fixture

`telemetry-gap.tlog` is an original, deterministic numerical test recording.
It contains no aircraft measurements, personal recordings or third-party data.
It was constructed directly as bytes; it was not recorded by QGroundControl,
MAVProxy, an autopilot or a simulator. Passing these fixture checks establishes
behavior on this example, not interoperability with a particular QGroundControl
release or evidence of an aircraft response.

Distribution licensing for the fixture and project remains an open release
decision; no redistribution license is assigned by this document.

## Reproduction and identity

From the repository root, using Python 3.12:

```sh
python scripts/generate_fixture.py --check
```

This compares freshly encoded bytes with both the stored test fixture and the
bundled copy at `src/uav_debugger/data/telemetry-gap.tlog`, and exits with a
nonzero status if either differs. It does not write any file. To deliberately
regenerate both copies of `telemetry-gap.tlog`, omit `--check`. Generation uses
only the Python standard library, has no clock/network inputs and does not modify the separately
maintained [expected observations](telemetry-gap.expected.json).

Analyze's **Load example** control reads the bundled copy through the same
importer as uploaded recordings. It identifies the source as
`telemetry-gap.tlog (synthetic example)` in the interface and reports; the
original bytes and fingerprint below remain unchanged.

| Property | Value |
| --- | --- |
| Input profile | `qgc-timestamped-mavlink-v1` (profile revision, not wire version) |
| Message definitions | `common`: HEARTBEAT and ATTITUDE |
| Size | 409 bytes |
| Records | 12: six MAVLink 1 and six unsigned MAVLink 2 frames |
| Sources | `(1, 1)`: four records; `(2, 1)`: eight records |
| Message counts | Five HEARTBEAT; seven ATTITUDE |
| SHA-256 | `7e5d859ef3bb4a62f129b5f7d5334715bc31b44933d034aeb32f2e55de2f4e95` |

## Container and observations

Each record is an 8-byte unsigned big-endian timestamp followed immediately by
one MAVLink frame. The artificial capture clock starts at
`1700000000000000` microseconds since the Unix epoch. Equal timestamps retain
the file order shown below; offsets are zero-based byte positions of the
timestamp, and the frame begins eight bytes later. No physical clock accuracy,
sensor latency or device-to-capture clock alignment is specified.

| Index | Offset | Capture seconds after epoch | Wire version | Source | Message | Sequence | Device `time_boot_ms` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 0 | 0 | 1 | `(1, 1)` | HEARTBEAT | 0 | — |
| 1 | 25 | 0 | 2 | `(2, 1)` | HEARTBEAT | 0 | — |
| 2 | 54 | 1 | 2 | `(1, 1)` | ATTITUDE | 1 | 1000 |
| 3 | 90 | 1 | 1 | `(2, 1)` | ATTITUDE | 1 | 11000 |
| 4 | 134 | 2 | 2 | `(2, 1)` | ATTITUDE | 2 | 12000 |
| 5 | 170 | 3 | 1 | `(2, 1)` | HEARTBEAT | 3 | — |
| 6 | 195 | 3 | 2 | `(2, 1)` | ATTITUDE | 4 | 13000 |
| 7 | 231 | 4 | 1 | `(2, 1)` | ATTITUDE | 5 | 14000 |
| 8 | 275 | 5 | 1 | `(1, 1)` | ATTITUDE | 2 | 5000 |
| 9 | 319 | 5 | 2 | `(2, 1)` | ATTITUDE | 6 | 15000 |
| 10 | 355 | 6 | 2 | `(1, 1)` | HEARTBEAT | 3 | — |
| 11 | 384 | 6 | 1 | `(2, 1)` | HEARTBEAT | 7 | — |

Selecting source `(1, 1)` and ATTITUDE returns indices 2 and 8, separated by
4,000,000 capture microseconds. There are no records from that source inside
the open interval; source `(2, 1)` still has ATTITUDE records at indices 4, 6
and 7. This is an absence of synthetic observations, with no claimed cause or
physical packet-loss count.

All HEARTBEAT messages contain `custom_mode=0`, `type=2`, `autopilot=0`,
`base_mode=0`, `system_status=4` and `mavlink_version=3`. The HEARTBEAT payload
field `mavlink_version` has value 3 in both wire versions, as specified by its
message definition.

Source `(1, 1)` ATTITUDE values are `roll=0.25`, `pitch=-0.5`, `yaw=1.0` radians;
source `(2, 1)` values are `roll=0.5`, `pitch=0.25`, `yaw=-1.0` radians. All three
angular speeds are zero radians per second. The values are exactly representable
as IEEE 754 binary32. MAVLink 1 includes all 28 ATTITUDE payload bytes; the
MAVLink 2 frames omit the twelve trailing zero bytes for the three angular
speeds, leaving a 16-byte payload. This is valid protocol truncation, distinct
from an incomplete file record.

## Independent expected evidence

The generator packs fields explicitly with `struct` and implements the
CRC-16/MCRF4XX bit algorithm directly. It does not import `pymavlink` or the UAV
Debugger importer. The manifest specifies expected semantic values separately,
rather than obtaining them from the decoder under test. Its frame hex strings,
byte spans and hash identify the exact fixture against which those assertions
apply. `--check` does not regenerate or update that manifest.

The first record is a literal golden MAVLink 1 HEARTBEAT frame:

```text
fe09000101000000000002000004031c09
```

It has source `(1, 1)`, sequence 0, message ID 0 and the HEARTBEAT values above.
Its checksum is `0x091c`, stored as bytes `1c 09`. The checksum covers the
header after `fe`, the payload, and `CRC_EXTRA=50`; it excludes both the start
marker and the outer timestamp. The generator verifies this literal before
writing the fixture. The independent importer tests compare decoded records
with the manifest rather than using the fixture encoder to compute expected
decoded values.

The initial checksum verification also compared all twelve frames against
Python's separate `binascii.crc_hqx` primitive with reflected input/output,
instead of the generator's bit loop. Neither check validates a physical clock
or authenticates a sender.

## Protocol references

The encoder's field ordering, lengths and CRC constants follow these primary
references:

- [MAVLink packet serialization](https://mavlink.io/en/guide/serialization.html):
  headers, checksum coverage and MAVLink 2 trailing-zero truncation.
- [HEARTBEAT reference implementation](https://github.com/mavlink/c_library_v2/blob/master/minimal/mavlink_msg_heartbeat.h):
  message ID 0, payload length 9, wire field offsets and `CRC_EXTRA=50`.
- [ATTITUDE reference implementation](https://github.com/mavlink/c_library_v2/blob/master/common/mavlink_msg_attitude.h):
  message ID 30, payload length 28, wire field offsets and `CRC_EXTRA=39`.
- [QGroundControl logger](https://github.com/mavlink/qgroundcontrol/blob/master/src/Comms/MAVLinkProtocol.cc):
  the external timestamp layout described by the input profile. The fixture
  reproduces that layout without using this logger.

These references informed the encoding; no implementation source or external
recording is copied into the fixture.
