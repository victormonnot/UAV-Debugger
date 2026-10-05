"""Real offline Instrument comparisons over constructed v1/v2 evidence."""

import hashlib
import json
import os
from urllib.parse import urlsplit

import pytest
from test_comparison import finalize, fixture_files
from test_instrument_browser import (
    ROOT,
    download_report,
    hold_next_response,
    load_example,
    release_response,
)
from test_instrument_browser import instrument_catalog_root as instrument_catalog_root
from test_instrument_browser import instrument_page as instrument_page
from test_instrument_browser import instrument_server as instrument_server
from test_instrument_runs_browser import await_run, run_section, snapshot, upload_run, write_run
from test_instrument_runs_browser import catalog_root as catalog_root

from uav_debugger import import_bytes
from uav_debugger.comparison import compare_runs
from uav_debugger.saved_run import load_run_files

pytestmark = pytest.mark.browser
ROLES = ("baseline", "blackout")
ORIGIN_NS = (1 << 53) + 123


def constructed_pair(version=2):
    return {
        role: fixture_files(
            role,
            version=version,
            origin_ns=ORIGIN_NS + index * 10_000_000_000,
            startup_ns=(index + 1) * 1_000_000_000,
        )
        for index, role in enumerate(ROLES)
    }


@pytest.fixture
def pair_files():
    return constructed_pair()


def upload_role(page, expect, role, directory, files):
    page.locator(f"#comparison-{role}-files").set_input_files(str(directory))
    expect(page.locator(f"#comparison-{role}-identity")).to_have_text(
        load_run_files(files).identity
    )


def open_uploaded_pair(page, expect, tmp_path, files):
    page.locator("#comparison-input-tab").click()
    paths = {}
    for role in ROLES:
        paths[role] = write_run(tmp_path / role, files[role])
        upload_role(page, expect, role, paths[role], files[role])
    return paths


def comparison(page, expect, *, available=True):
    expect(page.locator("#comparison-status")).to_have_text(
        "Comparison available" if available else "Comparison unavailable"
    )
    expect(page.locator("#comparison-download-report")).to_be_enabled()
    if available:
        page.wait_for_function(
            "() => document.querySelector('#comparison-plot').data?.length === 2"
        )


def chart(page):
    return page.locator("#comparison-plot").evaluate(
        "plot => ({traces: plot.data.map(({name, x, y, customdata}) => "
        "({name, x, y, customdata})), "
        "shapes: plot.layout.shapes || [], range: plot.layout.xaxis.range})"
    )


def assert_counts(graph, expected, point):
    assert {trace["name"].lower(): sum(trace["y"]) for trace in graph["traces"]} == {
        role: expected.metrics[role][point].count for role in ROLES
    }
    assert graph["traces"][0]["x"] == graph["traces"][1]["x"]
    assert graph["traces"][0]["customdata"] == graph["traces"][1]["customdata"]
    assert 0 < len(graph["traces"][0]["x"]) <= 200


def apply_comparison(page, expect, *, source=None, message=None, start=None, end=None):
    for selector, value in (("#comparison-source", source), ("#comparison-message", message)):
        if value is not None:
            page.locator(selector).select_option(value)
    for selector, value in (("#comparison-start", start), ("#comparison-end", end)):
        if value is not None:
            page.locator(selector).fill(value)
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/comparison"):
        page.locator("#comparison-apply").click()
    expect(page.locator("#comparison-filter-state")).to_have_text("Applied")
    expect(page.locator("#comparison-error")).to_be_hidden()


def comparison_report(page, destination):
    return download_report(page, destination, button="#comparison-download-report")


