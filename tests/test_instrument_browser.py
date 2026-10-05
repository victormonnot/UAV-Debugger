"""Real Instrument recording, applied-filter and appearance workflows in Chromium."""

import hashlib
import importlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures/telemetry-gap.tlog"
EXPECTED = json.loads(FIXTURE.with_suffix(".expected.json").read_text())


def recording(entries):
    """Keep golden MAVLink frames, varying only their synthetic outer timestamps."""
    return b"".join(
        timestamp.to_bytes(8, "big") + bytes.fromhex(EXPECTED["records"][index]["frame_hex"])
        for index, timestamp in entries
    )


def upload(page, data, name="capture.tlog"):
    page.locator('input[type="file"]').set_input_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": data}
    )


@pytest.fixture(scope="module")
def instrument_server(tmp_path_factory):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("instrument-server") / "server.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uav_debugger.instrument",
                "--port",
                str(port),
                "--classic-port",
                "8501",
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"Instrument server exited:\n{log_path.read_text(errors='replace')}"
                    )
                try:
                    with urlopen(f"{base_url}/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(
                    f"Instrument server did not start:\n{log_path.read_text(errors='replace')}"
                )
            yield base_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture
def instrument_page(instrument_server, request):
    api = importlib.import_module("playwright.sync_api")
    api.expect.set_options(timeout=15_000)
    external_requests = []
    page_errors = []

    def is_local(url):
        parsed = urlsplit(url)
        return parsed.scheme in {"data", "blob", "about"} or parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }

    def route_request(route):
        if is_local(route.request.url):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    def route_socket(websocket):
        if is_local(websocket.url):
            websocket.connect_to_server()
        else:
            external_requests.append(websocket.url)
            websocket.close(code=1008, reason="External connections are disabled for this test.")

    with api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            service_workers="block",
            viewport={"width": 1440, "height": 1000},
            color_scheme="dark",
        )
        context.route("**/*", route_request)
        context.route_web_socket("**/*", route_socket)
        context.on(
            "page", lambda opened: opened.on("pageerror", lambda e: page_errors.append(str(e)))
        )
        page = context.new_page()
        page.set_default_timeout(15_000)
        try:
            page.goto(instrument_server, wait_until="networkidle")
            api.expect(page.locator("#load-example")).to_be_visible()
            yield page, api.expect
        finally:
            try:
                if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
                    output_dir = ROOT / "local" / "instrument-brick2" / "browser"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=output_dir / f"{request.node.name}.png", full_page=True)
            finally:
                context.close()
                browser.close()
        assert not external_requests, f"Unexpected external requests: {external_requests}"
        assert not page_errors, f"Browser JavaScript errors: {page_errors}"


def load_example(page, expect):
    page.locator("#load-example").click()
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#selected-count")).to_have_text("12")
    page.wait_for_function("() => document.querySelector('#activity-plot').data?.length > 0")
    expect(page.locator("#observations-label")).to_have_text("Activity bins")
    expect(page.locator("#observations-count")).to_have_text("200")


def apply_filters(page, expect, *, source=None, message=None, start=None, end=None, count=None):
    for selector, value in (("#source-filter", source), ("#message-filter", message)):
        if value is not None:
            page.locator(selector).select_option(value)
    for selector, value in (("#start-filter", start), ("#end-filter", end)):
        if value is not None:
            page.locator(selector).fill(value)
    with page.expect_response(
        lambda response: urlsplit(response.url).path in {"/api/example", "/api/analyze"}
    ):
        page.locator("#apply-filters").click()
    expect(page.locator("#filter-state")).to_have_text("Applied")
    expect(page.locator("#filter-error")).to_be_hidden()
    if count is not None:
        expect(page.locator("#selected-count")).to_have_text(str(count))


def show_attitude(page, expect):
    page.locator("#attitude-tab").click()
    expect(page.locator("#attitude-tab")).to_have_attribute("aria-selected", "true")


def selected_attitude(page, expect):
    load_example(page, expect)
    apply_filters(page, expect, source="1:1", message="30", start="1", end="5", count=2)
    show_attitude(page, expect)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")


