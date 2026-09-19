"""Real upload, filtering, inspection and download through the Analyze browser UI."""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
FIXTURE = Path(__file__).parent / "fixtures" / "telemetry-gap.tlog"
EXPECTED = json.loads(FIXTURE.with_suffix(".expected.json").read_text())


def metric(page, label):
    return (
        page.get_by_test_id("stMetric")
        .filter(has=page.get_by_text(label, exact=True))
        .get_by_test_id("stMetricValue")
    )


def choose(page, label, option):
    # Streamlit streams a rerun incrementally; wait before interacting with a new widget.
    page.locator('[data-testid="stApp"][data-test-script-state="notRunning"]').wait_for()
    page.get_by_role("button", name="Download report", exact=True).wait_for(state="visible")
    field = page.get_by_role("combobox", name=label, exact=True)
    # Keyboard opening also handles an existing value that a click only focuses.
    field.click()
    field.press("ArrowDown")
    page.get_by_role("option", name=option, exact=True).click()


def set_interval(page, start, end):
    page.get_by_role("textbox", name="Start (s)", exact=True).fill(str(start))
    page.get_by_role("textbox", name="End (s)", exact=True).fill(str(end))
    page.get_by_role("button", name="Apply filters", exact=True).click()


def upload(page, content, name="telemetry-gap.tlog"):
    page.locator('input[type="file"]').set_input_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": content}
    )


def activity_plot(page):
    return page.locator(".st-key-activity_plot").get_by_test_id("stPlotlyChart")


def attitude_plot(page):
    return page.locator(".st-key-attitude_plot .js-plotly-plot")


def attitude_traces(page):
    return attitude_plot(page).evaluate(
        "graph => graph.data.map(({x, y, customdata}) => ({x, y, customdata}))"
    )


def click_attitude_point(page, point_index):
    graph = attitude_plot(page)
    graph.scroll_into_view_if_needed()
    point = graph.locator(".scatterlayer .trace").first.locator("path.point").nth(point_index)
    bounds = point.bounding_box()
    assert bounds is not None
    # Plotly's drag layer covers the marker, so click its actual screen position.
    page.mouse.click(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)


def download_blocks(page, path):
    page.locator('[data-testid="stApp"][data-test-script-state="notRunning"]').wait_for()
    with page.expect_download() as pending:
        page.get_by_role("button", name="Download report", exact=True).click()
    download = pending.value
    download.save_as(path)
    assert download.failure() is None
    return [
        json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", path.read_text(), re.S)
    ]


