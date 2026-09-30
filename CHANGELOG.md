# Changelog

## 0.2.0

Version **0.2.0** adds bounded local experiments and saved-run analysis to the
independent Analyze workflow. See [verification and release checks](docs/verification.md)
for package checks, hosted CI and publication status.

### Added

- Local **Experiment** browser mode with explicit Start/Stop, one worker per
  server, shared state across tabs and bounded shutdown. Synthetic and pinned
  SITL runs open directly in Analyze or supply a baseline/blackout comparison.
- An **Experiment** CLI with a synthetic 20 Hz MAVLink 2 `ATTITUDE` sender,
  byte-preserving UDP relay and receiver restricted to `127.0.0.1`. Run a baseline
  or one forwarding interruption within a bounded measurement duration, six
  seconds by default.
- Configurable blackout duration through `--blackout-duration` and the browser's
  **Blackout duration (s)** control, retaining the two-second default. The
  0.1–59.8-second interval must fit within the measurement with 0.1-second margins.
  Requested timing and actual gate transitions remain separate, including early
  shutdown. Existing two-second synthetic and SITL evidence remains supported.
- Separate timestamped MAVLink captures of actual socket reads before and after
  the relay. Manifests and JSONL traces retain requested settings, applied
  decisions, original frame references, raw UDP datagrams and explicit clocks.
- An optional, fingerprinted ArduCopter 4.6.3 SITL telemetry source, with mandatory
  loopback-only network isolation, bounded readiness, a separate measurement
  origin and owned-process shutdown. The simulator is installed separately and
  is not bundled with the application. Browser SITL workers use dedicated user,
  network and PID namespaces while the interface remains reachable.
- Offline saved Experiment inspection in Analyze: bounded synthetic v1/SITL v2
  directory import, artifact fingerprints, capture/trace references, partial
  evidence, requested/applied/observed summaries, a within-run monotonic timeline
  and a combined Markdown report. Either capture reuses the existing filters,
  plots and record inspector without starting execution.
- Offline baseline/blackout comparison with profile and evidence checks,
  explicit measurement-relative windows, observed counts/rates/intervals at both
  capture points, configuration differences and a report identifying both runs.
  Each blackout is checked against its own requested duration and actual gate
  transitions; incompatible or incomplete evidence blocks aggregate results.
- **Local experiments** in Analyze: read-only browsing of CLI and browser run
  directories under `--experiment-root`, including after a server restart.
  Explicit refresh lists declared manifest metadata; opening or comparing
  selected runs performs full evidence validation. **Browse saved experiments**
  opens the same catalog from Experiment without resuming execution.
- Separate controller request clocks, lifecycle and diagnostics in `control.json`,
  alongside original runner evidence. The latest 20 runs remain selectable
  during the server session; output directories remain available for later
  inspection through the catalog.
- Orderly `SIGINT`/`SIGTERM` handling, bounded draining, explicit completed,
  interrupted and failed outcomes, and rejection of existing output directories.

### Fixed

- Experiment identifiers and selected catalog paths retain literal punctuation
  instead of interpreting underscores or other Markdown characters as formatting.
- Experiment history selection keeps the selected run, displayed outcome and
  Analyze handoff consistent after another run finishes, including interruption
  after returning from saved analysis or comparison.

### Scope and limits

- Analyze retains its independent file-only import and browser workflow.
  Execution requires **Start experiment** in the separate Experiment mode or an
  explicit CLI command. Browsing, opening and comparing saved evidence require
  no vehicle, simulator or active runner and send no vehicle commands.
- The verified target is Linux x86_64 with Python 3.12 and Chromium. The optional
  simulator profile supports one exact ArduCopter executable. Broader platforms,
  simulators, input formats, video and arbitrary session comparison are outside
  this release's supported scope.
- Local UDP observations establish behavior on the configured telemetry path;
  they do not establish physical link performance or autopilot/failsafe behavior.
  Requested settings, controller requests, runner actions and observed messages
  retain their separate clocks. See the [Experiment guide](docs/experiment.md)
  for usage and observation limits.

## 0.1.0 — 2026-09-22

### Added

- A local **Analyze** interface for one saved recording per browser session.
  After dependency installation, analysis works offline with no vehicle,
  simulator or ARGOS installation. Opening a file sends no vehicle commands.
- File upload and a bundled synthetic example containing 12 records from two
  sources, with a documented interval without observations from one source.
- Source, message-type and inclusive time filters using exact integer capture
  timestamps. Filtered records retain their original order and indices.
- A message-activity plot and per-source `ATTITUDE` curves for roll, pitch and
  yaw in radians. Lines break for gaps above the configured maximum, repeated or
  regressing capture timestamps, unavailable values and absolute angle jumps
  greater than π. Clicking a point opens its original message in the inspector.
- Inspection of decoded fields, capture timestamps, source identities,
  checksum status, byte offsets and original frame bytes; paged import issues
  identify partial traversal and unprocessed input.
- Markdown evidence reports with input fingerprints, decoder/profile versions,
  applied filters, plot settings, interval endpoints and the inspected record.
- A file-only Python importer API and a command-line JSON summary.
- A verification workflow that builds and checks distributions, installs the
  wheel, and runs core and Chromium tests with only loopback networking.
- MIT licensing for the original source code, documentation and synthetic
  fixture, with license text and direct runtime dependency notices included in
  source and wheel distributions. Dependencies retain their own license terms.

### Supported input and limits

- The `qgc-timestamped-mavlink-v1` profile: repeated eight-byte big-endian Unix
  microsecond timestamps followed by unsigned MAVLink 1 or 2 frames, using the
  `common` definitions from `pymavlink==2.4.49`.
- A 10 MiB input limit and at most 5,000 selected `ATTITUDE` records per curve
  view. Larger curve selections require narrower filters; no points are
  silently decimated. The input limit does not bound total memory use.
- Unknown message IDs remain opaque with unverified checksums. Damaged or
  unsupported records stop traversal while retaining the accepted prefix and
  original input. Signed frames, custom dialects and other containers are
  outside the supported profile.
- Capture and device clocks remain distinct. Repeated or decreasing capture
  timestamps remain visible. Observation gaps and plotted angle changes do
  not establish packet loss, vehicle inactivity or a cause.

### Verification and scope

- The verified environment is Linux x86_64 with Python 3.12 and Chromium.
  See [verification and release preparation](docs/verification.md) for actual
  checks, reproduction commands and the status of hosted verification.
- Synthetic fixtures and [one historical public QGroundControl recording](docs/recording-validation.md)
  have been checked. That recording has partial decoding coverage; compatibility
  with current or other producer versions is not established.
- **Experiment** execution, video, session comparison and additional input
  formats remain future work. See the [Analyze guide](docs/analyze.md) for the
  implemented workflow and its observation limits.
