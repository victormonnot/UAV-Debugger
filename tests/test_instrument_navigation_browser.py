"""Cross-workspace navigation preserves intent and never revives replaced views."""

import json
from urllib.parse import urlsplit

import pytest
from test_instrument_browser import hold_next_response, release_response
from test_instrument_comparison_browser import (
    chart,
    comparison,
    comparison_report,
    constructed_pair,
    multiple_selections,
    open_catalog_pair,
    open_uploaded_pair,
)
from test_instrument_experiment_browser import experiment_root as experiment_root
from test_instrument_experiment_browser import (
    finished,
    open_experiment,
    start,
)
from test_instrument_experiment_browser import instrument_page as instrument_page
from test_instrument_experiment_browser import instrument_server as instrument_server
from test_instrument_runs_browser import run_section

pytestmark = pytest.mark.browser


def count_comparison_attempts(page):
    # Count fetch intent too: an immediately aborted intermediate request may
    # never reach the HTTP server, but still discards the old comparison draft.
    page.evaluate(
        """() => {
            window.navigationComparisons = [];
            const original = window.fetch.bind(window);
            window.fetch = (...args) => {
                const url = new URL(args[0], location.href);
                if (url.pathname === '/api/comparison') {
                    window.navigationComparisons.push(url.search);
                }
                return original(...args);
            };
        }"""
    )


@pytest.mark.parametrize("destination", ["experiment", "catalog"])
def test_comparison_resume_preserves_drafts_zoom_and_applied_report(
    instrument_page, tmp_path, destination
):
    page, expect = instrument_page
    files = {role: multiple_selections(value) for role, value in constructed_pair(1).items()}
    open_uploaded_pair(page, expect, tmp_path, files)
    comparison(page, expect)
    draft_source = "1:1" if destination == "experiment" else "2:1"
    draft_message = "HEARTBEAT" if destination == "experiment" else "ATTITUDE"
    page.locator("#comparison-source").select_option(draft_source)
    page.locator("#comparison-message").select_option(draft_message)
    page.locator("#comparison-start").fill("0.500000001")
    page.locator("#comparison-end").fill("2.300000001")
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    page.locator("#comparison-plot").scroll_into_view_if_needed()
    drag = page.locator("#comparison-plot .nsewdrag").bounding_box()
    assert drag is not None
    page.mouse.move(drag["x"] + drag["width"] / 3, drag["y"] + drag["height"] / 4)
    page.mouse.down()
    page.mouse.move(drag["x"] + drag["width"] / 2, drag["y"] + drag["height"] * 3 / 4, steps=8)
    page.mouse.up()
    page.wait_for_function(
        "() => document.querySelector('#comparison-plot').layout.xaxis.range[0] > 0.9"
    )
    original = chart(page)
    if destination == "experiment":
        open_experiment(page, expect)
        return_button = "#analyze-button"
    else:
        page.locator("#catalog-input-tab").click()
        expect(page.locator("#catalog-view")).to_have_attribute("aria-busy", "false")
        return_button = "#comparison-input-tab"
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/comparison"):
        page.locator(return_button).click()
    comparison(page, expect)
    expect(page.locator("#comparison-source")).to_have_value(draft_source)
    expect(page.locator("#comparison-message")).to_have_value(draft_message)
    expect(page.locator("#comparison-start")).to_have_value("0.500000001")
    expect(page.locator("#comparison-end")).to_have_value("2.300000001")
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    page.wait_for_function(
        "expected => document.querySelector('#comparison-plot').layout.xaxis.range"
        ".every((value, index) => Math.abs(value - expected[index]) < 1e-8)",
        arg=original["range"],
    )
    assert chart(page) == original
    _, blocks = comparison_report(page, tmp_path / "resumed.md")
    selection = run_section(blocks, "comparison_selection")
    assert selection["source"] == {"system_id": 1, "component_id": 1}
    assert selection["message_type"] == "ATTITUDE"
    assert selection["window"]["start_ns"] == 0
    assert selection["window"]["end_ns"] == 3_000_000_000


def test_experiment_catalog_handoff_does_not_reactivate_previous_comparison(
    instrument_page, tmp_path, experiment_root
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, constructed_pair())
    comparison(page, expect)
    open_experiment(page, expect)
    count_comparison_attempts(page)
    page.locator("#experiment-catalog").click()
    expect(page.locator("#catalog-input-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#catalog-view")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#experiment-view")).to_be_hidden()
    assert page.evaluate("navigationComparisons") == []
    assert not experiment_root.exists()


def test_experiment_saved_handoff_discards_earlier_completed_comparison_without_resume(
    instrument_page, tmp_path, experiment_root
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, constructed_pair())
    comparison(page, expect)
    hold_next_response(page, "/api/comparison")
    page.locator("#comparison-start").fill("0.1")
    page.locator("#comparison-apply").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=0.2)
    run = finished(page, expect, directory)
    count_comparison_attempts(page)
    page.locator("#experiment-open-analyze").click()
    expect(page.locator("#saved-input-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#download-report")).to_be_enabled()
    assert page.evaluate("navigationComparisons") == []
    release_response(page)
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#saved-input-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#comparison-view")).to_be_hidden()
    expect(page.locator("#experiment-view")).to_be_hidden()
    expect(page.locator("#capture-name")).to_have_text("receiver.tlog")
    assert page.evaluate("navigationComparisons") == []


def test_resuming_catalog_comparison_still_revalidates_changed_role(
    instrument_page, experiment_root
):
    page, expect = instrument_page
    open_catalog_pair(page, expect, experiment_root, constructed_pair())
    open_experiment(page, expect)
    manifest = experiment_root / "blackout" / "run.json"
    changed = json.loads(manifest.read_bytes())
    changed["integration_note"] = "Different manifest, identical captures"
    manifest.write_text(json.dumps(changed), encoding="utf-8")
    page.locator("#analyze-button").click()
    expect(page.locator("#comparison-blackout-error")).to_be_visible()
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    expect(page.locator("#comparison-metrics")).to_be_hidden()
    expect(page.locator("#comparison-blackout-identity")).to_have_text("Unavailable")
