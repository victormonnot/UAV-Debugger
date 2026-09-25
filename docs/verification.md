# Verification and release checks

Published UAV Debugger **0.1.0** provides the offline Analyze workflow. The
unreleased development version **0.2.0.dev0** adds the local Experiment CLI.
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
versions are not generally verified. The local synthetic Experiment runner is
an unreleased development capability. Simulator/bench integration, an Experiment
interface, automatic comparison and other formats remain future work.