def traces(page):
    return page.locator("#attitude-plot").evaluate(
        "plot => plot.data.map(({x, y, customdata}) => ({x, y, customdata}))"
    )


def assert_cleared(page, expect):
    expect(page.locator("#recording-name")).to_have_text("No recording selected")
    expect(page.locator("#record-count")).to_have_text("0")
    expect(page.locator("#selected-count")).to_have_text("0")
    assert page.locator("#attitude-plot").evaluate("plot => !plot.data?.length")
    assert page.locator("#activity-plot").evaluate("plot => !plot.data?.length")


def hold_next_response(page, endpoint):
    # The bytes arrive before Clear/replacement: request abort alone cannot protect state.
    page.evaluate(
        """endpoint => {
            const originalFetch = window.fetch.bind(window);
            let deferred = false;
            window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                if (deferred || new URL(args[0], location.href).pathname !== endpoint) {
                    return response;
                }
                deferred = true;
                const body = await response.arrayBuffer();
                const complete = new Response(body, {
                    status: response.status, headers: response.headers,
                });
                return new Promise(resolve => {
                    window.releaseResponse = () => resolve(complete);
                });
            };
        }""",
        endpoint,
    )


def release_response(page):
    page.evaluate("() => window.releaseResponse()")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


def test_example_retains_provenance_gaps_and_separate_sources(instrument_page):
    page, expect = instrument_page
    api_requests = []
    page.on(
        "request",
        lambda request: (
            api_requests.append((request.method, urlsplit(request.url).path))
            if "/api/" in request.url
            else None
        ),
    )
    page.locator("#load-example").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#source-count")).to_have_text("2")
    expect(page.locator("#issue-count")).to_have_text("5")
    expect(page.locator("#recording-name")).to_contain_text("telemetry-gap.tlog")
    expect(page.locator("#capture-origin")).to_have_text("1700000000000000")
    expect(page.locator("#evidence")).to_contain_text(EXPECTED["sha256"])
    expect(page.locator("#source-filter")).to_have_value("")
    expect(page.locator("#message-filter")).to_have_value("")
    expect(page.locator("#selected-count")).to_have_text("12")
    page.wait_for_function("() => document.querySelector('#activity-plot').data?.length > 0")
    assert (
        page.locator("#activity-plot").evaluate(
            "plot => plot.data.reduce((total, trace) => "
            "total + trace.y.reduce((sum, count) => sum + count, 0), 0)"
        )
        == 12
    )
    show_attitude(page, expect)
    expect(page.locator("#plot-unavailable-text")).to_have_text("Multiple attitude sources")
    apply_filters(page, expect, source="1:1", message="30", start="1", end="5", count=2)
    expect(page.locator("#applied-range")).to_have_text("1 to 5 s")
    expect(page.locator("#longest-interval")).to_have_text("4 s")
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    expect(page.locator("#observations-label")).to_have_text("Plotted observations")
    expect(page.locator("#observations-count")).to_have_text("2")
    for trace, value in zip(traces(page), (0.25, -0.5, 1.0), strict=True):
        assert [x for x in trace["x"] if x is not None] == [1, 5]
        assert [y for y in trace["y"] if y is not None] == [value, value]
        assert None in trace["y"]
        assert [point[0] for point in trace["customdata"] if point is not None] == [2, 8]
    assert traces(page)[0]["customdata"][0][1] == "1700000001000000"
    assert traces(page)[0]["customdata"][0][4:7] == [54, 62, 90]
    page.evaluate(
        "() => Plotly.Fx.hover(document.querySelector('#attitude-plot'), "
        "[{curveNumber: 0, pointNumber: 0}])"
    )
    expect(page.locator("#attitude-plot .hoverlayer")).to_contain_text("1700000001000000")

    apply_filters(page, expect, source="2:1", count=5)
    page.wait_for_function(
        "() => document.querySelector('#attitude-plot').data?.[0]?.y"
        ".filter(v => v !== null).length === 5"
    )
    for trace, value in zip(traces(page), (0.5, 0.25, -1.0), strict=True):
        assert trace["x"] == [1, 2, 3, 4, 5]
        assert trace["y"] == [value] * 5
        assert [point[0] for point in trace["customdata"]] == [3, 4, 6, 7, 9]

    page.locator("#issues-details summary").click()
    expect(page.locator("#issues-details")).to_have_attribute("open", "")
    expect(page.locator("#issues-details")).to_contain_text("timestamp")
    assert api_requests == [("GET", "/api/example")] * 3
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)


