"""Real Instrument recording, applied-filter and appearance workflows in Chromium."""

import hashlib
import importlib
import json
import os
import re
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
    page.locator("#recording-file").set_input_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": data}
    )


@pytest.fixture(scope="module")
def instrument_catalog_root(tmp_path_factory):
    return tmp_path_factory.mktemp("instrument-catalog") / "experiments"


@pytest.fixture(scope="module")
def instrument_server(tmp_path_factory, instrument_catalog_root):
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
                "--experiment-root",
                str(instrument_catalog_root),
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
                    output_dir = ROOT / "local" / "instrument-brick6" / "browser"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    page.evaluate(
                        "() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }"
                    )
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
        lambda response: (
            urlsplit(response.url).path
            in {"/api/example", "/api/analyze", "/api/run", "/api/catalog/open"}
        )
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


def download_report(page, destination, *, button="#download-report"):
    with page.expect_download() as pending:
        page.locator(button).click()
    download = pending.value
    download.save_as(destination)
    assert download.failure() is None
    assert download.suggested_filename.endswith(".md")
    report = destination.read_text(encoding="utf-8")
    blocks = [
        json.loads(match.group(2))
        for match in re.finditer(r"^(`{3,})json\n(.*?)\n\1$", report, re.M | re.S)
    ]
    assert len(blocks) >= 4
    return report, blocks


def click_attitude_marker(page, point_index):
    marker = (
        page.locator("#attitude-plot .scatterlayer .trace")
        .first.locator("path.point")
        .nth(point_index)
    )
    marker.scroll_into_view_if_needed()
    bounds = marker.bounding_box()
    assert bounds is not None
    page.mouse.click(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)


def inspect_record(page, expect, index):
    page.locator("#messages-tab").click()
    page.locator(f'#message-rows button[data-record-index="{index}"]').click()
    expect(page.locator("#inspector-title")).to_have_text(f"Record #{index}")
    expect(page.locator("#inspector-content")).to_be_visible()


def literal_payload_recording():
    from pymavlink.dialects.v20 import common

    encoder = common.MAVLink(None, srcSystem=1, srcComponent=1)
    payload_text = "<img src=x onerror=alert(1)> `literal`"
    frames = [
        common.MAVLink_system_time_message(
            time_unix_usec=(1 << 64) - 1, time_boot_ms=(1 << 32) - 1
        ).pack(encoder),
        common.MAVLink_attitude_message(
            time_boot_ms=123,
            roll=float("nan"),
            pitch=float("inf"),
            yaw=float("-inf"),
            rollspeed=0,
            pitchspeed=0,
            yawspeed=0,
        ).pack(encoder),
        common.MAVLink_statustext_message(severity=6, text=payload_text.encode()).pack(encoder),
        bytes.fromhex("fd 01 00 00 00 03 01 ff ff ff 00 00 00"),
    ]
    origin = (1 << 64) - 10
    data = b"".join(
        (origin + index).to_bytes(8, "big") + frame for index, frame in enumerate(frames)
    )
    return data, origin, frames, payload_text


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


def test_experiment_mode_is_explicit_and_does_not_execute(instrument_page):
    page, expect = instrument_page
    api_requests = []
    page.on(
        "request",
        lambda request: (
            api_requests.append(urlsplit(request.url).path) if "/api/" in request.url else None
        ),
    )
    page.locator("#experiment-button").click()
    expect(page.locator("#experiment-view")).to_be_visible()
    expect(page.locator("#experiment-start")).to_be_enabled()
    expect(page.locator("#experiment-process-state")).to_have_text("Idle")
    page.locator("#analyze-button").click()
    expect(page.locator("#experiment-view")).to_be_hidden()
    expect(page.locator("#load-example")).to_be_visible()
    assert api_requests and set(api_requests) == {"/api/experiment/status"}


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
    assert page.locator("#recording-file").evaluate("input => input.files.length") == 0
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


