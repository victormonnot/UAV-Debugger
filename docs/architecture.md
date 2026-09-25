# Architecture direction

UAV Debugger implements a Python file importer, in-memory evidence, exact filters,
activity and attitude plots, record inspection, Markdown reports and a JSON
command. The unreleased Experiment CLI adds a bounded synthetic local UDP path
and saves captures for those same analysis components. See the
[Analyze guide](analyze.md), [importer guide](importer.md) and
[Experiment guide](experiment.md) for current use. A pinned [ArduCopter SITL profile](sitl.md) adds one external local source;
broader integrations and cross-session comparison remain future work.

## Shared analysis, optional experiment execution

```mermaid
flowchart LR
    files["Supported saved recordings"] --> import["Import and validate"]
    import --> session["Session evidence"]
    session --> analyze["Analyze"]
    analyze --> report["Evidence report"]
    scenario["Baseline or blackout settings"] --> runner["Optional local Experiment CLI"]
    runner --> path["Synthetic or SITL source → relay → receiver"]
    path --> captures["Two timestamped MAVLink captures"]
    captures --> import
    runner --> trace["JSON manifest, actions and observations"]
    trace --> saved["Saved-run reader: check evidence references"]
    captures --> saved
    saved --> analyze
```

The runner's two captures use the existing import profile. Analyze opens an
individual recording or [one saved experiment](saved-experiments.md). The latter
checks manifest, trace and capture references without importing execution modules.
Its run timeline uses observed monotonic stamps; capture plots retain their
original wall-clock meaning. No clock conversion or cross-run alignment is inferred. File analysis has no operational dependency
on the runner. The explicit Experiment command is the only entry point that
starts the local sender and relay.

## Responsibilities

| Responsibility | Current contract |
| --- | --- |
| Import | Identify a supported format and its version, validate the input and retain its origin. Unsupported or damaged input produces an explicit outcome. |
| Session evidence | Preserve source identity, original ordering, timestamps with their meaning, decoded observations and references to original records. |
| Analysis | Select sources and intervals, inspect recorded measurements and preserve original meaning. Automatic comparison remains future work. |
| Experiment execution | Apply the baseline or blackout scenario to the local synthetic path and record actual actions, observations, termination and failures. |
| Reports | Present imported observations and limitations with references to their sources. Saved-run reports also distinguish declared outcome, evidence consistency, requested settings, applied gate intervals and observations. |

The importer now represents file bytes, decoded records and import issues in
`ImportResult`, `Record` and `ImportIssue`. This concrete recording model does
not depend on the runner's JSON event schema.

## Saved-run analysis boundary

`saved_run.py` reads fixed, bounded artifact names, retains bytes and fingerprints,
validates trace clocks and capture references, and preserves usable prefixes with
issues. It uses the existing importer and has no dependency on `experiment.py`,
`sitl.py`, subprocesses or network transports. `run_report.py` builds a bounded
Markdown summary and can append the existing selected-capture report.
`run_view.py` presents uploaded directory evidence, a within-run activity timeline
and paged trace references. `app.py` reuses the ordinary capture filters and
inspector for either observation point. A changed run or point invalidates the
previous selection; unreadable replacement evidence removes the previous export.

## Local execution boundary

`experiment.py` owns configuration validation, the synthetic MAVLink encoder,
loopback UDP sockets, selector scheduling, capture writing, JSON evidence
and shutdown. The synthetic sender, relay and receiver share one process. For the SITL
profile, `sitl.py` launches one verified native subprocess with its own working
directory and process group, and requires a loopback-only network namespace.
Startup readiness and the measured phase have separate timing; original
datagrams, including the pinned startup preamble, are retained before framing. The relay input is
observed before its forwarding/drop decision; the receiver is observed only
after an actual socket read. Captures preserve original datagram frame bytes.
No network or serial transport factory is exposed to file analysis.

