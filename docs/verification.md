# Verification and release checks

Published UAV Debugger **0.1.0** provides the offline Analyze workflow. The
unpublished release candidate **0.2.0rc1** adds local synthetic and pinned
ArduCopter SITL Experiment workflows, offline saved-run inspection, bounded
baseline/blackout comparison and explicit browser Start/Stop controls.
Development version **0.2.0rc2.dev0** adds read-only local experiment browsing;
its verification is separate from the retained rc1 results below.
The verification target is Linux x86_64, Python 3.12 and Chromium. Installation
metadata permits later Python versions; this does not establish their behavior
or support for other operating systems.

## Automated verification

The [Verification workflow](../.github/workflows/ci.yml) is configured to run on pull requests
targeting `main`, pushes to `main`, and manual dispatch. It uses Ubuntu 24.04,
Python 3.12, uv 0.12.13 and the dependencies in `uv.lock`. Action references are
pinned to full commit IDs. The job:

1. Installs locked runtime, development and browser dependencies into a fresh
   environment without installing the source project.
2. Checks Ruff lint/formatting and the deterministic synthetic fixture.
3. Builds the source distribution and then the wheel from that distribution.
4. Verifies archive contents, source bytes, package metadata and the bundled
   fixture against the repository's declared public inputs.
5. Installs that wheel without resolving dependencies again, and checks that
   imports resolve inside the verification environment.
6. Installs Chromium and its system dependencies, then runs the complete test
   suite with only loopback networking available.

The tests exercise the installed package, including the Streamlit AppTest file.
Browser tests additionally block non-local HTTP and WebSocket requests. Explicit
`--run-browser` verification fails if Playwright cannot be imported; a missing
Chromium installation also fails when the browser launches. Ordinary pytest
runs continue to skip browser checks unless requested.

The job has read-only repository permissions and does not upload distributions,
create releases or publish packages. Dependency and browser installation happen
before network isolation. In the GitHub runner, `sudo` creates a network
namespace and enables its loopback interface; `runuser` then runs Python and
Chromium as the original runner user. Failure to create that namespace fails
the job; there is no fallback to a test run with external networking enabled.