def test_import_issue_pages_are_bounded_without_changing_selection(instrument_page, tmp_path):
    page, expect = instrument_page
    data = recording((0, 1_700_000_000_000_000) for _ in range(205))
    upload(page, data, name="repeated-timestamps.tlog")
    expect(page.locator("#record-count")).to_have_text("205")
    expect(page.locator("#issue-count")).to_have_text("204")
    inspect_record(page, expect, 0)
    page.locator("#evidence-tab").click()
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
    expect(page.locator("#inspector-title")).to_have_text("Record #0")
    _, blocks = download_report(page, tmp_path / "after-issue-paging.md")
    assert blocks[2]["import_issue_count"] == 204
    assert blocks[2]["reported_issue_count"] == 100
    assert blocks[2]["omitted_issue_count"] == 104
    assert blocks[4]["index"] == 0


def test_messages_plot_inspection_and_report_share_original_evidence(instrument_page, tmp_path):
    page, expect = instrument_page
    selected_attitude(page, expect)
    expect(page.locator("#download-report")).to_be_enabled()
    _, blocks = download_report(page, tmp_path / "without-explicit-record.md")
    assert len(blocks) == 4
    assert blocks[2]["explicit_detail_record_count"] == 0

    page.locator("#attitude-tab").focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#messages-tab")).to_be_focused()
    expect(page.locator("#messages-tab")).to_have_attribute("aria-selected", "true")
    record_button = page.locator('#message-rows button[data-record-index="2"]')
    record_button.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#inspector-title")).to_have_text("Record #2")
    expect(page.locator("#inspector-content")).to_be_visible()
    expect(record_button).to_be_focused()
    expect(page.locator("#message-rows tr")).to_have_count(2)
    expect(page.locator("#messages-range")).to_have_text("1 to 2 of 2 records")
    expect(page.locator("#inspector-capture")).to_have_text("1700000001000000")
    expect(page.locator("#inspector-time")).to_have_text("1")
    expect(page.locator("#inspector-message")).to_contain_text("ATTITUDE")
    expect(page.locator("#inspector-source")).to_have_text("1 / 1")
    expect(page.locator("#inspector-checksum")).to_contain_text("valid")
    expect(page.locator("#inspector-record-range")).to_have_text("[54, 90)")
    expect(page.locator("#inspector-frame-range")).to_have_text("[62, 90)")
    assert (
        json.loads(page.locator("#inspector-fields").inner_text())
        == EXPECTED["records"][2]["fields"]
    )
    frame = EXPECTED["records"][2]["frame_hex"]
    assert re.sub(r"\s+", "", page.locator("#inspector-raw-frame").inner_text()) == frame
    page.locator("#record-bytes-tab").click()
    assert re.sub(r"\s+", "", page.locator("#inspector-raw-record").inner_text()) == (
        FIXTURE.read_bytes()[54:90].hex()
    )

    show_attitude(page, expect)
    click_attitude_marker(page, 1)
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    expect(page.locator("#attitude-tab")).to_have_attribute("aria-selected", "true")
    # Pending control edits and chart zoom are not changes to the applied evidence.
    page.locator("#source-filter").select_option("2:1")
    page.locator("#start-filter").fill("2")
    page.locator("#line-gap").fill("5")
    page.evaluate(
        "() => Plotly.relayout(document.querySelector('#attitude-plot'), "
        "{'xaxis3.range': [2, 4], 'xaxis3.autorange': false})"
    )
    page.locator("#theme-select").select_option("light")
    report, blocks = download_report(page, tmp_path / "selected-point.md")
    provenance, selection, coverage, issues, detail = blocks
    assert provenance["sha256"] == EXPECTED["sha256"]
    assert selection == {
        "sources": [[1, 1]],
        "message_ids": [30],
        "start_us": 1_700_000_001_000_000,
        "end_us": 1_700_000_005_000_000,
        "bounds": "inclusive",
        "relative_origin_us": 1_700_000_000_000_000,
    }
    assert coverage["filtered_record_count"] == 2
    assert coverage["explicit_detail_record_count"] == 1
    assert coverage["attitude_plot"]["max_gap_us"] == 1_000_000
    assert coverage["observation_intervals"]["longest"]["delta_us"] == 4_000_000
    assert len(issues) == 5
    assert detail["index"] == 8
    assert detail["raw_frame_hex"] == EXPECTED["records"][8]["frame_hex"]
    assert detail["fields"] == EXPECTED["records"][8]["fields"]
    assert "packet loss" in report and "clock" in report
    expect(page.locator("#filter-state")).to_have_text("Unapplied changes")

    page.locator("#apply-gap").click()
    expect(page.locator("#applied-gap")).to_have_text("Applied: 5 s")
    expect(page.locator("#inspector-content")).to_be_hidden()
    _, blocks = download_report(page, tmp_path / "new-gap.md")
    assert blocks[1] == selection
    assert blocks[2]["attitude_plot"]["max_gap_us"] == 5_000_000
    assert blocks[2]["explicit_detail_record_count"] == 0
    assert len(blocks) == 4


