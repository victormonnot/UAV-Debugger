"""Compare two saved runs through the real offline browser workflow."""

import json
import re
import shutil

import pytest
from test_analyze_browser import choose, download_blocks, expect_metric, wait_for_render

from uav_debugger.comparison import compare_runs
from uav_debugger.experiment import ExperimentConfig, run_experiment
from uav_debugger.saved_run import load_run_directory

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def comparison_directories(tmp_path_factory):
    root = tmp_path_factory.mktemp("comparison")
    paths = {}
    for role in ("baseline", "blackout"):
        path = root / role
        result = run_experiment(
            path,
            ExperimentConfig(
                scenario=role,
                duration_s=3,
                blackout_at_s=0.4 if role == "blackout" else None,
            ),
        )
        assert result["outcome"] == "completed", result
        paths[role] = path
    return paths


def open_pair(page, expect, paths):
    page.get_by_test_id("stRadio").get_by_text("Compare experiments", exact=True).click()
    expect(page.get_by_role("heading", name="Compare experiments", exact=True)).to_be_visible()
    for index, role in enumerate(("baseline", "blackout")):
        page.locator('input[type="file"][webkitdirectory]').nth(index).set_input_files(
            str(paths[role])
        )
        expect(
            page.get_by_text(re.compile(rf"^{role.title()} fingerprint: [0-9a-f]{{64}}$"))
        ).to_be_visible()
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)


def chart_data(page, point="Receiver"):
    page.wait_for_function(
        "title => { const graph = document.querySelector("
        "'.st-key-comparison_activity_plot .js-plotly-plot'); "
        "return graph?.data && graph.layout?.title?.text === title; }",
        arg=f"{point} observation activity",
    )
    wait_for_render(page)
    return page.locator(".st-key-comparison_activity_plot .js-plotly-plot").evaluate(
        "graph => ({traces: graph.data.map(({name, y}) => ({name, y})), "
        "shapes: graph.layout.shapes, xaxis: graph.layout.xaxis.title.text})"
    )


def test_comparison_window_point_and_export_follow_observed_evidence(
    analyze_page, comparison_directories, tmp_path
):
    page, expect = analyze_page
    runs = [load_run_directory(comparison_directories[role]) for role in ("baseline", "blackout")]
    expected = compare_runs(*runs)
    assert expected.comparable, expected.issues
    open_pair(page, expect, comparison_directories)
    chart = chart_data(page)
    assert "each run's measurement start" in chart["xaxis"]
    assert {trace["name"].lower(): sum(trace["y"]) for trace in chart["traces"]} == {
        role: expected.metrics[role]["receiver"].count for role in ("baseline", "blackout")
    }
    gate = expected.gates["blackout"][0]
    assert any(
        shape["x0"] == gate.start_ns / 1e9 and shape["x1"] == gate.end_ns / 1e9
        for shape in chart["shapes"]
    )
    choose(page, "Comparison observation point", "Relay input")
    assert {
        trace["name"].lower(): sum(trace["y"])
        for trace in chart_data(page, "Relay input")["traces"]
    } == {role: expected.metrics[role]["relay-input"].count for role in ("baseline", "blackout")}
    page.get_by_label("Window start (s)", exact=True).fill("0.5")
    page.get_by_label("Window end (s)", exact=True).fill("2.3")
    page.get_by_role("button", name="Apply comparison window", exact=True).click()
    expect(
        page.get_by_text(re.compile(r"^Comparison window: \[0\.500000000, 2\.300000000\)"))
    ).to_be_visible()
    wait_for_render(page)
    expected = compare_runs(*runs, start_ns=500_000_000, end_ns=2_300_000_000)
    assert {
        trace["name"].lower(): sum(trace["y"])
        for trace in chart_data(page, "Relay input")["traces"]
    } == {role: expected.metrics[role]["relay-input"].count for role in ("baseline", "blackout")}
    report_path = tmp_path / "comparison.md"
    download_blocks(page, report_path)
    report = report_path.read_text()
    for run in runs:
        assert run.identity in report
        for capture in run.captures.values():
            assert capture.sha256 in report
    assert "500000000" in report and "2300000000" in report
    assert "ATTITUDE" in report and "monotonic" in report
    assert "observations.jsonl" in report and "actions.jsonl" in report