The runner schedules using monotonic nanoseconds and records elapsed time from
the run origin. Capture timestamps use host wall-clock Unix microseconds sampled
after the read. The synthetic payload's `time_boot_ms` is sender elapsed time;
it stays separate from those capture clocks. Requested gate timing, actual gate
transitions and socket observations are different evidence. The implementation
does not measure kernel receive timestamps or infer exact transport latency.

The output directory is exclusive to one run. `run.json` records settings,
versions, endpoints, outcome, counters and fingerprints; `actions.jsonl` records
application decisions, and `observations.jsonl` links reads to their captures.
Normal completion or catchable termination stops production, drains local
datagrams for a bounded interval and closes resources. A failure to finalize
evidence cannot establish successful completion. The
[Experiment guide](experiment.md) defines the public contract and limitations.

## Evidence conventions

- **Source identity:** retain recording identity and any available vehicle,
  component and stream identity. A decoder also needs compatible message
  definitions; an identifier is not a substitute for the selected dialect.
- **Time:** retain both the original value and its meaning. Capture time,
  device time and local receipt time are different observations. A timestamp
  without a known clock relationship must not be silently aligned with another
  source. Missing synchronization leaves an explicit limit on comparison.
- **Original data:** preserve input bytes. Derived values and annotations refer
  back to source records and identify how they were calculated.
- **Experiment actions:** distinguish the planned perturbation, the action
  actually applied at a named point, and an effect observed elsewhere. A configured
  delay is not a measured end-to-end delay; a drop count at a proxy does not
  describe all losses in a physical link.
- **Completion:** record whether a run completed, stopped or failed, including
  late recording errors. A partial trace remains inspectable with that status.
- **Interpretation:** label inferences. Nearby timestamps do not by themselves
  establish causality, and an acknowledgement alone does not establish the
  intended physical outcome of an action.

