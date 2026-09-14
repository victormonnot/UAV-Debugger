"""Streamlit state checks complement real browser upload/download coverage."""

import io
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from uav_debugger import import_bytes

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "uav_debugger" / "app.py"
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
        app.selectbox(key="analysis_source").set_value((1, 1))
        app.selectbox(key="analysis_type").set_value(30)
        app.button[0].click().run()
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
        app.text_input(key="analysis_start").set_value("5")
        app.text_input(key="analysis_end").set_value("1")
        app.button[0].click().run()
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
        app.selectbox(key="analysis_source").set_value((1, 1))
        app.selectbox(key="analysis_type").set_value(30)
        app.button[0].click().run()
        assert not app.exception
        assert any("capture clock moves backward" in warning.value for warning in app.warning)
        assert app.session_state.analysis_view_intervals[0].delta_us is None
