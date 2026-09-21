# Verification and v0.1 release preparation

UAV Debugger remains a development version, **0.1.0.dev3**. The current
verification target is Linux x86_64, Python 3.12 and Chromium. Installation
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
When license metadata and explicit license-file paths are declared, their
headers must agree with `pyproject.toml`; source and wheel archives must retain
the declared files with their original bytes. The checker does not select a
license or infer redistribution rights from a filename.

The only packaged recording is `uav_debugger/data/telemetry-gap.tlog`, whose
bytes must match the test fixture and its expected SHA-256. The separately
downloaded [public QGroundControl recording](recording-validation.md) is neither
fetched by CI nor included in distributions. The content check validates the
declared inputs; changes to the inclusion list and source files still require
review.

## Verification status and release conditions

On 2026-09-21, the local sequence used uv 0.12.13 and Python 3.12.3 on Linux
x86_64: a fresh locked dependency environment, an actual source/wheel build,
content verification, wheel installation, import-location verification and the
complete loopback-only suite. **All 292 tests passed**, including seven Chromium
workflows, 68 distribution checks and two browser-dependency opt-in checks.
Ruff, fixture verification and workflow syntax checking with actionlint 1.7.12
also passed. The archives contained 44 source files and 15 wheel files; only
the synthetic fixture was packaged. This local run used the unprivileged
namespace command.

The hosted [Verification run #1](https://github.com/victormonnot/UAV-Debugger/actions/runs/35586054818)
completed successfully on 2026-09-21 for commit
`f5fb8d55edbc815b4e7131d3843ba25336615de0`. Its Ubuntu 24.04 / Python 3.12 /
Chromium job completed the distribution checks, wheel installation and tests
inside the privileged network-namespace setup. The public job status confirms
successful steps; the numerical test total above comes from the local run,
not hosted logs. This result applies to that commit. Later release candidates
require their own successful run.

The [Analyze guide](analyze.md#browser-workflow-checks)
records the bounded browser verification, and the
[public recording check](recording-validation.md) identifies the exact external
file and producer declaration tested.

Before a v0.1 release:

- Select the project and original synthetic fixture licenses, add their texts
  and package metadata, and review dependency notices for the intended
  distribution. These licensing decisions remain open; the
  [direct runtime dependency notices](../THIRD_PARTY_NOTICES.md) record the
  pinned libraries' upstream terms and the scope of that review.
- Run the complete installed-package and archive checks for the candidate,
  and confirm the hosted workflow result for that commit.
- Set the final version and prepare release notes describing the implemented
  input profile, tested platforms, capacity and observation limits. The
  [unreleased changelog](../CHANGELOG.md) records the current feature boundary.

The current input remains the bounded QGroundControl-style timestamped profile,
unsigned MAVLink 1/2 and pinned `common` definitions, with a 10 MiB file limit
and 5,000 selected ATTITUDE records per curve view. One historical producer
attachment has complete traversal and partial decoding; current producer
versions are not generally verified. Experiment execution and other formats
remain separate future capabilities.