def test_upload_filter_inspect_and_download(analyze_page, tmp_path):
    page, expect = analyze_page
    original = FIXTURE.read_bytes()
    page.locator('input[type="file"]').set_input_files(FIXTURE)
    expect(metric(page, "Imported records")).to_have_text("12")
    expect(metric(page, "Sources")).to_have_text("2")
    expect(metric(page, "Selected records")).to_have_text("12")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    expect(page.get_by_role("combobox", name="Message type", exact=True)).to_have_value(
        "All message types"
    )

    choose(page, "Source", "1 / 1")
    choose(page, "Message type", "ATTITUDE")
    page.get_by_role("button", name="Apply filters", exact=True).click()
    expect(metric(page, "Selected records")).to_have_text("2")
    set_interval(page, 1, 5)
    expect(metric(page, "Selected records")).to_have_text("2")
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#2 · ATTITUDE · 1 / 1"
    )
    expect(metric(page, "Longest observed interval")).to_have_text("4 s")
    expect(activity_plot(page)).to_be_visible()
    expect(
        page.get_by_text("Showing 1–2 of 2 records in original file order.", exact=True)
    ).to_be_visible()
    expect(page.get_by_text("Raw frame", exact=True)).to_be_visible()
    expect(page.get_by_text("Decoded fields", exact=True)).to_be_visible()
    # The visible raw bytes must still identify the original global record #2.
    raw_frame = EXPECTED["records"][2]["frame_hex"]
    assert raw_frame in re.sub(r"\s+", "", "".join(page.locator("code").all_text_contents()))
    expect(page.get_by_text("time_boot_ms", exact=False).first).to_be_visible()
    expect(page.get_by_text("0.25", exact=False).first).to_be_visible()

    with page.expect_download() as pending:
        page.get_by_role("button", name="Download report", exact=True).click()
    download = pending.value
    report_path = tmp_path / "evidence.md"
    download.save_as(report_path)
    assert download.failure() is None
    assert download.suggested_filename.endswith(".md")
    report = report_path.read_text()
    blocks = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", report, re.S)]
    provenance, selection, coverage, issues, detail = blocks
    assert provenance["sha256"] == hashlib.sha256(original).hexdigest()
    assert provenance["source_name"] == FIXTURE.name
    assert selection == {
        "sources": [[1, 1]],
        "message_ids": [30],
        "start_us": 1_700_000_001_000_000,
        "end_us": 1_700_000_005_000_000,
        "bounds": "inclusive",
        "relative_origin_us": 1_700_000_000_000_000,
    }
    assert coverage["filtered_record_count"] == 2
    assert coverage["imported_record_count"] == 12
    assert coverage["explicit_detail_record_count"] == 1
    assert coverage["observation_intervals"]["longest"] == {
        "delta_us": 4_000_000,
        "previous_index": 2,
        "index": 8,
        "system_id": 1,
        "component_id": 1,
        "message_id": 30,
    }
    assert len(issues) == 5
    assert detail["index"] == 2
    assert (detail["offset"], detail["frame_offset"], detail["end_offset"]) == (54, 62, 90)
    assert detail["raw_frame_hex"] == raw_frame
    assert detail["fields"] == EXPECTED["records"][2]["fields"]
    assert "host" in report.lower() and "clock" in report.lower()
    assert "packet loss" in report.lower()
    assert FIXTURE.read_bytes() == original

    set_interval(page, 2, 4)
    expect(metric(page, "Selected records")).to_have_text("0")
    expect(page.get_by_text("No records match these filters.", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_count(0)
    with page.expect_download() as pending:
        page.get_by_role("button", name="Download report", exact=True).click()
    pending.value.save_as(tmp_path / "empty-selection.md")
    empty_report = (tmp_path / "empty-selection.md").read_text()
    assert '"filtered_record_count": 0' in empty_report
    assert '"explicit_detail_record_count": 0' in empty_report
    assert "No record details were explicitly selected." in empty_report
    choose(page, "Source", "All sources")
    choose(page, "Message type", "All message types")
    set_interval(page, 0, 6)
    expect(metric(page, "Selected records")).to_have_text("12")
    expect(activity_plot(page)).to_be_visible()
    page.get_by_role("heading", name="Analyze", exact=True).scroll_into_view_if_needed()


def test_changed_bytes_reset_filters_and_record_selection(analyze_page):
    page, expect = analyze_page
    upload(page, FIXTURE.read_bytes())
    expect(metric(page, "Imported records")).to_have_text("12")
    choose(page, "Source", "2 / 1")
    choose(page, "Message type", "ATTITUDE")
    set_interval(page, 1, 5)
    expect(metric(page, "Selected records")).to_have_text("5")
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#3 · ATTITUDE · 2 / 1"
    )
    choose(page, "Record", "#9 · ATTITUDE · 2 / 1")
    expect(page.get_by_role("heading", name="Record #9", exact=True)).to_be_visible()

    # Keep the filename: a changed fingerprint must invalidate prior widget state.
    upload(page, FIXTURE.read_bytes()[:25])
    expect(metric(page, "Imported records")).to_have_text("1")
    expect(metric(page, "Sources")).to_have_text("1")
    expect(metric(page, "Selected records")).to_have_text("1")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    expect(page.get_by_role("combobox", name="Message type", exact=True)).to_have_value(
        "All message types"
    )
    expect(page.get_by_role("textbox", name="Start (s)", exact=True)).to_have_value("0")
    expect(page.get_by_role("textbox", name="End (s)", exact=True)).to_have_value("0")
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#0 · HEARTBEAT · 1 / 1"
    )


