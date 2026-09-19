"""Local Streamlit presentation of the file-only Analyze workflow."""

import hashlib
import json
from collections import Counter
from importlib.resources import files

import streamlit as st

from uav_debugger import InputTooLargeError, import_bytes
from uav_debugger.analysis import (
    Selection,
    observed_intervals,
    seconds_to_timestamp,
    select_records,
    timestamp_to_seconds,
)
from uav_debugger.charts import activity_chart, attitude_chart
from uav_debugger.model import ImportResult, Record
from uav_debugger.report import build_markdown_report
from uav_debugger.telemetry import build_attitude_view

PAGE_SIZE = 100
ISSUE_PAGE_SIZE = 100
ALL_SOURCES = "All sources"
ALL_MESSAGES = -1


def _message_label(record: Record) -> str:
    return record.message_name or f"UNKNOWN_{record.message_id}"


def _use_example() -> None:
    st.session_state.example_active = True
    # A fresh uploader prevents an earlier file from remaining visibly selected.
    st.session_state.upload_generation = st.session_state.get("upload_generation", 0) + 1


def _use_upload() -> None:
    st.session_state.example_active = False


def _recording_widget_key(name: str) -> str:
    # New input must also invalidate frontend widget values sent with an upload.
    identity = hashlib.sha256(st.session_state.import_token.encode()).hexdigest()
    return f"analysis_{name}_{identity}"


def _load_recording(data: bytes, source_name: str) -> tuple[ImportResult, str]:
    token = hashlib.sha256(data).hexdigest() + ":" + source_name
    if st.session_state.get("import_token") != token:
        # Retain one recording per browser session, with no shared/disk cache.
        for key in list(st.session_state):
            if key.startswith("analysis_"):
                del st.session_state[key]
        st.session_state.pop("import_result", None)
        st.session_state.pop("import_token", None)
        with st.spinner("Reading the recording…"):
            result = import_bytes(data, source_name=source_name)
        st.session_state.import_result = result
        st.session_state.import_token = token
        st.session_state.analysis_sources = sorted(
            {(record.system_id, record.component_id) for record in result.records}
        )
        st.session_state.analysis_types = dict(
            sorted({record.message_id: _message_label(record) for record in result.records}.items())
        )
        if result.records:
            st.session_state.analysis_bounds = (
                min(record.timestamp_us for record in result.records),
                max(record.timestamp_us for record in result.records),
            )
    return st.session_state.import_result, token


def _filters(result: ImportResult) -> Selection:
    low, high = st.session_state.analysis_bounds
    origin = result.records[0].timestamp_us
    sources = st.session_state.analysis_sources
    types = st.session_state.analysis_types
    if "analysis_selection" not in st.session_state:
        st.session_state.analysis_selection = Selection(start_us=low, end_us=high)
    with st.sidebar.form(_recording_widget_key("filters")):
        st.subheader("Filter observations")
        source = st.selectbox(
            "Source",
            [ALL_SOURCES, *sources],
            format_func=lambda value: value if value == ALL_SOURCES else f"{value[0]} / {value[1]}",
            key=_recording_widget_key("source"),
        )
        message_id = st.selectbox(
            "Message type",
            [ALL_MESSAGES, *types],
            format_func=lambda value: (
                "All message types" if value == ALL_MESSAGES else types[value]
            ),
            key=_recording_widget_key("type"),
        )
        start = st.text_input(
            "Start (s)", value=timestamp_to_seconds(low, origin), key=_recording_widget_key("start")
        )
        end = st.text_input(
            "End (s)", value=timestamp_to_seconds(high, origin), key=_recording_widget_key("end")
        )
        st.caption("Seconds from the first imported record. Both bounds are included.")
        submitted = st.form_submit_button("Apply filters", type="primary", width="stretch")
    if submitted:
        try:
            start_us = seconds_to_timestamp(start, origin)
            end_us = seconds_to_timestamp(end, origin)
            if start_us > end_us:
                raise ValueError("Start must not be later than End.")
            selection = Selection(
                sources=None if source == ALL_SOURCES else (source,),
                message_ids=None if message_id == ALL_MESSAGES else (message_id,),
                start_us=start_us,
                end_us=end_us,
            )
            # Core validation is shared with programmatic analysis and reports.
            select_records(result, selection)
        except ValueError as error:
            st.sidebar.error(f"Filters were not applied: {error}")
        else:
            st.session_state.analysis_selection = selection
    return st.session_state.analysis_selection


