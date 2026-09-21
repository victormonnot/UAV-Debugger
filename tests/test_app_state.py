"""Streamlit state checks complement real browser upload/download coverage."""

import io
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from uav_debugger import import_bytes

ROOT = Path(__file__).resolve().parents[1]
# Use the active installation so wheel verification also exercises its UI code.
APP = files("uav_debugger").joinpath("app.py")
FIXTURE = ROOT / "tests" / "fixtures" / "telemetry-gap.tlog"


class Upload(io.BytesIO):
    name = "telemetry-gap.tlog"


def test_import_is_retained_when_filters_and_record_selection_change():
    with (
        patch("streamlit.file_uploader", return_value=Upload(FIXTURE.read_bytes())),
        patch("uav_debugger.import_bytes", wraps=import_bytes) as parse,
    ):
        app = AppTest.from_file(APP).run()
        assert not app.exception
        assert parse.call_count == 1
        gap = next(box for box in app.text_input if box.label == "Maximum line gap (s)")
        gap.set_value("5").run()
        assert not app.exception
        assert app.session_state.analysis_attitude_view.max_gap_us == 5_000_000
        gap.set_value("0").run()
        assert not app.exception
        assert app.session_state.analysis_attitude_view.max_gap_us == 5_000_000
        assert any("Line gap was not applied" in error.value for error in app.error)
        assert parse.call_count == 1
        next(box for box in app.selectbox if box.label == "Source").set_value((1, 1))
        next(box for box in app.selectbox if box.label == "Message type").set_value(30)
        next(button for button in app.button if button.label == "Apply filters").click().run()
        assert not app.exception
        assert parse.call_count == 1
        assert [record.index for record in app.session_state.analysis_view_records] == [2, 8]
        next(box for box in app.selectbox if box.label == "Record").set_value(8).run()
        assert not app.exception
        assert parse.call_count == 1


def test_invalid_time_filter_keeps_the_applied_selection_and_reports_error():
    with patch("streamlit.file_uploader", return_value=Upload(FIXTURE.read_bytes())):
        app = AppTest.from_file(APP).run()
        applied = app.session_state.analysis_selection
        next(box for box in app.text_input if box.label == "Start (s)").set_value("5")
        next(box for box in app.text_input if box.label == "End (s)").set_value("1")
        next(button for button in app.button if button.label == "Apply filters").click().run()
        assert not app.exception
        assert app.session_state.analysis_selection == applied
        assert len(app.session_state.analysis_view_records) == 12
        assert any("Start must not be later than End" in error.value for error in app.error)


def test_clearing_a_recording_removes_its_evidence_from_session_state():
    with patch("streamlit.file_uploader", return_value=Upload(FIXTURE.read_bytes())) as upload:
        app = AppTest.from_file(APP).run()
        assert not app.exception
        assert app.session_state.import_result.decoded_count == 12
        upload.return_value = None
        app.run()
        assert not app.exception
        assert "import_result" not in app.session_state
        assert "analysis_selection" not in app.session_state
        assert not app.metric


def test_clock_regression_is_visible_even_outside_the_current_filters():
    data = bytearray(FIXTURE.read_bytes())
    data[134:142] = (1_699_999_999_999_999).to_bytes(8, "big")
    with patch("streamlit.file_uploader", return_value=Upload(data)):
        app = AppTest.from_file(APP).run()
        next(box for box in app.selectbox if box.label == "Source").set_value((1, 1))
        next(box for box in app.selectbox if box.label == "Message type").set_value(30)
        next(button for button in app.button if button.label == "Apply filters").click().run()
        assert not app.exception
        assert any("capture clock moves backward" in warning.value for warning in app.warning)
        assert app.session_state.analysis_view_intervals[0].delta_us is None


def test_bundled_example_uses_the_same_importer_and_can_be_cleared():
    with (
        patch("streamlit.file_uploader", return_value=None),
        patch("uav_debugger.import_bytes", wraps=import_bytes) as parse,
    ):
        app = AppTest.from_file(APP).run()
        assert not app.metric
        next(button for button in app.button if button.label == "Load example").click().run()
        assert not app.exception
        assert parse.call_count == 1
        assert app.session_state.import_result.raw_bytes == FIXTURE.read_bytes()
        assert (
            app.session_state.import_result.source_name == "telemetry-gap.tlog (synthetic example)"
        )
        assert len(app.session_state.analysis_view_records) == 12
        next(box for box in app.selectbox if box.label == "Source").set_value((1, 1))
        next(box for box in app.selectbox if box.label == "Message type").set_value(30)
        next(button for button in app.button if button.label == "Apply filters").click().run()
        assert not app.exception
        assert parse.call_count == 1
        assert [record.index for record in app.session_state.analysis_view_records] == [2, 8]
        next(button for button in app.button if button.label == "Clear example").click().run()
        assert not app.exception
        assert "import_result" not in app.session_state
        assert "analysis_selection" not in app.session_state
        assert not app.metric


def test_attitude_capacity_limit_can_be_resolved_with_time_filters():
    frame = import_bytes(FIXTURE.read_bytes()).records[2].raw_frame
    origin = 1_700_000_000_000_000
    data = b"".join((origin + index * 1_000).to_bytes(8, "big") + frame for index in range(5_001))
    with patch("streamlit.file_uploader", return_value=Upload(data)):
        app = AppTest.from_file(APP, default_timeout=10).run()
        assert not app.exception
        assert app.session_state.analysis_attitude_view.status == "too_many"
        assert app.session_state.analysis_attitude_chart is None
        assert len(app.session_state.analysis_view_records) == 5_001
        assert any("at most 5,000" in info.value for info in app.info)
        next(box for box in app.text_input if box.label == "End (s)").set_value("0.001")
        next(button for button in app.button if button.label == "Apply filters").click().run()
        assert not app.exception
        assert app.session_state.analysis_attitude_view.status == "ready"
        assert [
            sample.record.index for sample in app.session_state.analysis_attitude_view.samples
        ] == [0, 1]