See the primary documentation for [uv in GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/),
[Playwright CI installation](https://playwright.dev/python/docs/ci) and
[GitHub-hosted runner privileges](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges).

## Reproduce the package checks locally

From the repository root on Linux with Python 3.12 and uv installed, create a
new directory for each verification run. The following commands retain build
outputs under ignored `local/` and leave the development environment intact:

```sh
mkdir -p local
UAV_VERIFY_DIR="$(mktemp -d "$PWD/local/verification.XXXXXX")"
UV_PROJECT_ENVIRONMENT="$UAV_VERIFY_DIR/.venv" uv sync --locked --group browser --no-install-project
"$UAV_VERIFY_DIR/.venv/bin/ruff" check .
"$UAV_VERIFY_DIR/.venv/bin/ruff" format --check .
"$UAV_VERIFY_DIR/.venv/bin/python" scripts/generate_fixture.py --check
uv build --out-dir "$UAV_VERIFY_DIR/dist"
"$UAV_VERIFY_DIR/.venv/bin/python" scripts/check_distribution.py --dist-dir "$UAV_VERIFY_DIR/dist"
uv pip install --python "$UAV_VERIFY_DIR/.venv/bin/python" --no-deps "$UAV_VERIFY_DIR"/dist/*.whl
"$UAV_VERIFY_DIR/.venv/bin/python" -c 'import pathlib, sys, uav_debugger; assert pathlib.Path(uav_debugger.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())'
"$UAV_VERIFY_DIR/.venv/bin/python" -m playwright install --with-deps chromium
```

System dependency installation can require administrator access. If the
browser and its system dependencies are already installed, that installation
step need not be repeated. Use the verification environment's Python directly
after wheel installation; `uv run` may synchronize the source project again.

On Linux hosts permitting unprivileged user and network namespaces, run:

```sh
unshare --user --map-root-user --net bash -c '
  set -eu
  ip link set dev lo up
  exec "$1" -m pytest --run-browser
' bash "$UAV_VERIFY_DIR/.venv/bin/python"
```

This requires `unshare` and `ip`. Hosts that restrict unprivileged namespaces
need an administrator-provided isolated environment, or the privileged setup
shown in the workflow. A normal `python -m pytest --run-browser` run exercises
the browser's request restrictions but does not establish that the Python
server was isolated from external networking.

## Distribution contents

`scripts/check_distribution.py` requires exactly one source archive and one
wheel for the version declared in `pyproject.toml`. It compares source-archive
files against the explicit Hatch inclusion list, and wheel modules/data against
the package source. Unexpected, missing, duplicated or changed files cause a
failure. It also checks distribution metadata and wheel integrity entries.
The package declares `MIT` as its SPDX license expression. The corresponding
metadata headers must agree with `pyproject.toml`; source and wheel archives must
retain `LICENSE` and `THIRD_PARTY_NOTICES.md` with their original bytes. In wheels,
both documents reside under the package's `.dist-info/licenses/` directory.
The checker does not infer redistribution rights from a filename.

The only packaged recording is `uav_debugger/data/telemetry-gap.tlog`, whose
bytes must match the test fixture and its expected SHA-256. The separately
downloaded [public QGroundControl recording](recording-validation.md) is neither
fetched by CI nor included in distributions. The content check validates the
declared inputs; changes to the inclusion list and source files still require
review.

## Verification status and release conditions

### Local experiment catalog in 0.2.0rc2.dev0

The development scope adds declared-metadata browsing under `--experiment-root`,
explicit refresh and validated opening/comparison of retained CLI and browser
run directories. It does not resume execution, rebuild controller ownership,
write an index or modify saved evidence. The saved-run importer and comparison
contracts remain the validation boundary.

On 2026-09-30, the installed **0.2.0rc2.dev0** wheel passed **661 tests in
297.33 seconds**, with no failures or skips, in a fresh locked Python 3.12.3
environment with only loopback networking. This includes **31 Chromium
workflows**, all ten explicitly enabled native SITL cases and 52 new catalog
checks. The source archive contains **79 files** and the wheel **30 files**.
Final documentation updates leave all 24 application/data wheel members
byte-identical to the fully tested wheel.

The catalog's real browser workflow launches a baseline and blackout, restarts
the server, opens both retained runs and exports their reports and comparison.
Hashes and directory snapshots verify that browsing, refresh and reopening
neither change saved evidence nor create another run. Further workflows cover
external CLI additions, unavailable or replaced directories, blocked unfinalized
manifests and inspectable partial terminal evidence. A previously saved native
SITL baseline also opened consistently through the catalog without launching a
simulator or changing its ten retained files.

Fresh-process guards reject execution-module imports and subprocess launches
through catalog browsing, saved inspection and comparison; the core catalog
guard also rejects socket creation. Filesystem checks cover byte/count limits,
strict JSON, symlinks, FIFOs and directory replacement during an open. Existing
reader behavior and the independent recording workflow remain covered.
Ruff lint/format, deterministic fixture, lock, distribution-content and public
Markdown link/anchor checks pass. No hosted CI result is recorded for this
development increment; the rc1 results below apply to that earlier candidate.

### 0.2.0rc1 candidate scope

The candidate retains the existing Analyze workflow and the complete bounded
local Experiment path: explicit CLI or browser launch, synthetic or pinned
ArduCopter SITL source, baseline or two-second forwarding interruption, saved
capture inspection, compatible baseline/blackout comparison and Markdown export.
No runtime dependency, input format or experiment scenario is added for the
candidate. The application continues to start in file-only Analyze.

Candidate preparation identified a history-selection defect after returning
from analysis or comparison: the Run selector could identify the new run while
the summary and Analyze handoff still targeted an older result. Selection is
now retained separately from the widget state, and the selector's identity
changes when the newest saved run changes. The browser regression follows the
baseline-to-analysis, blackout-to-comparison and third-run interruption sequence,
then checks historical selection and the corresponding Analyze evidence.

The acceptance path from an installed wheel is:

1. Launch the local interface outside the source checkout, load the packaged
   example, apply the documented filters and export its report.
2. Start the default six-second synthetic baseline in Experiment and open its
   actual saved observations in Analyze.
3. Run the matching blackout, assign both comparison roles and export a report
   retaining the two identities, actual gate transitions and observed metrics.
4. Stop another active run and inspect its retained interrupted evidence.
5. Restart the server and reopen a saved evidence directory through Analyze,
   without starting another run. Server history is transient; saved files persist.
6. Verify the optional native profile separately with its pinned local executable,
   keeping its namespace requirements and observation limits explicit.

See [installation and launch](analyze.md#install-and-launch), the
[Experiment interface](experiment-ui.md), [saved-run inspection](saved-experiments.md)
and [comparison](comparison.md) for the corresponding user controls. This is a
release candidate, not a published v0.2.0 release; the published baseline remains
v0.1.0. Its hosted result below applies to the exact committed candidate.

On 2026-09-30, the corrected candidate passed **605 tests in 255.93 seconds**,
with no failures or skips, against its installed wheel in a fresh locked
Python 3.12.3 environment with only loopback networking. This includes
**28 Chromium workflows** and all ten explicitly enabled native SITL cases.
The history-selection regression failed before the application correction and
passed afterwards. Ruff lint/format, fixture and lock checks also passed.

After the full suite, a final test-only adjustment changed Experiment's idle-state
wait to sample on animation frames. Its previous 500 ms retry interval could
repeatedly miss short idle windows between 500 ms polling fragments; the required
visible state and skeleton checks are unchanged. All **seven affected Experiment
browser checks**, including native SITL and history selection, then passed in
59.93 seconds. The two affected execution/handoff workflows also passed with
ordered WebSocket delivery delays and fourfold CPU throttling in 53.81 seconds.
Metric checks retain exact expected values and require exactly one matching
element after transient duplicates disappear.

The final source archive contains **75 files** and the wheel **28 files**.
Their 22 application/data members match the wheel used for the full suite;
only verification code and documentation changed afterwards. Public Markdown
links and heading anchors resolve. The subsequent
[hosted Verification run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36706308675)
passed on the exact candidate commit `e9966f72006809ad7a24c1d71475d9d9ecf94635`:
**595 tests passed and ten native SITL cases were skipped in 193.63 seconds**.
The skips are expected because the simulator is not installed in CI; native
coverage is recorded by the local 605-test result above. Source/fixture,
distribution and installed-wheel checks also passed. The earlier dev4 failure
below remains historical evidence, not the candidate's hosted status.

On 2026-09-30, a separate fresh runtime environment installed the candidate wheel
with ordinary dependency resolution and launched the three installed console
commands from an empty directory outside the repository. The browser workflow
ran with only loopback networking and exported five reports: the packaged
example, baseline, comparison, interrupted run and baseline reopened after
server restart. All reports identified **0.2.0rc1**; reopening created no new
experiment and preserved every saved file's bytes.

In that six-second baseline/blackout pair, the relay input and receiver recorded
**120/120** and **120/79** messages respectively. The measured forwarding gate
lasted **2.000934989 seconds**. These are observations from that execution;
scheduling can change counts and timing. These results do not imply physical
packet loss or vehicle behavior. This runtime installation check is separate from the complete
suite's locked verification environment.

### Local Experiment controls

The controller checks explicit launch, configuration rejection before output
creation, one active worker across concurrent requests, unique output directories,
bounded history, idempotent Stop, watchdog escalation, ownership-pipe cleanup,
launch failures, diagnostics and unavailable or malformed terminal manifests.
Additional native checks cover isolated SITL completion and namespace cleanup
after forced termination. They use the separately obtained
pinned executable; ordinary CI does not install it.

Six additional Chromium workflows cover synthetic baseline/blackout execution
and comparison handoff, Stop and saved-run report export, invalid settings,
shared control across tabs already open at idle, normal server shutdown, and
native SITL Start/Stop followed by Analyze. The native browser case is opt-in.
Four execution-boundary checks verify that default Analyze does not import the
execution modules, entering or configuring Experiment does not launch a process,
and the launcher closes an already constructed controller on normal return or
exit. There are no inferred live telemetry counters.

On 2026-09-30, the **0.2.0.dev4** candidate passed **604 tests in 232.10 seconds**,
with no failures or skips, against an installed wheel in a fresh locked Python
3.12.3 environment restricted to loopback networking. This includes **27 Chromium
workflows**, 35 controller checks and the four execution-boundary checks; all
ten native SITL cases were explicitly enabled. Visual inspection of the same
installed wheel checked configuration, active controls across tabs with different
draft settings, and completed-run handoffs. Both additional ten-second synthetic
preview runs completed normally.

Ruff lint/format, fixture and lock checks passed. The final source archive
contains 74 files and the wheel 28 files; all 22 application/data members match
the tested wheel. Public documentation links resolve. These are local checks
of the development increment. The subsequent
[hosted run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36692647332)
on `757a48a820da3f342d3eb4d89e0700a818d763c2` failed two browser assertions, with
592 passes and ten expected native SITL skips. Both assertions encountered old
and current metrics during a Streamlit render transition: example-to-upload
replacement and completion of a second Experiment run. That failure is separate
from the retained local 604-test success and must not be described as a passing
candidate check.

See the [Experiment interface guide](experiment-ui.md) for lifecycle states,
ownership, isolation, clocks and partial-evidence limits.

### Offline baseline/blackout comparison

The comparison checks exact half-open measurement-relative windows, independent
origins, startup/drain boundaries, requested configuration differences, validated
production lifecycle and stop coverage, source/profile incompatibility, partial
artifacts and unknown metrics. Real synthetic UDP runs and constructed v1/v2
fixtures exercise this contract; the file-only API is checked with execution
and socket creation disabled. Reports preserve both identities, original
references, explicit omissions and unavailable deltas.

Four additional Chromium workflows cover pair upload, source/point/window
selection, observed counts and actual gate timing, report download, partial and
rejected replacements, missing durations, control resets and returning to the
independent Recording input. Decimal window checks reject values that would
otherwise round silently beyond nanosecond precision.

On 2026-09-30, the **0.2.0.dev3** candidate passed **559 tests**, with no failures
or skips, against an installed wheel in a fresh locked Python 3.12.3 environment
restricted to loopback networking. This includes **21 Chromium workflows**,
48 comparison-core checks, 15 comparison-report checks and 15 window/plot checks;
the six native SITL cases were explicitly enabled. A separate browser check
opened retained native baseline/blackout evidence and exported its comparison
without starting a simulator. Visual inspection then identified clipped plot
labels; the final margin adjustment was checked with the 19 affected window/plot
and comparison-browser tests on a rebuilt, installed wheel, plus the native
saved-evidence browser workflow.

Ruff lint/format, fixture and lock checks pass. The final source archive contains
67 files and the wheel 25 files; distribution checks verify source bytes,
metadata and bundled evidence. These are local development checks. The previous
[hosted run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36189292493)
on `420e544bfb6215510d809d7e9e35a9c91aac2387` failed one existing source-switch
browser case, with 470 passes and six expected native skips. Its retained/new
Source controls were reproduced by delaying Streamlit render completion. The
corrected test keeps strict reset assertions and waits for the completed current
render; all nine existing browser workflows pass with a one-second completion
delay and fourfold CPU throttling. The subsequent
[hosted run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36685515572)
passed on comparison commit `1ee65fa1370b672ebf28bdaa3d7e44ad72c7142b`: 553 tests
passed and six native SITL cases were skipped because the executable is not
installed in CI. This hosted result covers dev3, not the later browser controls.

See [Compare saved experiments](comparison.md) for eligibility, clock semantics,
per-point boundary effects and interpretation limits.

### Offline saved Experiment inspection

The saved-run reader checks real synthetic output and constructed bounded v2
traces, including grouped datagrams, opaque frames, legacy v1 clocks, duplicate
references, damaged prefixes, missing artifacts, hash mismatches, invalid clocks,
input bounds and file-type/path restrictions. A subprocess check imports and
reads saved evidence while rejecting execution-module imports and socket creation.
The report checks retain requested/applied/observed distinctions, original
provenance, partial evidence and explicitly bounded detail.

Four additional Chromium workflows cover synthetic directory upload, combined
report export, the monotonic timeline, capture-point selection resets, missing
receiver evidence, ambiguous directory replacement and recovery to ordinary
recording Analyze. Two native SITL saved-directory workflows reuse the existing
baseline and blackout fixtures and check startup clocks, datagram references,
opaque records and selected ATTITUDE reports. Additional native simulator working files are listed as excluded from analysis
and do not enter the run fingerprint or report.

On 2026-09-25, the final **0.2.0.dev2** candidate passed **477 tests**, with no
failures or skips, against its installed wheel in a fresh locked Python 3.12.3
Linux x86_64 environment restricted to loopback networking. This includes
**17 Chromium workflows**, 59 saved-run reader checks, 22 run-report checks and
18 upload/timeline checks; the six native SITL checks were explicitly enabled.
A saved native run was also inspected visually without launching a simulator.
Ruff lint/format (49 files), deterministic fixture and lock checks passed.
Final archives contain **59 source files / 22 wheel files**; their 16
application/data members match the tested wheel. These are local checks of an
uncommitted development increment; hosted verification requires its own result.

See [saved Experiment inspection](saved-experiments.md) for the supported schemas,
input limits and the distinction between declared outcome and evidence status.

### Browser synchronization and native SITL

The hosted [run for the first Experiment commit](https://github.com/victormonnot/UAV-Debugger/actions/runs/36173071856)
on `b2fca3306b65db22a4b47b0605cde3c1018f4582` ended with four Chromium failures
and 325 passing tests. The 329-test success below is separate local evidence.
The failures concerned stale filter/report state and record-menu interactions;
the browser checks now wait for semantic UI changes and finish scrolling
before selecting the exact option. Application code is unchanged by this browser correction. The subsequent
[hosted run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36177692793)
passed on commit `0bec4b1753ca6d2fb5aa01f6e716eb233f82158a`. This confirms the
ordinary hosted suite for that commit; native SITL remains separately opt-in.

The SITL profile has opt-in native checks. Ordinary CI does not download or
install ArduPilot. The default tests cover framing, byte preservation, readiness,
startup preambles, failure outcomes, isolation checks and owned-child cleanup
using explicitly synthetic subprocesses. To additionally test the verified
native executable, install it as in the [SITL guide](sitl.md), then run the
installed verification environment inside its isolated namespace:

```sh
UAV_DEBUGGER_SITL_BINARY="$PWD/local/sitl/arducopter" \
  unshare --user --map-root-user --net sh -c '
    set -eu
    ip link set dev lo up
    exec "$@"
  ' sh "$UAV_VERIFY_DIR/.venv/bin/python" -m pytest --run-browser
```

With the variable supplied, a missing/wrong executable or absent isolation fails
the native checks instead of silently skipping them. Native baseline and blackout
runs are shared by their capture checks and two browser workflows, which open
both captures, select ATTITUDE, inspect raw fields and export reports. Captures
include startup; assertions use the separate measurement origin for gate timing.

On 2026-09-25, the complete **0.2.0.dev1** suite passed against an installed
wheel in a fresh locked Linux x86_64 / Python 3.12.3 environment with only
loopback networking: **372 passed**, including eleven Chromium workflows and
the two native SITL scenarios. The browser correction separately passed all nine
original browser workflows and all four formerly failing cases with WebSocket
responses delayed by 300 ms; that delayed setup reproduced three failures before
the correction. Assertions were retained without retries or fixed sleeps.

In the installed-wheel SITL runs, readiness took about **2.45 seconds**. The
baseline retained **343 input / 343 receiver frames**. The blackout retained
**343 input / 217 receiver frames**, recording **126 dropped frames** and a
**2.002045110-second** monotonic gate interval. The four captures traversed
completely; the baseline had 142 decoded and 201 opaque frames at each point.
Opaque message IDs 164 and 178 remained byte-preserved. Receiver frames matched
the recorded forward decisions. All captured HEARTBEATs were disarmed; both
owned simulator processes exited with code 0 without kill escalation. Counts
include startup and describe these runs, not guaranteed rates or vehicle effects.

Ruff, fixture and lock checks passed. Final archives contain **51 source files /
19 wheel files**, with the guide and source tests included, but no simulator
binary, recordings or private notes. The application/data bytes match the
tested wheel. A focused shutdown test also verifies kill escalation after
telemetry readiness, avoiding assumptions about subprocess startup speed.

### Local Experiment development checks

On 2026-09-25, development version **0.2.0.dev0** was built and installed in a
fresh locked environment on Linux x86_64 / Python 3.12.3 with uv 0.12.13.
The complete suite ran against the installed wheel inside an unprivileged
user/network namespace with only loopback enabled: **329 tests passed**,
including nine Chromium workflows and 35 Experiment tests. Ruff lint/format,
fixture verification and lock consistency also passed. No dependency versions
changed. This is local verification of an unreleased increment, not hosted CI
or publication evidence for a new release.

Experiment tests exercise actual UDP receipt at both capture points, byte
preservation, a full two-second blackout and resumption, observation references
and fingerprints, sequence wrap, wall-clock regression, rejected configuration
and existing output, SIGINT/SIGTERM, and cleanup after socket setup/send/close
and capture-write failures. New Chromium cases open both captures from each
scenario, apply source/type filters, inspect a record and export its report.
Existing Analyze checks remain in the same complete suite.

The installed console command also ran the two default six-second scenarios
with only loopback networking. The baseline recorded **120 input / 120 receiver**
messages. The blackout recorded **120 input / 80 receiver**, with **40 actual
relay drops** and a measured monotonic gate interval of **2.000068776 seconds**.
The largest receiver observation interval was **2.049653 seconds** on the wall
clock. All four captures imported completely with no opaque records; forwarded
frame bytes matched receiver observations in these runs. These are measured
results, not guaranteed counts, precise timing or vehicle-behavior claims.

The development archives contain **48 source files / 18 wheel files**, including
the Experiment module, console entry point and source guide/tests. Content checks
verify public inputs, metadata, licensing and the unchanged synthetic fixture.
Experiment recordings and private local notes are excluded. See the
[Experiment guide](experiment.md) for reproduction and evidence limits.

### Published v0.1.0 baseline

On 2026-09-22, version **0.1.0** was checked locally with uv 0.12.13 and Python
3.12.3 on Linux x86_64: a fresh locked dependency environment, an actual
source/wheel build, content verification, wheel installation, import-location
verification and the complete loopback-only suite. **All 292 tests passed**,
including seven Chromium workflows, 68 distribution checks and two
browser-dependency opt-in checks. Ruff, fixture verification, lock consistency
and workflow syntax checking with actionlint 1.7.12 also passed. The run used
the unprivileged namespace command above.

The archives contain **45 source files and 17 wheel files**, including the
original license text and dependency notices, with `License-Expression: MIT`
and both `License-File` headers. Package installation preserves those documents
and identifies version `0.1.0`; reports use that installed version. The only
packaged recording is the unchanged synthetic fixture.

The hosted [Verification run #3](https://github.com/victormonnot/UAV-Debugger/actions/runs/35770134911)
completed successfully on 2026-09-22 for commit
`bb28fcc7987a6810a29adc2b35b20796035e10e4`, the `0.1.0` application candidate.
Its Ubuntu 24.04 / Python 3.12 /
Chromium job completed the distribution checks, wheel installation and tests
inside the privileged network-namespace setup. The public job status confirms
successful steps; the numerical test total above comes from the local run,
not hosted logs. This result applies to that commit. Subsequent commits require
their own successful run; release notes identify the run for the tagged commit.

The [Analyze guide](analyze.md#browser-workflow-checks)
records the bounded browser verification, and the
[public recording check](recording-validation.md) identifies the exact external
file and producer declaration tested.

## Release verification

- Review dependency notices for the intended distribution, especially if
  bundling a complete environment. The original project code, documentation
  and synthetic fixture use the [MIT License](../LICENSE); the
  [direct runtime dependency notices](../THIRD_PARTY_NOTICES.md) record the
  pinned libraries' separate upstream terms and the scope of that review.
- Run the complete installed-package and archive checks for the candidate,
  and confirm the hosted workflow result for that commit.
- Publish only after the candidate's verification succeeds. The
  [release notes](../CHANGELOG.md) describe the implemented input profile,
  tested platforms, capacity and observation limits. The verification workflow
  does not create tags, GitHub releases or package-index publications.

The current input remains the bounded QGroundControl-style timestamped profile,
unsigned MAVLink 1/2 and pinned `common` definitions, with a 10 MiB file limit
and 5,000 selected ATTITUDE records per curve view. One historical producer
attachment has complete traversal and partial decoding; current producer
versions are not generally verified. The candidate includes browser and CLI
Experiment controls, the local synthetic path, one pinned ArduCopter SITL
profile and bounded offline pair comparison. Broader simulator/bench integration,
additional perturbations, persistent run browsing, broader session comparison
and other formats remain future work.
