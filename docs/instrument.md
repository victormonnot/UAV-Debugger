# Instrument workspace

Instrument is UAV Debugger's custom local web interface. Its initial scope is
analysis of the distributed synthetic recording: provenance, import status,
source selection and attitude observations, with light and dark themes.
It uses the existing Python importer, telemetry model and plotting logic.

The complete interface remains available through `uav-debugger-analyze` for
other recordings, filters, record inspection, reports, saved experiments,
comparison and explicit Experiment execution. Instrument does not yet replace
those workflows and is not included in the published v0.2.0 artifacts.

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

## Analyze the bundled recording

Load the example to read the installed `telemetry-gap.tlog` fixture through the
real importer. The fixture contains 12 records from two sources and five
expected repeated-timestamp warnings. Its fingerprint identifies the exact
input bytes; the synthetic designation remains visible.

Select a source to view its `ATTITUDE` observations. Angles retain their original
radian values. The horizontal axis uses capture time relative to the first
record in the file; device timestamps do not replace that clock. Hover details
refer to original record ordinals, capture timestamps and byte offsets. For
source `1 / 1`, the four-second interval between records `#2` and `#8` remains
disconnected at the one-second maximum line gap.

Line breaks follow the existing chart rules for missing values, clock
regressions, angle jumps greater than pi and capture intervals beyond the line
gap. They do not diagnose packet loss or establish what the vehicle did during
an unobserved interval. Plot zoom changes the display, not the imported evidence.
The [fixture reference](../tests/fixtures/README.md) describes its provenance and
expected observations; the [Analyze guide](analyze.md) documents the full
recording workflow and observation limits.

The theme choice is stored in the browser. Clearing the example removes the
browser's current recording view; it does not modify the packaged recording.
Opening the page or changing a theme never starts an experiment.

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
Use the commands above to start and link it.

The existing `uav-debugger-analyze` and `uav-debugger-experiment` commands retain
their behavior. Starting the full interface does not execute an experiment;
execution still requires explicit Start or an Experiment CLI command.

## Local data boundary

Instrument serves a fixed bundled example, not arbitrary filesystem paths or
uploaded content. Its initial HTTP contract consists of read-only routes:

| Route | Response |
| --- | --- |
| `GET /health` | Server availability. |
| `GET /api/config` | Schema version, installed application version and optional local full-workspace URL. |
| `GET /api/example` | Imported recording provenance, counts, issues and per-source attitude summaries/figures. |
| `GET /` and `/assets/...` | Packaged interface, fonts and icons. |
| `GET /vendor/plotly.min.js` | JavaScript bundle from the installed Plotly dependency. |

The two API responses declare `schema_version: 1`. Exact integer capture
timestamps are decimal strings in JSON, including chart point references;
relative chart coordinates and attitude angles are display numbers. The browser
does not recover original timestamps from floating-point coordinates.

Each example request reads and imports the known installed fixture. The server
keeps no mutable recording session or execution controller. Browser tabs own
their displayed recording and source selection; theme preference is the only
persisted browser setting. There is no upload endpoint, user path parameter,
Experiment command endpoint or execution bridge. This bounded API is an
implementation contract for the current workspace, not a general integration
API.

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
