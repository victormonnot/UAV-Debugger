#!/usr/bin/env python3
"""Check this project's source archive and pure-Python wheel without extracting them.

Run after building into a clean directory. The source archive must contain the
public Hatch include list, and the wheel must match the package's Python modules,
sole bundled synthetic recording and explicitly declared license files. This is
a project check, not a general wheel or source-distribution validator.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import io
import json
import re
import stat
import sys
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

BUNDLED_FIXTURE = "uav_debugger/data/telemetry-gap.tlog"
TEST_FIXTURE = "tests/fixtures/telemetry-gap.tlog"
FIXTURE_MANIFEST = "tests/fixtures/telemetry-gap.expected.json"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _same_names(actual: set[str], expected: set[str], label: str) -> None:
    _require(
        actual == expected,
        f"{label}: missing {sorted(expected - actual)}; unexpected {sorted(actual - expected)}",
    )


def _archive_path(name: str, *, directory: bool = False) -> str:
    path = name.removesuffix("/") if directory else name
    _require(
        bool(path)
        and "\\" not in path
        and ":" not in path
        and not path.startswith("/")
        and all(part not in ("", ".", "..") for part in path.split("/")),
        f"Unsafe archive path: {name!r}",
    )
    return path


def _read_archive(path: Path, *, prefix: str | None = None) -> dict[str, bytes]:
    """Read only regular files; reject links, ambiguous names and extra directories."""
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    seen: set[str] = set()

    def add_name(name: str, directory: bool) -> str:
        name = _archive_path(name, directory=directory)
        _require(name not in seen, f"Duplicate archive path: {name}")
        seen.add(name)
        if prefix is not None:
            _require(
                name.startswith(prefix + "/") or (name == prefix and directory),
                f"Unexpected source-archive root: {name}",
            )
            name = name[len(prefix) :].removeprefix("/")
        if directory and name:
            directories.add(name)
        return name

    if prefix is not None:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                _require(member.isfile() or member.isdir(), f"Non-regular member: {member.name}")
                name = add_name(member.name, member.isdir())
                if member.isfile():
                    stream = archive.extractfile(member)
                    _require(stream is not None, f"Unreadable archive member: {member.name}")
                    with stream:
                        files[name] = stream.read()
    else:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                mode = stat.S_IFMT(member.external_attr >> 16)
                expected_mode = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
                _require(mode in (0, expected_mode), f"Non-regular member: {member.filename}")
                name = add_name(member.filename, member.is_dir())
                if not member.is_dir():
                    files[name] = archive.read(member)
    parents = {str(parent) for name in files for parent in PurePosixPath(name).parents}
    _require(
        directories <= parents, f"Unexpected archive directories: {sorted(directories - parents)}"
    )
    return files


def _source_bytes(root: Path, relative: str) -> bytes:
    path = root / relative
    _require(
        all(
            not parent.is_symlink()
            for parent in (path, *path.parents)
            if parent.is_relative_to(root)
        ),
        f"Source path must not use symlinks: {relative}",
    )
    _require(path.is_file(), f"Missing source file: {relative}")
    return path.read_bytes()


def _license_files(project: dict) -> tuple[str, ...]:
    """Accept this project's explicit PEP 639 paths without expanding patterns."""
    if "license" in project:
        expression = project["license"]
        _require(
            isinstance(expression, str) and bool(expression.strip()),
            "Project license must be an SPDX expression string",
        )
    files = project.get("license-files", [])
    _require(isinstance(files, list), "Project license-files must be a list")
    for name in files:
        _require(
            isinstance(name, str) and not any(char in name for char in "*?[]\r\n\x00"),
            "License files must use explicit relative paths",
        )
        _archive_path(name)
    _require(len(files) == len(set(files)), "Duplicate project license-file path")
    return tuple(files)


def _check_metadata(content: bytes, project: dict, label: str) -> None:
    metadata = BytesParser().parsebytes(content)
    for field, expected in (
        ("Name", project["name"]),
        ("Version", project["version"]),
        ("Requires-Python", project["requires-python"]),
    ):
        _require(metadata.get_all(field) == [expected], f"{label}: incorrect {field}")
    _require(
        sorted(metadata.get_all("Requires-Dist", [])) == sorted(project["dependencies"]),
        f"{label}: incorrect Requires-Dist",
    )
    expressions = [project["license"]] if "license" in project else []
    _require(
        metadata.get_all("License-Expression", []) == expressions,
        f"{label}: incorrect License-Expression",
    )
    _require(
        sorted(metadata.get_all("License-File", [])) == sorted(project.get("license-files", [])),
        f"{label}: incorrect License-File",
    )
    _require(not metadata.get_all("License"), f"{label}: unexpected legacy License header")
    if expressions or project.get("license-files"):
        versions = metadata.get_all("Metadata-Version", [])
        _require(
            len(versions) == 1
            and re.fullmatch(r"[0-9]+\.[0-9]+", versions[0]) is not None
            and tuple(map(int, versions[0].split("."))) >= (2, 4),
            f"{label}: license metadata requires Metadata-Version 2.4 or later",
        )