def test_empty_and_truncated_uploads_show_outcomes_without_crashing(analyze_page):
    page, expect = analyze_page
    upload(page, b"", name="empty.tlog")
    expect(metric(page, "Imported records")).to_have_text("0")
    page.get_by_text("Import issues (1)", exact=True).click()
    expect(page.get_by_text("empty_input", exact=False).first).to_be_visible()
    expect(page.get_by_test_id("stException")).to_have_count(0)

    upload(page, FIXTURE.read_bytes()[:-1], name="truncated.tlog")
    expect(metric(page, "Imported records")).to_have_text("11")
    expect(page.get_by_text("Import issues (5)", exact=True)).to_be_visible()
    if not page.get_by_text("incomplete_frame", exact=False).first.is_visible():
        page.get_by_text("Import issues (5)", exact=True).click()
    expect(page.get_by_text("incomplete_frame", exact=False).first).to_be_visible()
    expect(page.get_by_text("stopped", exact=False).first).to_be_visible()
    expect(metric(page, "Selected records")).to_have_text("11")
    expect(page.get_by_test_id("stException")).to_have_count(0)


def test_oversized_upload_removes_stale_results_and_allows_recovery(analyze_page):
    page, expect = analyze_page
    upload(page, FIXTURE.read_bytes())
    expect(metric(page, "Imported records")).to_have_text("12")
    upload(page, bytes(10 * 1024 * 1024 + 1), name="oversized.tlog")
    expect(
        page.get_by_text("Input exceeds the 10485760-byte size limit.", exact=True)
    ).to_be_visible()
    expect(page.get_by_test_id("stMetric")).to_have_count(0)
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)

    upload(page, FIXTURE.read_bytes()[:25], name="recovered.tlog")
    expect(metric(page, "Imported records")).to_have_text("1")
    expect(metric(page, "Selected records")).to_have_text("1")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")


def test_bundled_example_without_upload_and_explicit_source_switches(analyze_page, tmp_path):
    page, expect = analyze_page
    expect(page.get_by_test_id("stMetric")).to_have_count(0)
    expect(page.locator('input[type="file"]')).to_have_value("")
    page.get_by_role("button", name="Load example", exact=True).click()
    expect(metric(page, "Imported records")).to_have_text("12")
    expect(metric(page, "Sources")).to_have_text("2")
    expect(metric(page, "Selected records")).to_have_text("12")
    expect(page.locator('input[type="file"]')).to_have_value("")
    expect(page.get_by_role("button", name="Clear example", exact=True)).to_be_visible()
    expect(page.get_by_text(re.compile(r"^Synthetic example —"))).to_be_visible()

    choose(page, "Source", "1 / 1")
    choose(page, "Message type", "ATTITUDE")
    set_interval(page, 1, 5)
    expect(metric(page, "Selected records")).to_have_text("2")
    expect(metric(page, "Longest observed interval")).to_have_text("4 s")
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#2 · ATTITUDE · 1 / 1"
    )
    expect(activity_plot(page)).to_be_visible()
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        screenshot = FIXTURE.parents[2] / "local" / "example-analyze.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page.get_by_role("heading", name="Analyze", exact=True).scroll_into_view_if_needed()
        page.screenshot(path=screenshot, full_page=True)

    with page.expect_download() as pending:
        page.get_by_role("button", name="Download report", exact=True).click()
    download = pending.value
    report_path = tmp_path / "synthetic-example.md"
    download.save_as(report_path)
    assert download.failure() is None
    report = report_path.read_text()
    provenance, selection, coverage, _, detail = [
        json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", report, re.S)
    ]
    assert provenance["source_name"] == "telemetry-gap.tlog (synthetic example)"
    assert provenance["sha256"] == EXPECTED["sha256"]
    assert provenance["size_bytes"] == 409
    assert selection["sources"] == [[1, 1]]
    assert selection["message_ids"] == [30]
    assert selection["start_us"] == 1_700_000_001_000_000
    assert selection["end_us"] == 1_700_000_005_000_000
    assert selection["bounds"] == "inclusive"
    assert coverage["filtered_record_count"] == 2
    longest = coverage["observation_intervals"]["longest"]
    assert (longest["previous_index"], longest["index"], longest["delta_us"]) == (2, 8, 4_000_000)
    assert detail["index"] == 2
    assert detail["raw_frame_hex"] == EXPECTED["records"][2]["frame_hex"]
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        (FIXTURE.parents[2] / "local" / "example-report.md").write_text(report, encoding="utf-8")

    upload(page, FIXTURE.read_bytes()[:25], name="uploaded-short.tlog")
    expect(metric(page, "Imported records")).to_have_text("1")
    expect(metric(page, "Selected records")).to_have_text("1")
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    expect(page.get_by_role("button", name="Clear example", exact=True)).to_have_count(0)
    expect(page.get_by_text("telemetry-gap.tlog (synthetic example)", exact=True)).to_have_count(0)
    expect(page.get_by_text(re.compile(r"^Synthetic example —"))).to_have_count(0)

    # Removing an upload must not silently restore the earlier synthetic input.
    page.get_by_role("button", name="Remove uploaded-short.tlog", exact=True).click()
    expect(page.get_by_test_id("stMetric")).to_have_count(0)
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Clear example", exact=True)).to_have_count(0)

    upload(page, FIXTURE.read_bytes()[:25], name="uploaded-short.tlog")
    expect(metric(page, "Imported records")).to_have_text("1")
    page.get_by_role("button", name="Load example", exact=True).click()
    expect(metric(page, "Imported records")).to_have_text("12")
    expect(metric(page, "Selected records")).to_have_text("12")
    expect(page.locator('input[type="file"]')).to_have_value("")
    expect(page.get_by_role("button", name="Remove uploaded-short.tlog", exact=True)).to_have_count(
        0
    )
    expect(page.get_by_role("combobox", name="Source", exact=True)).to_have_value("All sources")
    page.get_by_role("button", name="Clear example", exact=True).click()
    expect(page.get_by_test_id("stMetric")).to_have_count(0)
    expect(page.get_by_role("button", name="Download report", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Clear example", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Load example", exact=True)).to_be_visible()


