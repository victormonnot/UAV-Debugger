"""Distribution checks reject missing, altered and unexpected packaged evidence."""

import base64
import csv
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_distribution.py"
STEM = "uav_debugger-0.1.0.dev3"
DIST_INFO = f"{STEM}.dist-info"
FIXTURE_NAME = "uav_debugger/data/telemetry-gap.tlog"

spec = importlib.util.spec_from_file_location("check_distribution", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def _metadata(version="0.1.0.dev3"):
    return (
        f"Metadata-Version: 2.4\nName: uav-debugger\nVersion: {version}\n"
        "Requires-Python: >=3.12\nRequires-Dist: pymavlink==2.4.49\n\n"
    ).encode()


def _write_source(dist, files, extra_members=()):
    with tarfile.open(dist / f"{STEM}.tar.gz", "w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(f"{STEM}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
        for member in extra_members:
            archive.addfile(member)


def _write_wheel(dist, files, *, refresh_record=True, extra_members=()):
    if refresh_record:
        record = io.StringIO(newline="")
        writer = csv.writer(record)
        for name, content in files.items():
            if name != f"{DIST_INFO}/RECORD":
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
                writer.writerow((name, f"sha256={digest.decode()}", len(content)))
        writer.writerow((f"{DIST_INFO}/RECORD", "", ""))
        files[f"{DIST_INFO}/RECORD"] = record.getvalue().encode()
    with zipfile.ZipFile(dist / f"{STEM}-py3-none-any.whl", "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
        for member in extra_members:
            archive.writestr(member, b"")


@pytest.fixture
def distribution(tmp_path):
    root = tmp_path / "source"
    dist = tmp_path / "build"
    root.mkdir()
    dist.mkdir()
    fixture = (ROOT / "tests" / "fixtures" / "telemetry-gap.tlog").read_bytes()
    files = {
        "pyproject.toml": b"""[project]
name = "uav-debugger"
version = "0.1.0.dev3"
requires-python = ">=3.12"
dependencies = ["pymavlink==2.4.49"]
[project.scripts]
uav-debugger = "uav_debugger.__main__:main"
[tool.hatch.build.targets.sdist]
include = ["/src/uav_debugger/*.py", "/src/uav_debugger/data/*.tlog",
           "/tests/fixtures/*", "/README.md", "/pyproject.toml", "/scripts/*.py"]
""",
        "README.md": b"# UAV Debugger\n",
        "src/uav_debugger/__init__.py": b'"""Package."""\n',
        "src/uav_debugger/__main__.py": b"def main():\n    return 0\n",
        f"src/{FIXTURE_NAME}": fixture,
        "tests/fixtures/telemetry-gap.tlog": fixture,
        "tests/fixtures/telemetry-gap.expected.json": json.dumps(
            {"sha256": hashlib.sha256(fixture).hexdigest(), "size_bytes": len(fixture)}
        ).encode(),
        "scripts/check_distribution.py": SCRIPT.read_bytes(),
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    source = files | {"PKG-INFO": _metadata()}
    wheel = {
        name.removeprefix("src/"): content for name, content in files.items() if name[:4] == "src/"
    }
    wheel |= {
        f"{DIST_INFO}/METADATA": _metadata(),
        f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{DIST_INFO}/entry_points.txt": (
            b"[console_scripts]\nuav-debugger = uav_debugger.__main__:main\n"
        ),
    }
    _write_source(dist, source)
    _write_wheel(dist, wheel)
    return root, dist, source, wheel


def test_valid_distribution_cli_works_with_only_standard_library(distribution, tmp_path):
    root, dist, _, _ = distribution
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(root / "scripts/check_distribution.py"),
            "--dist-dir",
            str(dist),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "source bytes, metadata and synthetic fixture match" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize("artifact", [".tar.gz", "-py3-none-any.whl"])
def test_missing_artifact_fails(distribution, artifact):
    root, dist, _, _ = distribution
    (dist / f"{STEM}{artifact}").unlink()
    with pytest.raises(ValueError, match="Build directory: missing"):
        checker.check_distribution(root, dist)


def test_mixed_versions_in_build_directory_fail(distribution):
    root, dist, _, _ = distribution
    (dist / "uav_debugger-0.1.0.dev2.tar.gz").write_bytes(b"older build")
    with pytest.raises(ValueError, match="unexpected.*dev2"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("name", ["docs/private.md", "local/flight.tlog"])
def test_extra_source_archive_file_is_rejected(distribution, name):
    root, dist, source, _ = distribution
    source[name] = b"must not ship"
    _write_source(dist, source)
    with pytest.raises(ValueError, match="Source archive:.*unexpected"):
        checker.check_distribution(root, dist)


def test_changed_public_file_is_detected(distribution):
    root, dist, source, _ = distribution
    source["README.md"] += b"stale documentation"
    _write_source(dist, source)
    with pytest.raises(ValueError, match="Source archive bytes differ: README.md"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("name", ["uav_debugger/data/flight.tlog", f"{DIST_INFO}/private.txt"])
def test_extra_wheel_file_is_rejected(distribution, name):
    root, dist, _, wheel = distribution
    wheel[name] = b"must not ship"
    _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="Wheel:.*unexpected"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("name", [FIXTURE_NAME, "uav_debugger/__main__.py"])
def test_changed_wheel_bytes_fail_even_with_updated_record(distribution, name):
    root, dist, _, wheel = distribution
    wheel[name] += b"altered"
    _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="Wheel bytes differ"):
        checker.check_distribution(root, dist)


def test_bundled_recording_must_match_test_fixture(distribution):
    root, dist, source, wheel = distribution
    changed = wheel[FIXTURE_NAME] + b"altered"
    wheel[FIXTURE_NAME] = source[f"src/{FIXTURE_NAME}"] = changed
    (root / f"src/{FIXTURE_NAME}").write_bytes(changed)
    _write_source(dist, source)
    _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="Bundled recording differs from the test fixture"):
        checker.check_distribution(root, dist)


def test_matching_copies_still_require_fixture_manifest(distribution):
    root, dist, source, wheel = distribution
    changed = wheel[FIXTURE_NAME] + b"altered"
    wheel[FIXTURE_NAME] = changed
    for name in (f"src/{FIXTURE_NAME}", "tests/fixtures/telemetry-gap.tlog"):
        source[name] = changed
        (root / name).write_bytes(changed)
    _write_source(dist, source)
    _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="Synthetic fixture does not match its manifest"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("archive", ["source", "wheel"])
def test_metadata_version_must_match_project(distribution, archive):
    root, dist, source, wheel = distribution
    if archive == "source":
        source["PKG-INFO"] = _metadata("0.1.0.dev2")
        _write_source(dist, source)
    else:
        wheel[f"{DIST_INFO}/METADATA"] = _metadata("0.1.0.dev2")
        _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="incorrect Version"):
        checker.check_distribution(root, dist)


def test_wheel_entry_point_must_match_project(distribution):
    root, dist, _, wheel = distribution
    wheel[f"{DIST_INFO}/entry_points.txt"] = b"[console_scripts]\nuav-debugger = wrong:main\n"
    _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match="Wheel entry points"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("archive", ["source", "wheel"])
@pytest.mark.parametrize(
    ("original", "changed", "field"),
    [(b">=3.12", b">=3.10", "Requires-Python"), (b"==2.4.49", b">=2", "Requires-Dist")],
)
def test_installation_requirements_match_project(distribution, archive, original, changed, field):
    root, dist, source, wheel = distribution
    if archive == "source":
        source["PKG-INFO"] = source["PKG-INFO"].replace(original, changed)
        _write_source(dist, source)
    else:
        wheel[f"{DIST_INFO}/METADATA"] = wheel[f"{DIST_INFO}/METADATA"].replace(original, changed)
        _write_wheel(dist, wheel)
    with pytest.raises(ValueError, match=f"incorrect {field}"):
        checker.check_distribution(root, dist)


def test_wheel_record_checksum_is_verified(distribution):
    root, dist, _, wheel = distribution
    wheel[f"{DIST_INFO}/RECORD"] = wheel[f"{DIST_INFO}/RECORD"].replace(b"sha256=", b"sha256=x", 1)
    _write_wheel(dist, wheel, refresh_record=False)
    with pytest.raises(ValueError, match="Wheel RECORD checksum or size differs"):
        checker.check_distribution(root, dist)


@pytest.mark.parametrize("archive", ["source", "wheel"])
@pytest.mark.parametrize("kind", ["duplicate", "traversal", "symlink"])
def test_ambiguous_or_unsafe_members_are_rejected(distribution, archive, kind):
    root, dist, source, wheel = distribution
    if archive == "source":
        name = (
            "README.md" if kind == "duplicate" else "../outside" if kind == "traversal" else "link"
        )
        member = tarfile.TarInfo(f"{STEM}/{name}")
        if kind == "symlink":
            member.type, member.linkname = tarfile.SYMTYPE, "/outside"
        _write_source(dist, source, extra_members=[member])
    else:
        name = (
            "uav_debugger/__init__.py"
            if kind == "duplicate"
            else "uav_debugger/../outside"
            if kind == "traversal"
            else "link"
        )
        member = zipfile.ZipInfo(name)
        if kind == "symlink":
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                _write_wheel(dist, wheel, extra_members=[member])
        else:
            _write_wheel(dist, wheel, extra_members=[member])
    with pytest.raises(ValueError, match="Duplicate archive path|Unsafe archive path|Non-regular"):
        checker.check_distribution(root, dist)
    assert not (root.parent / "outside").exists()


def test_cli_failure_is_nonzero_and_diagnostic(distribution):
    root, dist, _, _ = distribution
    (dist / f"{STEM}.tar.gz").unlink()
    result = subprocess.run(
        [sys.executable, str(root / "scripts/check_distribution.py"), "--dist-dir", str(dist)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.startswith("Distribution check failed: Build directory")
    assert "Traceback" not in result.stderr
    assert not result.stdout