def test_recording_is_not_inherited_by_another_tab(instrument_page, instrument_server):
    page, expect = instrument_page
    load_example(page, expect)
    other = page.context.new_page()
    try:
        other.goto(instrument_server, wait_until="networkidle")
        assert_cleared(other, expect)
        expect(page.locator("#record-count")).to_have_text("12")
        other.goto(f"{instrument_server}/?example=1", wait_until="networkidle")
        expect(other.locator("#record-count")).to_have_text("12")
        other.locator("#clear-recording").click()
        assert_cleared(other, expect)
        expect(page.locator("#record-count")).to_have_text("12")
    finally:
        other.close()


def test_failed_example_can_retry_without_retaining_results(instrument_page):
    page, expect = instrument_page
    load_example(page, expect)

    def fail_example(route):
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"error": "The example is temporarily unavailable."}),
        )

    page.route("**/api/example**", fail_example)
    page.locator("#load-example").click()
    expect(page.locator("#error")).to_be_visible()
    assert_cleared(page, expect)
    page.unroute("**/api/example**", fail_example)
    page.locator("#retry-example").click()
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator("#source-filter")).to_have_value("")


def test_clear_rejects_a_successful_response_that_arrives_late(instrument_page):
    page, expect = instrument_page
    hold_next_response(page, "/api/example")
    page.locator("#load-example").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)
    release_response(page)
    assert_cleared(page, expect)
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator("#load-example")).to_be_enabled()


def test_appearance_persists_and_system_preference_remains_live(instrument_page):
    page, expect = instrument_page
    theme = page.locator("#theme-select")
    theme.select_option("light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.reload(wait_until="networkidle")
    expect(theme).to_have_value("light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.emulate_media(color_scheme="dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    theme.select_option("system")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.emulate_media(color_scheme="light")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    theme.select_option("dark")
    page.reload(wait_until="networkidle")
    expect(theme).to_have_value("dark")
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")


def test_chart_controls_change_the_view_without_changing_the_recording(instrument_page):
    page, expect = instrument_page
    selected_attitude(page, expect)
    roll = page.locator('[data-series="0"]')
    roll.focus()
    page.keyboard.press("Space")
    expect(roll).to_have_attribute("aria-pressed", "false")
    page.wait_for_function("document.querySelector('#attitude-plot').data[0].visible === false")
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#selected-count")).to_have_text("2")
    page.evaluate(
        "() => Plotly.relayout(document.querySelector('#attitude-plot'), "
        "{'xaxis3.range': [2, 4], 'xaxis3.autorange': false})"
    )
    page.locator("#reset-chart").click()
    page.wait_for_function(
        "document.querySelector('#attitude-plot')._fullLayout.xaxis3.autorange === true"
    )
    expect(page.locator("#sample-count")).to_have_text("2")
    expect(page.locator("#applied-range")).to_have_text("1 to 5 s")
    expect(page.locator("#capture-origin")).to_have_text("1700000000000000")
    page.locator("#focus-chart").click()
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#attitude-plot")).to_be_visible()
    page.locator("#focus-chart").click()
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "false")
    apply_filters(page, expect, source="2:1", count=5)
    expect(roll).to_have_attribute("aria-pressed", "true")
    page.wait_for_function("document.querySelector('#attitude-plot').data[0].visible === true")
    expect(page.locator("#sample-count")).to_have_text("5")
    page.locator("#activity-tab").click()
    page.wait_for_function("() => document.querySelector('#activity-plot').data?.length > 0")
    page.evaluate(
        "() => Plotly.relayout(document.querySelector('#activity-plot'), "
        "{'xaxis.range': [2, 4], 'xaxis.autorange': false})"
    )
    expect(page.locator("#selected-count")).to_have_text("5")
    expect(page.locator("#applied-range")).to_have_text("1 to 5 s")
    page.locator("#reset-chart").click()
    page.wait_for_function(
        "() => document.querySelector('#activity-plot')._fullLayout.xaxis.autorange === true"
    )
    show_attitude(page, expect)
    page.locator("#focus-chart").click()
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "false")
    assert not page.locator("body").evaluate("body => body.classList.contains('is-focused')")
    load_example(page, expect)
    expect(page.locator("#source-filter")).to_be_visible()


