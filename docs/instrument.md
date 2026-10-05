# Instrument workspace

Instrument is UAV Debugger's custom local web interface for saved telemetry
recordings. Open a supported file or the bundled example, review import status
and provenance, apply exact source/type/time filters, and inspect activity and
attitude observations. Light, dark and system appearance use local display
assets. The importer, telemetry model and plotting rules are shared with the
existing interface.

Record inspection, report export, saved-experiment browsing and comparison, and
explicit Experiment execution remain available through `uav-debugger-analyze`.
Instrument does not yet replace those workflows and is not included in the
published v0.2.0 artifacts.

## Launch

From an installed source checkout:

```sh
uv sync --locked
uv run --locked uav-debugger-instrument
```

Open [Instrument](http://127.0.0.1:8765) on the same computer. `--port PORT`
selects another unused local port. The server binds only to `127.0.0.1`;
`Ctrl+C` stops it. No vehicle, simulator or ARGOS installation is required.

The browser loads scripts, fonts and icons from this server. Plotly's JavaScript
bundle is served from the installed pinned Python dependency. Font and icon
licenses and exact asset fingerprints are recorded in
[third-party notices](../THIRD_PARTY_NOTICES.md). Runtime use requires no CDN;
initial dependency installation can require network access.

### Access through SSH

When the server runs remotely, forward its loopback port from the computer
running the browser:

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 user@remote-host
```

Replace `user@remote-host` with the destination and authentication options used
for your normal SSH connection. Keep the tunnel running and open
[Instrument](http://127.0.0.1:8765) locally. **Open recording** selects a file on
the browser computer and sends its bytes through this connection to the server.
**Load example** reads the fixture installed on the server. See the
[Analyze SSH guide](analyze.md#access-through-ssh) for local port conflicts.

## Open a recording

Choose **Open recording** for a file up to **10 MiB**, or **Load example** for
the installed synthetic `telemetry-gap.tlog`. Both use the same importer and
analysis. The supported input is the bounded QGroundControl-style timestamped
MAVLink profile described in the [importer guide](importer.md); a `.tlog`
extension alone does not identify the format.

Opening another input replaces the previous recording and resets its filters.
**Clear recording** removes the current file and view from the tab without
changing the original file. Clearing an upload does not restore the example.
An oversized or unreadable replacement leaves no previous recording displayed
as if it were the newly selected file. Original bytes are never modified, and
the service does not save uploads to disk.

The example contains 12 records from two sources and five expected
repeated-timestamp warnings. Its synthetic designation and fingerprint identify
the exact input. To inspect its four-second observation interval:

1. Choose **Load example**.
2. Select source **1 / 1**, message type **ATTITUDE**, start **1** and end **5**.
3. Choose **Apply filters**, then **Attitude**.

Records **#2** and **#8** remain disconnected at the default one-second maximum
line gap. The other source has observations during that interval. This does
not establish packet loss or explain the vehicle's behavior. The
[fixture reference](../tests/fixtures/README.md) records its provenance and
expected observations.

## Apply filters

**Source** selects a system/component pair or all sources. **Message type**
selects a message ID or all types, including opaque IDs unsupported by the
pinned dialect. **Start (s)** and **End (s)** are decimal capture-time offsets
relative to the first imported record; changing filters never moves that
origin. Both bounds are inclusive. Values must resolve to whole microseconds:
`1.000001` is valid, but a nonzero fraction of a microsecond is rejected rather
than rounded. A capture-clock regression can produce negative offsets.

Choose **Apply filters** to update the selection. Editing controls alone leaves
the previous result active; invalid values display an error without replacing
that applied result. **Reset filters** restores all sources and message types,
with the full imported capture-time range. Original file order is preserved.
A valid empty selection remains visible with its applied filters and recording
provenance rather than being treated as an import failure.

Whole-recording counts and import issues do not change with selection filters.
Selected counts and observation plots describe only the applied selection.
Zooming, panning or hovering a chart changes its display, not the filter bounds.

## Read activity and attitude

**Activity** aggregates all selected records into at most **200 time bins**.
Hover details retain each bin's relative-time bounds and observation count.
Aggregation bounds the display without discarding selected observations.

The longest observed interval considers consecutive selected records of the
same source and message ID. It does not measure across different streams.
Pairs crossing a capture-clock regression, including a regression hidden by
filters, and pairs with opaque endpoints have no established duration.
Repeated timestamps can yield zero; no eligible pair means no interval metric.

**Attitude** plots unchanged roll, pitch and yaw in radians from checksum-valid
`ATTITUDE` records. Select one source when the applied selection contains
attitude observations from several sources. Selecting a different message type
leaves no attitude records. The display accepts at most **5,000 ATTITUDE
records** and asks for narrower filters above that limit; it never silently
decimates points. Activity and selected counts still describe the full applied
selection.

**Maximum line gap (s)** is a separate display setting, initially **1** second.
Choose **Apply line gap** to apply positive decimal seconds at whole-microsecond
precision. Invalid values preserve the previous setting. This does not change
selected records or the interval metric. Lines break at:

- Capture-clock regressions, including those hidden by filters, or equal
  sample timestamps.
- Capture intervals exceeding the applied maximum line gap.
- Missing, nonfinite or unverified values.
- Absolute angle changes greater than pi radians, without unwrapping values.

Markers remain observations, and connecting lines remain display aids. Neither
establishes continuous measurement or a cause for a gap. The horizontal axis is
relative capture time; device timestamps do not replace that clock. Hover
details retain original record indices, exact capture timestamps and byte
ranges. Point display coordinates do not reconstruct the original timestamps.
Detailed decoded-field and raw-frame inspection remains in the existing
workspace.

## Review import status

Provenance retains the source label, fingerprint, input size, selected profile,
dialect and decoder version. Traversal status, consumed bytes and the retained
remainder distinguish a completed traversal from a partial import. Unknown
message IDs can remain opaque without ending traversal; malformed or
unsupported records can stop it while leaving the accepted prefix usable.
An empty file or an input with no accepted records still has an import outcome,
provenance and any issues.

**Import issues** uses pages of up to **100 entries**, with original record
indices and byte offsets. Navigate the pages to inspect issues for the whole
input, including records outside the current filters. Total issue counts do not
mean that every entry is present on the current page.

Logging timestamps and device timestamps remain distinct. Timestamp units do
not establish clock resolution, accuracy or synchronization; the recording's
outer timestamps are not protected by the MAVLink frame checksum. No packet-loss
diagnosis or clock alignment is inferred. See the
[Analyze observation limits](analyze.md#read-the-activity-plot-and-intervals).

## Complete workflows

Start the existing interface in a separate terminal:

```sh
uv run --locked uav-debugger-analyze --port 8501
```

Then start Instrument with the matching optional handoff port:

```sh
uv run --locked uav-debugger-instrument --port 8765 --classic-port 8501
```

This exposes a link to the separately running full workspace. The ports must
differ. Instrument does not start, supervise or verify that server, and no
recording or selection is transferred across the link. Choose Analyze or
Experiment within the full workspace as needed. Instrument's Experiment control
opens the handoff dialog; it does not select a mode in the full workspace.
Without `--classic-port`, the dialog reports that no existing workspace is linked.
Use the commands above to start and link it. With SSH access, forward both
configured ports to open both interfaces from the browser computer.

The existing `uav-debugger-analyze` and `uav-debugger-experiment` commands retain
their behavior. Opening or analyzing any file, changing a theme or starting
either interface never executes an experiment; execution still requires
explicit Start or an Experiment CLI command.

## Local data boundary

The HTTP interface performs stateless analysis of the fixed example or uploaded
bytes. It does not accept an arbitrary server filesystem path.

| Route | Response |
| --- | --- |
| `GET /health` | Server availability. |
| `GET /api/config` | Schema version, installed application version and optional local full-workspace URL. |
| `GET /api/example` | Analysis of the installed example with the requested selection and issue page. |
| `POST /api/analyze` | Analysis of a raw `application/octet-stream` recording body, limited to 10 MiB. |
| `GET /` and `/assets/...` | Packaged interface, fonts and icons. |
| `GET /vendor/plotly.min.js` | JavaScript bundle from the installed Plotly dependency. |

The config and analysis responses declare `schema_version: 2`. Analysis accepts
these query parameters:

| Parameter | Meaning |
| --- | --- |
| `source` | `system_id:component_id`, or omitted for all sources. |
| `message_id` | Integer message ID, or omitted for all types. |
| `start`, `end` | Exact decimal seconds relative to the first imported record; omitted bounds use the imported minimum and maximum timestamps. |
| `gap` | Positive exact decimal seconds for the maximum line gap; defaults to `1`. |
| `issue_page` | Zero-based page of up to 100 global import issues; defaults to `0`. |
| `name` | Upload display label for `POST /api/analyze`; never resolved as a path. |

The response keeps whole-recording counts separate from selected counts. Its
`issues` list contains only the current page; `issue_count` and `issue_counts`
retain the full input's total and per-code counts. Exact integer capture
timestamps and filter bounds are decimal strings in JSON, including chart
point references. Relative chart coordinates and attitude angles are display
numbers. The browser does not recover original timestamps from floating-point
coordinates.

Invalid queries or filters return `400`, uploads exceeding the byte limit return
`413`, and unsupported media types or content encodings return `415`. Partial
and empty imports return analysis outcomes rather than transport errors.

Each request imports its own bytes and computes the applied selection. For an
uploaded file, the browser resends that file when applying filters, changing the
line gap or paging issues. The server keeps no mutable recording session or
cache and writes no recording to disk. Browser tabs own independent inputs and
selections; theme preference is the only persisted browser setting. Reloading
the page does not restore an uploaded file. The **10 MiB** input-byte limit does
not bound total process memory or guarantee performance.

There is no Experiment command endpoint or execution bridge. This bounded API
is an implementation contract for the current workspace, not a general
integration API.

## Development checks

The focused server and packaging checks are:

```sh
uv run --locked pytest tests/test_instrument.py tests/test_distribution.py
```

Instrument's browser workflows use the repository's optional Playwright setup;
see [browser workflow checks](analyze.md#browser-workflow-checks). After installing
the browser dependencies and Chromium, run:

```sh
uv run --locked --group browser pytest --run-browser tests/test_instrument_browser.py
```

Source and wheel archive verification requires every declared interface asset and license
to match its source bytes and rejects undeclared archive members. This also
ensures those assets are included when installing the package outside the
source tree; browser tests verify their runtime loading separately.