def _check_wheel_metadata(files: dict[str, bytes], prefix: str, project: dict) -> None:
    _check_metadata(files[f"{prefix}/METADATA"], project, "Wheel metadata")
    wheel = BytesParser().parsebytes(files[f"{prefix}/WHEEL"])
    for field, expected in (
        ("Wheel-Version", "1.0"),
        ("Root-Is-Purelib", "true"),
        ("Tag", "py3-none-any"),
    ):
        _require(wheel.get_all(field) == [expected], f"Wheel metadata: incorrect {field}")
    entry_points = configparser.ConfigParser(interpolation=None)
    entry_points.read_string(files[f"{prefix}/entry_points.txt"].decode("utf-8"))
    _require(
        entry_points.sections() == ["console_scripts"]
        and dict(entry_points["console_scripts"]) == project["scripts"],
        "Wheel entry points do not match project scripts",
    )
    record_name = f"{prefix}/RECORD"
    rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"))))
    _require(all(len(row) == 3 for row in rows), "Invalid wheel RECORD row")
    _require(len({row[0] for row in rows}) == len(rows), "Duplicate wheel RECORD row")
    _same_names({row[0] for row in rows}, set(files), "Wheel RECORD")
    for name, digest, size in rows:
        if name == record_name:
            _require(not digest and not size, "Wheel RECORD must leave its own hash and size empty")
            continue
        expected = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=")
        _require(
            digest == "sha256=" + expected.decode("ascii") and size == str(len(files[name])),
            f"Wheel RECORD checksum or size differs: {name}",
        )


def check_distribution(root: Path, dist_dir: Path) -> str:
    """Return a verification summary, or raise ValueError for a failed content check."""
    root = root.resolve()
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    license_files = _license_files(project)
    normalized_name = re.sub(r"[-_.]+", "_", project["name"]).lower()
    stem = f"{normalized_name}-{project['version']}"
    sdist_name, wheel_name = f"{stem}.tar.gz", f"{stem}-py3-none-any.whl"
    artifacts = {path.name for pattern in ("*.tar.gz", "*.whl") for path in dist_dir.glob(pattern)}
    _same_names(artifacts, {sdist_name, wheel_name}, "Build directory")
    for name in artifacts:
        _require(not (dist_dir / name).is_symlink(), f"Artifact must not be a symlink: {name}")

    expected_source: set[str] = set()
    patterns = metadata["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    for name in license_files:
        _require(
            name in {pattern.removeprefix("/") for pattern in patterns},
            f"License file needs an explicit source include: {name}",
        )
    for pattern in patterns:
        relative = _archive_path(pattern.removeprefix("/"))
        matches = {
            path.relative_to(root).as_posix() for path in root.glob(relative) if path.is_file()
        }
        _require(bool(matches), f"Source include pattern matches no files: {pattern}")
        expected_source.update(matches)

    source = _read_archive(dist_dir / sdist_name, prefix=stem)
    _same_names(set(source), expected_source | {"PKG-INFO"}, "Source archive")
    for name in expected_source:
        _require(source[name] == _source_bytes(root, name), f"Source archive bytes differ: {name}")
    _check_metadata(source["PKG-INFO"], project, "Source metadata")

    expected_package = {
        path.relative_to(root / "src").as_posix()
        for path in (root / "src" / "uav_debugger").glob("*.py")
        if path.is_file()
    } | {BUNDLED_FIXTURE}
    dist_info = f"{stem}.dist-info"
    expected_wheel = (
        expected_package
        | {f"{dist_info}/{name}" for name in ("METADATA", "WHEEL", "entry_points.txt", "RECORD")}
        | {f"{dist_info}/licenses/{name}" for name in license_files}
    )
    wheel = _read_archive(dist_dir / wheel_name)
    _same_names(set(wheel), expected_wheel, "Wheel")
    for name in expected_package:
        _require(wheel[name] == _source_bytes(root, f"src/{name}"), f"Wheel bytes differ: {name}")
    for name in license_files:
        _require(
            wheel[f"{dist_info}/licenses/{name}"] == _source_bytes(root, name),
            f"Wheel license bytes differ: {name}",
        )
    _check_wheel_metadata(wheel, dist_info, project)

    fixture = _source_bytes(root, TEST_FIXTURE)
    manifest = json.loads(_source_bytes(root, FIXTURE_MANIFEST))
    _require(wheel[BUNDLED_FIXTURE] == fixture, "Bundled recording differs from the test fixture")
    _require(
        hashlib.sha256(fixture).hexdigest() == manifest["sha256"]
        and len(fixture) == manifest["size_bytes"],
        "Synthetic fixture does not match its manifest",
    )
    return (
        f"Verified {sdist_name} ({len(source)} files) and {wheel_name} ({len(wheel)} files); "
        "source bytes, metadata and synthetic fixture match."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist-dir", type=Path, required=True, help="directory containing both builds"
    )
    args = parser.parse_args(argv)
    try:
        print(check_distribution(Path(__file__).resolve().parents[1], args.dist_dir))
    except (
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        zipfile.BadZipFile,
        configparser.Error,
        csv.Error,
    ) as error:
        print(f"Distribution check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
