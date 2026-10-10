"""Keyboard journeys and real Chromium browser-zoom integration checks."""

import base64
import json
import os
from urllib.parse import urlsplit

import pytest
from test_instrument_browser import (
    FIXTURE,
    ROOT,
    hold_next_response,
    load_example,
    release_response,
)
from test_instrument_comparison_browser import (
    comparison,
    constructed_pair,
    open_uploaded_pair,
)
from test_instrument_experiment_browser import experiment_root as experiment_root
from test_instrument_experiment_browser import finished, open_experiment, start
from test_instrument_experiment_browser import instrument_server as instrument_server
from test_instrument_runs_browser import await_run, upload_run, write_run

pytestmark = pytest.mark.browser
ARTIFACTS = ROOT / "local" / "instrument-brick7" / "browser"


@pytest.fixture
def integration_page(instrument_server, tmp_path, request):
    from playwright.sync_api import expect, sync_playwright

    extension = tmp_path / "zoom-extension"
    extension.mkdir()
    (extension / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Instrument browser zoom verification",
                "version": "1.0",
                "permissions": ["tabs"],
                "background": {"service_worker": "background.js"},
            }
        ),
        encoding="utf-8",
    )
    (extension / "background.js").write_text(
        "chrome.runtime.onInstalled.addListener(() => {});\n", encoding="utf-8"
    )
    unexpected = []
    errors = []

    def local(url):
        parsed = urlsplit(url)
        return parsed.scheme in {"data", "blob", "about", "chrome-extension"} or (
            parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        )

    def route_request(route):
        if local(route.request.url):
            route.continue_()
        else:
            unexpected.append(route.request.url)
            route.abort()

    def route_socket(socket):
        if local(socket.url):
            socket.connect_to_server()
        else:
            unexpected.append(socket.url)
            socket.close()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            tmp_path / "browser-profile",
            channel="chromium",
            headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
            viewport={"width": 1440, "height": 1000},
            color_scheme="dark",
        )
        context.route("**/*", route_request)
        context.route_web_socket("**/*", route_socket)
        worker = (
            context.service_workers[0]
            if context.service_workers
            else (context.wait_for_event("serviceworker"))
        )
        page = context.pages[0]
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.set_default_timeout(15_000)
        expect.set_options(timeout=15_000)
        try:
            page.goto(instrument_server, wait_until="networkidle")
            expect(page.locator("#load-example")).to_be_visible()
            yield page, expect, worker
        finally:
            if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
                ARTIFACTS.mkdir(parents=True, exist_ok=True)
                capture_full_page(page, ARTIFACTS / f"{request.node.name}.png")
            context.close()
        assert not unexpected, unexpected
        assert not errors, errors


def zoom(page, worker, factor):
    before = page.evaluate("({width: innerWidth, dpr: devicePixelRatio})")
    applied = worker.evaluate(
        """async ({url, factor}) => {
            const tab = (await chrome.tabs.query({})).find(tab => tab.url === url);
            await chrome.tabs.setZoom(tab.id, factor);
            return chrome.tabs.getZoom(tab.id);
        }""",
        {"url": page.url, "factor": factor},
    )
    assert applied == factor
    page.wait_for_function("width => innerWidth === width", arg=before["width"] / factor)
    metrics = page.evaluate(
        "({width: innerWidth, dpr: devicePixelRatio, scale: visualViewport.scale})"
    )
    assert metrics == {
        "width": before["width"] / factor,
        "dpr": before["dpr"] * factor,
        "scale": 1,
    }


def tab_to(page, selector):
    target = page.locator(selector)
    for _ in range(120):
        if target.evaluate("element => element === document.activeElement"):
            return target
        page.keyboard.press("Tab")
    pytest.fail(f"Control is not reachable by Tab: {selector}")


def key_activate(page, selector):
    tab_to(page, selector)
    page.keyboard.press("Enter")


def key_select(page, selector, value):
    control = tab_to(page, selector)
    values = control.locator("option").evaluate_all(
        "options => options.map(option => option.value)"
    )
    page.keyboard.press("Home")
    for _ in range(values.index(value)):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")


def key_text(page, selector, value):
    tab_to(page, selector)
    page.keyboard.press("Control+a")
    page.keyboard.insert_text(value)


def assert_visible_focus(page):
    focused = page.evaluate("""() => {
        const element = document.activeElement;
        return {id: element.id, tag: element.tagName, visible: element.checkVisibility(),
            outline: getComputedStyle(element).outlineStyle};
    }""")
    assert focused["tag"] not in {"BODY", "HTML"} and focused["visible"], focused
    return focused