def multiple_selections(files):
    """Change constructed v1 payloads, retaining every original point/time/reference."""
    from pymavlink.dialects.v20 import common

    files = dict(files)
    observations = [json.loads(line) for line in files["observations.jsonl"].splitlines()]
    by_record = {(item["point"], item["record_index"]): item for item in observations}
    for point in ("relay-input", "receiver"):
        raw = bytearray()
        for record in import_bytes(files[f"{point}.tlog"]).records:
            observation = by_record[point, record.index]
            relative = observation["elapsed_ns"]
            system_id = 1 if relative in (0, 100_000_000, 2_500_000_000) else 2
            encoder = common.MAVLink(None, srcSystem=system_id, srcComponent=1)
            if relative == 100_000_000:
                message = common.MAVLink_heartbeat_message(2, 3, 0, 0, 4, 3)
            else:
                message = common.MAVLink_attitude_message(**record.fields)
            frame = message.pack(encoder)
            observation.update(
                offset=len(raw),
                frame_size_bytes=len(frame),
                frame_sha256=hashlib.sha256(frame).hexdigest(),
            )
            raw.extend(record.timestamp_us.to_bytes(8, "big") + frame)
        files[f"{point}.tlog"] = bytes(raw)
    files["observations.jsonl"] = b"".join(
        (json.dumps(item) + "\n").encode() for item in observations
    )
    files = finalize(files, json.loads(files["run.json"]))
    assert load_run_files(files).evidence_status == "consistent"
    return files


def expected_comparison(files, **kwargs):
    return compare_runs(*(load_run_files(files[role]) for role in ROLES), **kwargs)


def choose_catalog_role(page, expect, key, role):
    button = page.get_by_role("button", name=f"Use {key} as {role}", exact=True)
    expect(button).to_be_enabled()
    button.click()
    expect(page.locator(f"#catalog-{role}-name")).to_contain_text(key)


def open_catalog_pair(page, expect, catalog_root, files):
    for role in ROLES:
        write_run(catalog_root / role, files[role])
    page.locator("#catalog-input-tab").click()
    for role in ROLES:
        choose_catalog_role(page, expect, role, role)
    page.locator("#catalog-compare").click()
    comparison(page, expect)


@pytest.mark.parametrize("version", [1, 2])
def test_uploaded_pair_uses_shared_bins_original_references_and_real_report(
    instrument_page, tmp_path, version
):
    page, expect = instrument_page
    files = constructed_pair(version)
    expected = expected_comparison(files)
    paths = open_uploaded_pair(page, expect, tmp_path, files)
    original = {role: snapshot(path) for role, path in paths.items()}
    comparison(page, expect)
    assert_counts(chart(page), expected, "receiver")
    graph = chart(page)
    assert graph["range"] == [0, 3]
    assert any(shape["x0"] == 0.2 and shape["x1"] == 2.2 for shape in graph["shapes"])
    expect(page.locator("#comparison-window")).to_contain_text("[0.000000000, 3.000000000)")
    expect(page.locator("#comparison-references-rows")).to_contain_text("observations.jsonl")
    expect(page.locator("#comparison-references-rows")).to_contain_text(
        str(expected.metrics["baseline"]["receiver"].first.observation.monotonic_ns)
    )
    expect(page.locator("#comparison-gate-rows")).to_contain_text("actions.jsonl")
    expect(page.locator("#comparison-difference-rows")).to_contain_text("scenario")

    downloads = []
    page.on("download", lambda download: downloads.append(download))
    report, blocks = comparison_report(page, tmp_path / "comparison.md")
    runs = {role: load_run_files(files[role]) for role in ROLES}
    assert downloads[0].suggested_filename == (
        f"uav-debugger-comparison-{runs['baseline'].identity[:8]}-"
        f"{runs['blackout'].identity[:8]}.md"
    )
    provenance = run_section(blocks, "comparison_provenance")["runs"]
    assert run_section(blocks, "comparison_status")["comparable"] is True
    selection = run_section(blocks, "comparison_selection")
    assert selection["window"]["start_ns"] == 0
    assert selection["window"]["end_ns"] == 3_000_000_000
    assert selection["window"]["bounds"] == "half-open [start, end)"
    assert selection["points"] == ["relay-input", "receiver"]
    metrics = run_section(blocks, "comparison_metrics")
    assert metrics["available"] is True
    assert metrics["delta_direction"] == "blackout minus baseline"
    for item in metrics["points"]:
        point = item["point"]
        for role in ROLES:
            observed = expected.metrics[role][point]
            assert item[role]["count"] == observed.count
            assert item[role]["rate_hz"] == observed.rate_hz
            assert item[role]["first"]["record_index"] == observed.first.record.index
            assert item[role]["first"]["monotonic_ns"] == (observed.first.observation.monotonic_ns)
            assert item[role]["last"]["measurement_ns"] == observed.last.measurement_ns
            assert item[role]["capture_sha256"] == runs[role].captures[point].sha256
        assert item["delta"]["count"] == item["blackout"]["count"] - item["baseline"]["count"]
    for role in ROLES:
        assert provenance[role]["bundle_sha256"] == runs[role].identity
        clocks = run_section(blocks, f"{role}_clocks")
        assert clocks["origin_monotonic_ns"] == runs[role].origin_monotonic_ns
        assert clocks["measurement_monotonic_ns"] == runs[role].measurement_monotonic_ns
        assert str(runs[role].origin_monotonic_ns) in report
    assert "packet loss" in report and "monotonic" in report
    assert {role: snapshot(path) for role, path in paths.items()} == original


