# Changelog

## Unreleased

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