def test_replacing_evidence_blocks_metrics_and_rejected_input_removes_download(
    analyze_page, comparison_directories, tmp_path
):
    page, expect = analyze_page
    open_pair(page, expect, comparison_directories)
    partial = tmp_path / "partial"
    shutil.copytree(comparison_directories["blackout"], partial)
    (partial / "observations.jsonl").unlink()
    page.get_by_role("button", name="Clear blackout", exact=True).click()
    expect(page.get_by_text("Select the saved blackout experiment directory.")).to_be_visible()
    page.locator('input[type="file"][webkitdirectory]').nth(1).set_input_files(str(partial))
    expect(page.get_by_text("Comparison unavailable", exact=True)).to_be_visible()
    wait_for_render(page)
    expect(page.get_by_role("heading", name="Observed comparison", exact=True)).to_have_count(0)
    expect(page.locator(".st-key-comparison_activity_plot")).to_have_count(0)
    report_path = tmp_path / "partial.md"
    download_blocks(page, report_path)
    assert load_run_directory(partial).identity in report_path.read_text()
    assert "observations.jsonl" in report_path.read_text()
    rejected = tmp_path / "rejected"
    rejected.mkdir()
    (rejected / "run.json").write_text(json.dumps({"schema": "unsupported"}))
    page.get_by_role("button", name="Clear blackout", exact=True).click()
    expect(page.get_by_text("Select the saved blackout experiment directory.")).to_be_visible()
    page.locator('input[type="file"][webkitdirectory]').nth(1).set_input_files(str(rejected))
    expect(page.get_by_text(re.compile("Blackout: .*schema"))).to_be_visible()
    page.locator('[data-testid="stApp"][data-test-script-state="notRunning"]').wait_for()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    expect(page.get_by_text(re.compile(r"^Blackout fingerprint:"))).to_have_count(0)


def test_clear_comparison_and_return_to_independent_analyze(analyze_page, comparison_directories):
    page, expect = analyze_page
    open_pair(page, expect, comparison_directories)
    page.get_by_role("button", name="Clear comparison", exact=True).click()
    expect(page.get_by_text("Select the saved baseline experiment directory.")).to_be_visible()
    expect(page.get_by_text("Select the saved blackout experiment directory.")).to_be_visible()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    page.get_by_test_id("stRadio").get_by_text("Recording", exact=True).click()
    page.get_by_role("button", name="Load example", exact=True).click()
    expect_metric(page, expect, "Imported records", "12")
    wait_for_render(page)
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    expect(page.get_by_role("heading", name="Compare experiments", exact=True)).to_have_count(0)


def test_valid_replacement_resets_window_and_missing_duration_still_exports_reasons(
    analyze_page, comparison_directories, tmp_path
):
    page, expect = analyze_page
    open_pair(page, expect, comparison_directories)
    page.get_by_label("Window start (s)", exact=True).fill("0.5")
    page.get_by_label("Window end (s)", exact=True).fill("2.3")
    page.get_by_role("button", name="Apply comparison window", exact=True).click()
    expect(
        page.get_by_text(re.compile(r"^Comparison window: \[0\.500000000, 2\.300000000\)"))
    ).to_be_visible()
    wait_for_render(page)
    replacement = tmp_path / "replacement"
    shutil.copytree(comparison_directories["blackout"], replacement)
    manifest_path = replacement / "run.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["environment"]["comparison_test_label"] = "replacement"
    manifest_path.write_text(json.dumps(manifest))
    page.get_by_role("button", name="Clear blackout", exact=True).click()
    expect(page.get_by_text("Select the saved blackout experiment directory.")).to_be_visible()
    page.locator('input[type="file"][webkitdirectory]').nth(1).set_input_files(str(replacement))
    expect(page.get_by_text("Comparison available", exact=True)).to_be_visible()
    wait_for_render(page)
    expect(page.get_by_label("Window start (s)", exact=True)).to_have_value("0")
    expect(page.get_by_label("Window end (s)", exact=True)).to_have_value("3.000000000")
    incomplete = tmp_path / "missing-duration"
    shutil.copytree(replacement, incomplete)
    del manifest["requested"]["duration_s"]
    (incomplete / "run.json").write_text(json.dumps(manifest))
    page.get_by_role("button", name="Clear blackout", exact=True).click()
    expect(page.get_by_text("Select the saved blackout experiment directory.")).to_be_visible()
    page.locator('input[type="file"][webkitdirectory]').nth(1).set_input_files(str(incomplete))
    expect(page.get_by_text("Comparison unavailable", exact=True)).to_be_visible()
    wait_for_render(page)
    expect(page.get_by_label("Window end (s)", exact=True)).to_have_count(0)
    report_path = tmp_path / "missing-duration-report.md"
    download_blocks(page, report_path)
    assert load_run_directory(incomplete).identity in report_path.read_text()
    assert "duration" in report_path.read_text()
