"""Opt-in Chromium checks for the Instrument example and appearance controls."""

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
EXPECTED = json.loads((Path(__file__).parent / "fixtures/telemetry-gap.expected.json").read_text())


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
                    output_dir = ROOT / "local" / "instrument-brick1" / "browser"
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
    page.wait_for_function("document.querySelector('#attitude-plot').data?.length === 3")


def traces(page):
    return page.locator("#attitude-plot").evaluate(
        "plot => plot.data.map(({x, y, customdata}) => ({x, y, customdata}))"
    )


def assert_cleared(page, expect):
    expect(page.locator("#source-list button")).to_have_count(0)
    expect(page.locator("#recording-name")).not_to_contain_text("telemetry-gap.tlog")
    assert page.locator("#attitude-plot").evaluate("plot => !plot.data?.length")


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
    page.wait_for_function("document.querySelector('#attitude-plot').data?.length === 3")
    source_one = page.locator('#source-list button[data-source="1:1"]')
    source_two = page.locator('#source-list button[data-source="2:1"]')
    expect(source_one).to_have_attribute("aria-pressed", "true")
    for trace, value in zip(traces(page), (0.25, -0.5, 1.0), strict=True):
        assert [x for x in trace["x"] if x is not None] == [1, 5]
        assert [y for y in trace["y"] if y is not None] == [value, value]
        assert None in trace["y"]
        assert [point[0] for point in trace["customdata"] if point is not None] == [2, 8]

    source_two.click()
    expect(source_two).to_have_attribute("aria-pressed", "true")
    expect(source_one).to_have_attribute("aria-pressed", "false")
    page.wait_for_function(
        "document.querySelector('#attitude-plot').data?.[0]?.y.filter(v => v !== null).length === 5"
    )
    for trace, value in zip(traces(page), (0.5, 0.25, -1.0), strict=True):
        assert trace["x"] == [1, 2, 3, 4, 5]
        assert trace["y"] == [value] * 5
        assert [point[0] for point in trace["customdata"]] == [3, 4, 6, 7, 9]

    page.locator("#issues-details summary").click()
    expect(page.locator("#issues-details")).to_have_attribute("open", "")
    expect(page.locator("#issues-details")).to_contain_text("timestamp")
    assert api_requests == [("GET", "/api/example")]
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

    page.route("**/api/example", fail_example)
    page.locator("#load-example").click()
    expect(page.locator("#error")).to_be_visible()
    assert_cleared(page, expect)
    page.unroute("**/api/example", fail_example)
    page.locator("#retry-example").click()
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#error")).to_be_hidden()
    expect(page.locator('#source-list button[data-source="1:1"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def test_clear_rejects_a_successful_response_that_arrives_late(instrument_page):
    page, expect = instrument_page
    # Hold a complete response past Clear, so request abort alone cannot protect state.
    page.evaluate(
        """() => {
            const originalFetch = window.fetch.bind(window);
            window.fetch = async (...args) => {
                const response = await originalFetch(...args);
                if (!String(args[0]).endsWith('/api/example')) return response;
                const body = await response.arrayBuffer();
                const complete = new Response(body, {
                    status: response.status, headers: response.headers,
                });
                return new Promise(resolve => {
                    window.releaseExample = () => resolve(complete);
                });
            };
        }"""
    )
    page.locator("#load-example").click()
    page.wait_for_function("() => typeof window.releaseExample === 'function'")
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)
    page.evaluate("window.releaseExample()")
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )
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
    load_example(page, expect)
    roll = page.locator('[data-series="0"]')
    roll.focus()
    page.keyboard.press("Space")
    expect(roll).to_have_attribute("aria-pressed", "false")
    page.wait_for_function("document.querySelector('#attitude-plot').data[0].visible === false")
    expect(page.locator("#record-count")).to_have_text("12")
    page.evaluate(
        "() => Plotly.relayout(document.querySelector('#attitude-plot'), "
        "{'xaxis3.range': [2, 4], 'xaxis3.autorange': false})"
    )
    page.locator("#reset-chart").click()
    page.wait_for_function(
        "document.querySelector('#attitude-plot')._fullLayout.xaxis3.autorange === true"
    )
    expect(page.locator("#sample-count")).to_have_text("2")
    expect(page.locator("#capture-origin")).to_have_text("1700000000000000")
    page.locator("#focus-chart").click()
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#attitude-plot")).to_be_visible()
    page.locator("#focus-chart").click()
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "false")
    page.locator('#source-list button[data-source="2:1"]').click()
    expect(roll).to_have_attribute("aria-pressed", "true")
    page.wait_for_function("document.querySelector('#attitude-plot').data[0].visible === true")
    expect(page.locator("#sample-count")).to_have_text("5")
    page.locator("#focus-chart").click()
    page.locator("#clear-recording").click()
    assert_cleared(page, expect)
    expect(page.locator("#focus-chart")).to_have_attribute("aria-pressed", "false")
    assert not page.locator("body").evaluate("body => body.classList.contains('is-focused')")
    load_example(page, expect)
    expect(page.locator('#source-list button[data-source="1:1"]')).to_be_visible()


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


@pytest.mark.parametrize("width", [1440, 1024, 390, 320])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_loaded_workspace_fits_and_remains_readable(instrument_page, width, theme):
    page, expect = instrument_page
    page.set_viewport_size({"width": width, "height": 1000})
    page.locator("#theme-select").select_option(theme)
    load_example(page, expect)
    page.wait_for_function(
        "document.querySelector('#attitude-plot .scatterlayer path.point') !== null"
    )
    bounds = page.evaluate(
        """() => ({
            viewport: innerWidth,
            document: document.documentElement.scrollWidth,
            controls: [...document.querySelectorAll('button, select, summary')]
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
    contrast = page.evaluate(
        """() => {
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
                '#source-list button[aria-pressed="true"]',
                '#source-list button[aria-pressed="false"]',
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
            const graph = document.querySelector('#attitude-plot');
            const plotBackground = graph.querySelector('.bglayer .bg');
            const backdrop = plotBackground
                ? over(rgba(getComputedStyle(plotBackground).fill), background(graph))
                : background(graph);
            for (const point of graph.querySelectorAll('.scatterlayer .trace path.point')) {
                results.push({
                    selector: 'attitude marker',
                    ratio: ratio(over(rgba(getComputedStyle(point).fill), backdrop), backdrop),
                    minimum: 3,
                });
            }
            for (const tick of graph.querySelectorAll('.xtick text, .ytick text')) {
                results.push({
                    selector: 'attitude axis tick',
                    ratio: ratio(over(rgba(getComputedStyle(tick).fill), backdrop), backdrop),
                    minimum: 4.5,
                });
            }
            return results;
        }"""
    )
    assert all(item["ratio"] >= item["minimum"] for item in contrast), contrast
    expect(page.locator("#attitude-plot .scatterlayer path.point")).to_have_count(6)
    expect(page.locator("#record-count")).to_have_text("12")
