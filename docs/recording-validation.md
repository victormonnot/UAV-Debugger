# Public recording verification

UAV Debugger **0.1.0.dev2** was checked on 2026-09-19 against the exact public
recording below. The importer traversed the entire file with partial message
definition coverage, and the browser open/filter/inspect/report workflow passed.
This verifies one historical attachment, not all recordings from a QGroundControl
release or the behavior of its vehicle.

## Source and identity

The recording is attached to [QGroundControl issue #5136](https://github.com/mavlink/qgroundcontrol/issues/5136).
The author associates the test with QGroundControl `master:da811e1a8` and
ArduCopter 3.4.4. That QGroundControl revision resolves to
[`da811e1a85ff0f0a474d4e22e62534e2a9fb6182`](https://github.com/mavlink/qgroundcontrol/commit/da811e1a85ff0f0a474d4e22e62534e2a9fb6182).
This build identification is the author's declaration; it is not attested by
the recording itself, and the original producer build was not rerun here.
The issue reports vehicle-control and QGroundControl replay problems. This
verification does not reproduce or resolve those problems.

Download the [original ZIP attachment](https://github.com/mavlink/qgroundcontrol/files/997323/QGC-noAutoTakeoff.tlog.zip)
and extract `QGC-noAutoTakeoff.tlog`. The attachment is not bundled with UAV
Debugger, and no redistribution license for it is asserted. The built-in
**Load example** recording remains the project's synthetic fixture.

| Property | Value |
| --- | --- |
| Recording size | 843,343 bytes |
| Recording SHA-256 | `9a55d86b52f8bcd7c3fb952bbc77096e4a75ab344c977fa0715820d5a74eddaf` |
| ZIP size | 449,269 bytes |
| ZIP SHA-256 | `c8b8478761a8a144f8d6f5dcae2a5220ed64350876b9183b7759d5429a07914d` |
| Tested importer profile | `qgc-timestamped-mavlink-v1` |
| Tested definitions | `common`, `pymavlink==2.4.49` |
| Application/runtime | UAV Debugger 0.1.0.dev2, Python 3.12.3, Linux x86_64 |

## Import outcome

| Observation | Result |
| --- | --- |
| Traversal | Complete; all 843,343 bytes consumed, no remainder |
| Accepted records | 22,291, all MAVLink 1 |
| Decoded, checksum-valid records | 15,235 |
| Opaque records | 7,056; fields and checksums unverified |
| Source `22 / 1` | 21,265 records |
| Source `51 / 68` | 1,026 records |
| `ATTITUDE` records | 1,329, all from `22 / 1` |
| First capture timestamp | `1494591406064000` µs |
| Last capture timestamp | `1494591918872000` µs |
| Observed capture-time span | 512.808 seconds |
| Adjacent repeated timestamps | 13,061 |
| Decreasing timestamps / import errors | 0 / 0 |

The opaque IDs are `150`, `152`, `158`, `163`, `165`, `166`, `178`, `182` and
`193`, outside the selected `common` definitions. They remain inspectable as raw
frames; this check does not add a dialect or invent their decoded fields.
Traversal follows their declared lengths, without verifying opaque framing.

The timestamp interpretation follows the declared profile. Repeats are retained
as warnings; neither the units nor the observed span establish clock accuracy,
vehicle-clock synchronization or a complete flight history.

## Reproduce the browser workflow

Launch Analyze using the [user guide](analyze.md), then:

1. Under **Open recording**, select the extracted `.tlog` file.
2. Expect **22,291 imported records**, **2 sources** and the explicit warning
   about **7,056 opaque records**.
3. Select source **22 / 1**, type **ATTITUDE**, start **100** and end **105**
   seconds, then click **Apply filters**.
4. Expect **15 selected records**, from original record **#4710** through
   **#4934**, and a longest observed interval of **0.772 s**.
5. Choose **Record #4769** and inspect its decoded fields, capture timestamp
   and raw frame. Download the Markdown report.

The largest interval in this selection is between records **#4769** and
**#4796**, whose capture timestamps are `1494591507436000` and
`1494591508208000` µs. Their record byte offsets are `181939` and `182936`.
The report retains both interval references even though only #4769 is selected
for full record details. This is an observed logging interval, not measured
physical packet loss or a diagnosis of the upstream issue.

The report contains **20,117 total import issues**: 7,056 unknown-message
warnings and 13,061 repeated-timestamp warnings. Its 100-entry issue list
explicitly reports **20,017 omitted entries**; the UI pages the complete list.
Those warnings describe the full input even after filtering to `ATTITUDE`.

For a CLI check, save the extracted file under an ignored local directory and run:

```sh
uv run --locked python -m uav_debugger local/recordings/QGC-noAutoTakeoff.tlog
```

The expected exit code is **1** because opaque records remain, despite complete
file traversal. See [CLI outcomes](importer.md#json-summary-and-exit-status).
The fingerprint above identifies the exact file covered by these results.

## Checks performed and boundaries

An independent standard-library byte scan checked all record boundaries,
headers, integer timestamps and raw frame bytes against the importer. A
separate bitwise CRC-16/MCRF4XX calculation validated all 15,235 known frames.
It used CRC_EXTRA metadata from the same pinned dialect, so it independently
checks the CRC calculation, not the dialect definitions themselves.

All seven fields of all 1,329 `ATTITUDE` messages were independently unpacked
with `struct.unpack("<I6f", payload)` and matched the importer exactly. The
15-record time selection and its 772,000 µs maximum interval were also checked
against that independent scan. Other decoded payload fields were not each
independently interpreted. The field layout follows the
[MAVLink ATTITUDE definition](https://mavlink.io/en/messages/common.html#ATTITUDE)
and [serialization specification](https://mavlink.io/en/guide/serialization.html).

The actual Chromium workflow uploaded the original file, applied the stated
filters, inspected #4769 and downloaded the report. The downloaded provenance,
counts, exact filter bounds, issue truncation, interval references, record
timestamp and raw bytes matched the input and expected results. The recording
remained byte-for-byte unchanged.

That browser check ran in a Linux network namespace with only loopback enabled;
browser HTTP/WebSocket requests outside loopback were also blocked. No external
requests, JavaScript errors or application exceptions occurred. Downloading the
original attachment was a separate online step before the offline check.

These results complement the [synthetic fixture checks](../tests/fixtures/README.md)
and [existing browser tests](analyze.md#browser-workflow-checks). The external
recording is not fetched by the normal test suite. This case establishes neither
complete decoding of this recording nor compatibility with current producer
versions, other dialects or other recording formats.
