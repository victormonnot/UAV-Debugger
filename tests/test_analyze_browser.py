"""Real upload, filtering, inspection and download through the Analyze browser UI."""

import hashlib
import json
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
    expect(page.get_by_test_id("stPlotlyChart")).to_be_visible()
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
    expect(page.get_by_test_id("stPlotlyChart")).to_be_visible()
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
