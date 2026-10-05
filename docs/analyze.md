# Analyze a saved recording

Analyze opens one supported telemetry recording in a local browser interface.
It provides source and time filters, activity and attitude plots, message inspection
and a Markdown evidence report. The same file-only importer is available through the
[Python API and command-line summary](importer.md).

**Instrument is the recommended interface for the current source checkout.**
This guide uses its recording controls unless an existing-interface difference
is stated. The historical Streamlit launcher and port 8501 remain available;
published v0.2.0 artifacts contain that interface, not Instrument. See
[interface coverage and differences](instrument.md#interface-coverage-and-differences).

The optional [Experiment interface](experiment-ui.md) and [CLI](experiment.md)
produce `relay-input.tlog` and `receiver.tlog` captures for this same workflow.
Open them individually; their outer timestamps identify actual reads at different
observation points. Analyze
also offers **Saved experiment** in the **0.2.0** source checkout to
[inspect a complete run directory](saved-experiments.md), including JSON traces
and both capture points. **Compare experiments** provides a bounded
[baseline/blackout comparison](comparison.md) with explicit windows and evidence
checks. Opening a capture or saved run never starts or resumes execution.
The application opens in **Analyze** mode; choose **Experiment** separately to
configure an explicit launch. A completed or interrupted browser run can open
directly in Analyze through **Open in Analyze**, without uploading its files.
Choose **Local experiments** under **Analyze input** to
[browse saved runs on the server](instrument.md#browse-local-experiments),
including runs retained before a restart. Browsing reads declared metadata;
opening or comparing selected runs performs the evidence checks.

## Install and launch

The initial runtime target is Linux with Python 3.12. From the repository root,
install the locked dependencies and start Analyze:

```sh
uv sync --locked
uv run --locked uav-debugger-instrument
```

Open [Instrument](http://127.0.0.1:8765) in a browser on the same
computer. Keep the terminal process running while using the interface; press
`Ctrl+C` there to stop it.

The module entry point supports another local port:

```sh
uv run --locked python -m uav_debugger.instrument --port 8766
```

For that command, open [Instrument on the alternate port](http://127.0.0.1:8766).
Ports must be between 1 and 65535. The launcher binds to `127.0.0.1`
and does not automatically open a browser. Its connection serves the
local interface; opening a recording never connects to a vehicle or starts an
experiment.

Dependency installation can require network access. The application uses local
files and bundled display components after installation; it needs no simulator,
ARGOS installation or external service to analyze a recording.

### Existing launcher compatibility

`uv run --locked uav-debugger-analyze` still starts Streamlit on port 8501.
`python -m uav_debugger.analyze --port 8502` remains its alternate-port module
entry point. It disables usage statistics and retains its existing controls;
neither command redirects to Instrument. This second server is optional, not
an Instrument prerequisite. Its uploads, filters, comparison roles, active
worker and in-memory history are not transferred. Reopen saved files or browse
the same explicit output root to analyze retained evidence in another interface.

### Install the wheel

Build the **0.2.0** wheel locally from this source checkout:

```sh
uv build --out-dir local/dist
```

From an empty directory outside the checkout, create an environment and install
that wheel. Replace the absolute path below with the generated artifact's path:

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python /absolute/path/to/uav_debugger-0.2.0-py3-none-any.whl
.venv/bin/uav-debugger-instrument --experiment-root ./experiments
```

This current-source wheel includes Instrument, the bundled example and optional Experiment
interface. New browser runs use the explicitly selected `experiments` root;
the local catalog reads that same root, which is created only by Start.
Dependency installation may need network
access. The source quick start above remains the path for installing from the
repository's complete dependency lock.
Although its package version is still 0.2.0, a newly built source wheel is not
the published v0.2.0 artifact. The latter does not include Instrument.

## Access through SSH

Start Analyze on the remote machine using the launch command above. In a
separate terminal **on the computer running your browser**, open a local SSH
forward using the same destination and authentication options as your normal
SSH connection:

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 user@remote-host
```

Replace `user@remote-host` with your SSH destination and keep the tunnel running.
Open [Instrument](http://127.0.0.1:8765) in your local browser.
Analyze continues listening on remote loopback; the SSH connection forwards the
browser traffic to it. If local port 8765 is occupied, change only the first
port to 8766 and open `http://127.0.0.1:8766` instead. For the unchanged
Streamlit launcher, forward its default port 8501 instead. See the
[OpenSSH local forwarding documentation](https://man.openbsd.org/ssh#L).

**Load example** reads the bundled recording on the server, so no file transfer
is needed to try the workflow. **Open recording** selects a file on the browser
computer and uploads it to the machine running Analyze. Reports download to the
browser computer. These instructions describe SSH forwarding; a connection to
your particular remote host is not part of the automated browser verification.

## Inspect the supplied example

1. Click **Load example**. The bundled recording opens without uploading a file
   and is identified as a **Synthetic example** in the interface and report.
2. Review **Input provenance** and **Import issues** under **Evidence**. The example contains
   12 decoded records from two sources, with five expected repeated-timestamp
   warnings from its artificial logging clock.
3. Set **Source** to **1 / 1**, **Message type** to **ATTITUDE**, **Start (s)** to
   **1**, and **End (s)** to **5**. Click **Apply filters**.
4. The selection contains records **#2** and **#8**. **Longest interval**
   reads **4 s**. **Attitude** shows two points per angle, disconnected at the
   default **Maximum line gap (s)** of **1**. Click a marker or a record button
   in **Messages** to inspect its fields and original bytes.
5. Click **Download report** to save a Markdown summary containing the applied
   selection and the currently inspected record.

The four-second interval is between recorded observations of source 1's
`ATTITUDE` messages. It does not establish physical packet loss or explain the
vehicle's behavior. The other source has recorded observations during that
interval. See the [fixture documentation](../tests/fixtures/README.md) for the
complete expected sequence, source identities and synthetic provenance.

The example uses the same importer, filters, inspector and report as uploaded
recordings. Its bytes match `tests/fixtures/telemetry-gap.tlog`; it is included
in installed packages and requires no download. **Clear recording** removes it
from the current session. Uploading a file replaces the example and resets the
filters. Clearing that upload leaves an empty view, without restoring the
example. Loading the example after an upload clears the uploader and replaces
the previous analysis.

## Apply source and time filters

**Source** selects one system/component pair or all sources. **Message type**
selects one message ID or all types; unsupported IDs appear as
`UNKNOWN_<message_id>`. Both controls act on the same active selection as the
time bounds.

**Start (s)** and **End (s)** are decimal seconds relative to the first imported
record's capture timestamp. They remain anchored to that record when filters
change. Both bounds are inclusive. For example, `1.000001` selects a boundary
one second and one microsecond after the origin. Values requiring fractions of
a microsecond are rejected rather than rounded. A decreasing capture clock can
produce negative relative times; the original file order remains unchanged.

Click **Apply filters** to update the plots, table, interval metric and report.
Editing the controls alone leaves the previous selection active. Invalid bounds
produce an error and preserve that previous selection. **Applied selection**
shows the bounds currently used. A valid selection with no matching records
still permits a report describing its filters and the import outcome.

**Recording total** and **Sources** summarize the complete accepted input.
**Selected** counts the active filtered selection. Import issues always
describe the whole input, including records outside the filters.

## Read the activity plot and intervals

**Message activity** aggregates every selected record into at most 200 time
bins. Hover over a bar to see its time bounds and observation count. Aggregation
keeps the plot bounded; use the message table and inspector for individual
timestamps. Zooming the plot changes its display without changing the filters
or report.

**Longest interval** considers consecutive selected records with the
same system ID, component ID and message ID. It does not measure between
different sources or message types. A pair crossing a capture-clock regression
has no established duration, even when the regressing record is hidden by a
filter. Pairs with opaque endpoints are also excluded from the duration metric.
Repeated timestamps can yield a zero interval. **N/A** means no eligible pair
exists in the selection.

Capture timestamps are integer Unix-epoch microseconds under the selected
host-logging profile. These units do not establish clock resolution, accuracy
or synchronization with the vehicle. Device timestamps remain separate decoded
fields. The outer recording timestamp is not covered by the MAVLink frame
checksum. No clock alignment, sensor latency or causal diagnosis is inferred.

## Inspect attitude curves

**Attitude** plots the `roll`, `pitch` and `yaw` fields of checksum-valid
`ATTITUDE` messages, retaining their original values in radians. The angles
follow the [MAVLink ATTITUDE definition](https://mavlink.io/en/messages/common.html#ATTITUDE).
The shared horizontal axis uses capture time relative to the first imported
record; `time_boot_ms` remains a separate field in the message inspector.

The plot respects all applied source, message-type and time filters. If the
selected `ATTITUDE` records contain several sources, select one **Source** and
apply the filters. Selecting another message type leaves no attitude records
to plot. Each plot accepts at most **5,000 ATTITUDE records**; larger selections
show a request to narrow the time bounds. No points are silently decimated, and
the message table and report still cover the full filtered selection.

**Maximum line gap (s)** controls whether consecutive samples are connected.
It defaults to **1 second** and takes effect with **Apply line gap** or Enter.
Leaving the field alone does not apply it in Instrument. It accepts positive decimal seconds with
microsecond precision. Invalid input preserves the last valid setting; the
caption shows the applied value. The setting changes the display and report,
not the selected records or interval metric.

Each angle's line breaks at:

- A capture-clock regression anywhere between its records, including a
  regression hidden by filters, or equal sample timestamps.
- An interval greater than the applied maximum line gap.
- A missing, nonfinite or unverified field value, which has no plotted point.
- An absolute change greater than π radians, avoiding a line across an angle
  wrap. Values are not unwrapped or otherwise corrected.

Connecting lines are visual guides between recorded samples; they do not
establish continuous measurement or identify an anomaly. Markers and original
file order are retained across breaks. Float coordinates are used only for
display; hover labels retain exact capture timestamps and record byte ranges.

Click a marker to inspect its original message. The **Messages** table moves to
the relevant page, **Inspector** opens that message, and the downloaded report
includes its evidence. Manual record selection remains available. Changing
filters, the gap setting or the recording clears the previous plot selection.
Plot zoom changes only the display and is not a report filter.

## Inspect records and import issues

The **Messages** table shows up to 100 selected records per page in original
file order. Use the paging controls and a record button to inspect a message,
or enter an original **Record index** and choose **Inspect record**. No record
is selected automatically. Record numbers are zero-based indices in the original imported
sequence; filtering and paging do not renumber them.

The inspector shows the original integer capture timestamp, wire version,
sequence number, checksum status, decoded fields and raw frame bytes. Byte
ranges are zero-based and use an exclusive end. The record range includes its
eight-byte outer timestamp; the frame range begins at the MAVLink marker.
Unknown message IDs retain raw bytes and unverified headers without invented
decoded fields.

**Import issues** has separate pages of up to 100 entries, with issue counts,
record indices and byte offsets. Damaged input or an unsupported feature stops
the importer at the affected record. Earlier accepted records remain
inspectable, and **Input provenance** identifies the input fingerprint,
decoded profile, consumed bytes and unprocessed remainder. Empty input has its
own visible outcome. These outcomes remain available in a status report.

## Download an evidence report

**Download report** saves a Markdown document named from the input's SHA-256
prefix. It contains:

- The source name, full input fingerprint, byte size, application version,
  importer profile, dialect and decoder version.
- The applied source/type filters and exact inclusive capture-time bounds.
- Complete import and filtered counts, traversal status and unprocessed bytes.
- Observation-interval counts and the longest established interval, including
  its duration and both original record references.
- Attitude plot availability, fields and units, selected/plotted counts, display
  limit, unavailable-value counts and the applied maximum line gap when the
  recording has imported records. The report describes the plot rather than
  embedding an image.
- Clock and observation limits, plus issues from the whole imported input.
- The currently inspected record's identity, byte references, decoded fields
  and original frame. A selection with no records includes no record details.

The issue list is limited to 100 entries. An error explaining an import stop
takes priority; remaining places contain the earliest issues, displayed in
their original order. Total, included and omitted issue counts are explicit.
The report includes details for the inspected record. It is an evidence summary,
not a recording archive.

External labels and decoded text appear as data in fenced JSON blocks. Byte
arrays and nonfinite numeric values use explicit tagged representations so that
their meaning remains inspectable. The source recording is never modified;
the browser handles saving the separate report.

## Input and capacity boundaries

The current `qgc-timestamped-mavlink-v1` profile accepts unsigned MAVLink 1/2
frames with QGroundControl-style timestamps and the pinned `common` dialect.
Verification uses synthetic recordings with documented bytes and expected
values, plus [one public QGroundControl recording](recording-validation.md)
with complete traversal and partial decoding coverage. Its reported producer
build and exact fingerprint are documented; broader version compatibility is
not established.
A `.tlog` filename does not identify its contents. See the
[importer guide](importer.md#input-profile) for exact failure and unsupported-input
behavior.

The interface accepts files up to **10 MiB (10,485,760 bytes)** and rejects
larger uploads. This limits input bytes, not total process memory or response
time. Decoded objects, selected views and browser data require additional
memory, which depends on record density and message contents.
Files over the analysis limit are rejected even if their browser upload completes.

A Chromium measurement before attitude plotting was added, on Linux x86_64
with Python 3.12.3 and the existing Streamlit interface, loaded
10,485,750 bytes containing 419,430 synthetic HEARTBEAT records with increasing
timestamps. Upload through a ready report took 6.800 seconds; applying inclusive
1–5 second bounds took 0.503 seconds and selected exactly 4,001 records. The
activity bins retained the full count and the downloaded report matched the
new selection. Peak server RSS was 548.2 MiB, excluding the browser. This is
one measured message mix, not a general response-time or memory guarantee.

Instrument retains the selected File in its browser tab and resends its bytes
for filters, paging, inspection and reports. Each request imports its own bytes;
there is no shared recording cache or saved analysis session. The existing
Streamlit interface instead retains one parsed input per browser session and
reuses that result. Changing input replaces the active recording in either
interface. Neither transfers an upload or selection to the other.

Active execution belongs to the separate [Experiment mode](experiment-ui.md).
Video synchronization, broader session comparison, additional recording formats
and vehicle connections remain outside this delivered Analyze workflow. Other
operating systems require their own verification.

## Browser workflow checks

Ordinary `uv run --locked pytest` runs the core checks and skips browser tests.
When `--run-browser` is explicitly requested, missing Playwright is an error
rather than a skipped browser suite.
To install the optional browser dependencies and run the Chromium workflow:

```sh
uv sync --locked --group browser
uv run --locked --group browser playwright install chromium
uv run --locked --group browser pytest --run-browser tests/test_instrument_browser.py
```

These checks launch the local interface and use a real browser. The browser
harness blocks non-local requests while exercising the application. Installing
the browser is separate from running the tests and may require network access.
The historical Streamlit recording workflows remain in
`tests/test_analyze_browser.py`. The results below describe their stated older
versions, not a current Instrument verification claim.

For version 0.1.0 on 2026-09-22, on Linux x86_64 with Python 3.12.3,
all 292 tests passed in a fresh environment
with locked dependencies and the installed application wheel, inside a Linux
network namespace with only loopback enabled. This includes seven Chromium
workflows covering upload, inclusive filtering, inspection, downloaded report
contents, replacement of a recording, empty and truncated input, rejection
above the analysis size limit, and the bundled example without an upload.
The attitude workflows use actual marker clicks to check inspector/report
agreement, navigation to another table page, gap-setting validation, manual
record selection and resetting stale plot/page state after filters or input change.
Core checks cover per-field unavailable values, capture-clock regressions hidden
by filters, repeated times, angle wraps, exact timestamp metadata and the
5,000-record display limit. A separate check of the
[public recording](recording-validation.md#attitude-plot-verification-in-010dev3)
compared all 1,329 plotted attitude records with independently unpacked payloads.
The example's packaged bytes match the original fixture; switching between
example and upload resets the controls and clearing an upload does not restore
previous example data.

See the [project overview](../README.md) for the remaining development checks
and the [architecture](architecture.md) for the shared analysis components.
For the automated workflow and checks against built distributions, see
[verification and release preparation](verification.md).
