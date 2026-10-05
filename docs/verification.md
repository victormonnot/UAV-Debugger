# Verification and release checks

This document records verification of UAV Debugger **0.2.0** and its preceding
development versions. The 0.2.0 package includes local synthetic and pinned
ArduCopter SITL Experiment workflows,
explicit browser Start/Stop and CLI controls, offline saved-run inspection,
baseline/blackout comparison, read-only local experiment browsing and bounded
configurable blackout duration. The published **0.1.0** baseline provides the
single-recording offline Analyze workflow. Historical development checks below
retain their original version and commit identities; see
[GitHub Releases](https://github.com/victormonnot/UAV-Debugger/releases) for
published versions and their exact release commit verification.
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
the package source. This includes the 16 explicitly declared Instrument HTML,
CSS, JavaScript, font and license files. Unexpected, missing, duplicated or changed files cause a
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

### Instrument saved-run and catalog checks

On 2026-10-05, saved-run inspection and catalog integration passed **610 focused
source-environment tests in 19.76 seconds**. A newly built wheel installed into
a separate locked Python 3.12.3 environment then passed **694 tests in 205.24
seconds** from an external working directory, with imports verified in
site-packages. This includes all **75 Instrument Chromium cases**, two existing
recording workflows, all four saved-run workflows and all three catalog
workflows in the existing interface.

Checks cover memory-only multipart parsing, file/body/header bounds, ignored
files, unsafe paths, duplicates, malformed and interrupted uploads, origin
guards and metadata-only catalog discovery. Opening and every later local-run
interaction retain the existing pinned-directory validation and complete run
identity. Changed manifests and captures reject stale analysis and reports;
running declarations never imply process liveness.

Browser checks exercise actual run-report downloads, point changes, capture
filter resets, independent monotonic timelines, requested/applied/observed
evidence, exact clocks beyond JavaScript's integer range, trace/issue paging,
missing versus empty captures, partial/unfinalized evidence, catalog limits and
completed-but-delayed responses. Failed trace changes restore the applied
control and rows without discarding the inspected record. The extracted pure
upload/timeline helpers retain the existing interface's behavior.

Run views pass rendered text/axis contrast of at least 4.5:1 and effective
trace contrast of at least 3:1 in light/dark themes at 320 and 1440 pixels;
the recording layouts also retain their 390/1024-pixel checks. Desktop/mobile
screenshots were reviewed. The live source preview passed native directory
selection, capture switching, inspection, Markdown download, read-only catalog
refresh and navigation to the existing interface, without JavaScript errors.

The new browser run fixtures construct saved v2 evidence without a simulator.
Existing reader and classic workflow fixtures separately create real synthetic
experiments. No native SITL execution or full Experiment-suite rerun was
performed. Browser external HTTP/WebSocket requests were blocked; the Python
server was not network-namespace isolated. These are local checks, not a
hosted CI result or a change to published v0.2.0 artifacts.

Ruff lint/formatting, JavaScript syntax, fixture, lock, whitespace and archive
checks pass. The final archives contain **104 source files and 48 wheel
members**, including all 16 interface assets; all 47 non-RECORD wheel payloads
match the tested installation. `python-multipart` 0.0.32 is now a direct
dependency; its already locked version and all other resolved versions are
unchanged. No recording profile or evidence schema changed.

### Instrument inspection and report checks

On 2026-10-05, Instrument record inspection and reporting passed **392 focused
source-environment tests in 14.18 seconds**. A newly built wheel installed in
a separate locked Python 3.12.3 environment then passed **440 tests in 103.72
seconds**, including all **46 Instrument Chromium cases** and two existing
Analyze browser workflows. Imports resolved to the installed package. The
remaining checks cover the importer, exact selections, telemetry, charts,
Markdown reports, execution boundaries, service and distribution.

The checks verify original record indices across 100-row message pages,
marker-to-record inspection across pages, keyboard focus retention, exact
capture and 64-bit payload clocks, nonfinite values, opaque frames and original
byte ranges/content. Actual downloaded reports retain applied filters and line
gap, explicitly inspected records and full-selection counts. Empty and partial
imports, empty selections, issue limits, invalid indices, fingerprint mismatch
and completed-but-delayed inspection/report responses are covered. Clicking an
unobserved interval or an activity bin does not select an individual record.

Both themes passed the rendered text/axis contrast threshold of 4.5:1 and
marker/bar threshold of 3:1 at 320, 390, 1024 and 1440 pixels. Marker checks now
include effective SVG opacity, and Instrument keeps markers fully opaque.
Message-table and inspector screenshots were reviewed. The live preview also
passed native file selection, filtering, keyboard inspection, original-byte
display, Markdown download and navigation to the existing interface.

Ruff lint/formatting, JavaScript syntax, fixture, lock and archive checks pass.
The archives contain 100 source files and 46 wheel members, including all 15
local interface assets. Browser external HTTP/WebSocket requests were blocked;
the Python server was not network-namespace isolated. These focused checks do
not repeat the complete Experiment/native SITL suite, establish hosted CI
success or change the published v0.2.0 artifacts. No dependency or input profile
changed.

### Instrument recording-workspace checks

On 2026-10-05, the expanded Instrument recording workspace passed **237 focused
tests in 12.20 seconds** in the development environment. A freshly built wheel,
installed into a separate locked Python 3.12.3 environment, then passed **358
tests in 76.29 seconds**. That run includes the importer, exact selections,
charts, telemetry, execution boundary, service and distribution checks, all
**30 Instrument Chromium cases**, and two existing Analyze browser workflows.
Imports resolved inside the installed environment, not to the source package.

The checks cover file opening/replacement/clear, immutable input provenance,
empty and partial imports, opaque messages, explicit filter application,
microsecond boundaries near the unsigned 64-bit limit, hidden capture-clock
regressions, nonfinite attitude values, the 5,000-point limit, paginated import
issues, stale-response rejection and independent browser tabs. Service checks
also enforce the streamed 10 MiB input cap, same-origin upload restrictions,
strict request validation and absence of execution or recording-file writes.

Both themes and both plots were checked at 320, 390, 1024 and 1440 pixels.
Rendered text/axis contrast met 4.5:1 and trace/bar contrast met 3:1; desktop and
mobile screenshots were reviewed. The live preview was checked through the
native file picker, filtering and explicit navigation to the existing interface.
Browser external HTTP/WebSocket requests were blocked in the automated tests;
the Python server was not network-namespace isolated. These focused checks do
not repeat the complete Experiment/native SITL suite or constitute hosted
release evidence.

Ruff lint/formatting, JavaScript syntax, fixture, dependency lock and archive
checks pass. The archives still contain 100 source files and 46 wheel members,
including all 15 local interface assets. No runtime dependency, supported input
profile or published v0.2.0 artifact changed in this addition.

### Instrument workspace development checks

On 2026-10-05, focused checks of the Instrument service, package contents and
reused analysis/chart/telemetry/execution boundaries passed **188 tests** in the
development environment. A freshly installed wheel in a separate locked Python
3.12.3 environment then passed **118 tests in 46.18 seconds** from a working
directory outside the source package: service and distribution checks, all
**15 Instrument Chromium cases**, and two existing Analyze browser workflows
covering upload, filtering, record inspection, report download and the bundled
example. Imports resolved inside the installed environment.

Instrument checks cover original timestamps and source separation, unconnected
observation gaps, failed and cancelled requests, independent browser tabs,
appearance persistence and explicit handoff without execution. Both themes were
checked at 320, 390, 1024 and 1440 pixels, with text/axis contrast of at least
4.5:1 and plotted trace contrast of at least 3:1 against their backgrounds.
Browser HTTP and WebSocket requests to non-local hosts were blocked; this run
did not isolate the Python server in a network namespace. It did not rerun the
complete Experiment or native SITL suites, and is not hosted release evidence.

Ruff lint/formatting, JavaScript syntax and archive checks passed. The archives
contain 100 source files and 46 wheel members, including all 15 Instrument
assets. These unreleased source additions retain version metadata 0.2.0 but do
not change the published v0.2.0 artifacts or their historical verification below.

### 0.2.0 release preparation

The initial 0.2.0 preparation retained the application code, evidence schemas
and locked dependencies of 0.2.0rc2.dev1, updating version metadata and public
documentation. The identifier-rendering correction below follows that initial check.
Analyze remains usable independently of experiment execution.

On 2026-09-30, the initial installed **0.2.0** wheel passed **758 tests in 354.21 seconds**,
with no failures or skips, in a fresh locked Linux x86_64 / Python 3.12.3
environment restricted to loopback networking. All **36 Chromium workflows**
and **13 explicitly enabled native SITL cases** ran. The suite covers independent
recording analysis, synthetic and native execution, configurable and interrupted
blackouts, controller shutdown, saved-run inspection, catalog reopening and
offline comparison. Imports resolved to the installed wheel, not the checkout.

The native baseline and half-second blackout generated by this run were also
compared offline, preserving all original file bytes and starting no simulator.
The comparison was eligible and its report identified version 0.2.0.

Ruff lint/format (70 files), deterministic fixture, lock, archive contents and
public Markdown links/anchors pass. The archives contain **80 source files**
and **30 wheel members**. All 24 application/data members of that initial wheel
were byte-identical to the previously tested development wheel. Only verification
documentation changed before the preparation commit; these archive identities
precede the identifier-rendering correction below.

The first [hosted 0.2.0 preparation run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36725637626)
on commit `c042c91ac771a1b30a1187245e2338aaa90f5e3a` failed one browser check:
**744 passed, one failed and 13 expected native SITL skips in 329.07 seconds**.
The check could not find the exact displayed run identifier `run-_z6bg9i_`.
The interface rendered identifiers as Markdown, interpreting paired underscores
as formatting. Deterministic browser checks against the original installed
wheel reproduced the problem for both `run-_z6bg9i_` and `run-__gap___`: the
rendered text lost delimiter characters to italic or bold formatting.

Experiment identifiers, selected comparison identifiers, output paths and
selected catalog keys now use literal text rendering. Identifier generation,
saved evidence and execution behavior are unchanged. Three focused Chromium
workflows passed in 58.81 seconds on the corrected installed wheel, including
the originally failing workflow and both deterministic identifier cases.
The new cases verify exact identifiers after Start and completion, assignment,
Analyze/comparison handoffs, catalog selection, exported evidence fingerprints
and unchanged original files. Existing exact identity assertions and timeouts
are retained.

The corrected installed **0.2.0** wheel subsequently passed **760 tests in
398.48 seconds**, with no failures or skips, in a fresh locked Python 3.12.3
environment restricted to loopback networking. All **38 Chromium workflows**
and **13 native SITL cases** ran. This result includes the final regression
checks without relying on a short-lived intermediate process state. The final
archives contain **81 source files** and **30 wheel members**; the final wheel
members match the corrected, fully tested wheel. Ruff lint/format (71 files),
fixture, lock, archive and public link checks pass. These corrected artifacts
supersede the initial preparation archives.

Publication requires a successful hosted Verification run on the exact release
commit. The release notes identify that commit and run when published; the
historical development result below is not a substitute for that check.

### Configurable blackout duration in 0.2.0rc2.dev1

The CLI and browser accept a requested blackout duration from 0.1 to 59.8 seconds,
subject to the existing measurement bound and 0.1-second margins. Controller
requests preserve the setting; actual gate actions and observation clocks remain
separate. Saved comparison validates the actual interval against that run's
requested duration, including older two-second runs.

On 2026-09-30, the installed **0.2.0rc2.dev1** wheel passed **758 tests in
353.41 seconds**, with no failures or skips, in a fresh locked Python 3.12.3
environment with only loopback networking. This includes **36 Chromium
workflows** and all **13 explicitly enabled native SITL cases**.

New checks cover invalid and non-fitting requests before output creation,
decimal boundary rounding, CLI and controller-to-worker propagation, scheduling
from actual gate activation, and orderly stop during a longer requested gate.
Controlled-clock checks include the 59.8-second boundary without claiming a
60-second real-time run. Offline comparisons cover short, long and legacy
two-second gates, applied intervals one nanosecond below the request, missing
timing and invalid baseline requests. Reports preserve requested values and
actual action references for both synthetic and SITL schemas.

Real browser workflows exercise a half-second blackout through saved analysis
and comparison reports, an early stop during a requested ten-second blackout,
and disabled baseline controls with invalid combinations rejected before launch.
In the full run, the half-second synthetic gate recorded **500,761,702 ns** and
60/49 relay-input/receiver records. The interrupted ten-second request retained
a **100,011,513 ns** gate, its interrupted outcome and a blocked comparison.
These observations are evidence from those runs, not timer or message-count
guarantees.

The pinned native source also completed a half-second blackout: its recorded
gate lasted **500,623,161 ns**, with 343/310 retained capture records. Both capture
and complete-directory browser inspection passed, and subsequent offline
comparison with the actual native baseline was eligible. Reading and reporting
those files retained their original bytes and launched no simulator. Default
two-second native and synthetic paths, the catalog and independent Analyze
checks remain covered by the same full suite.

Ruff lint/format, fixture, lock, distribution contents and public Markdown
links/anchors pass. Final archives contain **80 source files** and **30 wheel
members**; all 24 application/data members match the fully tested wheel.
The subsequent [hosted Verification run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36721934008)
passed on commit `25069ff784693b9d445b053b7e154d7225db5d06`: **745 tests passed
and 13 expected native SITL skips in 313.22 seconds**. The native executable is
not configured on the hosted runner; all 13 native cases ran successfully in
the local check above. These results identify the development version; release
verification of 0.2.0 is recorded separately.

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
Markdown link/anchor checks pass. The subsequent
[hosted catalog Verification run](https://github.com/victormonnot/UAV-Debugger/actions/runs/36717123676)
passed on commit `34bfb56f0d32e66a5464879f070c733b296d522d` with **651 tests passed
and ten expected native SITL skips in 274.05 seconds**. These results apply to
the catalog version; the rc1 results below apply to that earlier candidate.

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
versions are not generally verified. The 0.2.0 package includes browser and CLI
Experiment controls, the local synthetic path, one pinned ArduCopter SITL
profile, configurable blackout duration, read-only saved-run browsing and bounded
offline pair comparison. Broader simulator/bench integration, additional
perturbation types, broader session comparison and other formats remain future
work.