def test_point_is_only_a_view_and_preserves_unapplied_window_and_export(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    page.locator("#comparison-start").fill("0.500000001")
    page.locator("#comparison-end").fill("2.300000001")
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/comparison"):
        page.locator("#comparison-point").select_option("relay-input")
    assert_counts(chart(page), expected_comparison(pair_files), "relay-input")
    expect(page.locator("#comparison-start")).to_have_value("0.500000001")
    expect(page.locator("#comparison-end")).to_have_value("2.300000001")
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    _, blocks = comparison_report(page, tmp_path / "before-apply.md")
    assert run_section(blocks, "comparison_selection")["window"]["start_ns"] == 0
    assert run_section(blocks, "comparison_selection")["window"]["end_ns"] == 3_000_000_000

    apply_comparison(page, expect)
    expected = expected_comparison(pair_files, start_ns=500_000_001, end_ns=2_300_000_001)
    assert_counts(chart(page), expected, "relay-input")
    expect(page.locator("#comparison-window")).to_contain_text("[0.500000001, 2.300000001)")
    page.locator("#comparison-plot").evaluate(
        "plot => Plotly.relayout(plot, {'xaxis.range': [1, 1.5]})"
    )
    _, blocks = comparison_report(page, tmp_path / "after-zoom.md")
    selection = run_section(blocks, "comparison_selection")["window"]
    assert selection["start_ns"] == 500_000_001 and selection["end_ns"] == 2_300_000_001
    metrics = run_section(blocks, "comparison_metrics")["points"]
    assert {item["point"]: item["blackout"]["count"] for item in metrics} == {
        "relay-input": 2,
        "receiver": 0,
    }
    gate = run_section(blocks, "blackout_applied_actions")["intervals"][0]
    assert gate["start"]["measurement_ns"] == 200_000_000
    assert gate["end"]["measurement_ns"] == 2_200_000_000


def test_source_and_message_drafts_apply_together_and_allow_real_zero_counts(
    instrument_page, tmp_path
):
    page, expect = instrument_page
    files = {role: multiple_selections(value) for role, value in constructed_pair(1).items()}
    open_uploaded_pair(page, expect, tmp_path, files)
    comparison(page, expect)
    page.locator("#comparison-message").select_option("HEARTBEAT")
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    _, blocks = comparison_report(page, tmp_path / "draft-message.md")
    assert run_section(blocks, "comparison_selection")["message_type"] == "ATTITUDE"
    apply_comparison(page, expect)
    assert_counts(chart(page), expected_comparison(files, message_type="HEARTBEAT"), "receiver")
    apply_comparison(page, expect, source="2:1", message="ATTITUDE")
    expected = expected_comparison(files, source=(2, 1), message_type="ATTITUDE")
    assert_counts(chart(page), expected, "receiver")
    _, blocks = comparison_report(page, tmp_path / "source-two.md")
    assert run_section(blocks, "comparison_selection")["source"] == {
        "system_id": 2,
        "component_id": 1,
    }
    receiver = next(
        item
        for item in run_section(blocks, "comparison_metrics")["points"]
        if item["point"] == "receiver"
    )
    assert receiver["blackout"]["count"] == 0
    assert receiver["blackout"]["first"] is None
    assert receiver["blackout"]["longest_interval"] is None


@pytest.mark.parametrize("bad_end", ["0", "not-a-time", "1.0000000001"])
def test_invalid_window_retains_applied_chart_and_report(
    instrument_page, pair_files, tmp_path, bad_end
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    graph = chart(page)
    page.locator("#comparison-end").fill(bad_end)
    page.locator("#comparison-apply").click()
    expect(page.locator("#comparison-error")).to_be_visible()
    expect(page.locator("#comparison-filter-state")).to_have_text("Unapplied changes")
    assert chart(page) == graph
    _, blocks = comparison_report(page, tmp_path / "after-invalid-window.md")
    assert run_section(blocks, "comparison_selection")["window"]["end_ns"] == 3_000_000_000
    assert run_section(blocks, "comparison_status")["comparable"] is True


@pytest.mark.parametrize("kind", ["missing-observations", "unfinalized", "outside-window"])
def test_blocked_comparison_keeps_reasons_and_never_invents_zero_metrics(
    instrument_page, pair_files, tmp_path, kind
):
    page, expect = instrument_page
    if kind == "missing-observations":
        del pair_files["blackout"]["observations.jsonl"]
    elif kind == "unfinalized":
        manifest = json.loads(pair_files["blackout"]["run.json"])
        manifest["outcome"] = "running"
        pair_files["blackout"]["run.json"] = json.dumps(manifest).encode()
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    if kind == "outside-window":
        comparison(page, expect)
        apply_comparison(page, expect, end="4")
    comparison(page, expect, available=False)
    expect(page.locator("#comparison-issue-rows")).not_to_be_empty()
    assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")
    _, blocks = comparison_report(page, tmp_path / f"{kind}.md")
    status = run_section(blocks, "comparison_status")
    assert status["comparable"] is False and status["blocking_reason_count"] > 0
    metrics = run_section(blocks, "comparison_metrics")
    assert metrics["available"] is False
    assert all(item[role] is None for item in metrics["points"] for role in ROLES)


def test_role_replacement_clears_metrics_immediately_and_resets_applied_window(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    apply_comparison(page, expect, start="0.5", end="2.3")
    replacement = dict(pair_files["blackout"])
    manifest = json.loads(replacement["run.json"])
    manifest["environment"]["replacement_label"] = "new identity, same capture bytes"
    replacement["run.json"] = json.dumps(manifest).encode()
    hold_next_response(page, "/api/run")
    page.locator("#comparison-blackout-files").set_input_files(
        str(write_run(tmp_path / "replacement", replacement))
    )
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")
    release_response(page)
    comparison(page, expect)
    expect(page.locator("#comparison-blackout-identity")).to_have_text(
        load_run_files(replacement).identity
    )
    expect(page.locator("#comparison-start")).to_have_value("0.000000000")
    _, blocks = comparison_report(page, tmp_path / "replacement.md")
    assert run_section(blocks, "comparison_selection")["window"]["end_ns"] == 3_000_000_000
    assert run_section(blocks, "comparison_provenance")["runs"]["blackout"]["bundle_sha256"] == (
        load_run_files(replacement).identity
    )


@pytest.mark.parametrize("clear", ["baseline", "blackout", "all"])
def test_clearing_roles_removes_report_without_changing_independent_recording_analysis(
    instrument_page, pair_files, tmp_path, clear
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    page.locator("#comparison-clear" if clear == "all" else f"#comparison-clear-{clear}").click()
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")
    page.locator("#recording-input-tab").click()
    load_example(page, expect)
    expect(page.locator("#comparison-view")).to_be_hidden()
    expect(page.locator("#record-count")).to_have_text("12")
    _, blocks = download_report(page, tmp_path / "independent-recording.md")
    assert not any("comparison_provenance" in block for block in blocks if isinstance(block, dict))


def test_unsupported_role_removes_previous_comparison_but_keeps_other_role(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    bad = write_run(tmp_path / "unsupported", {"run.json": b'{"schema":"unsupported"}'})
    page.locator("#comparison-blackout-files").set_input_files(str(bad))
    expect(page.locator("#comparison-blackout-error")).to_be_visible()
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    expect(page.locator("#comparison-baseline-identity")).to_have_text(
        load_run_files(pair_files["baseline"]).identity
    )
    assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")


@pytest.mark.parametrize("action", ["clear", "replace"])
def test_completed_role_upload_cannot_revive_cleared_or_replaced_evidence(
    instrument_page, pair_files, tmp_path, action
):
    page, expect = instrument_page
    page.locator("#comparison-input-tab").click()
    upload_role(
        page,
        expect,
        "baseline",
        write_run(tmp_path / "baseline", pair_files["baseline"]),
        pair_files["baseline"],
    )
    hold_next_response(page, "/api/run")
    page.locator("#comparison-blackout-files").set_input_files(
        str(write_run(tmp_path / "old-blackout", pair_files["blackout"]))
    )
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if action == "clear":
        page.locator("#comparison-clear-blackout").click()
    else:
        replacement = dict(pair_files["blackout"])
        manifest = json.loads(replacement["run.json"])
        manifest["environment"]["replacement"] = "new role wins"
        replacement["run.json"] = json.dumps(manifest).encode()
        upload_role(
            page,
            expect,
            "blackout",
            write_run(tmp_path / "new-blackout", replacement),
            replacement,
        )
        comparison(page, expect)
    release_response(page)
    if action == "clear":
        expect(page.locator("#comparison-download-report")).to_be_disabled()
        assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")
    else:
        expect(page.locator("#comparison-blackout-identity")).to_have_text(
            load_run_files(replacement).identity
        )
        _, blocks = comparison_report(page, tmp_path / "newest-role.md")
        identity = run_section(blocks, "comparison_provenance")["runs"]["blackout"]["bundle_sha256"]
        assert identity == load_run_files(replacement).identity


def test_service_error_on_point_change_retains_applied_view_and_report(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    before = chart(page)

    def fail_request(route):
        route.fulfill(status=500, content_type="application/json", body='{"error":"Unavailable"}')

    page.route("**/api/comparison?*", fail_request)
    page.locator("#comparison-point").select_option("relay-input")
    expect(page.locator("#comparison-error")).to_be_visible()
    expect(page.locator("#comparison-point")).to_have_value("receiver")
    assert chart(page) == before
    page.unroute("**/api/comparison?*", fail_request)
    _, blocks = comparison_report(page, tmp_path / "after-point-error.md")
    assert run_section(blocks, "comparison_status")["comparable"] is True


def test_initial_comparison_service_error_can_retry_without_reimporting_evidence(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page

    def fail_request(route):
        route.fulfill(status=500, content_type="application/json", body='{"error":"Unavailable"}')

    page.route("**/api/comparison?*", fail_request)
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    expect(page.locator("#comparison-error")).to_be_visible()
    expect(page.locator("#comparison-retry")).to_be_enabled()
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    for role in ROLES:
        expect(page.locator(f"#comparison-{role}-identity")).to_have_text(
            load_run_files(pair_files[role]).identity
        )
    page.unroute("**/api/comparison?*", fail_request)
    page.locator("#comparison-retry").click()
    comparison(page, expect)
    expect(page.locator("#comparison-retry")).to_be_hidden()
    _, blocks = comparison_report(page, tmp_path / "retried-comparison.md")
    assert run_section(blocks, "comparison_status")["comparable"] is True


def test_catalog_pair_is_read_only_and_explicitly_validated_before_comparison(
    instrument_page, pair_files, catalog_root, tmp_path
):
    page, expect = instrument_page
    for role in ROLES:
        write_run(catalog_root / role, pair_files[role])
    original = snapshot(catalog_root)
    requests = []
    page.on("request", lambda request: requests.append(urlsplit(request.url).path))
    page.locator("#catalog-input-tab").click()
    for role in ROLES:
        choose_catalog_role(page, expect, role, role)
    assert "/api/comparison" not in requests
    page.locator("#catalog-compare").click()
    comparison(page, expect)
    expect(page.locator("#comparison-input-tab")).to_have_attribute("aria-selected", "true")
    _, blocks = comparison_report(page, tmp_path / "catalog-pair.md")
    provenance = run_section(blocks, "comparison_provenance")["runs"]
    assert all(provenance[role]["source_name"] == role for role in ROLES)
    assert snapshot(catalog_root) == original
    assert set(requests) <= {"/api/catalog", "/api/catalog/open", "/api/comparison"}


def test_mixed_catalog_and_uploaded_roles_can_be_inspected_and_returned_to_comparison(
    instrument_page, pair_files, catalog_root, tmp_path
):
    page, expect = instrument_page
    directory = write_run(tmp_path / "uploaded-blackout", pair_files["blackout"])
    upload_run(page, directory)
    await_run(page, expect, pair_files["blackout"])
    page.locator("#use-run-blackout").click()
    write_run(catalog_root / "catalog-baseline", pair_files["baseline"])
    page.locator("#catalog-input-tab").click()
    choose_catalog_role(page, expect, "catalog-baseline", "baseline")
    page.locator("#catalog-compare").click()
    comparison(page, expect)
    page.locator("#comparison-inspect-baseline").click()
    await_run(page, expect, pair_files["baseline"])
    expect(page.locator("#saved-input-tab")).to_have_attribute("aria-selected", "true")
    page.locator("#compare-saved-runs").click()
    comparison(page, expect)
    _, blocks = comparison_report(page, tmp_path / "mixed-pair.md")
    provenance = run_section(blocks, "comparison_provenance")["runs"]
    for role in ROLES:
        assert provenance[role]["bundle_sha256"] == load_run_files(pair_files[role]).identity


@pytest.mark.parametrize("change", ["manifest", "missing"])
def test_changed_catalog_bundle_invalidates_pair_and_cannot_export_stale_evidence(
    instrument_page, pair_files, catalog_root, tmp_path, change
):
    page, expect = instrument_page
    open_catalog_pair(page, expect, catalog_root, pair_files)
    path = catalog_root / "blackout" / "run.json"
    if change == "manifest":
        manifest = json.loads(path.read_bytes())
        manifest["environment"]["changed"] = "capture bytes are unchanged"
        path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        path.unlink()
    downloads = []
    page.on("download", lambda item: downloads.append(item))
    page.locator("#comparison-download-report").click()
    error = page.locator("#comparison-error:visible, #comparison-report-error:visible")
    expect(error).to_have_count(1)
    expect(page.locator("#comparison-download-report")).to_be_disabled()
    assert not downloads
    assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")


@pytest.mark.parametrize("action", ["clear", "leave", "new-apply"])
def test_completed_comparison_response_cannot_overwrite_newer_intent(
    instrument_page, pair_files, tmp_path, action
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    hold_next_response(page, "/api/comparison")
    page.locator("#comparison-start").fill("0.5")
    page.locator("#comparison-end").fill("2.3")
    page.locator("#comparison-apply").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if action == "clear":
        page.locator("#comparison-clear").click()
    elif action == "leave":
        page.locator("#recording-input-tab").click()
        load_example(page, expect)
    else:
        apply_comparison(page, expect, start="1", end="2")
    release_response(page)
    if action == "new-apply":
        expect(page.locator("#comparison-window")).to_contain_text("[1.000000000, 2.000000000)")
        _, blocks = comparison_report(page, tmp_path / "newer-window.md")
        assert run_section(blocks, "comparison_selection")["window"]["start_ns"] == 1_000_000_000
    elif action == "clear":
        expect(page.locator("#comparison-download-report")).to_be_disabled()
        assert page.locator("#comparison-plot").evaluate("plot => !plot.data?.length")
    else:
        expect(page.locator("#recording-input-tab")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#record-count")).to_have_text("12")


@pytest.mark.parametrize("action", ["clear", "leave", "new-apply"])
def test_completed_download_is_discarded_after_pair_or_applied_selection_changes(
    instrument_page, pair_files, tmp_path, action
):
    page, expect = instrument_page
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    downloads = []
    page.on("download", lambda item: downloads.append(item))
    hold_next_response(page, "/api/comparison")
    page.locator("#comparison-download-report").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if action == "clear":
        page.locator("#comparison-clear").click()
    elif action == "leave":
        page.locator("#recording-input-tab").click()
        load_example(page, expect)
    else:
        apply_comparison(page, expect, start="1", end="2")
    release_response(page)
    assert not downloads
    if action == "new-apply":
        _, blocks = comparison_report(page, tmp_path / "fresh-report.md")
        assert run_section(blocks, "comparison_selection")["window"]["start_ns"] == 1_000_000_000


def test_run_metadata_is_literal_and_never_executes_uploaded_markup(
    instrument_page, pair_files, tmp_path
):
    page, expect = instrument_page
    literal = '<img src=x onerror="window.uploadedMarkupExecuted=true"> ```json'
    manifest = json.loads(pair_files["baseline"]["run.json"])
    manifest["environment"]["literal_label"] = literal
    pair_files["baseline"]["run.json"] = json.dumps(manifest).encode()
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    expect(page.locator("#comparison-difference-rows")).to_contain_text("<img src=x onerror=")
    assert page.locator("#comparison-view img").count() == 0
    assert page.evaluate("window.uploadedMarkupExecuted === undefined")
    _, blocks = comparison_report(page, tmp_path / "literal.md")
    difference = next(
        item
        for item in run_section(blocks, "comparison_differences")["items"]
        if item["field"] == "environment.literal_label"
    )
    assert difference["baseline"] == literal
    assert run_section(blocks, "comparison_status")["comparable"] is True


@pytest.mark.parametrize("width", [1440, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_comparison_is_readable_and_nonblank_in_both_themes_and_viewports(
    instrument_page, pair_files, tmp_path, width, theme
):
    page, expect = instrument_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#theme-select").select_option(theme)
    open_uploaded_pair(page, expect, tmp_path, pair_files)
    comparison(page, expect)
    assert_counts(chart(page), expected_comparison(pair_files), "receiver")
    page.locator("#comparison-baseline-details summary").click()
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
            const results = ['#comparison-status', '#comparison-window', '#comparison-source',
                '#comparison-baseline-identity', '#comparison-blackout-evidence'].map(selector => {
                    const element = document.querySelector(selector);
                    return {
                        selector, ratio: contrast(element, 'color', background(element)), min: 4.5,
                    };
                });
            const graph = document.querySelector('#comparison-plot');
            const plotBackground = graph.querySelector('.bglayer .bg');
            const backdrop = plotBackground
                ? over(rgba(getComputedStyle(plotBackground).fill), background(graph))
                : background(graph);
            for (const tick of graph.querySelectorAll('.xtick text, .ytick text, .legendtext')) {
                results.push({
                    selector: 'plot text', ratio: contrast(tick, 'fill', backdrop), min: 4.5,
                });
            }
            for (const marker of graph.querySelectorAll('.scatterlayer .point')) {
                results.push({
                    selector: 'plot marker', ratio: contrast(marker, 'fill', backdrop), min: 3,
                });
            }
            return results;
        }"""
    )
    assert any(item["selector"] == "plot marker" for item in contrast)
    assert all(item["ratio"] >= item["min"] for item in contrast), contrast
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        destination = ROOT / "local" / "instrument-brick5" / "browser"
        destination.mkdir(parents=True, exist_ok=True)
        page.evaluate("() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }")
        page.screenshot(
            path=destination / f"comparison-evidence-{theme}-{width}.png", full_page=True
        )
        page.locator("#comparison-baseline-details summary").click()
        page.screenshot(path=destination / f"comparison-{theme}-{width}.png", full_page=True)
