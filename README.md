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

**0.2.0rc1 is an unpublished release candidate.** Version **0.1.0** remains the
latest published release and provides single-recording offline Analyze. The
commands below use this candidate source checkout. See
[verification and release preparation](docs/verification.md) for actual checks
and hosted CI status.

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
| **Analyze** | Open recordings, inspect sources and timing, investigate an interval and export evidence. | Local browser interface, source/time filters, activity and attitude plots, message inspection, Markdown reports, saved-run inspection and bounded baseline/blackout comparison, Python API and JSON import summary. |
| **Experiment** | Run reproducible protocol experiments on explicit simulation or bench targets and inspect their observations in Analyze. | Candidate browser Start/Stop and CLI; synthetic or pinned ArduCopter SITL source, local UDP relay and receiver; baseline and two-second interruption; saved evidence opens in Analyze or comparison. |

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
| [Analyze guide](docs/analyze.md) | Launch, inspect a recording, apply filters and export a report. |
| [Experiment interface](docs/experiment-ui.md) | Explicit Start/Stop, shared execution, simulator setup and saved-evidence handoff. |
| [Compare saved experiments](docs/comparison.md) | Pair eligibility, measurement-relative windows, observed differences and reports. |
| [Saved Experiment inspection](docs/saved-experiments.md) | Read a saved run, validate references and inspect requested, applied and observed evidence. |
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
| [Changelog](CHANGELOG.md) | Candidate and published features and limitations. |
| [Dependency notices](THIRD_PARTY_NOTICES.md) | Licensing information for the pinned direct runtime dependencies. |

## License

The original source code, documentation and synthetic telemetry fixture are
licensed under the [MIT License](LICENSE). Dependencies retain their own
licenses; see the [direct runtime dependency notices](THIRD_PARTY_NOTICES.md).
Separately obtained recordings are not covered by the project license.
