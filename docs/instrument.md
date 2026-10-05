# Instrument workspace

Instrument is UAV Debugger's custom local web interface for saved telemetry
recordings. Open a supported file or the bundled example, review import status
and provenance, apply exact source/type/time filters, and inspect activity and
attitude observations. Inspect original messages and download Markdown evidence
reports for the applied selection. Light, dark and system appearance use local
display assets. The importer, telemetry model, plotting rules and report builder
are shared with the existing interface. **Saved experiment** opens one evidence
directory, while **Local experiments** discovers saved runs under the configured
server-side root.

Saved-run comparison and explicit Experiment execution remain available through
`uav-debugger-analyze`. Instrument does not yet replace those workflows and is
not included in the published v0.2.0 artifacts.

## Launch

From an installed source checkout:

```sh
uv sync --locked
uv run --locked uav-debugger-instrument
```

Open [Instrument](http://127.0.0.1:8765) on the same computer. `--port PORT`
selects another unused local port. The server binds only to `127.0.0.1`;
`Ctrl+C` stops it. No vehicle, simulator or ARGOS installation is required.
The optional `--experiment-root PATH` selects the root for local saved-run
browsing, defaulting to `local/experiments` relative to the launch directory.
The launcher interprets it as an absolute location without creating it. This
setting permits read-only browsing; it does not enable Experiment execution.

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
Reports download to the computer running the browser.
Saved-directory uploads also select files on the browser computer; the local
catalog instead reads the configured root on the server.

## Open a recording

In **Recording**, choose **Open recording** for a file up to **10 MiB**, or **Load example** for
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
4. Inspect a marker or choose record **#2** from **Messages** to read its
   original evidence in **Inspector**.
5. Choose **Download report** to export this applied selection and the
   inspected record.

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
Clicking an attitude marker inspects its original record and selects its message
page while leaving the chart visible. Activity bins and unobserved gaps do not
identify an individual record and cannot select one for inspection.

## Inspect messages

**Messages** shows at most **100 selected records per page**, in original file
order. Paging changes the visible rows, not the applied selection or chart
counts. Each row retains its original zero-based record index, capture time,
source, message identity and checksum status. Filtering and paging never
renumber the records.

Select a record from a table row or use **Record index** to inspect a specific
original index. The index must belong to the applied selection; inspection
navigates to its corresponding message page. No record is selected implicitly
when a recording is opened or a page is displayed.

The right-hand **Inspector** shows the exact integer capture timestamp, source,
wire version, sequence number, checksum status, decoded fields and original
bytes. **Evidence** retains the recording's global provenance and import issues.
Record byte ranges include the eight-byte outer timestamp; frame byte ranges
begin at the MAVLink marker. Both use zero-based offsets and an exclusive end.
The hexadecimal byte displays retain the original timestamp and frame bytes,
including the frame's checksum bytes.

Decoded fields are shown as JSON text without rounding large integer values
through JavaScript numbers. Nonfinite floating-point values and byte arrays use
explicit tagged representations. Opaque records keep their headers and bytes
with an unverified checksum status, without invented decoded fields.

Successful filter or line-gap changes and message-page changes clear the
inspected record. Replacing or clearing a recording also clears it. Draft
edits, invalid filter submissions, chart zoom and switching views do not choose
a different record or silently apply filters.

## Download a report

**Download report** creates a fresh Markdown evidence summary from the applied
source/type/time filters, the applied line gap and the explicitly inspected
record, if any. Unapplied control edits and plot zoom are not report filters.
The report includes:

- Input name, fingerprint, byte size, application version, importer profile,
  dialect and decoder version.
- Exact inclusive capture-time bounds, source/type filters and the original
  relative-time origin.
- Complete import and selected counts, traversal status, consumed bytes and
  unprocessed remainder.
- Observation-interval evidence, attitude plot coverage, point limits and the
  applied line-gap setting when imported records exist.
- Clock and observation limits, global import issues and any explicitly
  inspected record's decoded fields, byte references and original raw frame.

Counts cover the **whole applied selection**, not just the visible message
page. Without an explicit inspection, the report contains no individual record
details. A valid empty selection, empty file or partial import still permits an
evidence report describing its outcome.

Reports include at most **100 import issues**, with an explicit omitted count.
Errors take priority within the limit, followed by the earliest other issues;
included entries keep their original order. This does not truncate the imported
or selected counts. The report describes plot settings and source evidence,
not a chart image or a complete recording archive.

The service checks the supplied bytes against the applied recording's SHA-256
before export and rejects a mismatch rather than exporting evidence for another
input. The download filename uses that fingerprint. Uploaded recordings and
generated reports are not saved to server disk.

## Inspect a saved experiment

Choose **Saved experiment**, then **Open saved experiment** and select the
directory containing `run.json`, traces and captures. For a run produced by the
existing browser interface, select `run-<identifier>/evidence`, not its parent.
The directory is read as supplied files, not executed or extracted as an
archive. Opening another directory replaces the current input.

Uploads accept at most **64 files** and **64 MiB of file bytes in total**,
including files excluded from evidence analysis. Each capture retains the
**10 MiB** limit. Mixed directories, duplicate names and unsafe relative paths
are rejected. Only the fixed evidence names contribute to the run fingerprint;
other selected files are identified as excluded. Multipart bytes are parsed in
memory without temporary upload files. See the
[saved Experiment guide](saved-experiments.md#what-is-checked) for fixed names,
per-artifact limits and consistency checks.

Read **Declared outcome** and **Evidence status** separately. A declared
`completed` run can have invalid evidence, and a declared `failed` run can have
consistent evidence. Uploaded evidence declaring `running` remains inspectable
as unfinalized; it does not establish an active process. Missing captures are
unavailable, not empty captures with invented zero counts.

**Requested settings**, **Applied forwarding interruption** and **Observed
captures** distinguish configuration, validated action references and actual
captured records. **Run timeline** counts consistent observation references in
at most **200 bins** of this run's monotonic clock. Startup, measurement origin
and gate markings come from recorded evidence. An unclosed gate has no
established duration. Above **100 applied gate intervals**, Instrument declines
to render the timeline instead of omitting intervals silently; the full counts
and paged interval references remain available.

**Evidence issues** and **Trace references** page up to **100 entries** at a
time, independently of the capture filters. **Run provenance** retains original
artifact fingerprints and clock metadata. Exact integer clock values remain
text rather than rounded browser numbers. A mismatched artifact fingerprint
excludes its references from the consistent timeline; a readable capture can
still be inspected with its evidence issues visible.

Choose **Relay input** or **Receiver** under **Observation point** to use the
ordinary capture filters, charts, messages and inspector. Receiver is selected
initially when present, otherwise relay input; no point is invented if neither
capture exists. Changing point resets the capture filters and inspected record.
Capture plots remain relative to that file's first Unix capture timestamp;
they are not aligned to the run timeline or payload clocks.

**Download report** now exports the saved-run summary together with the selected
capture's applied filters, line gap and explicitly inspected record. It remains
available without a capture and reports unavailable evidence explicitly.
Run report excerpts retain their existing bounds and omission counts; the
visible trace or issue page does not restrict the summary. The complete run
fingerprint guards subsequent analysis and export, not just the capture's hash.
Clearing or replacing the input invalidates the preceding capture, inspector
and pending report without deleting the original directory.

## Browse local experiments

Start Instrument with the intended server-side root, for example:

```sh
uv run --locked uav-debugger-instrument --experiment-root local/experiments
```

Choose **Local experiments** and review the listed **declared** metadata.
**Refresh catalog** requests a new snapshot. **Open in Analyze** rereads the
selected evidence through the saved-run validator; listing metadata never
establishes capture integrity. Runs remain browsable after restarting the
service without restoring execution state.

The catalog recognizes only `<root>/<name>/run.json` and
`<root>/<name>/evidence/run.json`. Root-relative keys identify entries, including
both layouts when present below one child. The root comes from the launcher;
the browser cannot supply an arbitrary server path. An absent root produces an
empty catalog with an explicit issue and is not created. Links and special
files are rejected, and directory descriptors stay pinned while evidence is
read.

Scanning examines at most **200 direct children**, reads at most **1 MiB per
manifest** and **8 MiB of manifest bytes in total**, and identifies a partial
catalog when a scan bound is reached. It reads no captures, traces or
`control.json`. A manifest declaring `running` is listed as unfinalized but
cannot be opened through the catalog; no process liveness is inferred.

Each later interaction rereads the selected local evidence and checks the
expected run fingerprint. If the files have changed, reopen the run to analyze
their new state. A stale response or report must not replace the currently
selected run or capture. Browsing and opening remain read-only; comparison and
Experiment execution are separate workflows in the existing interface.

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

The HTTP interface performs stateless analysis of the fixed example, uploaded
bytes or a fixed-layout catalog entry beneath the configured root. It does not
accept an arbitrary server filesystem path.

| Route | Response |
| --- | --- |
| `GET /health` | Server availability. |
| `GET /api/config` | Schema version, installed application version and optional local full-workspace URL. |
| `GET /api/example` | Analysis or Markdown report for the installed example with the requested selection. |
| `POST /api/analyze` | Analysis or Markdown report for a raw `application/octet-stream` recording body, limited to 10 MiB. |
| `POST /api/run` | Saved-run analysis or report from bounded, in-memory `multipart/form-data` directory uploads. |
| `GET /api/catalog` | Bounded snapshot of declared manifest metadata beneath the configured experiment root. |
| `GET /api/catalog/open` | Reread and validate a selected root-relative catalog key for analysis or report export. |
| `GET /` and `/assets/...` | Packaged interface, fonts and icons. |
| `GET /vendor/plotly.min.js` | JavaScript bundle from the installed Plotly dependency. |

The config and JSON analysis responses declare `schema_version: 4`. Recording
analysis accepts these query parameters:

| Parameter | Meaning |
| --- | --- |
| `source` | `system_id:component_id`, or omitted for all sources. |
| `message_id` | Integer message ID, or omitted for all types. |
| `start`, `end` | Exact decimal seconds relative to the first imported record; omitted bounds use the imported minimum and maximum timestamps. |
| `gap` | Positive exact decimal seconds for the maximum line gap; defaults to `1`. |
| `issue_page` | Zero-based page of up to 100 global import issues; defaults to `0`. |
| `record_page` | Zero-based page of up to 100 selected messages; defaults to `0` unless resolved from `record_index`. |
| `record_index` | Explicit original record index in the applied selection; omission leaves the inspector empty. |
| `format` | `json` by default, or `markdown` for a report download. |
| `sha256` | Expected input fingerprint: 64 lowercase hexadecimal characters, required for Markdown export and optional for JSON analysis. |
| `name` | Upload display label for `POST /api/analyze`; never resolved as a path. |

The response keeps whole-recording counts separate from selected counts. Its
`issues` list contains only the current page; `issue_count` and `issue_counts`
retain the full input's total and per-code counts. Exact integer capture
timestamps and filter bounds are decimal strings in JSON, including chart
point references. Relative chart coordinates and attitude angles are display
numbers. The browser does not recover original timestamps from floating-point
coordinates.

`selection.messages` contains the page number, page size/count, complete
selected count and the current rows. `inspector` is `null` unless `record_index`
is explicitly supplied. When supplied alone, that index resolves its message
page; when a page is also supplied, the index must belong to that page. Decoded
`fields_json` stays a serialized string, not a JavaScript object with rounded
integers. `raw_frame_hex` contains the original frame; `raw_record_hex` also
includes its outer timestamp. Markdown responses use `text/markdown` and an
attachment filename derived from the recording fingerprint.

Saved-run requests additionally accept `point` (`receiver` or `relay-input`),
`run_sha256` for the expected complete evidence fingerprint, and independent
zero-based `evidence_page`, `gate_page` and `trace_page` selections. The `trace`
parameter accepts `actions` (default) or `observations`. Run reports require
`run_sha256`; a capture-only hash cannot guard changed manifests or actions.
Their filename is `uav-debugger-run-<first 12 fingerprint characters>.md`.
`GET /api/catalog/open` requires the catalog `key`, never a filesystem path.
An explicitly requested missing observation point is rejected instead of being
silently replaced by another point.

`POST /api/run` takes file parts named `files`, with each multipart filename set
to its relative directory name, as supplied by the browser directory picker.
A shared selected-directory prefix, when present, is removed before fixed
artifact names are validated. All file parts contribute to the 64-file and 64 MiB input bounds,
even when their contents do not contribute to the evidence fingerprint.
The whole multipart body is limited to 65 MiB, with at most 1 MiB of multipart
overhead. Each part accepts at most eight headers and 8 KiB of header content;
relative filenames are bounded to 4,096 bytes and the boundary to 70 bytes.

Invalid queries or filters return `400`, uploads exceeding the byte limit return
`413`, and unsupported media types or content encodings return `415`. A mismatch
with the expected SHA-256 returns `409`. Partial and empty imports return
analysis outcomes rather than transport errors.

Each request imports its own bytes and computes the applied selection. For an
uploaded file, the browser resends that file when applying filters, changing the
line gap, inspecting a message, paging or exporting a report. Uploaded run
requests similarly resend their selected files; catalog requests reread the
fixed local evidence. The server keeps no mutable analysis session or cache and
writes no uploaded recording, run or report to disk. Browser tabs own independent
inputs, selections and inspectors; theme preference is the only persisted
browser setting. Reloading the page does not restore an uploaded input. Input
byte limits do not bound total process memory or guarantee performance.

There is no Experiment command endpoint or execution bridge. This bounded API
is an implementation contract for the current workspace, not a general
integration API.

## Development checks

The focused server and packaging checks are:

```sh
uv run --locked pytest tests/test_instrument.py tests/test_report.py tests/test_distribution.py
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