def test_experiment_handoff_is_explicit_and_does_not_execute(instrument_page):
    page, expect = instrument_page
    api_requests = []
    page.on(
        "request",
        lambda request: (
            api_requests.append(urlsplit(request.url).path) if "/api/" in request.url else None
        ),
    )
    page.locator("#experiment-button").click()
    expect(page.locator("#workspace-dialog")).to_be_visible()
    expect(page.locator("#classic-link")).to_have_attribute("href", "http://127.0.0.1:8501/")
    expect(page.locator("#classic-link")).to_have_text("Open existing workspace")
    page.keyboard.press("Escape")
    expect(page.locator("#workspace-dialog")).to_be_hidden()
    expect(page.locator("#experiment-button")).to_be_focused()
    assert api_requests == []


def test_upload_replacement_and_example_reset_input_and_applied_selection(instrument_page):
    page, expect = instrument_page
    original = FIXTURE.read_bytes()
    upload(page, original, name="flight_<capture>.tlog")
    expect(page.locator("#recording-name")).to_have_text("flight_<capture>.tlog")
    expect(page.locator("#recording-kind")).not_to_contain_text("Synthetic")
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#sha256")).to_have_text(hashlib.sha256(original).hexdigest())
    apply_filters(page, expect, source="1:1", message="30", start="1", end="5", count=2)

    replacement = recording([(3, 123_456_789), (3, 123_456_790)])
    upload(page, replacement, name="replacement.tlog")
    expect(page.locator("#recording-name")).to_have_text("replacement.tlog")
    expect(page.locator("#record-count")).to_have_text("2")
    expect(page.locator("#selected-count")).to_have_text("2")
    expect(page.locator("#capture-origin")).to_have_text("123456789")
    expect(page.locator("#source-filter")).to_have_value("")
    expect(page.locator("#message-filter")).to_have_value("")
    expect(page.locator("#start-filter")).to_have_value("0")
    expect(page.locator("#end-filter")).to_have_value("0.000001")
    expect(page.locator("#sha256")).to_have_text(hashlib.sha256(replacement).hexdigest())
    load_example(page, expect)
    assert page.locator('input[type="file"]').evaluate("input => input.files.length") == 0
    expect(page.locator("#sha256")).to_have_text(EXPECTED["sha256"])
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)
    assert FIXTURE.read_bytes() == original


def test_exact_inclusive_filters_keep_original_large_capture_timestamps(instrument_page):
    page, expect = instrument_page
    origin = 2**53 + 101
    data = recording(
        [(0, origin), (2, origin + 1_000_000), (2, origin + 1_000_001), (2, origin + 1_000_002)]
    )
    upload(page, data, name="microseconds.tlog")
    expect(page.locator("#record-count")).to_have_text("4")
    expect(page.locator("#capture-origin")).to_have_text(str(origin))
    apply_filters(
        page, expect, source="1:1", message="30", start="1.000001", end="1.000001", count=1
    )
    expect(page.locator("#applied-range")).to_have_text("1.000001 to 1.000001 s")
    show_attitude(page, expect)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    assert [point[0] for point in traces(page)[0]["customdata"]] == [2]
    assert traces(page)[0]["customdata"][0][1] == str(origin + 1_000_001)
    expect(page.locator("#capture-origin")).to_have_text(str(origin))