def test_keyboard_recording_filter_inspector_and_download_keep_visible_focus(
    integration_page, tmp_path
):
    page, expect, _ = integration_page
    page.locator("#open-recording").focus()
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Enter")
    chooser.value.set_files(FIXTURE)
    expect(page.locator("#record-count")).to_have_text("12")
    expect(page.locator("#open-recording")).to_be_focused()
    key_select(page, "#source-filter", "1:1")
    key_select(page, "#message-filter", "30")
    key_text(page, "#start-filter", "1")
    key_text(page, "#end-filter", "5")
    page.keyboard.press("Enter")
    expect(page.locator("#selected-count")).to_have_text("2")
    assert_visible_focus(page)
    tab_to(page, "#activity-tab")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#attitude-tab")).to_be_focused()
    page.keyboard.press("ArrowRight")
    expect(page.locator("#messages-tab")).to_be_focused()
    key_activate(page, '#message-rows button[data-record-index="8"]')
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    expect(page.locator('#message-rows button[data-record-index="8"]')).to_be_focused()
    tab_to(page, "#download-report")
    with page.expect_download() as pending:
        page.keyboard.press("Enter")
    report = tmp_path / "keyboard-report.md"
    pending.value.save_as(report)
    assert '"index": 8' in report.read_text()
    expect(page.locator("#download-report")).to_be_enabled()
    assert_visible_focus(page)


def test_keyboard_catalog_handoff_places_focus_in_visible_saved_analysis(
    integration_page, experiment_root
):
    page, expect, _ = integration_page
    files = constructed_pair()["baseline"]
    write_run(experiment_root / "baseline", files)
    page.locator("#catalog-input-tab").focus()
    page.keyboard.press("Enter")
    button = page.get_by_role("button", name="Open baseline in Analyze", exact=True)
    expect(button).to_be_enabled()
    button.focus()
    page.keyboard.press("Enter")
    await_run(page, expect, files)
    assert_visible_focus(page)


def test_keyboard_experiment_handoff_places_focus_in_visible_saved_analysis(
    integration_page, experiment_root
):
    page, expect, _ = integration_page
    open_experiment(page, expect)
    directory = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, directory)
    page.locator("#experiment-open-analyze").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#run-view")).to_be_visible()
    expect(page.locator("#run-outcome")).to_have_text("completed")
    assert_visible_focus(page)


def test_keyboard_comparison_upload_filter_report_and_inspection_keep_visible_focus(
    integration_page, tmp_path
):
    page, expect, _ = integration_page
    # Keep interception enabled between the two keyboard-opened folder choosers.
    # Removing the last listener makes Playwright disable it asynchronously.
    opened_choosers = []
    page.on("filechooser", lambda chooser: opened_choosers.append(chooser))
    page.locator("#recording-input-tab").focus()
    page.keyboard.press("End")
    expect(page.locator("#comparison-input-tab")).to_be_focused()
    files = constructed_pair()
    for role in ("baseline", "blackout"):
        directory = write_run(tmp_path / role, files[role])
        tab_to(page, f"#comparison-open-{role}")
        with page.expect_file_chooser() as chooser:
            page.keyboard.press("Enter")
        assert chooser.value.element.get_attribute("id") == f"comparison-{role}-files"
        chooser.value.set_files(directory)
        expect(page.locator(f"#comparison-{role}-name")).to_have_text(role)
    assert len(opened_choosers) == 2
    comparison(page, expect)
    assert_visible_focus(page)
    key_text(page, "#comparison-start", "0.5")
    key_text(page, "#comparison-end", "2.3")
    page.keyboard.press("Enter")
    expect(page.locator("#comparison-window")).to_contain_text("[0.500000000, 2.300000000)")
    assert_visible_focus(page)
    key_select(page, "#comparison-point", "relay-input")
    expect(page.locator("#comparison-point")).to_have_value("relay-input")
    expect(page.locator("#comparison-download-report")).to_be_enabled()
    tab_to(page, "#comparison-download-report")
    with page.expect_download() as pending:
        page.keyboard.press("Enter")
    report = tmp_path / "keyboard-comparison.md"
    pending.value.save_as(report)
    assert '"start_ns": 500000000' in report.read_text()
    expect(page.locator("#comparison-download-report")).to_be_enabled()
    assert_visible_focus(page)
    key_activate(page, "#comparison-inspect-blackout")
    await_run(page, expect, files["blackout"])
    assert_visible_focus(page)


@pytest.mark.parametrize("mode", ["analyze", "experiment"])
def test_skip_link_targets_the_visible_workspace_and_classic_is_optional(integration_page, mode):
    page, expect, _ = integration_page
    if mode == "experiment":
        open_experiment(page, expect)
    page.locator(".skip-link").focus()
    expect(page.locator(".skip-link")).to_have_text("Skip to workspace")
    page.keyboard.press("Enter")
    expect(page.locator("#main")).to_be_focused()
    assert_visible_focus(page)
    expect(page.locator("#full-workspace")).to_be_hidden()