MAVLink's [packet format](https://mavlink.io/en/guide/serialization.html) describes
message identity and decoding conventions. Its
[time synchronization protocol](https://mavlink.io/en/services/timesync.html)
and [command protocol](https://mavlink.io/en/services/command.html) are primary
references for timing and acknowledgement semantics. The implemented file
profile and synthetic runner do not implement time synchronization or commands.

## Input boundaries

| Input category | Current status |
| --- | --- |
| MAVLink recordings | Implemented bounded QGroundControl-style timestamped profile, unsigned MAVLink 1/2, pinned `common` dialect; checked with synthetic inputs and [one public producer recording](recording-validation.md) with partial decoding coverage. |
| Onboard flight logs | Possible later integration; no formats selected or implemented. |
| Video and associated timing | Intended analysis capability; encoding and synchronization support remain open. |
| Experiment captures and traces | Two local timestamped MAVLink captures feed the existing importer; Saved-run analysis validates the separate manifest/actions/observations and links them to both captures; raw datagrams support SITL references. |
| ARGOS recordings | Possible adapter; core analysis remains independent of ARGOS. |

Different input adapters may describe different observations. Converting them
into one interface must not invent missing fields or equate their timing and
measurement semantics. An initial importer does not imply all UAVs are supported.

## Recommended starting stack

These choices implement the [v0.1 workflow](first-milestone.md). Python 3.12,
pymavlink 2.4.49, Streamlit 1.63.0, Plotly 7.0.0, in-memory records, uv, pytest
and Ruff are in use. Playwright is an optional browser-test dependency.

| Concern | Implementation | Reason |
| --- | --- | --- |
| Runtime | Python 3.12; Linux x86_64 | One runtime for decoding, analysis, presentation and local execution. |
| Frame decoding | Pinned `pymavlink`, explicit `common` dialect | Reuse MAVLink message definitions and checksum handling. |
| Interface | Streamlit served on `127.0.0.1` | Local file selection, filters, record inspection and download in one application. |
| Timeline | Plotly with explicit source/type/time controls | Activity and attitude plots with point-to-record inspection; zoom does not change filters. |
| Session data | Python data structures in memory | One recording per browser session; persistence needs are initially limited to report export. |
| Report | Markdown download | Readable evidence summary with source fingerprints and record references. |
| Experiment | Standard-library sockets, selectors, clocks, signals and JSON; existing pinned MAVLink encoder | A bounded local path without extra dependencies or a generic target framework. |
| Environment and checks | `pyproject.toml`, `uv.lock`, pytest and Ruff | Reproducible dependencies, behavioral tests and basic source checks. |

Streamlit runs the Python backend on the host and presents the interface in a
browser. For this release both run on the same machine. Configure its listener
for loopback and disable usage statistics; bundle required display assets and
verify operation without external networking after installation. See
[Streamlit architecture](https://docs.streamlit.io/develop/concepts/architecture/architecture)
and [configuration](https://docs.streamlit.io/develop/api-reference/configuration/config.toml).

Keep four concrete responsibilities within one Python package: reading the
recording container, representing imported evidence, querying that evidence,
and presenting/exporting results. The first three must be callable without
Streamlit. Read input bytes through a file-only boundary and use the generated
MAVLink decoder directly; user input must not reach a factory that also opens
network or serial transports. See the
[pymavlink guide](https://mavlink.io/en/mavgen_python/).

`analysis.py` applies inclusive integer-time filters in file order. It segments
the clock at regressions in the complete input before finding observation
intervals; a filter cannot hide a reset and create a false elapsed duration.
`telemetry.py` extracts a single source's selected ATTITUDE observations, retaining
record references, original radians and capture-clock segments. It permits at
most 5,000 records per plot and exposes explicit empty, multiple-source and
capacity outcomes. Unavailable field values remain gaps. `charts.py` counts all
selected observations in at most 200 activity bins and plots the three attitude
angles without decimation. Connecting lines break at clock discontinuities,
unavailable values, angle jumps greater than π and the user-selected maximum
capture-time interval. This threshold is a display setting, not a diagnosis.
The Streamlit
presentation pages records and issues rather than transferring an entire dense
recording to the browser. `report.py` exports provenance, applied filters,
coverage, interval references, attitude plot settings, global import issues and explicitly inspected
record details without depending on Streamlit.

For this input, retain the recording fingerprint and raw bytes, original record
ordinal and byte range, capture timestamp and declared clock convention, frame
identity, decoded fields and per-record issues. Keep traversal completion
separate from decoding coverage and integrity/authenticity status. Tables and
plots are derived views; device-time fields do not silently replace capture time.

Streamlit reruns application code on interaction. Retain an import result in
browser-session state so changing filters does not parse the file again. Inputs
and decoded data occupy memory, so capacity must be bounded and measured before
release. The Record control or an attitude point selection chooses the inspected
message; point selection also navigates to its table page. Plot widgets are keyed
by recording, applied filters and line-gap setting so stale events cannot attach
an earlier point to a new view. Explicit interval controls remain the source of
filter state. Plot zoom only changes the display.
See [execution and caching](https://docs.streamlit.io/develop/concepts/architecture/caching)
and [Plotly charts](https://docs.streamlit.io/develop/api-reference/charts/st.plotly_chart).

This approach provides a small navigable analyzer alongside the JSON inspection
command. An API and custom web frontend would become
useful if richer coordinated interactions or synchronized video justify them.
Database storage, native packaging and separate execution services would follow
demonstrated needs. The local Experiment schema covers the implemented path;
unused adapters and a general target framework are outside this increment.

Lock concrete dependency versions when implementing and validate installation
against that lock. `uv` supports refusing implicit lock changes via `--locked`;
see [locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/).
The source distribution uses an explicit public-file inclusion list. The
project's original code, documentation and synthetic fixture use the
[MIT License](../LICENSE). Source and wheel distributions include the license
text and [direct runtime dependency notices](../THIRD_PARTY_NOTICES.md);
dependencies retain their own terms.

See the [first milestone](first-milestone.md) for the published Analyze scope,
and the [mode workflows](workflows.md) for current and intended user experience.