def test_filter_drafts_and_invalid_bounds_preserve_applied_evidence(instrument_page):
    page, expect = instrument_page
    selected_attitude(page, expect)
    before = traces(page)
    page.locator("#source-filter").select_option("2:1")
    expect(page.locator("#filter-state")).to_have_text("Unapplied changes")
    expect(page.locator("#selected-count")).to_have_text("2")
    expect(page.locator("#applied-source")).to_contain_text("1 / 1")
    assert traces(page) == before
    page.locator("#source-filter").select_option("1:1")
    for invalid_start in ("1.0000001", "not a number", "6"):
        page.locator("#start-filter").fill(invalid_start)
        page.locator("#apply-filters").click()
        expect(page.locator("#filter-error")).to_be_visible()
        expect(page.locator("#filter-state")).to_have_text("Unapplied changes")
        expect(page.locator("#selected-count")).to_have_text("2")
        expect(page.locator("#applied-range")).to_have_text("1 to 5 s")
        expect(page.locator("#issue-count")).to_have_text("5")
        assert traces(page) == before
    apply_filters(page, expect, start="1", end="1", count=1)
    expect(page.locator("#applied-range")).to_have_text("1 to 1 s")


def test_gap_is_display_only_and_does_not_apply_pending_filters(instrument_page):
    page, expect = instrument_page
    selected_attitude(page, expect)
    page.locator("#source-filter").select_option("2:1")
    page.locator("#start-filter").fill("2")
    page.locator("#line-gap").fill("4")
    page.locator("#apply-gap").click()
    page.wait_for_function(
        "() => document.querySelector('#attitude-plot').data?.[0]?.y.length === 2"
    )
    assert traces(page)[0]["y"] == [0.25, 0.25]
    expect(page.locator("#selected-count")).to_have_text("2")
    expect(page.locator("#longest-interval")).to_have_text("4 s")
    expect(page.locator("#applied-range")).to_have_text("1 to 5 s")
    expect(page.locator("#source-filter")).to_have_value("2:1")
    expect(page.locator("#start-filter")).to_have_value("2")
    expect(page.locator("#filter-state")).to_have_text("Unapplied changes")
    page.locator("#theme-select").select_option("light")
    expect(page.locator("#start-filter")).to_have_value("2")
    expect(page.locator("#filter-state")).to_have_text("Unapplied changes")
    page.locator("#line-gap").fill("0.0000001")
    page.locator("#apply-gap").click()
    expect(page.locator("#gap-error")).to_be_visible()
    assert traces(page)[0]["y"] == [0.25, 0.25]
    expect(page.locator("#selected-count")).to_have_text("2")
    page.locator("#line-gap").fill("1")
    page.locator("#apply-gap").click()
    page.wait_for_function(
        "() => document.querySelector('#attitude-plot').data[0].y.includes(null)"
    )
    expect(page.locator("#gap-error")).to_be_hidden()


def test_regression_hidden_by_filters_still_breaks_attitude_lines(instrument_page):
    page, expect = instrument_page
    origin = 1_700_000_000_000_000
    data = recording(
        [(2, origin), (1, origin - 1), (2, origin + 1_000_000), (2, origin + 2_000_000)]
    )
    upload(page, data, name="clock-regression.tlog")
    expect(page.locator("#record-count")).to_have_text("4")
    expect(page.locator("#start-filter")).to_have_value("-0.000001")
    apply_filters(page, expect, source="1:1", message="30", start="0", end="2", count=3)
    expect(page.locator("#longest-interval")).to_have_text("1 s")
    show_attitude(page, expect)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    assert traces(page)[0]["x"] == [0, None, 1, 2]
    assert [point[0] for point in traces(page)[0]["customdata"] if point is not None] == [0, 2, 3]
    page.locator("#line-gap").fill("3")
    page.locator("#apply-gap").click()
    expect(page.locator("#applied-gap")).to_have_text("Applied: 3 s")
    assert traces(page)[0]["x"] == [0, None, 1, 2]
    expect(page.locator("#capture-origin")).to_have_text(str(origin))


