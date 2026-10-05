"""Read-only Instrument workflows over constructed saved evidence and a local catalog."""

import hashlib
import json
import os
import shutil
from urllib.parse import urlsplit

import pytest
from test_instrument_browser import (
    FIXTURE,
    ROOT,
    apply_filters,
    download_report,
    hold_next_response,
    inspect_record,
    load_example,
    release_response,
)
from test_instrument_browser import (
    instrument_catalog_root as instrument_catalog_root,
)
from test_instrument_browser import (
    instrument_page as instrument_page,
)
from test_instrument_browser import (
    instrument_server as instrument_server,
)
from test_saved_run import finalized, json_bytes, lines_bytes
from test_saved_run import sitl_files as sitl_files

from uav_debugger.saved_run import load_run_files

pytestmark = pytest.mark.browser
ORIGIN_NS = (1 << 53) + 123


@pytest.fixture
def run_files(sitl_files):
    """Reuse constructed v2 evidence, moving the monotonic clock beyond JS integers."""
    files = dict(sitl_files)
    manifest = json.loads(files["run.json"])
    shift = ORIGIN_NS - manifest["origin_monotonic_ns"]
    manifest["origin_monotonic_ns"] += shift
    for name in ("start", "end", "measurement_start"):
        manifest[name]["monotonic_ns"] += shift
    files["run.json"] = json_bytes(manifest)
    for name in ("actions.jsonl", "observations.jsonl", "datagrams.jsonl"):
        events = [json.loads(line) for line in files[name].splitlines()]
        for event in events:
            event["monotonic_ns"] += shift
        files[name] = lines_bytes(events)
    files = finalized(files)
    run = load_run_files(files)
    assert run.evidence_status == "consistent" and run.issues == ()
    assert run.origin_monotonic_ns == ORIGIN_NS
    assert run.measurement_monotonic_ns == ORIGIN_NS + 300
    return files


@pytest.fixture
def catalog_root(instrument_catalog_root):
    assert not instrument_catalog_root.exists()
    yield instrument_catalog_root
    if instrument_catalog_root.exists():
        shutil.rmtree(instrument_catalog_root)


def write_run(directory, files):
    for name, data in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return directory