def test_attitude_points_gap_settings_and_inspector_stay_consistent(analyze_page, tmp_path):
    page, expect = analyze_page
    page.get_by_role("button", name="Load example", exact=True).click()
    expect(metric(page, "Imported records")).to_have_text("12")
    # Two independent attitude sources must not be joined in a single plot.
    expect(attitude_plot(page)).to_have_count(0)
    choose(page, "Source", "1 / 1")
    choose(page, "Message type", "ATTITUDE")
    set_interval(page, 1, 5)
    expect(metric(page, "Selected records")).to_have_text("2")
    expect(attitude_plot(page)).to_be_visible()

    traces = attitude_traces(page)
    assert len(traces) == 3
    for trace, value in zip(traces, (0.25, -0.5, 1.0), strict=True):
        assert [x for x in trace["x"] if x is not None] == [1, 5]
        assert [y for y in trace["y"] if y is not None] == [value, value]
        assert None in trace["y"]  # Four seconds exceeds the default one-second line gap.
        assert [point[0] for point in trace["customdata"] if point is not None] == [2, 8]

    click_attitude_point(page, 1)
    expect(page.get_by_role("heading", name="Record #8", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#8 · ATTITUDE · 1 / 1"
    )
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "attitude-point.md")
    assert detail["index"] == 8
    assert detail["raw_frame_hex"] == EXPECTED["records"][8]["frame_hex"]
    assert detail["fields"] == EXPECTED["records"][8]["fields"]
    assert coverage["attitude_plot"]["status"] == "ready"
    assert coverage["attitude_plot"]["unit"] == "rad"
    assert coverage["attitude_plot"]["fields"] == ["roll", "pitch", "yaw"]
    assert coverage["attitude_plot"]["max_gap_us"] == 1_000_000

    # A persisted Plotly selection must not override a later manual inspection.
    choose(page, "Record", "#2 · ATTITUDE · 1 / 1")
    expect(page.get_by_role("heading", name="Record #2", exact=True)).to_be_visible()
    gap = page.get_by_role("textbox", name="Maximum line gap (s)", exact=True)
    gap.fill("5")
    gap.press("Enter")
    page.wait_for_function(
        "document.querySelector('.st-key-attitude_plot .js-plotly-plot')"
        "?.data?.[0]?.y.every(value => value !== null)"
    )
    assert attitude_traces(page)[0]["y"] == [0.25, 0.25]
    expect(page.get_by_role("heading", name="Record #2", exact=True)).to_be_visible()
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "connected-attitude.md")
    assert coverage["attitude_plot"]["max_gap_us"] == 5_000_000
    assert detail["index"] == 2

    gap.fill("0")
    gap.press("Enter")
    expect(page.get_by_test_id("stAlert").filter(has_text="gap")).to_be_visible()
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "invalid-gap.md")
    assert coverage["attitude_plot"]["max_gap_us"] == 5_000_000
    assert detail["index"] == 2

    click_attitude_point(page, 1)
    expect(page.get_by_role("heading", name="Record #8", exact=True)).to_be_visible()
    choose(page, "Message type", "HEARTBEAT")
    set_interval(page, 0, 6)
    expect(metric(page, "Selected records")).to_have_text("2")
    expect(attitude_plot(page)).to_have_count(0)
    expect(page.get_by_role("heading", name="Record #0", exact=True)).to_be_visible()
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "heartbeat-selection.md")
    assert coverage["attitude_plot"]["status"] == "empty"
    assert detail["index"] == 0

    upload(page, FIXTURE.read_bytes()[:25], name="replacement.tlog")
    expect(metric(page, "Imported records")).to_have_text("1")
    expect(gap).to_have_value("1")
    expect(attitude_plot(page)).to_have_count(0)
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "replacement.md")
    assert coverage["attitude_plot"]["max_gap_us"] == 1_000_000
    assert detail["index"] == 0