def test_attitude_table_keeps_original_order_when_first_roll_is_nonfinite(instrument_page):
    from pymavlink.dialects.v20 import common

    encoder = common.MAVLink(None, srcSystem=1, srcComponent=1)
    data = b""
    for index, roll in enumerate((float("nan"), 0.5)):
        frame = common.MAVLink_attitude_message(
            time_boot_ms=index,
            roll=roll,
            pitch=0.25,
            yaw=1.0,
            rollspeed=0,
            pitchspeed=0,
            yawspeed=0,
        ).pack(encoder)
        data += (1_700_000_000_000_000 + index).to_bytes(8, "big") + frame
    page, expect = instrument_page
    upload(page, data, name="nonfinite-roll.tlog")
    expect(page.locator("#record-count")).to_have_text("2")
    show_attitude(page, expect)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    page.locator("#observations-details summary").click()
    rows = page.locator("#observation-rows tr")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator("td").nth(0)).to_have_text("#0")
    expect(rows.nth(0).locator("td").nth(2)).to_have_text("N/A")
    expect(rows.nth(0).locator("td").nth(3)).to_have_text("0.25")
    expect(rows.nth(0).locator("td").nth(4)).to_have_text("1")
    expect(rows.nth(1).locator("td").nth(0)).to_have_text("#1")
    expect(rows.nth(1).locator("td").nth(2)).to_have_text("0.5")


def test_empty_selection_and_non_attitude_type_are_distinct_from_multiple_sources(instrument_page):
    page, expect = instrument_page
    load_example(page, expect)
    show_attitude(page, expect)
    expect(page.locator("#plot-unavailable-text")).to_have_text("Multiple attitude sources")
    apply_filters(page, expect, message="0", count=5)
    expect(page.locator("#plot-unavailable-text")).to_have_text("No attitude observations")
    apply_filters(page, expect, source="1:1", message="30", start="2", end="4", count=0)
    expect(page.locator("#plot-unavailable-text")).to_have_text("No attitude observations")
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#issue-count")).to_have_text("5")
    page.locator("#reset-filters").click()
    expect(page.locator("#selected-count")).to_have_text("12")
    expect(page.locator("#source-filter")).to_have_value("")
    expect(page.locator("#message-filter")).to_have_value("")
    expect(page.locator("#plot-unavailable-text")).to_have_text("Multiple attitude sources")


@pytest.mark.parametrize(
    ("kind", "count", "outcome", "remaining"),
    [
        ("empty", 0, "Empty recording", 0),
        ("invalid", 0, "Partial import", 3),
        ("partial", 11, "Partial import", 22),
    ],
)
def test_empty_invalid_and_partial_files_retain_original_input_provenance(
    instrument_page, kind, count, outcome, remaining
):
    page, expect = instrument_page
    data = {"empty": b"", "invalid": b"bad", "partial": FIXTURE.read_bytes()[:-3]}[kind]
    upload(page, data, name=f"{kind}.tlog")
    expect(page.locator("#recording-name")).to_have_text(f"{kind}.tlog")
    expect(page.locator("#import-status")).to_have_text(outcome)
    expect(page.locator("#record-count")).to_have_text(str(count))
    expect(page.locator("#selected-count")).to_have_text(str(count))
    expect(page.locator("#sha256")).to_have_text(hashlib.sha256(data).hexdigest())
    expect(page.locator("#remaining-size")).to_have_text(f"{remaining} B")
    expect(page.locator("#error")).to_be_hidden()
    if count == 0:
        expect(page.locator("#filter-fields")).to_have_js_property("disabled", True)
        expect(page.locator("#filter-state")).to_have_text("No records")
        expect(page.locator("#filter-state")).to_have_attribute("data-pending", "false")
        expect(page.locator("#source-filter")).to_be_disabled()
        expect(page.locator("#start-filter")).to_be_disabled()
        expect(page.locator("#apply-filters")).to_be_disabled()
        page.locator("#theme-select").select_option("light")
        expect(page.locator("#filter-state")).to_have_text("No records")
    else:
        expect(page.locator("#filter-fields")).to_have_js_property("disabled", False)
        expect(page.locator("#filter-state")).to_have_text("Applied")
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)


