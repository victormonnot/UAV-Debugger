"""Bounded discovery reads declarations; explicit opening validates saved evidence."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from uav_debugger import catalog
from uav_debugger.catalog import load_catalog_entry, scan_catalog
from uav_debugger.experiment import ExperimentConfig, run_experiment


def manifest(**updates):
    return {
        "schema": "uav-debugger-experiment-v1",
        "outcome": "completed",
        "requested": {"scenario": "baseline", "source": "synthetic", "duration_s": 6.0},
        "start": {"unix_us": 1_700_000_000_000_000},
        **updates,
    }


def write_manifest(path, value=None, *, raw=None):
    path.mkdir(parents=True, exist_ok=True)
    (path / "run.json").write_bytes(
        raw if raw is not None else json.dumps(value or manifest()).encode()
    )


@pytest.fixture(scope="module")
def actual_run(tmp_path_factory):
    output = tmp_path_factory.mktemp("catalog-source") / "original"
    result = run_experiment(output, ExperimentConfig(duration_s=0.2))
    assert result["outcome"] == "completed"
    return output


def fingerprints(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_cli_and_browser_layouts_reopen_real_evidence_without_modification(tmp_path, actual_run):
    root = tmp_path / "runs"
    shutil.copytree(actual_run, root / "cli")
    shutil.copytree(actual_run, root / "browser" / "evidence")
    (root / "browser" / "control.json").write_text('{"state": "running"}')
    original = fingerprints(root)
    result = scan_catalog(root)
    assert result.root == root
    assert result.issues == () and result.truncated is False
    assert [entry.key for entry in result.entries] == ["browser/evidence", "cli"]
    for entry in result.entries:
        assert entry.openable and entry.issue is None
        assert entry.scenario == "baseline" and entry.source == "synthetic"
        assert entry.outcome == "completed" and entry.duration_s == 0.2
        assert entry.start_unix_us > 0
        opened = load_catalog_entry(root, entry.key)
        assert opened.source_name == entry.key
        assert opened.evidence_status == "consistent"
        assert opened.captures["receiver"].records
        assert "control.json" not in opened.files
    assert fingerprints(root) == original
    assert scan_catalog(root) == result  # The catalog survives without in-memory runner state.
    with pytest.raises(FrozenInstanceError):
        result.entries[0].outcome = "running"


def test_scan_ignores_captures_and_controller_metadata(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    write_manifest(root / "ui" / "evidence")
    os.mkfifo(root / "ui" / "control.json")
    os.mkfifo(root / "ui" / "evidence" / "receiver.tlog")
    original_open = os.open

    def guarded_open(path, *args, **kwargs):
        assert os.fspath(path) not in {"control.json", "receiver.tlog", "actions.jsonl"}
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(catalog.os, "open", guarded_open)
    (entry,) = scan_catalog(root).entries
    assert entry.openable
    assert entry.outcome == "completed"


def test_child_can_contain_both_layouts_without_recursive_discovery(tmp_path):
    write_manifest(tmp_path / "both")
    write_manifest(tmp_path / "both" / "evidence", manifest(outcome="failed"))
    write_manifest(tmp_path / "deep" / "nested" / "evidence")
    result = scan_catalog(tmp_path)
    assert [(entry.key, entry.openable) for entry in result.entries] == [
        ("both", True),
        ("both/evidence", True),
        ("deep", False),
    ]
    assert "No run.json" in result.entries[-1].issue


@pytest.mark.parametrize("outcome", ("completed", "interrupted", "failed"))
def test_terminal_partial_run_remains_inspectable_with_reader_issues(tmp_path, outcome):
    write_manifest(tmp_path / "partial", manifest(outcome=outcome))
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.openable and entry.outcome == outcome
    run = load_catalog_entry(tmp_path, entry.key)
    assert run.declared_outcome == outcome
    assert run.evidence_status in {"incomplete", "invalid"}
    assert run.issues
    assert "receiver" not in run.captures


def test_unfinalized_manifest_is_visible_without_claiming_process_liveness(tmp_path):
    write_manifest(tmp_path / "unfinalized", manifest(outcome="running"))
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.outcome == "running"
    assert not entry.openable
    assert "does not establish an active process" in entry.issue
    with pytest.raises(ValueError, match="Unfinalized"):
        load_catalog_entry(tmp_path, entry.key)


@pytest.mark.parametrize(
    "raw",
    (
        b"{",
        b"[]",
        b'{"schema":"uav-debugger-experiment-v1","outcome":"completed","outcome":"running"}',
        b'{"schema":"uav-debugger-experiment-v1","outcome":"completed","bad":NaN}',
        b'{"schema":"uav-debugger-experiment-v1","outcome":"completed","bad":1e999}',
        b'{"schema":"future-schema","outcome":"completed"}',
        b'{"schema":{},"outcome":"completed"}',
        b'{"schema":"uav-debugger-experiment-v1","outcome":{}}',
        b'{"schema":"uav-debugger-experiment-v1","nested":' + b"[" * 30 + b"0" + b"]" * 30 + b"}",
    ),
)
def test_malformed_or_unsupported_manifest_is_retained_as_blocked_entry(tmp_path, raw):
    write_manifest(tmp_path / "damaged", raw=raw)
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.key == "damaged" and entry.issue and not entry.openable
    with pytest.raises(ValueError):
        load_catalog_entry(tmp_path, entry.key)


def test_metadata_is_allowlisted_not_inferred_from_other_clocks_or_file_times(tmp_path):
    value = manifest(
        schema="uav-debugger-experiment-v2",
        requested={
            "scenario": ["baseline"],
            "source": {"name": "synthetic"},
            "duration_s": 10**500,
        },
        start={"unix_us": True, "monotonic_ns": 123},
        measurement_start={"unix_us": 999},
        arbitrary_private_field="never copied into catalog metadata",
    )
    write_manifest(tmp_path / "odd", value)
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.openable
    assert entry.scenario is entry.source is entry.duration_s is entry.start_unix_us is None
    assert "never copied" not in repr(entry)


def test_missing_requested_settings_do_not_prevent_terminal_evidence_inspection(tmp_path):
    write_manifest(tmp_path / "partial", manifest(requested=None))
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.openable and entry.scenario is None
    assert "Requested settings" in entry.issue
    assert load_catalog_entry(tmp_path, entry.key).issues


def test_missing_root_is_reported_without_creating_it(tmp_path):
    root = tmp_path / "not-created"
    result = scan_catalog(root)
    assert result.entries == () and result.issues
    assert not result.truncated and not root.exists()


def test_missing_ui_manifest_is_visible_without_reading_control_file(tmp_path):
    (tmp_path / "partial" / "evidence").mkdir(parents=True)
    (tmp_path / "partial" / "control.json").write_text('{"state":"finished"}')
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.key == "partial/evidence"
    assert entry.issue and not entry.openable


@pytest.mark.parametrize("target", ("root", "ancestor", "child", "evidence", "manifest"))
def test_symlinks_are_rejected_at_each_path_component(tmp_path, target):
    real = tmp_path / "real"
    write_manifest(real / "entry" / "evidence")
    root = real
    if target == "root":
        root = tmp_path / "alias"
        root.symlink_to(real, target_is_directory=True)
    elif target == "ancestor":
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / "real"
    elif target == "child":
        (real / "entry").rename(real / "original")
        (real / "entry").symlink_to(real / "original", target_is_directory=True)
    elif target == "evidence":
        (real / "entry" / "evidence").rename(real / "entry" / "original")
        (real / "entry" / "evidence").symlink_to(
            real / "entry" / "original", target_is_directory=True
        )
    else:
        manifest_path = real / "entry" / "evidence" / "run.json"
        manifest_path.rename(manifest_path.with_name("original.json"))
        manifest_path.symlink_to(manifest_path.with_name("original.json"))
    result = scan_catalog(root)
    if target in {"root", "ancestor"}:
        assert not result.entries and result.issues
    else:
        entry = next(entry for entry in result.entries if entry.key.split("/")[0] == "entry")
        assert not entry.openable and entry.issue
    with pytest.raises((OSError, ValueError)):
        load_catalog_entry(root, "entry/evidence")


@pytest.mark.parametrize("target", ("root", "child", "evidence", "manifest"))
def test_fifo_and_special_paths_never_block_catalog_or_open(tmp_path, target):
    root = tmp_path / "root"
    if target == "root":
        os.mkfifo(root)
    else:
        root.mkdir()
        path = root / "entry"
        if target != "child":
            path.mkdir()
            path /= "evidence"
        if target == "manifest":
            path.mkdir()
            path /= "run.json"
        os.mkfifo(path)
    script = """from pathlib import Path
