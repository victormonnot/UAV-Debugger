"""A terminal worker remains selected after returning from saved Analyze views."""

import pytest
from test_analyze_browser import expect_metric, wait_for_render
from test_experiment_browser import analyze_server as analyze_server
from test_experiment_browser import (
    await_finished,
    open_experiment,
    select_option,
    settle_controls,
    start_run,
    wait_for_measurement,
)
from test_experiment_browser import experiment_root as experiment_root
from test_experiment_browser import experiment_server as experiment_server
from test_saved_run_browser import await_saved_capture

pytestmark = pytest.mark.browser


def test_stopped_run_stays_selected_after_comparison_and_history_stays_selectable(
    analyze_page, experiment_root
):
    page, expect = analyze_page
    open_experiment(page, expect)
    baseline = start_run(page, expect, experiment_root, duration=2.6)
    await_finished(page, expect)
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, baseline)
    open_experiment(page, expect)
    page.get_by_role("button", name="Use as baseline", exact=True).click()
    settle_controls(page)
    start_run(page, expect, experiment_root, duration=2.6, scenario="Blackout")
    await_finished(page, expect)
    page.get_by_role("button", name="Use as blackout", exact=True).click()
    settle_controls(page)
    page.get_by_role("button", name="Compare selected runs", exact=True).click()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    open_experiment(page, expect)
    interrupted = start_run(page, expect, experiment_root, duration=30)
    wait_for_measurement(interrupted)
    page.get_by_role("button", name="Stop experiment", exact=True).click()
    expect_metric(page, expect, "Process state", "finished", timeout=30_000)
    expect(page.get_by_text(f"Run ID: {interrupted.parent.name}", exact=True)).to_be_visible()
    await_finished(page, expect, outcome="interrupted")

    select_option(page, expect, "Run", f"baseline · completed · {baseline.parent.name}")
    expect(page.get_by_text(f"Run ID: {baseline.parent.name}", exact=True)).to_be_visible()
    expect_metric(page, expect, "Declared outcome", "completed")
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, baseline)
    open_experiment(page, expect)
    expect(page.get_by_text(f"Run ID: {baseline.parent.name}", exact=True)).to_be_visible()
    select_option(page, expect, "Run", f"baseline · interrupted · {interrupted.parent.name}")
    page.get_by_role("button", name="Open in Analyze", exact=True).click()
    await_saved_capture(page, expect, interrupted)