def test_point_limit_keeps_complete_selection_and_can_be_narrowed(instrument_page):
    page, expect = instrument_page
    data = recording((2, 1_700_000_000_000_000 + index) for index in range(5001))
    upload(page, data, name="many-attitudes.tlog")
    expect(page.locator("#record-count")).to_have_text("5001")
    expect(page.locator("#selected-count")).to_have_text("5001")
    show_attitude(page, expect)
    expect(page.locator("#plot-unavailable-text")).to_have_text("Attitude display limit reached")
    assert page.locator("#attitude-plot").evaluate("plot => !plot.data?.length")
    apply_filters(page, expect, start="0", end="0.000001", count=2)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    assert [point[0] for point in traces(page)[0]["customdata"]] == [0, 1]
    expect(page.locator("#record-count")).to_have_text("5001")


def test_capture_larger_than_ten_mebibytes_is_rejected_before_upload(instrument_page):
    page, expect = instrument_page
    uploaded = []
    page.on(
        "request",
        lambda request: (
            uploaded.append(request.url) if urlsplit(request.url).path == "/api/analyze" else None
        ),
    )
    upload(page, b"x" * (10 * 1024 * 1024 + 1), name="too-large.tlog")
    expect(page.locator("#error")).to_be_visible()
    expect(page.locator("#error-message")).to_contain_text("10 MiB")
    assert_cleared(page, expect)
    assert uploaded == []


@pytest.mark.parametrize("next_action", ["clear", "replace"])
def test_completed_filter_response_cannot_revive_cleared_or_replaced_upload(
    instrument_page, next_action
):
    page, expect = instrument_page
    upload(page, FIXTURE.read_bytes(), name="initial.tlog")
    expect(page.locator("#record-count")).to_have_text("12")
    hold_next_response(page, "/api/analyze")
    page.locator("#source-filter").select_option("1:1")
    page.locator("#apply-filters").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if next_action == "clear":
        page.locator("#clear-recording").click()
        assert_cleared(page, expect)
    else:
        upload(page, recording([(3, 123_456_789)]), name="new.tlog")
        expect(page.locator("#recording-name")).to_have_text("new.tlog")
        expect(page.locator("#record-count")).to_have_text("1")
    release_response(page)
    if next_action == "clear":
        assert_cleared(page, expect)
    else:
        expect(page.locator("#recording-name")).to_have_text("new.tlog")
        expect(page.locator("#record-count")).to_have_text("1")
        expect(page.locator("#selected-count")).to_have_text("1")
        expect(page.locator("#source-filter")).to_have_value("")
    expect(page.locator("#error")).to_be_hidden()


