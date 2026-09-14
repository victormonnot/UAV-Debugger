"""Opt-in browser checks for the local Analyze application."""

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

ROOT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser):
    parser.addoption(
        "--run-browser",
        action="store_true",
        default=False,
        help="Run real Chromium Analyze tests (requires the browser dependency group).",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "browser: real browser workflow, enabled by --run-browser")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-browser"):
        return
    skip = pytest.mark.skip(reason="Browser workflow requires --run-browser")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="module")
def analyze_server(tmp_path_factory):
    pytest.importorskip("playwright.sync_api", reason="Install the browser dependency group")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("analyze-server") / "streamlit.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "uav_debugger.analyze", "--port", str(port)],
            cwd=ROOT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(f"Analyze server exited:\n{log_path.read_text(errors='replace')}")
                try:
                    with urlopen(f"{base_url}/_stcore/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail(
                    f"Analyze server did not start:\n{log_path.read_text(errors='replace')}"
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
def analyze_page(analyze_server, request):
    api = pytest.importorskip("playwright.sync_api")
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
            accept_downloads=True,
            service_workers="block",
            viewport={"width": 1440, "height": 1100},
        )
        context.route("**/*", route_request)
        context.route_web_socket("**/*", route_socket)
        page = context.new_page()
        page.set_default_timeout(15_000)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            page.goto(analyze_server, wait_until="domcontentloaded")
            api.expect(page.get_by_role("heading", name="Analyze", exact=True)).to_be_visible()
            yield page, api.expect
            api.expect(page.get_by_test_id("stException")).to_have_count(0)
        finally:
            try:
                if os.environ.get("UAV_DEBUGGER_BROWSER_SCREENSHOTS") == "1":
                    output_dir = ROOT / "local" / "browser"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=output_dir / f"{request.node.name}.png", full_page=True)
            finally:
                context.close()
                browser.close()
        assert not external_requests, f"Unexpected external requests: {external_requests}"
        assert not page_errors, f"Browser JavaScript errors: {page_errors}"
