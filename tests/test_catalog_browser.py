"""Discover persistent local evidence without restarting an experiment."""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from test_analyze_browser import download_blocks, expect_metric, set_interval, wait_for_render
from test_comparison_browser import chart_data
from test_experiment_browser import (
    await_finished,
    open_experiment,
    select_option,
    settle_controls,
    start_run,
)
from test_saved_run_browser import await_saved_capture, run_report_block

from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


def tree_snapshot(root):
    """Detect extra directories as well as changes to any retained file bytes."""
    if not root.exists():
        return None
    return {
        path.relative_to(root).as_posix(): (
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
    }


class CatalogServer:
    def __init__(self, tmp_path):
        self.root = tmp_path / "experiments"
        self.tmp_path = tmp_path
        self.process = None
        self.generation = 0

    def start(self):
        assert self.process is None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.generation += 1
        log_path = self.tmp_path / f"server-{self.generation}.log"
        with log_path.open("wb") as output:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uav_debugger.analyze",
                    "--port",
                    str(port),
                    "--experiment-root",
                    str(self.root),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                pytest.fail(f"Server exited:\n{log_path.read_text(errors='replace')}")
            try:
                with urlopen(f"{self.url}/_stcore/health", timeout=0.5) as response:
                    if response.status == 200:
                        return self.url
            except (URLError, TimeoutError):
                pass
            time.sleep(0.1)
        pytest.fail(f"Server did not start:\n{log_path.read_text(errors='replace')}")

    def stop(self):
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None

    def restart(self, page, expect):
        self.stop()
        page.goto(self.start(), wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="Analyze", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Load example", exact=True)).to_be_visible()


@pytest.fixture
def catalog_server(tmp_path):
    server = CatalogServer(tmp_path)
    try:
        server.start()
        yield server
    finally:
        server.stop()


@pytest.fixture
def analyze_server(catalog_server):
    return catalog_server.url


def open_catalog(page, expect):
    page.get_by_test_id("stRadio").get_by_text("Local experiments", exact=True).click()
    expect(page.get_by_role("heading", name="Local experiments", exact=True)).to_be_visible()
    settle_controls(page)
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)


def choose_run(page, expect, root, directory, scenario, outcome="completed"):
    key = directory.relative_to(root).as_posix()
    select_option(page, expect, "Saved run", f"{scenario} · {outcome} · {key}")
    evidence = f"Evidence directory: {directory}"
    expect(page.get_by_text(evidence, exact=True)).to_have_text([evidence])
    settle_controls(page)


def assign_run(page, expect, root, directory, role):
    choose_run(page, expect, root, directory, role)
    page.get_by_role("button", name=f"Use as {role}", exact=True).click()
    caption = f"Selected {role}: {directory.relative_to(root).as_posix()}"
    expect(page.get_by_text(caption, exact=True)).to_have_text([caption])
    settle_controls(page)


def run_cli(root, name, scenario="baseline", duration=3):
    directory = root / name
    command = [
        sys.executable,
        "-m",
        "uav_debugger.experiment",
        "--output",
        str(directory),
        "--scenario",
        scenario,
        "--duration",
        str(duration),
    ]
    if scenario == "blackout":
        command.extend(["--blackout-at", "0.4"])
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    return directory


def test_catalog_reopens_real_worker_runs_after_restart_without_changing_evidence(
    analyze_page, catalog_server, tmp_path
):
    page, expect = analyze_page
    root = catalog_server.root
    open_experiment(page, expect)
    baseline = start_run(page, expect, root)
    await_finished(page, expect)
    blackout = start_run(page, expect, root, scenario="Blackout")
    await_finished(page, expect)
    original = tree_snapshot(root)
    page.get_by_role("button", name="Browse saved experiments", exact=True).click()
    expect(page.get_by_role("heading", name="Local experiments", exact=True)).to_be_visible()
    assert tree_snapshot(root) == original
    catalog_server.restart(page, expect)
    assert tree_snapshot(root) == original

    open_catalog(page, expect)
    page.get_by_role("button", name="Refresh catalog", exact=True).click()
    settle_controls(page)
    choose_run(page, expect, root, baseline, "baseline")
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, baseline)
    set_interval(page, 0, 0)
    expect_metric(page, expect, "Selected records", "1")
    report = tmp_path / "catalog-baseline.md"
    blocks = download_blocks(page, report)
    assert run_report_block(blocks, "requested")["scenario"] == "baseline"
    assert load_run_directory(baseline).identity in report.read_text()

    open_catalog(page, expect)
    choose_run(page, expect, root, blackout, "blackout")
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    imported = await_saved_capture(page, expect, blackout)
    expect_metric(page, expect, "Selected records", str(len(imported.records)))
    report = tmp_path / "catalog-blackout.md"
    blocks = download_blocks(page, report)
    assert run_report_block(blocks, "requested")["scenario"] == "blackout"
    assert load_run_directory(blackout).identity in report.read_text()

    open_catalog(page, expect)
    for scenario, directory in (("baseline", baseline), ("blackout", blackout)):
        assign_run(page, expect, root, directory, scenario)
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    runs = [load_run_directory(directory) for directory in (baseline, blackout)]
    comparison = compare_runs(*runs)
    assert comparison.comparable, comparison.issues
    assert {trace["name"].lower(): sum(trace["y"]) for trace in chart_data(page)["traces"]} == {
        role: comparison.metrics[role]["receiver"].count for role in ("baseline", "blackout")
    }
    report = tmp_path / "catalog-comparison.md"
    download_blocks(page, report)
    for run in runs:
        assert run.identity in report.read_text()
        assert all(capture.sha256 in report.read_text() for capture in run.captures.values())

    open_catalog(page, expect)
    page.get_by_test_id("stRadio").get_by_text("Recording", exact=True).click()
    expect(page.get_by_role("button", name="Load example", exact=True)).to_be_visible()
    catalog_server.restart(page, expect)
    open_catalog(page, expect)
    choose_run(page, expect, root, blackout, "blackout")
    assert tree_snapshot(root) == original


