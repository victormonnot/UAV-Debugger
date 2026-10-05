# UAV Debugger

A local tool for inspecting UAV recordings and running bounded MAVLink
experiments, with original frames, explicit clocks and evidence reports.

**Analyze** opens saved recordings, filters sources and time, plots attitude,
inspects message activity and raw frames, and exports Markdown reports. It also
inspects saved experiments and compares a baseline with a blackout.
**Experiment** runs a synthetic sender or pinned ArduCopter SITL source through
a local UDP relay to a receiver. Explicit Start/Stop controls and a CLI produce
captures before and after the relay, retaining requested settings, applied
actions and actual observations separately.

**This source checkout targets version 0.2.0.** It adds local Experiment
execution, saved-run inspection and comparison, a read-only experiment catalog
and configurable forwarding interruptions to the offline Analyze workflow.
The commands below use this checkout. Published versions and their artifacts
are listed in [GitHub Releases](https://github.com/victormonnot/UAV-Debugger/releases). See
[verification and release preparation](docs/verification.md) for actual checks
and hosted CI status.

The source checkout also provides the new [Instrument workspace](docs/instrument.md):
a custom local interface for opening supported recordings, applying source,
message-type and exact time filters, reading activity and attitude plots,
inspecting original messages and exporting Markdown evidence reports.
It includes input provenance, import issues and light/dark themes. The existing
interface below retains the complete saved-run and Experiment workflows during
the transition.

See the [Experiment interface](docs/experiment-ui.md),
[saved Experiment inspection](docs/saved-experiments.md) and
[baseline/blackout comparison](docs/comparison.md).

## Quick start

The initial runtime target is **Linux with Python 3.12**. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/), then run these
commands from the repository root:

```sh
uv sync --locked
uv run --locked uav-debugger-analyze
```