def snapshot(directory):
    if not directory.exists():
        return None
    return {
        path.relative_to(directory).as_posix(): (
            None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in directory.rglob("*")
    }


def upload_run(page, directory):
    page.locator("#saved-input-tab").click()
    page.locator("#run-files").set_input_files(str(directory))


def await_run(page, expect, files, *, point=None):
    run = load_run_files(files)
    point = point or ("receiver" if "receiver" in run.captures else "relay-input")
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#run-outcome")).to_have_text(run.declared_outcome)
    expect(page.locator("#run-evidence-status")).to_have_text(run.evidence_status)
    expect(page.locator("#run-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#observation-point")).to_have_value(point)
    expect(page.locator("#capture-name")).to_have_text(f"{point}.tlog")
    expect(page.locator("#record-count")).to_have_text(str(len(run.captures[point].records)))
    expect(page.locator("#download-report")).to_be_enabled()
    return run


def run_section(blocks, name):
    return next(block[name] for block in blocks if isinstance(block, dict) and name in block)


def capture_sections(blocks):
    provenance = next(block for block in blocks if isinstance(block, dict) and "sha256" in block)
    selection = next(
        block for block in blocks if isinstance(block, dict) and "relative_origin_us" in block
    )
    coverage = next(
        block for block in blocks if isinstance(block, dict) and "filtered_record_count" in block
    )
    details = [block for block in blocks if isinstance(block, dict) and "raw_frame_hex" in block]
    return provenance, selection, coverage, details


def timeline(page):
    page.wait_for_function("() => document.querySelector('#run-timeline').data?.length > 0")
    return page.locator("#run-timeline").evaluate(
        "plot => ({traces: plot.data.map(({name, x, y}) => ({name, x, y})), "
        "shapes: plot.layout.shapes, axis: plot.layout.xaxis.title.text})"
    )


def open_catalog(page, expect):
    page.locator("#catalog-input-tab").click()
    expect(page.locator("#catalog-view")).to_be_visible()
    expect(page.locator("#download-report")).to_be_disabled()


def open_entry(page, key):
    page.get_by_role("button", name=f"Open {key} in Analyze", exact=True).click()


def test_saved_upload_keeps_requested_applied_observed_and_original_capture_evidence(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    directory = write_run(tmp_path / "saved_literal_run", run_files)
    extra = directory / "simulator" / "working-cache.bin"
    extra.write_bytes(b"Excluded simulator cache, not evidence.\x00\xff")
    original = snapshot(directory)
    upload_run(page, directory)
    run = await_run(page, expect, run_files)
    page.locator("#run-provenance-details summary").click()
    expect(page.locator("#run-schema")).to_have_text(run.schema)
    expect(page.locator("#run-ignored-files")).to_contain_text("simulator/working-cache.bin")
    declared = json.loads(page.locator("#run-provenance").inner_text())
    assert declared["origin_monotonic_ns"] == ORIGIN_NS
    assert declared["measurement_start"]["monotonic_ns"] == ORIGIN_NS + 300
    assert json.loads(page.locator("#run-requested").inner_text()) == dict(run.requested)
    expect(page.locator('#run-capture-rows tr[data-point="relay-input"]')).to_contain_text("5")
    expect(page.locator('#run-capture-rows tr[data-point="receiver"]')).to_contain_text("4")
    expect(page.locator("#run-gate-rows")).to_contain_text("actions.jsonl")
    graph = timeline(page)
    assert "run elapsed" in graph["axis"].lower()
    expect(page.locator("#run-view .run-clock")).to_contain_text("monotonic")
    assert {trace["name"]: sum(trace["y"]) for trace in graph["traces"]} == {
        "Relay input": 5,
        "Receiver": 4,
    }
    assert all(len(trace["y"]) <= 200 for trace in graph["traces"])
    gate = run.gate_intervals[0]
    assert any(
        shape["type"] == "rect"
        and shape["x0"] == gate.start.elapsed_ns / 1e9
        and shape["x1"] == gate.end.elapsed_ns / 1e9
        for shape in graph["shapes"]
    )
    assert any(shape["type"] == "rect" and shape["x1"] == 300 / 1e9 for shape in graph["shapes"])
    page.locator("#run-trace-details summary").click()
    expect(page.locator("#run-trace-rows")).to_contain_text(
        f"actions.jsonl:{gate.start.line_number}"
    )
    page.locator("#run-trace-kind").select_option("observations")
    expect(page.locator("#run-trace-rows")).to_contain_text("observations.jsonl:1")
    inspect_record(page, expect, 1)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    report, blocks = download_report(page, tmp_path / "saved-receiver.md")
    assert len(downloads) == 1
    assert downloads[0].suggested_filename == f"uav-debugger-run-{run.identity[:12]}.md"
    provenance = run_section(blocks, "run_provenance")
    assert provenance["bundle_sha256"] == run.identity
    assert provenance["selected_point"] == "receiver"
    assert "simulator/working-cache.bin" not in provenance["files"]
    assert run_section(blocks, "requested") == dict(run.requested)
    assert run_section(blocks, "applied_actions")["intervals"][0]["start"]["monotonic_ns"] == (
        gate.start.monotonic_ns
    )
    assert run_section(blocks, "clocks")["origin_monotonic_ns"] == ORIGIN_NS
    assert run_section(blocks, "clocks")["measurement_monotonic_ns"] == ORIGIN_NS + 300
    capture, _, coverage, details = capture_sections(blocks)
    assert capture["source_name"] == "receiver.tlog"
    assert capture["sha256"] == run.captures["receiver"].sha256
    assert coverage["filtered_record_count"] == 4
    assert details[0]["index"] == 1
    assert details[0]["raw_frame_hex"] == run.captures["receiver"].records[1].raw_frame.hex()
    assert "packet loss" in report and "monotonic" in report
    assert snapshot(directory) == original


def test_point_switch_resets_capture_selection_without_filtering_run_timeline(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    upload_run(page, write_run(tmp_path / "run", run_files))
    run = await_run(page, expect, run_files)
    before = timeline(page)
    page.locator("#messages-tab").click()
    apply_filters(page, expect, source="1:1", message="30", start="0", end="0", count=1)
    inspect_record(page, expect, 1)
    page.locator("#attitude-tab").click()
    page.locator("#line-gap").fill("5")
    page.locator("#apply-gap").click()
    expect(page.locator("#applied-gap")).to_have_text("Applied: 5 s")
    inspect_record(page, expect, 1)
    page.locator("#run-tab").click()
    assert timeline(page) == before
    _, blocks = download_report(page, tmp_path / "filtered-receiver.md")
    assert capture_sections(blocks)[2]["filtered_record_count"] == 1
    assert capture_sections(blocks)[3][0]["index"] == 1
    assert run_section(blocks, "observed_evidence")["points"][0]["capture"]["record_count"] == 5

    page.locator("#observation-point").select_option("relay-input")
    expect(page.locator("#capture-name")).to_have_text("relay-input.tlog")
    expect(page.locator("#record-count")).to_have_text("5")
    expect(page.locator("#selected-count")).to_have_text("5")
    expect(page.locator("#source-filter")).to_have_value("")
    expect(page.locator("#message-filter")).to_have_value("")
    expect(page.locator("#line-gap")).to_have_value("1")
    expect(page.locator("#inspector-content")).to_be_hidden()
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    page.locator("#run-tab").click()
    assert timeline(page) == before
    _, blocks = download_report(page, tmp_path / "new-point.md")
    assert run_section(blocks, "run_provenance")["selected_point"] == "relay-input"
    assert capture_sections(blocks)[2]["filtered_record_count"] == 5
    assert capture_sections(blocks)[2]["explicit_detail_record_count"] == 0


def test_point_change_removes_previous_capture_while_retaining_run_context(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    upload_run(page, write_run(tmp_path / "run", run_files))
    run = await_run(page, expect, run_files)
    inspect_record(page, expect, 1)
    hold_next_response(page, "/api/run")
    page.locator("#observation-point").select_option("relay-input")
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    expect(page.locator("#record-count")).to_have_text("N/A")
    expect(page.locator("#inspector-content")).to_be_hidden()
    expect(page.locator("#download-report")).to_be_disabled()
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    release_response(page)
    expect(page.locator("#record-count")).to_have_text("5")
    expect(page.locator("#capture-name")).to_have_text("relay-input.tlog")
    expect(page.locator("#download-report")).to_be_enabled()


@pytest.mark.parametrize("state", ["incomplete", "invalid", "unfinalized", "open-gate"])
def test_saved_evidence_status_and_unavailable_observations_remain_explicit(
    instrument_page, run_files, tmp_path, state
):
    page, expect = instrument_page
    files = dict(run_files)
    manifest = json.loads(files["run.json"])
    if state == "incomplete":
        del files["receiver.tlog"]
    elif state == "invalid":
        manifest["artifacts"]["observations.jsonl"]["sha256"] = "0" * 64
    elif state == "unfinalized":
        manifest.update(outcome="running", end=None, artifacts={})
    else:
        actions = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
        files["actions.jsonl"] = lines_bytes(
            [event for event in actions if event["action"] != "forwarding_enabled"]
        )
    files["run.json"] = json_bytes(manifest)
    if state == "open-gate":
        files = finalized(files)
    upload_run(page, write_run(tmp_path / state, files))
    run = await_run(page, expect, files)
    assert run.evidence_status != "consistent"
    page.locator("#run-issues-details summary").click()
    assert page.locator("#run-issue-rows tr").count() > 0
    if state == "incomplete":
        expect(page.locator('#observation-point option[value="receiver"]')).to_be_disabled()
        expect(page.locator('#run-capture-rows tr[data-point="receiver"]')).to_contain_text(
            "Unavailable"
        )
    elif state == "invalid":
        graph = page.locator("#run-timeline").evaluate("plot => plot.data || []")
        assert sum(sum(trace.get("y", [])) for trace in graph) == 0
    elif state == "unfinalized":
        expect(page.locator("#run-outcome")).to_have_text("running")
        expect(page.locator("#run-evidence-status")).to_have_text("incomplete")
    else:
        gate = run.gate_intervals[0]
        assert gate.end is None
        shapes = timeline(page)["shapes"]
        assert any(
            shape["type"] == "line" and shape["x0"] == shape["x1"] == gate.start.elapsed_ns / 1e9
            for shape in shapes
        )
        assert not any(shape["type"] == "rect" and shape["x0"] > 0 for shape in shapes)
    _, blocks = download_report(page, tmp_path / f"{state}.md")
    assert run_section(blocks, "status")["declared_outcome"] == run.declared_outcome
    assert run_section(blocks, "status")["evidence_status"] == run.evidence_status
    if state == "open-gate":
        interval = run_section(blocks, "applied_actions")["intervals"][0]
        assert interval["end"] is None and interval["duration_ns"] is None


def test_missing_both_captures_still_exports_run_evidence(instrument_page, run_files, tmp_path):
    page, expect = instrument_page
    files = {name: raw for name, raw in run_files.items() if not name.endswith(".tlog")}
    run = load_run_files(files)
    upload_run(page, write_run(tmp_path / "no-captures", files))
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#download-report")).to_be_enabled()
    for name in ("record", "decoded", "opaque", "source", "selected"):
        expect(page.locator(f"#{name}-count")).to_have_text("N/A")
    expect(page.locator("#filter-fields")).to_have_js_property("disabled", True)
    expect(page.locator("#filter-state")).to_have_text("No capture")
    expect(page.locator("#messages-empty")).to_have_text("No available capture")
    _, blocks = download_report(page, tmp_path / "run-only.md")
    assert run_section(blocks, "run_provenance")["selected_point"] is None
    assert not any(isinstance(block, dict) and "relative_origin_us" in block for block in blocks)


def test_present_empty_captures_keep_zero_counts_instead_of_unavailable(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    files = finalized({**run_files, "relay-input.tlog": b"", "receiver.tlog": b""})
    upload_run(page, write_run(tmp_path / "empty-captures", files))
    await_run(page, expect, files)
    for name in ("record", "decoded", "opaque", "source", "selected"):
        expect(page.locator(f"#{name}-count")).to_have_text("0")
    expect(page.locator("#filter-state")).to_have_text("No records")
    expect(page.locator("#capture-name")).to_have_text("receiver.tlog")
    _, blocks = download_report(page, tmp_path / "empty-capture.md")
    assert run_section(blocks, "run_provenance")["selected_point"] == "receiver"
    assert capture_sections(blocks)[2]["imported_record_count"] == 0


def test_failed_trace_change_restores_applied_controls_rows_and_report(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    upload_run(page, write_run(tmp_path / "run", run_files))
    run = await_run(page, expect, run_files)
    inspect_record(page, expect, 1)
    page.locator("#run-tab").click()
    page.locator("#run-trace-details summary").click()
    expect(page.locator("#run-trace-kind")).to_have_value("actions")
    rows = page.locator("#run-trace-rows").inner_text()

    def fail_trace(route):
        route.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps({"error": "Trace temporarily unavailable."}),
        )

    page.route("**/api/run?*", fail_trace)
    page.locator("#run-trace-kind").select_option("observations")
    expect(page.locator("#run-error")).to_contain_text("Trace temporarily unavailable.")
    expect(page.locator("#run-trace-kind")).to_have_value("actions")
    expect(page.locator("#run-trace-page")).to_have_value("1")
    expect(page.locator("#run-trace-rows")).to_have_text(rows, use_inner_text=True)
    expect(page.locator("#run-identity")).to_have_text(run.identity)
    expect(page.locator("#inspector-title")).to_have_text("Record #1")
    page.unroute("**/api/run?*", fail_trace)
    _, blocks = download_report(page, tmp_path / "retained-after-trace-error.md")
    assert run_section(blocks, "run_provenance")["bundle_sha256"] == run.identity
    assert capture_sections(blocks)[3][0]["index"] == 1
    page.locator("#run-trace-kind").select_option("observations")
    expect(page.locator("#run-trace-rows")).to_contain_text("observations.jsonl:1")
    expect(page.locator("#run-trace-kind")).to_have_value("observations")
    expect(page.locator("#run-error")).to_be_hidden()


def test_run_trace_and_issue_pages_keep_totals_and_report_omissions(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    files = dict(run_files)
    actions = [json.loads(line) for line in files["actions.jsonl"].splitlines()]
    for index in range(205):
        elapsed = 2_500_000_000 + index
        actions.append(
            {
                "action": "unsupported_<img>_action",
                "monotonic_ns": ORIGIN_NS + elapsed,
                "elapsed_ns": elapsed,
                "unix_us": 1_700_000_000_000_000 + elapsed // 1000,
            }
        )
    files["actions.jsonl"] = lines_bytes(actions)
    files = finalized(files)
    upload_run(page, write_run(tmp_path / "paged-traces", files))
    run = await_run(page, expect, files)
    page.locator("#run-trace-details summary").click()
    expect(page.locator("#run-trace-rows tr")).to_have_count(100)
    expect(page.locator("#run-trace-previous")).to_be_disabled()
    page.locator("#run-trace-next").click()
    expect(page.locator("#run-trace-page")).to_have_value("2")
    expect(page.locator("#run-trace-rows")).to_contain_text("actions.jsonl:101")
    page.locator("#run-trace-next").click()
    expect(page.locator("#run-trace-rows tr")).to_have_count(len(run.actions) - 200)
    expect(page.locator("#run-trace-next")).to_be_disabled()
    assert page.locator("#run-trace-rows img").count() == 0
    page.locator("#run-trace-kind").select_option("observations")
    expect(page.locator("#run-trace-page")).to_have_value("1")
    expect(page.locator("#run-trace-rows tr")).to_have_count(len(run.observations))
    page.locator("#run-issues-details summary").click()
    expect(page.locator("#run-issue-rows tr")).to_have_count(100)
    page.locator("#run-issues-next").click()
    expect(page.locator("#run-issues-page")).to_have_value("2")
    expect(page.locator("#run-issue-rows tr")).to_have_count(100)
    page.locator("#run-issues-next").click()
    expect(page.locator("#run-issue-rows tr")).to_have_count(len(run.issues) - 200)
    expect(page.locator("#run-issues-next")).to_be_disabled()
    _, blocks = download_report(page, tmp_path / "paged-report.md")
    assert run_section(blocks, "action_trace")["entry_count"] == len(run.actions)
    assert run_section(blocks, "action_trace")["included_reference_count"] == 100
    assert run_section(blocks, "issues")["total_count"] == len(run.issues)
    assert run_section(blocks, "issues")["included_count"] == 100
    assert run_section(blocks, "issues")["omitted_count"] == len(run.issues) - 100


def test_invalid_directory_replacement_and_clear_remove_stale_run_exports(
    instrument_page, run_files, tmp_path
):
    page, expect = instrument_page
    upload_run(page, write_run(tmp_path / "good", run_files))
    await_run(page, expect, run_files)
    malformed = write_run(tmp_path / "malformed", {"run.json": b"{broken JSON"})
    page.locator("#run-files").set_input_files(str(malformed))
    expect(page.locator("#error")).to_be_visible()
    expect(page.locator("#download-report")).to_be_disabled()
    expect(page.locator("#record-count")).to_have_text("0")
    page.locator("#clear-recording").click()
    expect(page.locator("#download-report")).to_be_disabled()
    page.locator("#recording-input-tab").click()
    load_example(page, expect)
    _, blocks = download_report(page, tmp_path / "recording-after-run.md")
    assert len(blocks) == 4
    assert blocks[0]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert not any(isinstance(block, dict) and "run_provenance" in block for block in blocks)


def test_upload_file_count_bound_includes_excluded_files_before_sending(instrument_page, tmp_path):
    page, expect = instrument_page
    directory = write_run(tmp_path / "too-many", {"run.json": b"{}"})
    for index in range(64):
        (directory / f"extra-{index}.bin").write_bytes(b"not evidence")
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request.url) if urlsplit(request.url).path == "/api/run" else None
        ),
    )
    upload_run(page, directory)
    expect(page.locator("#error")).to_be_visible()
    expect(page.locator("#error-message")).to_contain_text("64")
    expect(page.locator("#download-report")).to_be_disabled()
    assert requests == []


def test_catalog_missing_root_refresh_literal_keys_and_validated_handoff(
    instrument_page, catalog_root, run_files, tmp_path
):
    page, expect = instrument_page
    open_catalog(page, expect)
    expect(page.locator("#catalog-root")).to_have_text(str(catalog_root))
    expect(page.locator("#catalog-rows tr")).to_have_count(0)
    expect(page.locator("#catalog-issues")).not_to_be_empty()
    assert not catalog_root.exists()
    key = "run_literal_<img>_key"
    write_run(catalog_root / key / "evidence", run_files)
    (catalog_root / key / "control.json").write_text('{"state":"running"}')
    write_run(catalog_root / "cli", run_files)
    original = snapshot(catalog_root)
    page.locator("#catalog-refresh").click()
    expect(page.locator("#catalog-rows tr")).to_have_count(2)
    expect(
        page.get_by_role("button", name=f"Open {key}/evidence in Analyze", exact=True)
    ).to_be_enabled()
    assert page.locator("#catalog-rows img").count() == 0
    open_entry(page, f"{key}/evidence")
    run = await_run(page, expect, run_files)
    expect(page.locator("#saved-input-tab")).to_have_attribute("aria-selected", "true")
    _, blocks = download_report(page, tmp_path / "catalog.md")
    assert run_section(blocks, "run_provenance")["bundle_sha256"] == run.identity
    assert run_section(blocks, "run_provenance")["source_name"] == f"{key}/evidence"
    assert "control.json" not in run_section(blocks, "run_provenance")["files"]
    assert snapshot(catalog_root) == original


def test_catalog_blocks_unfinalized_and_malformed_but_opens_terminal_partial_evidence(
    instrument_page, catalog_root, run_files, tmp_path
):
    page, expect = instrument_page
    partial = {name: data for name, data in run_files.items() if name != "receiver.tlog"}
    write_run(catalog_root / "partial", partial)
    unfinished = dict(run_files)
    manifest = json.loads(unfinished["run.json"])
    manifest.update(outcome="running", end=None, artifacts={})
    unfinished["run.json"] = json_bytes(manifest)
    write_run(catalog_root / "unfinished", unfinished)
    write_run(catalog_root / "malformed", {"run.json": b"{bad"})
    original = snapshot(catalog_root)
    open_catalog(page, expect)
    expect(page.locator("#catalog-rows tr")).to_have_count(3)
    expect(
        page.get_by_role("button", name="Open unfinished in Analyze", exact=True)
    ).to_be_disabled()
    expect(
        page.get_by_role("button", name="Open malformed in Analyze", exact=True)
    ).to_be_disabled()
    expect(page.locator('#catalog-rows tr[data-key="unfinished"]')).to_contain_text("Unfinalized")
    open_entry(page, "partial")
    await_run(page, expect, partial)
    _, blocks = download_report(page, tmp_path / "catalog-partial.md")
    assert any(
        issue["code"] == "missing_file" and issue["file_name"] == "receiver.tlog"
        for issue in run_section(blocks, "issues")["items"]
    )
    assert snapshot(catalog_root) == original


def test_catalog_pages_bounded_snapshot_without_claiming_full_scan(
    instrument_page, catalog_root, run_files
):
    page, expect = instrument_page
    for index in range(205):
        write_run(catalog_root / f"entry-{index:03}", {"run.json": run_files["run.json"]})
    original = snapshot(catalog_root)
    open_catalog(page, expect)
    expect(page.locator("#catalog-partial")).to_be_visible()
    expect(page.locator("#catalog-rows tr")).to_have_count(50)
    expect(page.locator("#catalog-previous")).to_be_disabled()
    first = page.locator("#catalog-rows tr").first.get_attribute("data-key")
    page.locator("#catalog-next").click()
    expect(page.locator("#catalog-previous")).to_be_enabled()
    assert page.locator("#catalog-rows tr").first.get_attribute("data-key") != first
    page.locator("#catalog-next").click()
    page.locator("#catalog-next").click()
    expect(page.locator("#catalog-next")).to_be_disabled()
    expect(page.locator("#catalog-rows tr")).to_have_count(50)
    expect(page.locator("#download-report")).to_be_disabled()
    assert snapshot(catalog_root) == original


def test_catalog_changes_between_listing_opening_and_report_cannot_export_old_evidence(
    instrument_page, catalog_root, run_files
):
    page, expect = instrument_page
    directory = write_run(catalog_root / "changing", run_files)
    open_catalog(page, expect)
    expect(page.get_by_role("button", name="Open changing in Analyze", exact=True)).to_be_enabled()
    (directory / "run.json").write_bytes(b"{invalid replacement")
    open_entry(page, "changing")
    expect(page.locator("#catalog-error")).to_be_visible()
    expect(page.locator("#download-report")).to_be_disabled()
    expect(page.locator("#record-count")).to_have_text("0")
    write_run(directory, run_files)
    page.locator("#catalog-refresh").click()
    open_entry(page, "changing")
    await_run(page, expect, run_files)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    changed_files = dict(run_files)
    changed_files["simulator.log"] += b"Changed after validated opening.\n"
    changed_files = finalized(changed_files)
    write_run(directory, changed_files)
    original = snapshot(catalog_root)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/catalog/open"):
        page.locator("#download-report").click()
    expect(page.locator("#download-report")).to_be_disabled()
    expect(page.locator("#record-count")).to_have_text("0")
    assert downloads == []
    assert snapshot(catalog_root) == original


@pytest.mark.parametrize("pending_kind", ["inspector", "report"])
@pytest.mark.parametrize("next_action", ["point", "clear", "recording"])
def test_completed_run_responses_cannot_restore_obsolete_point_or_input(
    instrument_page, run_files, tmp_path, pending_kind, next_action
):
    page, expect = instrument_page
    upload_run(page, write_run(tmp_path / "run", run_files))
    await_run(page, expect, run_files)
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    hold_next_response(page, "/api/run")
    if pending_kind == "inspector":
        page.locator("#messages-tab").click()
        page.locator('#message-rows button[data-record-index="1"]').click()
    else:
        page.locator("#download-report").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if next_action == "point":
        page.locator("#observation-point").select_option("relay-input")
        expect(page.locator("#record-count")).to_have_text("5")
    elif next_action == "clear":
        page.locator("#clear-recording").click()
    else:
        page.locator("#recording-input-tab").click()
        load_example(page, expect)
    release_response(page)
    expect(page.locator("#inspector-content")).to_be_hidden()
    assert downloads == []
    if next_action == "point":
        expect(page.locator("#capture-name")).to_have_text("relay-input.tlog")
        expect(page.locator("#record-count")).to_have_text("5")
    elif next_action == "clear":
        expect(page.locator("#record-count")).to_have_text("0")
        expect(page.locator("#download-report")).to_be_disabled()
    else:
        expect(page.locator("#record-count")).to_have_text("12")
        expect(page.locator("#recording-name")).to_contain_text("telemetry-gap.tlog")


def test_completed_catalog_open_cannot_override_a_new_recording(
    instrument_page, catalog_root, run_files
):
    page, expect = instrument_page
    write_run(catalog_root / "saved", run_files)
    open_catalog(page, expect)
    expect(page.get_by_role("button", name="Open saved in Analyze", exact=True)).to_be_enabled()
    hold_next_response(page, "/api/catalog/open")
    open_entry(page, "saved")
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    page.locator("#recording-input-tab").click()
    load_example(page, expect)
    release_response(page)
    expect(page.locator("#recording-input-tab")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#recording-name")).to_contain_text("telemetry-gap.tlog")


def test_completed_run_upload_cannot_revive_a_cleared_input(instrument_page, run_files, tmp_path):
    page, expect = instrument_page
    hold_next_response(page, "/api/run")
    upload_run(page, write_run(tmp_path / "run", run_files))
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    page.locator("#clear-recording").click()
    release_response(page)
    expect(page.locator("#record-count")).to_have_text("0")
    expect(page.locator("#download-report")).to_be_disabled()


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_run_timeline_and_evidence_remain_readable_at_desktop_and_mobile_sizes(
    instrument_page, run_files, tmp_path, width, theme
):
    page, expect = instrument_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#theme-select").select_option(theme)
    upload_run(page, write_run(tmp_path / "saved_run_long_literal_identity", run_files))
    await_run(page, expect, run_files)
    timeline(page)
    page.locator("#run-provenance-details summary").click()
    page.locator("#run-trace-details summary").click()
    expect(page.locator("#run-trace-rows")).to_contain_text("actions.jsonl:1")
    assert json.loads(page.locator("#run-provenance").inner_text())["origin_monotonic_ns"] == (
        ORIGIN_NS
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    contrast = page.evaluate(
        """() => {
            const rgba = value => value.match(/[\\d.]+/g).map(Number);
            const over = (front, back) => front.slice(0, 3).map((value, index) =>
                value * (front[3] ?? 1) + back[index] * (1 - (front[3] ?? 1)));
            const background = element => {
                const parents = [];
                for (let current = element; current; current = current.parentElement) {
                    parents.unshift(current);
                }
                return parents.reduce((color, parent) =>
                    over(rgba(getComputedStyle(parent).backgroundColor), color), [255, 255, 255]);
            };
            const luminance = values => values.map(value => {
                const channel = value / 255;
                return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
            }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
            const contrast = (element, property, backdrop) => {
                const style = getComputedStyle(element), foreground = rgba(style[property]);
                let alpha = foreground[3] ?? 1;
                if (property === 'fill') alpha *= Number(style.fillOpacity);
                for (let current = element; current; current = current.parentElement) {
                    alpha *= Number(getComputedStyle(current).opacity);
                }
                foreground[3] = alpha;
                const values = [luminance(over(foreground, backdrop)), luminance(backdrop)]
                    .sort((a, b) => a - b);
                return (values[1] + .05) / (values[0] + .05);
            };
            const results = ['#run-outcome', '#run-evidence-status', '#run-requested',
                '#run-provenance', '#run-identity', '#observation-point'].map(selector => {
                    const element = document.querySelector(selector);
                    return {
                        selector, ratio: contrast(element, 'color', background(element)), min: 4.5,
                    };
                });
            const graph = document.querySelector('#run-timeline');
            const plotBackground = graph.querySelector('.bglayer .bg');
            const backdrop = plotBackground
                ? over(rgba(getComputedStyle(plotBackground).fill), background(graph))
                : background(graph);
            for (const tick of graph.querySelectorAll('.xtick text, .ytick text')) {
                results.push({
                    selector: 'timeline tick', ratio: contrast(tick, 'fill', backdrop), min: 4.5,
                });
            }
            for (const bar of graph.querySelectorAll('.barlayer .point path')) {
                results.push({
                    selector: 'timeline bar', ratio: contrast(bar, 'fill', backdrop), min: 3,
                });
            }
            return results;
        }"""
    )
    assert any(item["selector"] == "timeline bar" for item in contrast)
    assert all(item["ratio"] >= item["min"] for item in contrast), contrast
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        destination = ROOT / "local" / "instrument-brick4" / "browser"
        destination.mkdir(parents=True, exist_ok=True)
        page.evaluate("() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }")
        page.screenshot(path=destination / f"saved-run-{theme}-{width}.png", full_page=True)
