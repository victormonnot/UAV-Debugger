"""Open saved experiment directories without starting an experiment from Analyze."""

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from test_analyze_browser import (
    FIXTURE,
    apply_changed_filters,
    choose,
    download_blocks,
    metric,
    set_interval,
    wait_for_render,
)

from uav_debugger import import_file
from uav_debugger.experiment import ExperimentConfig, run_experiment

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def saved_experiment_directories(tmp_path_factory):
    """Generate actual evidence before browser upload; the UI only inspects files."""
    root = tmp_path_factory.mktemp("saved-experiments")
    outputs = {}
    for scenario in ("baseline", "blackout"):
        output = root / scenario
        config = ExperimentConfig(
            scenario=scenario,
            duration_s=0.15 if scenario == "baseline" else 2.3,
            blackout_at_s=None if scenario == "baseline" else 0.1,
        )
        result = run_experiment(output, config)
        assert result["outcome"] == "completed", result["error"]
        outputs[scenario] = output
    return outputs


def upload_directory(page, directory: Path):
    page.get_by_test_id("stRadio").get_by_text("Saved experiment", exact=True).click()
    # Chromium reports each file's relative directory path, including the root.
    page.locator('input[type="file"][webkitdirectory]').set_input_files(str(directory))


def await_saved_capture(page, expect, directory: Path, point="relay-input"):
    imported = import_file(directory / f"{point}.tlog")
    expect(page.get_by_role("heading", name="Saved experiment", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Run fingerprint: [0-9a-f]{64}$"))).to_be_visible()
    expect(page.locator("code").filter(has_text=imported.sha256)).to_have_count(1)
    expect(metric(page, "Imported records")).to_have_text(str(len(imported.records)))
    wait_for_render(page)
    return imported


def capture_report_blocks(blocks, source_name):
    provenance = next(block for block in blocks if block.get("source_name") == source_name)
    detail = next(block for block in blocks if "raw_frame_hex" in block)
    selection = next(block for block in blocks if "relative_origin_us" in block)
    return provenance, selection, detail


def run_report_block(blocks, name):
    return next(block[name] for block in blocks if name in block)


def timeline_data(page):
    return page.locator(".st-key-run_activity_plot .js-plotly-plot").evaluate(
        "graph => ({traces: graph.data.map(({name, y}) => ({name, y})), "
        "shapes: graph.layout.shapes, xaxis: graph.layout.xaxis.title.text})"
    )


def test_saved_baseline_directory_inspection_exports_run_and_capture_evidence(
    analyze_page, saved_experiment_directories, tmp_path
):
    page, expect = analyze_page
    directory = saved_experiment_directories["baseline"]
    original = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    upload_directory(page, directory)
    imported = await_saved_capture(page, expect, directory)

    expect(metric(page, "Declared outcome")).to_have_text("completed")
    expect(metric(page, "Evidence status")).to_have_text("consistent")
    expect(metric(page, "Relay input records")).to_have_text(str(len(imported.records)))
    expect(metric(page, "Receiver records")).to_have_text(str(len(imported.records)))
    inspected = imported.records[1]
    choose(page, "Record", f"#{inspected.index} · ATTITUDE · 1 / 1")
    report_path = tmp_path / "baseline-report.md"
    blocks = download_blocks(page, report_path)
    report = report_path.read_text()
    provenance, selection, detail = capture_report_blocks(blocks, "relay-input.tlog")
    assert provenance["sha256"] == imported.sha256
    assert detail["index"] == inspected.index
    assert detail["raw_frame_hex"] == inspected.raw_frame.hex()
    assert selection["sources"] is None
    run_provenance = run_report_block(blocks, "run_provenance")
    assert run_provenance["selected_point"] == "relay-input"
    assert run_provenance["files"]["relay-input.tlog"]["sha256"] == imported.sha256
    assert run_report_block(blocks, "status")["evidence_status"] == "consistent"
    assert run_report_block(blocks, "requested")["scenario"] == "baseline"
    assert hashlib.sha256(original["run.json"]).hexdigest() in report
    fingerprint = page.get_by_text(re.compile(r"^Run fingerprint: [0-9a-f]{64}$")).inner_text()
    assert fingerprint.split(": ", 1)[1] in report
    assert "baseline" in report and "completed" in report
    assert "monotonic" in report.lower()
    timeline = timeline_data(page)
    assert "monotonic" in timeline["xaxis"]
    assert {trace["name"]: sum(trace["y"]) for trace in timeline["traces"]} == {
        "Relay input": len(imported.records),
        "Receiver": len(imported.records),
    }
    assert {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    } == original


def test_saved_blackout_point_switch_resets_selection_and_keeps_applied_evidence(
    analyze_page, saved_experiment_directories, tmp_path
):
    page, expect = analyze_page
    directory = saved_experiment_directories["blackout"]
    manifest = json.loads((directory / "run.json").read_text())
    actions = [json.loads(line) for line in (directory / "actions.jsonl").read_text().splitlines()]
    disabled = next(action for action in actions if action["action"] == "forwarding_disabled")
    enabled = next(action for action in actions if action["action"] == "forwarding_enabled")

    upload_directory(page, directory)
    before = await_saved_capture(page, expect, directory)
    choose(page, "Source", "1 / 1")
    choose(page, "Message type", "ATTITUDE")
    apply_changed_filters(page)
    set_interval(page, 0, 0)
    expect(metric(page, "Selected records")).to_have_text("1")

    choose(page, "Observation point", "Receiver")
    after = await_saved_capture(page, expect, directory, "receiver")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    expect(page.get_by_role("combobox", name="Message type", exact=True)).to_have_value(
        "All message types"
    )
    expect(metric(page, "Selected records")).to_have_text(str(len(after.records)))
    expect(metric(page, "Relay input records")).to_have_text(str(len(before.records)))
    expect(metric(page, "Receiver records")).to_have_text(str(len(after.records)))
    assert len(before.records) > len(after.records) > 1
    inspected = after.records[1]
    choose(page, "Record", f"#{inspected.index} · ATTITUDE · 1 / 1")
    report_path = tmp_path / "blackout-receiver-report.md"
    blocks = download_blocks(page, report_path)
    provenance, selection, detail = capture_report_blocks(blocks, "receiver.tlog")
    assert provenance["sha256"] == after.sha256
    assert selection["sources"] is None
    assert selection["start_us"] == after.records[0].timestamp_us
    assert selection["end_us"] == after.records[-1].timestamp_us
    assert detail["raw_frame_hex"] == inspected.raw_frame.hex()
    assert run_report_block(blocks, "run_provenance")["selected_point"] == "receiver"
    applied = run_report_block(blocks, "applied_actions")
    assert applied["interval_count"] == 1
    interval = applied["intervals"][0]
    assert interval["start"]["monotonic_ns"] == disabled["monotonic_ns"]
    assert interval["end"]["monotonic_ns"] == enabled["monotonic_ns"]
    assert interval["start"]["file_name"] == "actions.jsonl"
    assert interval["start"]["line_number"] == actions.index(disabled) + 1
    assert interval["start"]["valid"] is True
    evidence = run_report_block(blocks, "observed_evidence")
    assert evidence["declared_counters"] == manifest["counters"]
    timeline = timeline_data(page)
    assert {trace["name"]: sum(trace["y"]) for trace in timeline["traces"]} == {
        "Relay input": len(before.records),
        "Receiver": len(after.records),
    }
    assert any(
        shape["type"] == "rect"
        and shape["x0"] == disabled["elapsed_ns"] / 1e9
        and shape["x1"] == enabled["elapsed_ns"] / 1e9
        for shape in timeline["shapes"]
    )
    report = report_path.read_text()
    assert "forwarding_disabled" in report and "forwarding_enabled" in report
    assert str(disabled["monotonic_ns"]) in report
    assert str(enabled["monotonic_ns"]) in report
    assert str(manifest["counters"]["relay_dropped"]) in report
    assert "blackout" in report and "receiver" in report


def test_partial_saved_run_reports_missing_capture_without_claiming_completion(
    analyze_page, saved_experiment_directories, tmp_path
):
    page, expect = analyze_page
    directory = tmp_path / "partial-run"
    shutil.copytree(saved_experiment_directories["baseline"], directory)
    (directory / "receiver.tlog").unlink()
    unrelated = directory / "working-cache.bin"
    unrelated.write_bytes(b"Unrelated simulator working bytes\x00\xff")
    unrelated_bytes = unrelated.read_bytes()
    manifest = json.loads((directory / "run.json").read_text())
    manifest["outcome"] = "running"
    manifest["end"] = None
    (directory / "run.json").write_text(json.dumps(manifest) + "\n")

    upload_directory(page, directory)
    imported = await_saved_capture(page, expect, directory)
    expect(page.locator('[data-testid="stFileChip"][aria-invalid="true"]')).to_have_count(0)
    excluded_files = page.get_by_text("Other files excluded from analysis (1)", exact=True)
    expect(excluded_files).to_be_visible()
    excluded_files.click()
    expect(page.get_by_text("working-cache.bin", exact=True)).to_be_visible()
    expect(metric(page, "Declared outcome")).to_have_text("running")
    expect(metric(page, "Evidence status")).to_have_text(re.compile("incomplete|invalid", re.I))
    expect(metric(page, "Receiver records")).to_have_text(re.compile("^(Unavailable|—)$", re.I))
    expect(
        page.get_by_test_id("stAlert").filter(has_text="evidence is incomplete or inconsistent")
    ).to_be_visible()
    report_path = tmp_path / "partial-report.md"
    blocks = download_blocks(page, report_path)
    provenance, _, _ = capture_report_blocks(blocks, "relay-input.tlog")
    assert provenance["sha256"] == imported.sha256
    issues = run_report_block(blocks, "issues")
    assert any(
        issue["code"] == "missing_file" and issue["file_name"] == "receiver.tlog"
        for issue in issues["items"]
    )
    assert run_report_block(blocks, "status")["declared_outcome"] == "running"
    assert "working-cache.bin" not in run_report_block(blocks, "run_provenance")["files"]
    assert unrelated.read_bytes() == unrelated_bytes
    report = report_path.read_text()
    assert "running" in report and "receiver.tlog" in report
    assert hashlib.sha256((directory / "run.json").read_bytes()).hexdigest() in report


def test_invalid_directory_replacement_removes_stale_export_and_clear_restores_recording(
    analyze_page, saved_experiment_directories, tmp_path
):
    page, expect = analyze_page
    original = saved_experiment_directories["baseline"]
    upload_directory(page, original)
    await_saved_capture(page, expect, original)
    broken = tmp_path / "invalid-run"
    broken.mkdir()
    (broken / "run.json").write_text("{invalid JSON")

    # Streamlit directory selections are additive. A second root is invalid,
    # and cannot leave the first run's valid report available for download.
    page.locator('input[type="file"][webkitdirectory]').set_input_files(str(broken))
    expect(
        page.get_by_test_id("stAlert").filter(has_text="Select exactly one experiment directory")
    ).to_be_visible()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    expect(metric(page, "Imported records")).to_have_count(0)

    page.get_by_role("button", name="Clear experiment", exact=True).click()
    expect(
        page.get_by_test_id("stAlert").filter(has_text="Select one saved experiment directory")
    ).to_be_visible()
    expect(page.get_by_role("heading", name="Saved experiment", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    page.locator('input[type="file"][webkitdirectory]').set_input_files(str(broken))
    expect(
        page.get_by_test_id("stAlert").filter(has_text="Expecting property name")
    ).to_be_visible()
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Clear experiment", exact=True).click()
    expect(
        page.get_by_test_id("stAlert").filter(has_text="Select one saved experiment directory")
    ).to_be_visible()
    page.get_by_test_id("stRadio").get_by_text("Recording", exact=True).click()
    page.locator('input[type="file"]:not([webkitdirectory])').set_input_files(FIXTURE)
    expect(metric(page, "Imported records")).to_have_text("12")
    expect(page.get_by_role("combobox", name="Observation point", exact=True)).to_have_count(0)
    wait_for_render(page)
    report_path = tmp_path / "standalone-recording-report.md"
    blocks = download_blocks(page, report_path)
    assert blocks[0]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert len(blocks) == 5
    assert "Run fingerprint:" not in report_path.read_text()