def test_attitude_point_opens_its_original_record_on_another_page(analyze_page, tmp_path):
    page, expect = analyze_page
    # Repeat a documented synthetic frame; only the independent outer clock advances.
    frame = bytes.fromhex(EXPECTED["records"][2]["frame_hex"])
    origin_us = EXPECTED["capture_clock"]["first_timestamp_us"]
    count = 121
    content = b"".join(
        (origin_us + index * 100_000).to_bytes(8, "big") + frame for index in range(count)
    )
    upload(page, content, name="paged-attitude.tlog")
    expect(metric(page, "Imported records")).to_have_text(str(count))
    expect(attitude_plot(page)).to_be_visible()
    expect(page.get_by_role("spinbutton", name="Page", exact=True)).to_have_value("1")
    expect(page.get_by_role("heading", name="Record #0", exact=True)).to_be_visible()

    click_attitude_point(page, 110)
    expect(page.get_by_role("spinbutton", name="Page", exact=True)).to_have_value("2")
    expect(page.get_by_role("heading", name="Record #110", exact=True)).to_be_visible()
    expect(page.get_by_role("combobox", name="Record", exact=True)).to_have_value(
        "#110 · ATTITUDE · 1 / 1"
    )
    expect(
        page.get_by_text("Showing 101–121 of 121 records in original file order.")
    ).to_be_visible()
    provenance, _, coverage, _, detail = download_blocks(page, tmp_path / "point-page-two.md")
    assert provenance["sha256"] == hashlib.sha256(content).hexdigest()
    assert coverage["filtered_record_count"] == count
    assert detail["index"] == 110
    assert detail["timestamp_us"] == origin_us + 110 * 100_000
    assert detail["offset"] == 110 * (8 + len(frame))
    assert detail["frame_offset"] == detail["offset"] + 8
    assert detail["raw_frame_hex"] == frame.hex()
    assert detail["fields"] == EXPECTED["records"][2]["fields"]

    # Applying new bounds cannot reuse the point selected in the previous view.
    set_interval(page, 0, 1)
    expect(metric(page, "Selected records")).to_have_text("11")
    expect(page.get_by_role("spinbutton", name="Page", exact=True)).to_have_value("1")
    expect(page.get_by_role("heading", name="Record #0", exact=True)).to_be_visible()
    _, _, coverage, _, detail = download_blocks(page, tmp_path / "narrowed-attitude.md")
    assert coverage["filtered_record_count"] == 11
    assert detail["index"] == 0