def test_catalog_refresh_detects_external_cli_runs_and_invalidates_changed_pair(
    analyze_page, catalog_server, tmp_path
):
    page, expect = analyze_page
    root = catalog_server.root
    open_catalog(page, expect)
    page.get_by_role("button", name="Refresh catalog", exact=True).click()
    settle_controls(page)
    expect(page.get_by_role("combobox", name="Saved run", exact=True)).to_have_count(0)
    assert not root.exists()
    baseline = run_cli(root, "baseline-cli")
    blackout = run_cli(root, "blackout-cli", "blackout")
    original = tree_snapshot(root)
    page.get_by_role("button", name="Refresh catalog", exact=True).click()
    settle_controls(page)
    for scenario, directory in (("baseline", baseline), ("blackout", blackout)):
        assign_run(page, expect, root, directory, scenario)
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    report = tmp_path / "external-comparison.md"
    download_blocks(page, report)
    assert load_run_directory(baseline).identity in report.read_text()
    assert tree_snapshot(root) == original

    open_catalog(page, expect)
    (baseline / "run.json").write_text('{"schema": "unsupported"}\n')
    changed = tree_snapshot(root)
    page.get_by_role("button", name="Refresh catalog", exact=True).click()
    settle_controls(page)
    expect(page.get_by_role("button", name="Compare selected runs", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    assert tree_snapshot(root) == changed

    shutil.rmtree(blackout)
    changed = tree_snapshot(root)
    page.get_by_role("button", name="Refresh catalog", exact=True).click()
    settle_controls(page)
    expect(page.get_by_role("button", name="Compare selected runs", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Open in Analyze", exact=True)).to_be_disabled()
    expect(
        page.get_by_test_id("stAlert").filter(has_text=re.compile("schema|invalid", re.I))
    ).to_be_visible()
    assert tree_snapshot(root) == changed


def test_catalog_blocks_running_metadata_but_opens_incomplete_terminal_evidence(
    analyze_page, catalog_server, tmp_path
):
    page, expect = analyze_page
    root = catalog_server.root
    terminal = run_cli(root, "terminal", duration=0.2)
    partial = root / "partial"
    running = root / "running"
    shutil.copytree(terminal, partial)
    shutil.copytree(terminal, running)
    (partial / "receiver.tlog").unlink()
    manifest = json.loads((running / "run.json").read_text())
    manifest["outcome"] = "running"
    manifest["end"] = None
    (running / "run.json").write_text(json.dumps(manifest) + "\n")
    original = tree_snapshot(root)

    open_catalog(page, expect)
    choose_run(page, expect, root, running, "baseline", "running")
    expect(page.get_by_role("button", name="Open in Analyze", exact=True)).to_be_disabled()
    expect(page.get_by_role("button", name="Use as baseline", exact=True)).to_be_disabled()
    expect(page.get_by_test_id("stAlert").filter(has_text="Unfinalized manifest")).to_be_visible()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)

    choose_run(page, expect, root, partial, "baseline")
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, partial)
    expect_metric(page, expect, "Evidence status", re.compile("incomplete|invalid", re.I))
    report = tmp_path / "partial-catalog.md"
    blocks = download_blocks(page, report)
    issues = run_report_block(blocks, "issues")
    assert any(
        issue["code"] == "missing_file" and issue["file_name"] == "receiver.tlog"
        for issue in issues["items"]
    )
    assert load_run_directory(partial).identity in report.read_text()
    assert tree_snapshot(root) == original
