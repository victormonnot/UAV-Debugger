"""The browser launch boundary preserves file-only Analyze and owned shutdown."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_recording_analyze_does_not_import_execution_or_create_output(tmp_path):
    root = tmp_path / "unused-experiments"
    script = """
import importlib.abc
import sys
from importlib.resources import files
from pathlib import Path
from streamlit.testing.v1 import AppTest

class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args, **kwargs):
        if fullname in {
            'uav_debugger.experiment', 'uav_debugger.experiment_control',
            'uav_debugger.experiment_worker', 'uav_debugger.experiment_view',
            'uav_debugger.sitl',
        }:
            raise AssertionError('Execution module imported by Analyze: ' + fullname)

sys.meta_path.insert(0, Guard())
app = AppTest.from_file(files('uav_debugger').joinpath('app.py')).run()
assert not app.exception
next(button for button in app.button if button.label == 'Load example').click().run()
assert not app.exception
assert app.session_state.import_result.decoded_count == 12
assert not Path(sys.argv[1]).exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env={**os.environ, "UAV_DEBUGGER_EXPERIMENT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_entering_experiment_without_start_never_launches_or_creates_outputs(tmp_path):
    root = tmp_path / "unused-experiments"
    script = """
import sys
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

app = AppTest.from_file(files('uav_debugger').joinpath('app.py')).run()
with patch('subprocess.Popen', side_effect=AssertionError('Unexpected process launch')):
    next(widget for widget in app.radio if widget.label == 'Mode').set_value('Experiment').run()
    assert not app.exception
    scenario = next(widget for widget in app.selectbox if widget.label == 'Scenario')
    scenario.set_value('blackout').run()
    assert not app.exception
    app.run()
    assert not app.exception
assert not Path(sys.argv[1]).exists()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        env={**os.environ, "UAV_DEBUGGER_EXPERIMENT_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("raises", [False, True])
def test_launcher_closes_loaded_controller_even_when_streamlit_exits(monkeypatch, tmp_path, raises):
    from uav_debugger.analyze import main

    closed = []
    module = SimpleNamespace(shutdown_all=lambda: closed.append(True))
    monkeypatch.setitem(sys.modules, "uav_debugger.experiment_control", module)
    monkeypatch.setattr(sys, "argv", ["uav-debugger-analyze"])
    monkeypatch.setenv("UAV_DEBUGGER_EXPERIMENT_ROOT", "previous")
    monkeypatch.setenv("UAV_DEBUGGER_UI_SITL_BINARY", "previous")

    def fake_cli():
        assert os.environ["UAV_DEBUGGER_EXPERIMENT_ROOT"] == str(tmp_path / "outputs")
        assert "UAV_DEBUGGER_UI_SITL_BINARY" not in os.environ
        assert not (tmp_path / "outputs").exists()
        if raises:
            raise SystemExit(0)
        return 0

    monkeypatch.setattr("streamlit.web.cli.main", fake_cli)
    if raises:
        with pytest.raises(SystemExit):
            main(["--experiment-root", str(tmp_path / "outputs")])
    else:
        assert main(["--experiment-root", str(tmp_path / "outputs")]) == 0
    assert closed == [True]