def _provenance(result: ImportResult) -> None:
    if result.traversal == "stopped":
        st.warning(
            f"Import stopped. {len(result.records)} records remain inspectable; "
            f"{result.remaining_bytes:,} bytes were not processed. See import issues below."
        )
    elif result.traversal == "empty":
        st.warning("The file is empty. No records were imported.")
    elif result.opaque_count:
        st.warning(
            f"File traversal completed with {result.opaque_count} opaque records. "
            "Their fields, checksum and framing are unverified."
        )
    else:
        st.caption("Import complete · all accepted frames decoded")
    regressions = sum(issue.code == "timestamp_regression" for issue in result.issues)
    if regressions:
        st.warning(
            f"The capture clock moves backward at {regressions} records. "
            "Time bins can overlap across clock segments; intervals crossing a regression "
            "are unavailable. Inspect the original record order."
        )
    with st.expander("Recording provenance"):
        st.text(result.source_name)
        st.code(result.sha256, language=None)
        st.write(
            {
                "Input bytes": len(result.raw_bytes),
                "Profile": result.profile,
                "Dialect": result.dialect,
                "Decoder": f"pymavlink {result.decoder_version}",
                "Traversal": result.traversal,
                "Consumed bytes": result.consumed_bytes,
                "Remaining bytes": result.remaining_bytes,
            }
        )
        st.caption(
            "Capture timestamps are unsigned Unix microseconds under the selected host-logging "
            "profile. Accuracy, resolution and synchronization are unknown. "
            "Device timestamps remain separate."
        )


