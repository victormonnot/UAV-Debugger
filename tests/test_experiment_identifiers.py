"""Literal generated identifiers survive browser rendering and evidence handoffs."""

from textwrap import dedent

import pytest
from test_analyze_browser import download_blocks, wait_for_render
from test_catalog_browser import assign_run, open_catalog, tree_snapshot
from test_experiment_browser import analyze_server as analyze_server
from test_experiment_browser import (
    await_finished,
    open_experiment,
    run_directories,
    settle_controls,
    start_run,
)
from test_saved_run_browser import await_saved_capture

from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser


@pytest.fixture(params=[("run-_z6bg9i_", "run-__gap___"), ("run-__gap___", "run-_z6bg9i_")])
def run_ids(request):
    # Both are valid eight-character tempfile candidates with Markdown delimiters.
    return request.param


@pytest.fixture
def experiment_root(tmp_path):
    return tmp_path / "_experiments_"


@pytest.fixture
def experiment_launcher(tmp_path, experiment_root, run_ids):
    """Control only this server's run names, retaining real workers and evidence."""
    launcher = tmp_path / "launch_with_known_identifiers.py"
    launcher.write_text(
        dedent(
            f"""\
            import runpy
            import tempfile
            from pathlib import Path
            import uav_debugger

            print(f"Application package: {{uav_debugger.__file__}}", flush=True)
            root = Path({str(experiment_root)!r})
            identifiers = iter({run_ids!r})
            original_mkdtemp = tempfile.mkdtemp

            def run_directory(suffix=None, prefix=None, dir=None):
                if prefix == "run-" and dir is not None and Path(dir) == root:
                    assert suffix in (None, "")
                    directory = root / next(identifiers)
                    directory.mkdir(mode=0o700)
                    return str(directory)
                return original_mkdtemp(suffix=suffix, prefix=prefix, dir=dir)

            tempfile.mkdtemp = run_directory
            runpy.run_module("uav_debugger.analyze", run_name="__main__")
            """
        )
    )
    return [str(launcher)]


@pytest.fixture
def experiment_server(experiment_launcher):
    return {"launcher": experiment_launcher}


@pytest.fixture
def identifier_page(analyze_page, tmp_path):
    page, expect = analyze_page
    try:
        yield page, expect
    finally:
        # Retain literal rendered text as well as the screenshot on any failure.
        (tmp_path / "rendered-text.txt").write_text(page.locator("body").inner_text())
        (tmp_path / "rendered-page.html").write_text(page.content())
        page.screenshot(path=tmp_path / "identifiers.png", full_page=True)


def exact_text(page, expect, value):
    expect(page.get_by_text(value, exact=True)).to_have_text([value])


def test_generated_identifiers_remain_literal_across_execution_and_saved_handoffs(
    identifier_page, experiment_root, run_ids, tmp_path
):
    page, expect = identifier_page
    open_experiment(page, expect)
    baseline = start_run(page, expect, experiment_root, duration=6)
    assert baseline.parent.name == run_ids[0]
    exact_text(page, expect, f"Run ID: {run_ids[0]}")
    await_finished(page, expect)
    exact_text(page, expect, f"Run ID: {run_ids[0]}")
    page.get_by_role("button", name="Use as baseline", exact=True).click()
    settle_controls(page)
    exact_text(page, expect, f"Selected baseline: {run_ids[0]}")
    exact_text(page, expect, f"Output directory: {experiment_root}")

    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, baseline)
    baseline_run = load_run_directory(baseline)
    report = tmp_path / "baseline-report.md"
    download_blocks(page, report)
    assert baseline_run.identity in report.read_text()

    open_experiment(page, expect)
    blackout = start_run(page, expect, experiment_root, duration=6, scenario="Blackout")
    assert blackout.parent.name == run_ids[1]
    exact_text(page, expect, f"Run ID: {run_ids[1]}")
    await_finished(page, expect)
    exact_text(page, expect, f"Run ID: {run_ids[1]}")
    page.get_by_role("button", name="Use as blackout", exact=True).click()
    settle_controls(page)
    for role, identifier in zip(("baseline", "blackout"), run_ids, strict=True):
        exact_text(page, expect, f"Selected {role}: {identifier}")

    original = tree_snapshot(experiment_root)
    runs = (baseline_run, load_run_directory(blackout))
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    report = tmp_path / "controller-comparison.md"
    download_blocks(page, report)
    for run in runs:
        assert run.identity in report.read_text()
        assert all(capture.sha256 in report.read_text() for capture in run.captures.values())

    open_catalog(page, expect)
    for role, directory in (("baseline", baseline), ("blackout", blackout)):
        assign_run(page, expect, experiment_root, directory, role)
        exact_text(page, expect, f"Selected {role}: {directory.relative_to(experiment_root)}")
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    report = tmp_path / "catalog-comparison.md"
    download_blocks(page, report)
    for run in runs:
        assert run.identity in report.read_text()
        assert all(capture.sha256 in report.read_text() for capture in run.captures.values())
    assert run_directories(experiment_root) == {baseline, blackout}
    assert tree_snapshot(experiment_root) == original
