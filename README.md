# UAV Debugger

A standalone tool for inspecting UAV recordings, with controlled simulation and
bench experiments planned as a separate workflow.

**Status: first Analyze component implemented.** A file-only Python importer and
JSON command-line summary inspect one documented timestamped MAVLink profile.
An interactive interface, filtering, plots, Markdown reports and Experiment are
not implemented yet.

## Quick start

The initial runtime target is **Linux with Python 3.12**. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/), then run these
commands from the repository root:

```sh
uv sync --locked
uv run --locked python -m uav_debugger tests/fixtures/telemetry-gap.tlog
```

The supplied synthetic recording produces 12 decoded records from two sources:
7 `ATTITUDE` and 5 `HEARTBEAT` messages, with complete file traversal. Five
`timestamp_repeated` warnings are expected because some messages share the same
artificial logging timestamp; the command exits successfully.

To inspect another recording, replace the path with a regular local file. The
default size limit is **10 MiB (10,485,760 bytes)**. The importer preserves the
source and requires no vehicle, simulator or ARGOS installation. Dependency
installation may require network access; importing a file uses no transport
connection.

See the [importer guide](docs/importer.md) for the Python API, JSON fields, exit
codes and exact input boundaries. The [fixture documentation](tests/fixtures/README.md)
describes its provenance and expected observations.

## Current input support

The `qgc-timestamped-mavlink-v1` profile reads repeated 8-byte big-endian Unix
microsecond timestamps followed by unsigned MAVLink 1 or 2 frames, decoded with
the `common` dialect from `pymavlink==2.4.49`.

This is a QGroundControl-style byte layout, verified here with synthetic input;
it is not a claim of compatibility with recordings from a tested producer
version. A `.tlog` extension alone does not identify the format. Unknown message
IDs remain opaque, and damaged or unsupported records stop traversal with an
explicit issue and the original remainder retained.

Logging timestamps and device timestamps remain distinct. An interval without
recorded observations does not establish packet loss, vehicle inactivity or a
cause.

## Product direction

| Mode | Direction | Current implementation |
| --- | --- | --- |
| **Analyze** | Open recordings, inspect sources and timing, investigate an interval and export evidence. | File importer, evidence objects and JSON import summary. |
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
rewriting the fixture. Tests exercise importer evidence and failure boundaries,
plus actual command-line invocations.

## Documentation

| Document | Contents |
| --- | --- |
| [Importer guide](docs/importer.md) | Installation, input profile, API, CLI outcomes and limits. |
| [Synthetic fixture](tests/fixtures/README.md) | Provenance, byte references and expected observations. |
| [Project scope](docs/project-scope.md) | Users, boundaries and product principles. |
| [Mode workflows](docs/workflows.md) | Intended Analyze and Experiment workflows. |
| [Architecture direction](docs/architecture.md) | Implemented foundations and proposed application responsibilities. |
| [First milestone](docs/first-milestone.md) | Delivered importer foundation and remaining v0.1 workflow. |
