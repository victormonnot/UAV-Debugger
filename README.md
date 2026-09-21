# UAV Debugger

A standalone tool for inspecting UAV recordings, following observations back to
their source frames and exporting analysis evidence.

**Status: local Analyze workflow implemented.** Open one timestamped MAVLink
recording in a browser, filter sources and time, plot attitude, inspect message
activity and raw frames, and download a Markdown report. A Python API and JSON command-line
summary expose the same imported evidence. Experiment remains planned.

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
holds one recording per browser session in memory. The **10 MiB input limit**
bounds file size, not total memory use or guaranteed performance.

See the [Analyze guide](docs/analyze.md) for filtering, reports, alternate ports
and observation limits. For a command-line summary:

```sh
uv run --locked python -m uav_debugger tests/fixtures/telemetry-gap.tlog
```

The [importer guide](docs/importer.md) documents the Python API, JSON output and
exit codes; the [fixture documentation](tests/fixtures/README.md) records the
example's provenance and expected observations.

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
| **Analyze** | Open recordings, inspect sources and timing, investigate an interval and export evidence. | Local browser interface, source/time filters, activity and attitude plots, message inspection, Markdown report, Python API and JSON import summary. |
| **Experiment** | Run reproducible protocol experiments on explicit simulation or bench targets and inspect their observations in Analyze. | Planned; no active execution. |

Analyze remains independently usable from saved files. Opening a recording never
starts an experiment or sends vehicle commands. Video, session comparison and
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
command-line invocations. Browser tests are opt-in; installation and commands
are documented in the [Analyze guide](docs/analyze.md#browser-workflow-checks).
The [Verification workflow](.github/workflows/ci.yml) is configured to check source,
build and inspect distributions, then run the installed package's complete test suite
with only loopback networking. See [verification and release preparation](docs/verification.md)
for local reproduction, the hosted-run status boundary and remaining release decisions.

## Documentation

| Document | Contents |
| --- | --- |
| [Analyze guide](docs/analyze.md) | Launch, inspect a recording, apply filters and export a report. |
| [Importer guide](docs/importer.md) | Installation, input profile, API, CLI outcomes and limits. |
| [Public recording verification](docs/recording-validation.md) | Download source, exact fingerprint, observed coverage and a reproducible browser case. |
| [Synthetic fixture](tests/fixtures/README.md) | Provenance, byte references and expected observations. |
| [Project scope](docs/project-scope.md) | Users, boundaries and product principles. |
| [Mode workflows](docs/workflows.md) | Intended Analyze and Experiment workflows. |
| [Architecture direction](docs/architecture.md) | Imported evidence, analysis, local presentation and future execution boundaries. |
| [First milestone](docs/first-milestone.md) | Delivered Analyze workflow, verification and remaining release work. |
| [Verification and release preparation](docs/verification.md) | Automated checks, distribution contents, installed-package testing and v0.1 release conditions. |
| [Changelog](CHANGELOG.md) | Features and limitations prepared for the first release. |
| [Dependency notices](THIRD_PARTY_NOTICES.md) | Licensing information for the pinned direct runtime dependencies. |
