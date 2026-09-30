"""Bounded, read-only discovery of local saved Experiment directories.

Catalog values are declarations from manifests, never validated observations or
live process status. Only an explicit open reads the complete saved evidence.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .saved_run import SCHEMAS, SavedRun, _json, _load_run_directory_fd

MAX_CATALOG_CHILDREN = 200
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CATALOG_BYTES = 8 * 1024 * 1024
_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_TERMINAL = {"completed", "interrupted", "failed"}


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    key: str
    directory: Path
    scenario: str | None = None
    source: str | None = None
    outcome: str | None = None
    duration_s: float | None = None
    start_unix_us: int | None = None
    issue: str | None = None
    openable: bool = False


@dataclass(frozen=True, slots=True)
class Catalog:
    root: Path
    entries: tuple[CatalogEntry, ...]
    issues: tuple[str, ...]
    truncated: bool


@dataclass
class _Budget:
    remaining: int = MAX_CATALOG_BYTES
    exhausted: bool = False


class _BudgetExceeded(ValueError):
    pass


def _absolute(root: Path) -> Path:
    # Normalize . and .. without following a symlink in the configured root.
    return Path(os.path.abspath(root))


@contextmanager
def _root_directory(root: Path):
    """Pin each root component without following an ancestor symlink."""
    descriptor = os.open(root.anchor, _FLAGS | os.O_DIRECTORY)
    try:
        for component in root.parts[1:]:
            child = os.open(component, _FLAGS | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _directory(parent: int, name: str):
    descriptor = os.open(name, _FLAGS | os.O_DIRECTORY, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _exists(directory: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _manifest(directory: int, budget: _Budget) -> bytes:
    descriptor = os.open("run.json", _FLAGS, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Manifest must be a regular file")
        if info.st_size > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds the 1 MiB size limit")
        if budget.remaining == 0 or info.st_size > budget.remaining:
            budget.exhausted = True
            raise _BudgetExceeded("Catalog manifest reads reached the 8 MiB total budget")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(min(MAX_MANIFEST_BYTES + 1, budget.remaining))
        budget.remaining -= len(raw)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("Manifest exceeds the 1 MiB size limit")
        if os.fstat(descriptor).st_size > len(raw):
            if budget.remaining == 0:
                budget.exhausted = True
                raise _BudgetExceeded("Manifest grew beyond the remaining catalog read budget")
            raise ValueError("Manifest changed during catalog read; refresh the catalog")
        return raw
    finally:
        os.close(descriptor)


def _entry(root: Path, key: str, raw: bytes) -> CatalogEntry:
    manifest = _json(raw)
    if manifest.get("schema") not in SCHEMAS:
        raise ValueError("Unsupported or missing saved Experiment schema")
    issues = []
    outcome = manifest.get("outcome")
    if not isinstance(outcome, str) or outcome not in _TERMINAL | {"running"}:
        outcome = None
        issues.append("Unsupported or missing declared outcome")
    elif outcome == "running":
        issues.append(
            "Unfinalized manifest (declared running); this does not establish an active process"
        )
    requested = manifest.get("requested")
    if not isinstance(requested, dict):
        requested = {}
        issues.append("Requested settings are missing or invalid")
    scenario = requested.get("scenario")
    if scenario not in ("baseline", "blackout"):
        scenario = None
    source = requested.get("source")
    if source not in ("synthetic", "arducopter-sitl"):
        source = None
    duration = requested.get("duration_s")
    if type(duration) not in (int, float) or not 0.1 <= duration <= 60:
        duration = None
    start = manifest.get("start")
    unix_us = start.get("unix_us") if isinstance(start, dict) else None
    if type(unix_us) is not int or not 0 <= unix_us < 2**64:
        unix_us = None
    return CatalogEntry(
        key=key,
        directory=root / key,
        scenario=scenario,
        source=source,
        outcome=outcome,
        duration_s=float(duration) if duration is not None else None,
        start_unix_us=unix_us,
        issue="; ".join(issues) or None,
        openable=outcome in _TERMINAL,
    )


def _read_entry(root: Path, key: str, directory: int, budget: _Budget) -> CatalogEntry:
    try:
        return _entry(root, key, _manifest(directory, budget))
    except (OSError, ValueError) as error:
        return CatalogEntry(key, root / key, issue=f"Cannot inspect manifest: {error}"[:512])


def _scan_child(root: Path, parent: int, name: str, budget: _Budget) -> list[CatalogEntry]:
    entries = []
    try:
        _key_parts(name)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(info.st_mode):
            return []  # Unrelated files count toward the scan cap, but are not runs.
        with _directory(parent, name) as child:
            if _exists(child, "run.json"):
                entries.append(_read_entry(root, name, child, budget))
            if not budget.exhausted and _exists(child, "evidence"):
                key = f"{name}/evidence"
                try:
                    with _directory(child, "evidence") as evidence:
                        entries.append(_read_entry(root, key, evidence, budget))
                except OSError as error:
                    entries.append(
                        CatalogEntry(
                            key, root / key, issue=f"Cannot inspect directory: {error}"[:512]
                        )
                    )
            if not entries:
                entries.append(
                    CatalogEntry(name, root / name, issue="No run.json or evidence/run.json found")
                )
    except (OSError, ValueError) as error:
        entries.append(
            CatalogEntry(name, root / name, issue=f"Cannot inspect directory: {error}"[:512])
        )
    return entries


def scan_catalog(root: Path) -> Catalog:
    """Inspect bounded direct children without reading captures or creating files.

    The first 200 filesystem entries encountered are examined; the retained
    results are sorted by relative key. A child can contain a CLI run and a UI
    evidence directory, so there may be up to 400 results. No deeper tree is read.
    """
    root = _absolute(root)
    entries = []
    issues = []
    budget = _Budget(remaining=MAX_CATALOG_BYTES)
    truncated = False
    try:
        with _root_directory(root) as descriptor, os.scandir(descriptor) as children:
            for index, child in enumerate(children):
                if index >= MAX_CATALOG_CHILDREN:
                    issues.append(
                        f"Catalog limited to {MAX_CATALOG_CHILDREN} direct children; "
                        "remaining children were not examined"
                    )
                    truncated = True
                    break
                entries.extend(_scan_child(root, descriptor, child.name, budget))
                if budget.exhausted:
                    issues.append(
                        "Catalog manifest read budget reached; remaining entries were not examined"
                    )
                    truncated = True
                    break
    except OSError as error:
        issues.append(f"Cannot scan catalog root: {error}"[:512])
    return Catalog(
        root, tuple(sorted(entries, key=lambda entry: entry.key)), tuple(issues), truncated
    )


def _key_parts(key: str) -> list[str]:
    if not isinstance(key, str) or "\\" in key or "\x00" in key:
        raise ValueError("Invalid catalog entry key")
    parts = key.split("/")
    if (
        len(parts) not in (1, 2)
        or any(part in ("", ".", "..") for part in parts)
        or (len(parts) == 2 and parts[1] != "evidence")
    ):
        raise ValueError("Catalog key must name one child or its evidence directory")
    return parts


def load_catalog_entry(root: Path, key: str) -> SavedRun:
    """Reopen a fixed catalog path and validate its current terminal manifest.

    Path traversal is pinned with directory descriptors, including while the
    saved reader loads artifacts. Scan metadata is never used as opening proof.
    """
    parts = _key_parts(key)
    root = _absolute(root)
    with _root_directory(root) as parent, _directory(parent, parts[0]) as child:
        descriptor = child
        owned = None
        try:
            if len(parts) == 2:
                owned = os.open("evidence", _FLAGS | os.O_DIRECTORY, dir_fd=child)
                descriptor = owned
            entry = _read_entry(root, key, descriptor, _Budget())
            if not entry.openable:
                raise ValueError(entry.issue or "Saved Experiment is not ready for catalog opening")
            run = _load_run_directory_fd(descriptor, source_name=key)
            if run.declared_outcome not in _TERMINAL:
                raise ValueError(
                    "Manifest changed to an unfinalized or unsupported outcome; refresh the catalog"
                )
            return run
        finally:
            if owned is not None:
                os.close(owned)