def test_filter_completion_does_not_steal_focus_moved_during_request(integration_page):
    page, expect, _ = integration_page
    load_example(page, expect)
    page.locator("#end-filter").fill("5")
    hold_next_response(page, "/api/example")
    page.locator("#end-filter").focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => typeof window.releaseResponse === 'function'")
    page.locator("#theme-select").focus()
    release_response(page)
    expect(page.locator("#selected-count")).to_have_text("10")
    expect(page.locator("#theme-select")).to_be_focused()


def assert_surface(page, selectors):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    contrast = page.evaluate(
        """selectors => {
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
            return selectors.map(selector => {
                const element = document.querySelector(selector), style = getComputedStyle(element);
                const backdrop = background(element), foreground = rgba(style.color);
                let alpha = foreground[3] ?? 1;
                for (let current = element; current; current = current.parentElement) {
                    alpha *= Number(getComputedStyle(current).opacity);
                }
                foreground[3] = alpha;
                const values = [luminance(over(foreground, backdrop)), luminance(backdrop)]
                    .sort((a, b) => a - b);
                return {selector, visible: element.checkVisibility(),
                    ratio: (values[1] + .05) / (values[0] + .05)};
            });
        }""",
        selectors,
    )
    assert all(item["visible"] and item["ratio"] >= 4.5 for item in contrast), contrast


def photograph(page, name):
    if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        page.evaluate("() => { scrollTo(0, 0); return new Promise(requestAnimationFrame); }")
        capture_full_page(page, ARTIFACTS / f"{name}.png")


def capture_full_page(page, path):
    session = page.context.new_cdp_session(page)
    try:
        # Screenshot clip units include browser zoom; CSS dimensions crop a zoomed page.
        content = session.send("Page.getLayoutMetrics")["contentSize"]
        encoded = session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {**content, "scale": 1},
            },
        )["data"]
        pixels = base64.b64decode(encoded)
        assert int.from_bytes(pixels[16:20], "big") == content["width"]
        assert int.from_bytes(pixels[20:24], "big") == content["height"]
        path.write_bytes(pixels)
    finally:
        session.detach()


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("viewport_width,factor", [(1440, 1), (320, 1), (1440, 2), (640, 2)])
def test_every_workspace_reflows_under_real_browser_zoom_and_narrow_viewports(
    integration_page, experiment_root, tmp_path, theme, viewport_width, factor
):
    page, expect, worker = integration_page
    page.set_viewport_size({"width": viewport_width, "height": 1000})
    zoom(page, worker, factor)
    page.locator("#theme-select").select_option(theme)
    suffix = f"{theme}-{viewport_width}-zoom{factor}"
    load_example(page, expect)
    assert_surface(page, ["#recording-name", "#filter-state", "#applied-range", "#import-status"])
    assert page.locator("#activity-plot").evaluate("plot => plot.data.length > 0")
    photograph(page, "recording-" + suffix)

    page.locator("#messages-tab").click()
    page.locator('#message-rows button[data-record-index="8"]').click()
    expect(page.locator("#inspector-title")).to_have_text("Record #8")
    assert_surface(page, ["#messages-range", "#inspector-title", "#inspector-capture"])
    photograph(page, "messages-" + suffix)

    files = constructed_pair()
    directory = write_run(tmp_path / "uploaded-baseline", files["baseline"])
    upload_run(page, directory)
    await_run(page, expect, files["baseline"])
    page.locator("#run-provenance-details summary").click()
    assert_surface(page, ["#run-outcome", "#run-evidence-status", "#run-identity", "#capture-name"])
    page.wait_for_function("document.querySelector('#run-timeline').data?.length > 0")
    photograph(page, "saved-" + suffix)

    for role in ("baseline", "blackout"):
        write_run(experiment_root / role, files[role])
    page.locator("#catalog-input-tab").click()
    expect(page.locator("#catalog-rows tr")).to_have_count(2)
    assert_surface(page, ["#catalog-root", "#catalog-page", "#catalog-input-tab"])
    photograph(page, "catalog-" + suffix)

    open_uploaded_pair(page, expect, tmp_path, files)
    comparison(page, expect)
    assert_surface(page, ["#comparison-status", "#comparison-window", "#comparison-point"])
    photograph(page, "comparison-" + suffix)

    open_experiment(page, expect)
    finished_run = start(page, expect, experiment_root, duration=0.2)
    finished(page, expect, finished_run)
    page.locator("#experiment-requested-details summary").click()
    assert (
        json.loads(page.locator("#experiment-requested").inner_text())
        == json.loads((finished_run.parent / "control.json").read_bytes())["requested"]
    )
    page.locator("#experiment-details summary").click()
    expect(page.locator("#experiment-control-file")).to_have_text(
        str(finished_run.parent / "control.json")
    )
    assert_surface(
        page, ["#experiment-connection", "#experiment-process-state", "#experiment-clocks"]
    )
    photograph(page, "experiment-" + suffix)
