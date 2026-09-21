"""Explicit browser verification must not succeed by skipping missing tooling."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("browser_requested", [False, True])
def test_missing_playwright_is_optional_only_without_browser_opt_in(tmp_path, browser_requested):
    (tmp_path / "conftest.py").write_bytes(Path(__file__).with_name("conftest.py").read_bytes())
    (tmp_path / "playwright.py").write_text(
        "raise ImportError('Playwright is absent for this test')\n"
    )
    (tmp_path / "test_browser.py").write_text(
        "import pytest\n"
        "@pytest.mark.browser\n"
        "def test_browser():\n"
        "    raise AssertionError('Browser check must not run without Playwright')\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *(["--run-browser"] if browser_requested else [])],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    if browser_requested:
        assert result.returncode == pytest.ExitCode.USAGE_ERROR
        assert "Browser checks were requested but Playwright could not be imported" in result.stderr
        assert "skipped" not in result.stdout
    else:
        assert result.returncode == pytest.ExitCode.OK
        assert "1 skipped" in result.stdout