def test_original_record_paging_plot_selection_and_direct_index(instrument_page, tmp_path):
    page, expect = instrument_page
    origin = 1_700_000_000_000_000
    data = recording((2, origin + index * 100_000) for index in range(205))
    upload(page, data, name="paged-observations.tlog")
    expect(page.locator("#record-count")).to_have_text("205")
    page.locator("#messages-tab").click()
    expect(page.locator("#message-rows tr")).to_have_count(100)
    expect(page.locator("#messages-range")).to_have_text("1 to 100 of 205 records")
    expect(page.locator("#messages-previous")).to_be_disabled()
    page.locator("#messages-next").click()
    expect(page.locator("#messages-page")).to_have_value("2")
    expect(page.locator("#message-rows tr").first).to_have_attribute("data-record-index", "100")
    inspect_record(page, expect, 110)
    page.locator("#messages-next").click()
    expect(page.locator("#message-rows tr")).to_have_count(5)
    expect(page.locator("#messages-next")).to_be_disabled()
    expect(page.locator("#inspector-content")).to_be_hidden()
    page.locator("#messages-page").fill("1")
    page.locator("#messages-page").press("Enter")
    expect(page.locator("#message-rows tr").first).to_have_attribute("data-record-index", "0")

    show_attitude(page, expect)
    page.wait_for_function("() => document.querySelector('#attitude-plot').data?.length === 3")
    page.evaluate(
        "() => Plotly.relayout(document.querySelector('#attitude-plot'), "
        "{'xaxis3.range': [10.7, 11.3], 'xaxis3.autorange': false})"
    )
    click_attitude_marker(page, 110)
    expect(page.locator("#inspector-title")).to_have_text("Record #110")
    expect(page.locator("#inspector-capture")).to_have_text(str(origin + 11_000_000))
    page.locator("#messages-tab").click()
    expect(page.locator("#messages-page")).to_have_value("2")
    expect(page.locator('#message-rows button[data-record-index="110"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    _, blocks = download_report(page, tmp_path / "page-two-point.md")
    assert blocks[0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert blocks[2]["filtered_record_count"] == 205
    assert blocks[4]["index"] == 110
    frame = bytes.fromhex(EXPECTED["records"][2]["frame_hex"])
    assert blocks[4]["offset"] == 110 * (8 + len(frame))
    assert blocks[4]["timestamp_us"] == origin + 11_000_000
    assert blocks[4]["raw_frame_hex"] == frame.hex()

    page.locator("#record-index").fill("204")
    page.locator("#inspect-record").click()
    expect(page.locator("#inspector-title")).to_have_text("Record #204")
    expect(page.locator("#messages-page")).to_have_value("3")
    page.locator("#record-index").fill("205")
    page.locator("#inspect-record").click()
    expect(page.locator("#inspector-error")).to_be_visible()
    expect(page.locator("#inspector-title")).to_have_text("Record #204")
    _, blocks = download_report(page, tmp_path / "rejected-index.md")
    assert blocks[4]["index"] == 204
    apply_filters(page, expect, start="0", end="1", count=11)
    expect(page.locator("#inspector-content")).to_be_hidden()
    expect(page.locator("#messages-page")).to_have_value("1")
    _, blocks = download_report(page, tmp_path / "narrowed-records.md")
    assert len(blocks) == 4
    assert blocks[2]["filtered_record_count"] == 11
    assert blocks[2]["explicit_detail_record_count"] == 0


def test_inspector_keeps_device_clock_nonfinite_opaque_and_text_literal(instrument_page, tmp_path):
    page, expect = instrument_page
    data, origin, frames, payload_text = literal_payload_recording()
    name = "capture_<img src=x onerror=alert(1)>```_.tlog"
    upload(page, data, name=name)
    expect(page.locator("#record-count")).to_have_text("4")
    expect(page.locator("#capture-origin")).to_have_text(str(origin))
    inspect_record(page, expect, 0)
    expect(page.locator("#inspector-capture")).to_have_text(str(origin))
    fields = json.loads(page.locator("#inspector-fields").inner_text())
    assert fields["time_unix_usec"] == (1 << 64) - 1
    assert fields["time_boot_ms"] == (1 << 32) - 1
    _, blocks = download_report(page, tmp_path / "exact-clocks.md")
    assert blocks[0]["source_name"] == name
    assert blocks[4]["timestamp_us"] == origin
    assert blocks[4]["fields"]["time_unix_usec"] == (1 << 64) - 1

    inspect_record(page, expect, 1)
    fields = json.loads(page.locator("#inspector-fields").inner_text())
    assert fields["roll"] == {"non_finite_float": "nan"}
    assert fields["pitch"] == {"non_finite_float": "inf"}
    assert fields["yaw"] == {"non_finite_float": "-inf"}
    _, blocks = download_report(page, tmp_path / "nonfinite.md")
    assert blocks[4]["fields"] == fields

    inspect_record(page, expect, 2)
    fields = json.loads(page.locator("#inspector-fields").inner_text())
    assert fields["text"] == payload_text
    assert page.locator("#inspector-fields img, #recording-name img").count() == 0
    report, blocks = download_report(page, tmp_path / "literal-labels.md")
    assert blocks[0]["source_name"] == name
    assert blocks[4]["fields"]["text"] == payload_text
    assert "<img" not in report

    inspect_record(page, expect, 3)
    expect(page.locator("#inspector-opaque")).to_be_visible()
    expect(page.locator("#inspector-fields")).to_be_hidden()
    expect(page.locator("#inspector-checksum")).to_contain_text("unverified")
    expect(page.locator("#inspector-capture")).to_have_text(str(origin + 3))
    assert re.sub(r"\s+", "", page.locator("#inspector-raw-frame").inner_text()) == frames[3].hex()
    _, blocks = download_report(page, tmp_path / "opaque.md")
    assert blocks[4]["index"] == 3
    assert blocks[4]["fields"] is None
    assert blocks[4]["raw_frame_hex"] == frames[3].hex()


@pytest.mark.parametrize("kind", ["empty", "invalid", "partial"])
def test_report_keeps_empty_and_partial_import_outcomes(instrument_page, tmp_path, kind):
    page, expect = instrument_page
    data = {"empty": b"", "invalid": b"bad", "partial": FIXTURE.read_bytes()[:-3]}[kind]
    upload(page, data, name=f"{kind}.tlog")
    expect(page.locator("#sha256")).to_have_text(hashlib.sha256(data).hexdigest())
    expect(page.locator("#download-report")).to_be_enabled()
    report, blocks = download_report(page, tmp_path / f"{kind}.md")
    provenance, _, coverage, issues = blocks
    assert provenance["sha256"] == hashlib.sha256(data).hexdigest()
    assert provenance["size_bytes"] == len(data)
    assert coverage["imported_record_count"] == (11 if kind == "partial" else 0)
    assert coverage["filtered_record_count"] == coverage["imported_record_count"]
    assert coverage["explicit_detail_record_count"] == 0
    assert coverage["consumed_bytes"] + coverage["remaining_bytes"] == len(data)
    assert coverage["traversal"] == ("empty" if kind == "empty" else "stopped")
    if kind != "empty":
        assert any(issue["severity"] == "error" for issue in issues)
    assert "No record details were explicitly selected." in report


def test_no_match_report_and_explicit_clear_do_not_export_previous_inspection(
    instrument_page, tmp_path
):
    page, expect = instrument_page
    selected_attitude(page, expect)
    inspect_record(page, expect, 8)
    page.locator("#clear-inspector").click()
    expect(page.locator("#inspector-content")).to_be_hidden()
    _, blocks = download_report(page, tmp_path / "cleared-inspector.md")
    assert blocks[2]["explicit_detail_record_count"] == 0
    assert len(blocks) == 4
    inspect_record(page, expect, 2)
    apply_filters(page, expect, start="2", end="4", count=0)
    expect(page.locator("#message-rows tr")).to_have_count(0)
    expect(page.locator("#inspector-content")).to_be_hidden()
    _, blocks = download_report(page, tmp_path / "no-matching-records.md")
    assert blocks[1]["start_us"] == 1_700_000_002_000_000
    assert blocks[1]["end_us"] == 1_700_000_004_000_000
    assert blocks[2]["filtered_record_count"] == 0
    assert blocks[2]["explicit_detail_record_count"] == 0
    assert len(blocks) == 4


@pytest.mark.parametrize("pending_kind", ["inspector", "report"])
@pytest.mark.parametrize("next_action", ["clear", "replace", "filter"])
def test_completed_inspection_and_download_cannot_survive_changed_evidence(
    instrument_page, pending_kind, next_action
):
    page, expect = instrument_page
    upload(page, FIXTURE.read_bytes(), name="initial.tlog")
    expect(page.locator("#record-count")).to_have_text("12")
    downloads = []
    page.on("download", lambda download: downloads.append(download))
    hold_next_response(page, "/api/analyze")
    if pending_kind == "inspector":
        page.locator("#messages-tab").click()
        page.locator('#message-rows button[data-record-index="8"]').click()
    else:
        page.locator("#download-report").click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    if next_action == "clear":
        page.locator("#clear-recording").click()
        assert_cleared(page, expect)
    elif next_action == "replace":
        upload(page, recording([(3, 123_456_789)]), name="replacement.tlog")
        expect(page.locator("#record-count")).to_have_text("1")
    else:
        apply_filters(page, expect, source="2:1", message="30", count=5)
    release_response(page)
    expect(page.locator("#inspector-content")).to_be_hidden()
    assert downloads == []
    if next_action == "clear":
        assert_cleared(page, expect)
        expect(page.locator("#download-report")).to_be_disabled()
    elif next_action == "replace":
        expect(page.locator("#recording-name")).to_have_text("replacement.tlog")
        expect(page.locator("#record-count")).to_have_text("1")
        expect(page.locator("#capture-origin")).to_have_text("123456789")
    else:
        expect(page.locator("#selected-count")).to_have_text("5")
        expect(page.locator("#applied-source")).to_contain_text("2 / 1")


def test_failed_report_preserves_inspection_and_retries_current_evidence(instrument_page, tmp_path):
    page, expect = instrument_page
    selected_attitude(page, expect)
    inspect_record(page, expect, 8)

    def fail_report(route):
        route.fulfill(
            status=503,
            content_type="application/json",
            body=json.dumps({"error": "Report is temporarily unavailable."}),
        )

    page.route("**/api/example?*format=markdown*", fail_report)
    page.locator("#download-report").click()
    expect(page.locator("#report-error")).to_be_visible()
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    page.unroute("**/api/example?*format=markdown*", fail_report)
    _, blocks = download_report(page, tmp_path / "retried-report.md")
    assert blocks[4]["index"] == 8
    expect(page.locator("#report-error")).to_be_hidden()


def test_newer_explicit_record_wins_over_completed_older_inspection(instrument_page, tmp_path):
    page, expect = instrument_page
    selected_attitude(page, expect)
    page.locator("#messages-tab").click()
    hold_next_response(page, "/api/example")
    page.locator('#message-rows button[data-record-index="2"]').click()
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    page.locator('#message-rows button[data-record-index="8"]').click()
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    release_response(page)
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    expect(page.locator('#message-rows button[data-record-index="8"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    _, blocks = download_report(page, tmp_path / "latest-explicit-record.md")
    assert blocks[4]["index"] == 8


def test_empty_attitude_interval_and_activity_bins_do_not_select_records(instrument_page, tmp_path):
    page, expect = instrument_page
    selected_attitude(page, expect)
    click_attitude_marker(page, 0)
    expect(page.locator("#inspector-title")).to_have_text("Record #2")
    markers = page.locator("#attitude-plot .scatterlayer .trace").first.locator("path.point")
    markers.first.scroll_into_view_if_needed()
    first, last = markers.nth(0).bounding_box(), markers.nth(1).bounding_box()
    assert first is not None and last is not None
    page.mouse.click(
        (first["x"] + first["width"] / 2 + last["x"] + last["width"] / 2) / 2,
        first["y"] + first["height"] / 2,
    )
    expect(page.locator("#inspector-title")).to_have_text("Record #2")
    page.locator("#clear-inspector").click()
    expect(page.locator("#inspector-content")).to_be_hidden()
    page.locator("#activity-tab").click()
    bar = page.locator("#activity-plot .barlayer .point path").first
    expect(bar).to_be_visible()
    bar.scroll_into_view_if_needed()
    bounds = bar.bounding_box()
    assert bounds is not None and bounds["width"] > 0 and bounds["height"] > 0
    page.mouse.click(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
    expect(page.locator("#inspector-content")).to_be_hidden()
    _, blocks = download_report(page, tmp_path / "activity-does-not-select.md")
    assert len(blocks) == 4
    assert blocks[2]["explicit_detail_record_count"] == 0


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
    click_attitude_marker(page, 1)
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    expect(page.locator("#inspector-content")).to_be_visible()
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
            const painted = (element, property, backdrop) => {
                const style = getComputedStyle(element), foreground = rgba(style[property]);
                let alpha = foreground[3] ?? 1;
                if (property === 'fill') alpha *= Number(style.fillOpacity);
                for (let current = element; current; current = current.parentElement) {
                    alpha *= Number(getComputedStyle(current).opacity);
                }
                foreground[3] = alpha;
                return over(foreground, backdrop);
            };
            const selectors = [
                '#recording-name', '#capture-origin', '#record-count', '#source-count',
                '#issue-count', '#load-example', '#theme-select', '#status',
                '#source-filter', '#message-filter', '#start-filter', '#end-filter',
                '#applied-range', '#selected-count', '#filter-state', '#line-gap',
                '#download-report', '#inspector-title', '#inspector-capture',
                '#inspector-fields', '#inspector-raw-frame', '#inspector-checksum',
            ];
            const results = selectors.map(selector => {
                const element = document.querySelector(selector);
                const backdrop = background(element);
                return {
                    selector,
                    ratio: ratio(painted(element, 'color', backdrop), backdrop),
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
                        ratio: ratio(painted(point, 'fill', backdrop), backdrop),
                        minimum: 3,
                    });
                }
                for (const tick of graph.querySelectorAll('.xtick text, .ytick text')) {
                    results.push({
                        selector: graph.id + ' axis tick',
                        ratio: ratio(painted(tick, 'fill', backdrop), backdrop),
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
    page.locator("#messages-tab").click()
    expect(page.locator("#message-rows tr")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        output_dir = ROOT / "local" / "instrument-brick6" / "browser"
        output_dir.mkdir(parents=True, exist_ok=True)
        page.evaluate("() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }")
        page.screenshot(path=output_dir / f"messages-{theme}-{width}.png", full_page=True)
    show_attitude(page, expect)