def _issues(result: ImportResult) -> None:
    if not result.issues:
        return
    with st.expander(f"Import issues ({len(result.issues):,})"):
        counts = Counter(issue.code for issue in result.issues)
        st.text(" · ".join(f"{name}: {count:,}" for name, count in counts.items()))
        pages = (len(result.issues) + ISSUE_PAGE_SIZE - 1) // ISSUE_PAGE_SIZE
        page = st.number_input("Issues page", 1, pages, 1, key=_recording_widget_key("issue_page"))
        start = (page - 1) * ISSUE_PAGE_SIZE
        st.dataframe(
            [
                {
                    "Record": issue.record_index,
                    "Byte offset": issue.offset,
                    "Severity": issue.severity,
                    "Issue": issue.code,
                    "Details": issue.message,
                }
                for issue in result.issues[start : start + ISSUE_PAGE_SIZE]
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Issues describe the whole imported file, including records outside the filters."
        )


def _attitude_view(result: ImportResult, selection: Selection) -> tuple[int | None, int]:
    st.subheader("Attitude")
    text = st.text_input(
        "Maximum line gap (s)",
        value="1",
        key=_recording_widget_key("attitude_gap"),
        help="Connect consecutive points only up to this capture-time interval. "
        "This is a display setting, not a packet-loss threshold.",
    )
    try:
        gap_us = seconds_to_timestamp(text, 0)
        if gap_us == 0:
            raise ValueError("Maximum line gap must be positive.")
    except ValueError as error:
        st.error(f"Line gap was not applied: {error}")
    else:
        st.session_state.analysis_attitude_gap_us = gap_us
    gap_us = st.session_state.get("analysis_attitude_gap_us", 1_000_000)
    cache_key = (selection, gap_us)
    if st.session_state.get("analysis_attitude_view_key") != cache_key:
        view = build_attitude_view(result, selection, max_gap_us=gap_us)
        st.session_state.analysis_attitude_view = view
        st.session_state.analysis_attitude_chart = (
            attitude_chart(view, origin_us=result.records[0].timestamp_us)
            if view.status == "ready"
            else None
        )
        st.session_state.analysis_attitude_view_key = cache_key
        st.session_state.analysis_attitude_event = None
    view = st.session_state.analysis_attitude_view
    st.caption(
        f"Applied maximum line gap: {timestamp_to_seconds(gap_us, 0)} s. "
        "Original ATTITUDE angles in radians, against the recording's capture time."
    )
    if view.status == "empty":
        st.info("No ATTITUDE records match the applied filters.")
    elif view.status == "multiple_sources":
        st.info("Select one source and apply filters to plot attitude.")
    elif view.status == "too_many":
        st.info(
            f"The selection contains {view.record_count:,} ATTITUDE records. "
            f"Narrow the time filters to at most {view.point_limit:,} to plot them. "
            "The message table and report still cover the full selection."
        )
    else:
        invalid = {
            field: sum(getattr(sample, field) is None for sample in view.samples)
            for field in ("roll", "pitch", "yaw")
        }
        if any(invalid.values()):
            st.warning(
                "Unplotted values (missing, nonfinite or unverified): "
                + ", ".join(f"{field}: {count:,}" for field, count in invalid.items())
                + ". Inspect the original records for details."
            )
        st.caption(
            f"Source {view.source[0]} / {view.source[1]} · "
            f"{view.record_count:,} ATTITUDE records. Click a point to inspect its message. "
            "Lines break at repeated or regressing capture times, longer gaps, unavailable "
            "values and angle jumps greater than π. Lines are visual guides; "
            "no angle unwrapping or clock alignment is applied. Zoom leaves filters unchanged."
        )
        chart_key = (
            _recording_widget_key("attitude_chart")
            + hashlib.sha256(repr(cache_key).encode()).hexdigest()
        )
        with st.container(key="attitude_plot"):
            event = st.plotly_chart(
                st.session_state.analysis_attitude_chart,
                width="stretch",
                theme=None,
                key=chart_key,
                on_select="rerun",
                selection_mode="points",
                config={"displaylogo": False, "scrollZoom": False},
            )
        points = event.selection.points
        signature = json.dumps(points, sort_keys=True)
        if signature != st.session_state.get("analysis_attitude_event"):
            st.session_state.analysis_attitude_event = signature
            indices = {sample.record.index for sample in view.samples}
            for point in reversed(points):
                data = point.get("customdata")
                if isinstance(data, (list, tuple)) and data:
                    index = data[0]
                    if type(index) is int and index in indices:
                        return index, gap_us
    return None, gap_us


def _record_view(
    records: tuple[Record, ...],
    origin_us: int,
    selection: Selection,
    *,
    focused_record: int | None = None,
) -> int | None:
    st.subheader("Messages")
    if not records:
        st.info("No records match these filters.")
        return None
    pages = (len(records) + PAGE_SIZE - 1) // PAGE_SIZE
    page_key = _recording_widget_key("page") + hashlib.sha256(repr(selection).encode()).hexdigest()
    if focused_record is not None:
        position = next(
            index for index, record in enumerate(records) if record.index == focused_record
        )
        st.session_state[page_key] = position // PAGE_SIZE + 1
    page = st.number_input("Page", 1, pages, 1, key=page_key)
    start = (page - 1) * PAGE_SIZE
    page_records = records[start : start + PAGE_SIZE]
    st.caption(
        f"Showing {start + 1:,}–{start + len(page_records):,} of {len(records):,} records "
        "in original file order."
    )
    st.dataframe(
        [
            {
                "Record": record.index,
                "Time (s)": timestamp_to_seconds(record.timestamp_us, origin_us),
                "Source": f"{record.system_id} / {record.component_id}",
                "Message": _message_label(record),
                "Sequence": record.sequence,
                "Byte offset": record.offset,
                "Checksum": record.checksum_status,
            }
            for record in page_records
        ],
        hide_index=True,
        width="stretch",
        height=260,
    )
    by_index = {record.index: record for record in page_records}
    # A changed filter/page cannot leave an old record attached to the inspector.
    record_key = (
        _recording_widget_key("record")
        + hashlib.sha256(repr((selection, page)).encode()).hexdigest()
    )
    if focused_record is not None:
        st.session_state[record_key] = focused_record
    chosen = st.selectbox(
        "Record",
        list(by_index),
        format_func=lambda index: (
            f"#{index} · {_message_label(by_index[index])} · "
            f"{by_index[index].system_id} / {by_index[index].component_id}"
        ),
        key=record_key,
    )
    record = by_index[chosen]
    with st.container(border=True):
        st.subheader(f"Record #{record.index}")
        st.text(
            f"Capture timestamp: {record.timestamp_us} µs\n"
            f"Record bytes: [{record.offset}, {record.end_offset}) · "
            f"frame bytes: [{record.frame_offset}, {record.end_offset})\n"
            f"MAVLink {record.wire_version} · sequence {record.sequence} · "
            f"checksum {record.checksum_status}"
        )
        left, right = st.columns(2)
        with left:
            st.markdown("**Decoded fields**")
            if record.fields is None:
                st.info("No message definition is available. This record remains opaque.")
            else:
                st.code(
                    json.dumps(dict(record.fields), indent=2, ensure_ascii=True), language="json"
                )
        with right:
            st.markdown("**Raw frame**")
            st.code(record.raw_frame.hex(" "), language=None, wrap_lines=True)
            st.caption("Original bytes. Byte ranges and record indices are zero-based.")
    return chosen


def main() -> None:
    st.set_page_config(page_title="UAV Debugger · Analyze", page_icon="▥", layout="wide")
    st.markdown(
        "<style>.block-container{padding-top:2rem;max-width:1500px}"
        "[data-testid=stMetricValue]{font-variant-numeric:tabular-nums}"
        "h1{letter-spacing:-.04em}</style>",
        unsafe_allow_html=True,
    )
    with st.sidebar:
        st.title("UAV Debugger")
        st.caption("Recorded telemetry · local analysis")
        st.divider()
        st.button(
            "Load example",
            on_click=_use_example,
            disabled=st.session_state.get("example_active", False),
            width="stretch",
        )
        st.caption("Synthetic recording · 12 messages · 2 sources. No file needed.")
        if st.session_state.get("example_active", False):
            st.button("Clear example", on_click=_use_upload, width="stretch")
        uploaded = st.file_uploader(
            "Open recording",
            key=f"recording_upload_{st.session_state.get('upload_generation', 0)}",
            on_change=_use_upload,
            max_upload_size=11,
            help="QGroundControl-style timestamps + unsigned MAVLink 1/2, "
            "common dialect. Up to 10 MiB.",
        )
        st.caption("Analysis limit: 10 MiB (10,485,760 bytes).")
        # Remove the previous download before any new selection is rendered.
        # Large recordings can take time to produce the updated report.
        export_area = st.empty()
    st.title("Analyze")
    st.caption("Trace an observation back to its recorded evidence.")
    using_example = st.session_state.get("example_active", False)
    if uploaded is None and not using_example:
        for key in list(st.session_state):
            if key.startswith("analysis_") or key in ("import_result", "import_token"):
                del st.session_state[key]
        st.info("Upload a recording or choose Load example to start analyzing.")
        st.markdown(
            "Start with the supplied **telemetry-gap.tlog** example: two sources, "
            "12 messages and a known interval without observations from one source."
        )
        st.caption(
            "Files are processed on the computer running Analyze. "
            "Opening a file never connects to a vehicle."
        )
        return
    if using_example:
        st.info(
            "Synthetic example — telemetry-gap.tlog. "
            "Select source 1 / 1, ATTITUDE and 1–5 s, then Apply filters "
            "to inspect a four-second observation interval."
        )
    try:
        if using_example:
            data = files("uav_debugger").joinpath("data", "telemetry-gap.tlog").read_bytes()
            result, _ = _load_recording(data, "telemetry-gap.tlog (synthetic example)")
        else:
            result, _ = _load_recording(uploaded.getvalue(), uploaded.name)
    except (InputTooLargeError, ValueError) as error:
        st.error(str(error))
        return
    _provenance(result)
    if result.records:
        selection = _filters(result)
        if st.session_state.get("analysis_view_selection") != selection:
            records = select_records(result, selection)
            intervals = observed_intervals(result, selection)
            st.session_state.analysis_view_records = records
            st.session_state.analysis_view_intervals = intervals
            st.session_state.analysis_view_chart = activity_chart(
                records, origin_us=result.records[0].timestamp_us
            )
            st.session_state.analysis_view_selection = selection
        records = st.session_state.analysis_view_records
        intervals = st.session_state.analysis_view_intervals
    else:
        selection, records, intervals = Selection(), (), ()
    columns = st.columns(4)
    columns[0].metric("Imported records", f"{len(result.records):,}")
    columns[1].metric("Sources", str(len(st.session_state.analysis_sources)))
    columns[2].metric("Selected records", f"{len(records):,}")
    deltas = [interval.delta_us for interval in intervals if interval.delta_us is not None]
    columns[3].metric(
        "Longest observed interval",
        f"{timestamp_to_seconds(max(deltas), 0)} s" if deltas else "—",
        help="Between selected records of the same source and type, "
        "within a continuous clock segment.",
    )
    _issues(result)
    chosen = None
    attitude_gap_us = None
    if result.records:
        origin_us = result.records[0].timestamp_us
        st.caption(
            "Applied time range: "
            f"{timestamp_to_seconds(selection.start_us, origin_us)} to "
            f"{timestamp_to_seconds(selection.end_us, origin_us)} s · "
            "Apply filters to update the view and report."
        )
        st.subheader("Message activity")
        if records:
            with st.container(key="activity_plot"):
                st.plotly_chart(
                    st.session_state.analysis_view_chart,
                    width="stretch",
                    theme=None,
                    config={"displaylogo": False, "scrollZoom": False},
                )
            st.caption(
                "All selected records are counted in up to 200 time bins. Plot zoom does not "
                "change filters. A gap is an absence of selected observations, "
                "not measured packet loss."
            )
        focused_record, attitude_gap_us = _attitude_view(result, selection)
        chosen = _record_view(records, origin_us, selection, focused_record=focused_record)
    else:
        st.info("No records match these filters.")
    report = build_markdown_report(
        result,
        selection,
        selected_indices=() if chosen is None else (chosen,),
        attitude_plot_gap_us=attitude_gap_us,
    )
    with export_area.container():
        st.download_button(
            "Download report",
            report,
            file_name=f"uav-debugger-{result.sha256[:12]}.md",
            mime="text/markdown",
            on_click="ignore",
            type="primary",
            width="stretch",
        )
        st.caption("Applied filters, source evidence and the inspected record.")


if __name__ == "__main__":
    main()