def test_import_issue_pages_are_bounded_without_changing_selection(instrument_page):
    page, expect = instrument_page
    data = recording((0, 1_700_000_000_000_000) for _ in range(205))
    upload(page, data, name="repeated-timestamps.tlog")
    expect(page.locator("#record-count")).to_have_text("205")
    expect(page.locator("#issue-count")).to_have_text("204")
    page.locator("#issues-details summary").click()
    expect(page.locator("#issue-list li")).to_have_count(100)
    expect(page.locator("#issues-previous")).to_be_disabled()
    page.locator("#issues-next").click()
    expect(page.locator("#issue-list li").first).to_contain_text("Record #101")
    expect(page.locator("#issue-list li")).to_have_count(100)
    page.locator("#issues-next").click()
    expect(page.locator("#issue-list li")).to_have_count(4)
    expect(page.locator("#issues-next")).to_be_disabled()
    expect(page.locator("#selected-count")).to_have_text("205")
    expect(page.locator("#sha256")).to_have_text(hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_loaded_workspace_fits_and_remains_readable(instrument_page, width, theme):
    page, expect = instrument_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#theme-select").select_option(theme)
    selected_attitude(page, expect)
    page.wait_for_function(
        "document.querySelector('#attitude-plot .scatterlayer path.point') !== null"
    )
    bounds = page.evaluate(
        """() => ({
            viewport: innerWidth,
            document: document.documentElement.scrollWidth,
            controls: [...document.querySelectorAll('button, select, input, summary')]
                .filter(element => element.getClientRects().length)
                .map(element => {
                    const box = element.getBoundingClientRect();
                    return {
                        id: element.id || element.textContent, left: box.left, right: box.right,
                    };
                }),
        })"""
    )
    assert bounds["document"] <= bounds["viewport"] + 1, bounds
    assert all(
        control["left"] >= -1 and control["right"] <= width + 1 for control in bounds["controls"]
    ), bounds
    contrast_script = """() => {
            const rgba = value => value.match(/[\\d.]+/g).map(Number);
            const over = (front, back) => {
                const alpha = front[3] ?? 1;
                return front.slice(0, 3).map((value, i) => value * alpha + back[i] * (1 - alpha));
            };
            const background = element => {
                const parents = [];
                for (let current = element; current; current = current.parentElement) {
                    parents.unshift(current);
                }
                return parents.reduce(
                    (result, parent) => over(
                        rgba(getComputedStyle(parent).backgroundColor), result,
                    ),
                    [255, 255, 255],
                );
            };
            const luminance = rgb => rgb.map(value => {
                const channel = value / 255;
                return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4;
            }).reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
            const ratio = (foreground, backdrop) => {
                const values = [luminance(foreground), luminance(backdrop)].sort((a, b) => a - b);
                return (values[1] + .05) / (values[0] + .05);
            };
            const selectors = [
                '#recording-name', '#capture-origin', '#record-count', '#source-count',
                '#issue-count', '#load-example', '#theme-select', '#status',
                '#source-filter', '#message-filter', '#start-filter', '#end-filter',
                '#applied-range', '#selected-count', '#filter-state', '#line-gap',
            ];
            const results = selectors.map(selector => {
                const element = document.querySelector(selector);
                const backdrop = background(element);
                return {
                    selector,
                    ratio: ratio(over(rgba(getComputedStyle(element).color), backdrop), backdrop),
                    minimum: 4.5,
                };
            });
            for (const graph of document.querySelectorAll('#attitude-plot, #activity-plot')) {
                if (!graph.getClientRects().length) continue;
                const plotBackground = graph.querySelector('.bglayer .bg');
                const backdrop = plotBackground
                    ? over(rgba(getComputedStyle(plotBackground).fill), background(graph))
                    : background(graph);
                for (const point of graph.querySelectorAll(
                    '.scatterlayer .trace path.point, .barlayer .point path'
                )) {
                    results.push({
                        selector: graph.id + ' data marker',
                        ratio: ratio(over(rgba(getComputedStyle(point).fill), backdrop), backdrop),
                        minimum: 3,
                    });
                }
                for (const tick of graph.querySelectorAll('.xtick text, .ytick text')) {
                    results.push({
                        selector: graph.id + ' axis tick',
                        ratio: ratio(over(rgba(getComputedStyle(tick).fill), backdrop), backdrop),
                        minimum: 4.5,
                    });
                }
            }
            return results;
        }"""
    contrast = page.evaluate(contrast_script)
    assert all(item["ratio"] >= item["minimum"] for item in contrast), contrast
    expect(page.locator("#attitude-plot .scatterlayer path.point")).to_have_count(6)
    expect(page.locator("#record-count")).to_have_text("12")
    page.locator("#activity-tab").click()
    page.wait_for_function("() => document.querySelector('#activity-plot .barlayer .point path')")
    activity_contrast = page.evaluate(contrast_script)
    assert any(item["selector"] == "activity-plot data marker" for item in activity_contrast)
    assert all(item["ratio"] >= item["minimum"] for item in activity_contrast), activity_contrast
    show_attitude(page, expect)