Open [Analyze](http://127.0.0.1:8501) in a browser on the same computer and click
**Load example**. The bundled synthetic recording needs no upload and contains
12 messages from two sources. Select source **1 / 1**, message type
**ATTITUDE**, start **1** and end **5**, then click **Apply filters**: records
**#2** and **#8** show a four-second interval between those observations.

The **Attitude** plot shows roll, pitch and yaw in radians. Click a point or use
**Record** to inspect its decoded fields, original capture timestamp and frame
bytes. The example's four-second gap remains disconnected at the default
one-second maximum line gap. **Download report** exports the applied filters, plot settings, input
provenance, import limitations and the currently inspected record. Five repeated
timestamp warnings are expected in this fixture.

Choose your own file under **Open recording** to replace the example, or use
**Clear example** to return to an empty view. To use Analyze on a remote machine,
see [access through SSH](docs/analyze.md#access-through-ssh).

The launcher listens on `127.0.0.1` and disables usage statistics. Analysis
requires no vehicle, simulator or ARGOS installation. Dependency installation
can require network access. Recordings remain unchanged, and the application
holds one recording, one saved run, or a selected pair for comparison per browser
session in memory. The **10 MiB capture limit** and **64 MiB saved-run limit** bound input bytes,
not total memory use or guaranteed performance.

See the [Analyze guide](docs/analyze.md) for filtering, reports, alternate ports
and observation limits. For a command-line summary:

```sh
uv run --locked python -m uav_debugger tests/fixtures/telemetry-gap.tlog
```

The [importer guide](docs/importer.md) documents the Python API, JSON output and
exit codes; the [fixture documentation](tests/fixtures/README.md) records the
example's provenance and expected observations.

## Instrument workspace

After installation, start the custom interface with:

```sh
uv run --locked uav-debugger-instrument
```

Open [Instrument](http://127.0.0.1:8765) and choose **Load example** or
**Open recording**. Files up to **10 MiB** use the existing importer and retain
their fingerprint, capture clock and import warnings. Select a source, message
type and inclusive time bounds, then choose **Apply filters**. **Activity** and
**Attitude** use the same analysis logic as the full interface; plot zoom does
not change the applied filters. **Messages** pages the selected records in
original order. Inspect a record from the table or an attitude marker to read
its exact timestamp, decoded fields and original bytes. **Download report**
exports the applied selection, plot settings and explicitly inspected record,
with counts for the whole selection rather than only the visible page.
Fonts, icons and plotting assets are served locally. See the
[Instrument guide](docs/instrument.md) for partial imports, capacity limits and
file replacement behavior.

Saved-experiment browsing, inspection and comparison, and Experiment execution
remain in `uav-debugger-analyze`. Instrument can link to that interface
when it is started separately; see the [launch and handoff guide](docs/instrument.md).
Instrument does not launch an experiment, start the other server or transfer a
recording to it. This addition is not part of the published v0.2.0 artifacts.

## Run a local Experiment

In the current source checkout, choose **Experiment** under **Mode** in the
local interface. Select **Synthetic**, keep the six-second duration and click
**Start experiment** for a baseline. **Stop experiment** requests orderly
interruption. When the worker finishes, **Open in Analyze** validates and opens
the saved run. Repeat with **Blackout**, assign the two results with **Use as
baseline** and **Use as blackout**, then choose **Compare selected runs**.

All tabs share one active worker per server. Switching modes or closing a tab
lets the bounded run continue. Outputs remain on disk under
`local/experiments/run-<identifier>/evidence`; the separate `control.json` records
controller requests and state. See the [Experiment interface guide](docs/experiment-ui.md)
for server configuration, shutdown and the optional SITL source.

To find saved runs after restarting the server, choose **Local experiments**
under **Analyze input**, or **Browse saved experiments** in Experiment. The
catalog reads declared manifest metadata under `--experiment-root`; **Open in
Analyze** and **Compare selected runs** validate the selected evidence before
inspection. It does not restart runs. See
[browsing local experiments](docs/saved-experiments.md#browse-local-experiments).

The same scenarios are available from the command line:

```sh
uv run --locked uav-debugger-experiment --output local/experiments/baseline --scenario baseline
uv run --locked uav-debugger-experiment --output local/experiments/blackout --scenario blackout
```

Each run defaults to six seconds and requires a new output directory. A synthetic
20 Hz `ATTITUDE` sender, a byte-preserving relay and a receiver exchange UDP
datagrams on `127.0.0.1` in one process. The relay input and receiver record
messages they actually read as `relay-input.tlog` and `receiver.tlog`. Open each
file independently in Analyze, or choose **Saved experiment** and open the run
directory to inspect settings, applied actions and both capture points together.
Their clocks remain explicit; `Ctrl+C` stops the runner cleanly.

Blackout duration defaults to two seconds. Set **Blackout duration (s)** in the
interface or `--blackout-duration SECONDS` in the CLI to change it. The accepted
range is 0.1–59.8 seconds, with at least 0.1 second before and after the requested
interval inside the measurement duration. Analyze retains requested timing and
actual gate transitions separately, including interruptions stopped early.

See the [Experiment guide](docs/experiment.md) for configuration, output files,
termination and evidence limits. The optional [ArduCopter SITL guide](docs/sitl.md)
describes the separately installed, fingerprinted simulator, startup readiness
and mandatory network isolation. Choose **Compare experiments** in Analyze to
compare a saved baseline and blackout on an explicit common window, with
configuration checks, observed counts/rates/intervals and an evidence report.
Experiment execution, saved-run inspection and comparison are not part of
published v0.1.0.

## Current input support

The `qgc-timestamped-mavlink-v1` profile reads repeated 8-byte big-endian Unix
microsecond timestamps followed by unsigned MAVLink 1 or 2 frames, decoded with
the `common` dialect from `pymavlink==2.4.49`.

This is a QGroundControl-style byte layout, checked with synthetic fixtures and
[one public recording associated with an identified QGroundControl build](docs/recording-validation.md).
That file has partial message-definition coverage; this is not general producer
compatibility. A `.tlog` extension alone does not identify the format. Unknown message
IDs remain opaque, and damaged or unsupported records stop traversal with an
explicit issue and the original remainder retained.

Logging timestamps and device timestamps remain distinct. An interval without
recorded observations does not establish packet loss, vehicle inactivity or a
cause.

## Product direction

| Mode | Direction | Current implementation |
| --- | --- | --- |
| **Analyze** | Open recordings, inspect sources and timing, investigate an interval and export evidence. | Local browser interface, source/time filters, activity and attitude plots, message inspection, Markdown reports, local saved-run browsing, inspection and bounded baseline/blackout comparison, Python API and JSON import summary. |
| **Experiment** | Run reproducible protocol experiments on explicit simulation or bench targets and inspect their observations in Analyze. | Browser Start/Stop and CLI; synthetic or pinned ArduCopter SITL source, local UDP relay and receiver; baseline and configurable interruption (two seconds by default); saved evidence opens in Analyze or comparison. |

Analyze remains independently usable from saved files. Opening a recording never
starts an experiment or sends vehicle commands. Video, broader session comparison and
additional input formats are future work.

## Development checks

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python scripts/generate_fixture.py --check
```

The fixture generator's `--check` mode compares deterministic bytes without
rewriting the fixture. Core tests cover importer boundaries, exact time
selection, observation intervals, plot counts and discontinuities, report provenance and actual
command-line invocations. Experiment checks exercise local UDP captures,
blackout decisions, evidence references and shutdown. Browser tests are opt-in;
installation and commands
are documented in the [Analyze guide](docs/analyze.md#browser-workflow-checks).
The [Verification workflow](.github/workflows/ci.yml) is configured to check source,
build and inspect distributions, then run the installed package's complete test suite
with only loopback networking. See [verification and release preparation](docs/verification.md)
for local reproduction, current development checks and published-release evidence.

## Documentation

| Document | Contents |
| --- | --- |
| [Instrument workspace](docs/instrument.md) | Open recordings, apply exact filters, inspect charts and original messages, and export evidence reports. |
| [Analyze guide](docs/analyze.md) | Launch, inspect a recording, apply filters and export a report. |
| [Experiment interface](docs/experiment-ui.md) | Explicit Start/Stop, shared execution, simulator setup and saved-evidence handoff. |
| [Compare saved experiments](docs/comparison.md) | Pair eligibility, measurement-relative windows, observed differences and reports. |
| [Saved Experiment inspection](docs/saved-experiments.md) | Browse local runs, open saved evidence and distinguish declared metadata from validated observations. |
| [ArduCopter SITL](docs/sitl.md) | Pinned local simulator setup, isolation, timing and retained evidence. |
| [Experiment guide](docs/experiment.md) | Run the local synthetic baseline/interruption, inspect captures and interpret execution evidence. |
| [Importer guide](docs/importer.md) | Installation, input profile, API, CLI outcomes and limits. |
| [Public recording verification](docs/recording-validation.md) | Download source, exact fingerprint, observed coverage and a reproducible browser case. |
| [Synthetic fixture](tests/fixtures/README.md) | Provenance, byte references and expected observations. |
| [Project scope](docs/project-scope.md) | Users, boundaries and product principles. |
| [Mode workflows](docs/workflows.md) | Current Analyze and Experiment workflows and later capabilities. |
| [Architecture](docs/architecture.md) | Imported evidence, analysis, local presentation and optional execution. |
| [First milestone](docs/first-milestone.md) | Delivered v0.1.0 Analyze workflow and its verification boundary. |
| [Verification and release preparation](docs/verification.md) | Automated checks, distribution contents, installed-package testing and release evidence. |
| [Changelog](CHANGELOG.md) | Version features and limitations. |
| [Dependency notices](THIRD_PARTY_NOTICES.md) | Licensing information for the pinned direct runtime dependencies. |

## License

The original source code, documentation and synthetic telemetry fixture are
licensed under the [MIT License](LICENSE). Dependencies retain their own
licenses; see the [direct runtime dependency notices](THIRD_PARTY_NOTICES.md).
Separately obtained recordings are not covered by the project license.