import sys
from uav_debugger.catalog import scan_catalog, load_catalog_entry
root = Path(sys.argv[1])
result = scan_catalog(root)
assert result.issues or any(entry.issue and not entry.openable for entry in result.entries)
try:
    load_catalog_entry(root, 'entry/evidence')
except (OSError, ValueError):
    pass
else:
    raise AssertionError('Special file was accepted')
"""
    subprocess.run([sys.executable, "-c", script, str(root)], check=True, timeout=5)


@pytest.mark.parametrize(
    "key",
    (
        "",
        ".",
        "..",
        "../outside",
        "/outside",
        "entry/../outside",
        "entry/other",
        "entry//evidence",
        "entry/evidence/child",
        "entry\\evidence",
        "entry/\x00",
    ),
)
def test_entry_keys_cannot_escape_fixed_catalog_layout(tmp_path, key):
    with pytest.raises(ValueError):
        load_catalog_entry(tmp_path, key)


def test_unrepresentable_local_name_is_blocked_in_scan_too(tmp_path):
    write_manifest(tmp_path / "back\\slash")
    (entry,) = scan_catalog(tmp_path).entries
    assert not entry.openable and "Invalid catalog entry key" in entry.issue


def test_scan_stops_at_child_bound_and_marks_incomplete_result(tmp_path):
    for index in range(catalog.MAX_CATALOG_CHILDREN + 1):
        write_manifest(tmp_path / f"run-{index:03d}")
    result = scan_catalog(tmp_path)
    assert len(result.entries) == catalog.MAX_CATALOG_CHILDREN
    assert result.truncated and any("200 direct children" in issue for issue in result.issues)
    assert [entry.key for entry in result.entries] == sorted(entry.key for entry in result.entries)


def test_manifest_size_bound_rejects_only_oversized_entry(tmp_path):
    write_manifest(tmp_path / "large", raw=b" " * (catalog.MAX_MANIFEST_BYTES + 1))
    write_manifest(tmp_path / "valid")
    result = scan_catalog(tmp_path)
    large, valid = result.entries
    assert not large.openable and "1 MiB" in large.issue
    assert valid.openable
    assert not result.truncated


def test_total_manifest_read_budget_includes_all_layouts(tmp_path, monkeypatch):
    raw = json.dumps(manifest()).encode()
    raw += b" " * (catalog.MAX_MANIFEST_BYTES - len(raw))
    for index in range(5):
        write_manifest(tmp_path / f"run-{index}", raw=raw)
        write_manifest(tmp_path / f"run-{index}" / "evidence", raw=raw)
    consumed = []
    original = catalog._manifest

    def tracked(directory, budget):
        before = budget.remaining
        try:
            return original(directory, budget)
        finally:
            consumed.append(before - budget.remaining)

    monkeypatch.setattr(catalog, "_manifest", tracked)
    result = scan_catalog(tmp_path)
    assert sum(consumed) == catalog.MAX_CATALOG_BYTES
    assert result.truncated and any("budget reached" in issue for issue in result.issues)
    assert sum(entry.openable for entry in result.entries) == 8
    assert any(not entry.openable and "8 MiB" in entry.issue for entry in result.entries)


@pytest.mark.parametrize("replacement", ("running", "corrupt", "removed"))
def test_open_revalidates_manifest_instead_of_trusting_scan(tmp_path, monkeypatch, replacement):
    directory = tmp_path / "entry"
    write_manifest(directory)
    (entry,) = scan_catalog(tmp_path).entries
    assert entry.openable
    if replacement == "removed":
        (directory / "run.json").unlink()
    elif replacement == "corrupt":
        (directory / "run.json").write_bytes(b"{")
    else:
        write_manifest(directory, manifest(outcome="running"))

    def no_capture_read(*args, **kwargs):
        raise AssertionError("Full evidence read despite invalid current manifest")

    monkeypatch.setattr(catalog, "_load_run_directory_fd", no_capture_read)
    with pytest.raises(ValueError):
        load_catalog_entry(tmp_path, entry.key)


@pytest.mark.parametrize("component", ("root", "child", "evidence"))
def test_open_pins_directories_while_paths_are_replaced(
    tmp_path, actual_run, monkeypatch, component
):
    root = tmp_path / "root"
    shutil.copytree(actual_run, root / "entry" / "evidence")
    original = catalog._load_run_directory_fd
    expected_manifest = (actual_run / "run.json").read_bytes()
    external = tmp_path / "external"
    write_manifest(external / "entry" / "evidence", manifest(outcome="failed"))

    def replace_then_read(descriptor, *, source_name):
        old = {"root": root, "child": root / "entry", "evidence": root / "entry" / "evidence"}[
            component
        ]
        target = {
            "root": external,
            "child": external / "entry",
            "evidence": external / "entry" / "evidence",
        }[component]
        old.rename(old.with_name(old.name + "-original"))
        old.symlink_to(target, target_is_directory=True)
        return original(descriptor, source_name=source_name)

    monkeypatch.setattr(catalog, "_load_run_directory_fd", replace_then_read)
    run = load_catalog_entry(root, "entry/evidence")
    assert run.files["run.json"].raw_bytes == expected_manifest
    assert run.declared_outcome == "completed" and run.evidence_status == "consistent"


def test_outcome_change_during_full_read_is_not_returned_as_terminal(tmp_path, monkeypatch):
    directory = tmp_path / "entry"
    write_manifest(directory)
    original = catalog._load_run_directory_fd

    def changed(descriptor, *, source_name):
        write_manifest(directory, manifest(outcome="running"))
        return original(descriptor, source_name=source_name)

    monkeypatch.setattr(catalog, "_load_run_directory_fd", changed)
    with pytest.raises(ValueError, match="Manifest changed"):
        load_catalog_entry(tmp_path, "entry")


def test_scan_and_open_do_not_import_execution_or_open_transports(tmp_path, actual_run):
    shutil.copytree(actual_run, tmp_path / "entry")
    script = """import importlib.abc, socket, subprocess, sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args, **kwargs):
        if fullname in {'uav_debugger.experiment', 'uav_debugger.experiment_control',
                        'uav_debugger.experiment_worker', 'uav_debugger.sitl'}:
            raise AssertionError('Execution import: ' + fullname)
sys.meta_path.insert(0, Guard())
def forbidden(*args, **kwargs):
    raise AssertionError('Catalog started an active operation')
socket.socket = forbidden
subprocess.Popen = forbidden
from uav_debugger.catalog import scan_catalog, load_catalog_entry
root = Path(sys.argv[1])
entry, = scan_catalog(root).entries
run = load_catalog_entry(root, entry.key)
assert run.evidence_status == 'consistent'
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, timeout=10)
