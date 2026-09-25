"""Offline presentation of saved Experiment evidence; no execution controls."""

import math
from collections import Counter
from pathlib import PurePosixPath

import plotly.graph_objects as go
import streamlit as st

from .saved_run import KNOWN_FILES, SavedRun, load_run_files

POINT_LABELS = {"relay-input": "Relay input", "receiver": "Receiver"}
MAX_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_UPLOAD_FILES = 64
TRACE_PAGE_SIZE = 100


def uploaded_run_files(uploaded) -> tuple[dict[str, bytes], str, tuple[str, ...]]:
    """Strip exactly one selected directory prefix without following any path."""
    if len(uploaded) > MAX_UPLOAD_FILES:
        raise ValueError("A saved experiment accepts at most 64 uploaded files.")
    if sum(item.size for item in uploaded) > MAX_UPLOAD_BYTES:
        raise ValueError("A saved experiment accepts at most 64 MiB in total.")
    names = [item.name for item in uploaded]
    for name in names:
        parts = name.split("/")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or "\x00" in name
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("Uploaded experiment paths must be relative directory paths.")
    manifests = [name for name in names if PurePosixPath(name).name == "run.json"]
    if len(manifests) != 1 or len(manifests[0].split("/")) > 2:
        raise ValueError("Select exactly one experiment directory containing run.json.")
    prefix = manifests[0][: -len("run.json")]
    if any(not name.startswith(prefix) for name in names):
        raise ValueError("Files from different experiment directories cannot be combined.")
    relative_names = [name[len(prefix) :] for name in names]
    if len(set(relative_names)) != len(relative_names):
        raise ValueError("Duplicate experiment filenames are not accepted; clear and reopen.")
    contents = {
        name: item.getvalue()
        for name, item in zip(relative_names, uploaded, strict=True)
        if name in KNOWN_FILES
    }
    ignored = tuple(name for name in relative_names if name not in KNOWN_FILES)
    if sum(len(data) for data in contents.values()) > MAX_UPLOAD_BYTES:
        raise ValueError("A saved experiment accepts at most 64 MiB in total.")
    return contents, prefix[:-1] or "saved experiment", ignored


def _seconds(ns: int | None) -> str:
    if type(ns) is not int:
        return "Unavailable"
    sign = "-" if ns < 0 else ""
    seconds, fraction = divmod(abs(ns), 1_000_000_000)
    return f"{sign}{seconds}.{fraction:09d}"


def run_activity_chart(run: SavedRun) -> go.Figure:
    """Count every valid reference in <=200 bins of this run's monotonic clock."""
    observations = [event for event in run.observations if event.valid]
    times = [event.elapsed_ns for event in observations if event.elapsed_ns is not None]
    times.extend(
        event.elapsed_ns for event in run.actions if event.valid and event.elapsed_ns is not None
    )
    figure = go.Figure()
    if not times:
        return figure
    low, high = min(0, min(times)), max(times)
    span = max(1, high - low)
    bins = min(200, max(1, len(set(times))))
    width = max(1, (span + bins - 1) // bins)
    count = (span // width) + 1
    if count > 200:
        width += 1
        count = (span // width) + 1
    for point, color in (("relay-input", "#007f6d"), ("receiver", "#326ca8")):
        counts = [0] * count
        for event in observations:
            if event.point == point and event.elapsed_ns is not None:
                counts[(event.elapsed_ns - low) // width] += 1
        figure.add_trace(
            go.Bar(
                name=POINT_LABELS[point],
                x=[(low + (index + 0.5) * width) / 1e9 for index in range(count)],
                y=counts,
                width=width / 1e9,
                marker_color=color,
                customdata=[
                    [_seconds(low + index * width), _seconds(low + (index + 1) * width)]
                    for index in range(count)
                ],
                hovertemplate=(
                    "%{fullData.name}: %{y} records<br>Run elapsed [%{customdata[0]}, "
                    "%{customdata[1]}) s<extra></extra>"
                ),
            )
        )
    origin = run.origin_monotonic_ns
    measurement = run.measurement_monotonic_ns
    if origin is not None and measurement is not None:
        start = (measurement - origin) / 1e9
        if start > 0:
            figure.add_vrect(x0=0, x1=start, fillcolor="#64748b", opacity=0.1, line_width=0)
        figure.add_vline(x=start, line_dash="dot", line_color="#64748b")
    for gate in run.gate_intervals:
        if gate.end is not None:
            figure.add_vrect(
                x0=gate.start.elapsed_ns / 1e9,
                x1=gate.end.elapsed_ns / 1e9,
                fillcolor="#d15353",
                opacity=0.15,
                line_width=0,
            )
        else:
            figure.add_vline(x=gate.start.elapsed_ns / 1e9, line_color="#d15353")
    figure.update_layout(
        height=300,
        barmode="group",
        bargap=0,
        margin=dict(l=15, r=15, t=10, b=30),
        xaxis_title="Host monotonic seconds since this run's origin",
        yaxis_title="Observed records per bin",
        legend=dict(orientation="h"),
    )
    return figure


def clear_run_state() -> None:
    for key in list(st.session_state):
        if key.startswith(("saved_run_", "analysis_")) or key in ("import_result", "import_token"):
            del st.session_state[key]


def clear_experiment() -> None:
    clear_run_state()
    st.session_state.run_upload_generation = st.session_state.get("run_upload_generation", 0) + 1


def open_saved_run() -> SavedRun | None:
    """Read uploaded directory bytes, keeping one bounded result per session."""
    with st.sidebar:
        uploaded = st.file_uploader(
            "Open saved experiment",
            accept_multiple_files="directory",
            key=f"experiment_upload_{st.session_state.get('run_upload_generation', 0)}",
            max_upload_size=35,
            help="Select one saved run directory. No simulator or experiment is started.",
        )
        st.caption("Run limit: 64 MiB total · 64 files · 10 MiB per capture.")
        if uploaded:
            st.button("Clear experiment", on_click=clear_experiment, width="stretch")
    if not uploaded:
        clear_run_state()
        st.info("Select one saved experiment directory to inspect its evidence.")
        return None
    token = tuple((item.name, item.size, item.file_id) for item in uploaded)
    if st.session_state.get("saved_run_upload_token") != token:
        clear_run_state()
        try:
            contents, name, ignored = uploaded_run_files(uploaded)
            with st.spinner("Checking experiment evidence…"):
                run = load_run_files(contents, source_name=name)
        except ValueError as error:
            st.error(str(error))
            return None
        st.session_state.saved_run_ignored = ignored
        st.session_state.saved_run_result = run
        st.session_state.saved_run_upload_token = token
    ignored = st.session_state.get("saved_run_ignored", ())
    if ignored:
        with st.expander(f"Other files excluded from analysis ({len(ignored)})"):
            for name in ignored:
                st.text(name)
            st.caption(
                "Only the fixed run evidence files contribute to the run fingerprint and report."
            )
    return st.session_state.saved_run_result


def _trace_rows(run: SavedRun, kind: str, start: int):
    if kind == "Actions":
        return [
            {
                "Reference": f"{event.file_name}:{event.line_number}",
                "Action": str(event.data.get("action")),
                "Point": str(event.data.get("point")),
                "Record": str(event.data.get("record_index")),
                "Run elapsed (s)": _seconds(event.elapsed_ns),
                "Unix (µs)": str(event.unix_us),
                "Consistent reference": event.valid,
            }
            for event in run.actions[start : start + TRACE_PAGE_SIZE]
        ]
    return [
        {
            "Reference": f"{event.file_name}:{event.line_number}",
            "Point": str(event.point),
            "Record": str(event.record_index),
            "Byte offset": str(event.data.get("offset")),
            "Run elapsed (s)": _seconds(event.elapsed_ns),
            "Unix (µs)": str(event.unix_us),
            "Consistent reference": event.valid,
        }
        for event in run.observations[start : start + TRACE_PAGE_SIZE]
    ]


def present_run(run: SavedRun) -> None:
    st.subheader("Saved experiment")
    st.text(run.source_name)
    st.text(f"Run fingerprint: {run.identity}")
    columns = st.columns(4)
    columns[0].metric("Declared outcome", run.declared_outcome)
    columns[1].metric("Evidence status", run.evidence_status)
    for column, point in zip(columns[2:], POINT_LABELS, strict=True):
        capture = run.captures.get(point)
        column.metric(
            f"{POINT_LABELS[point]} records", str(len(capture.records)) if capture else "—"
        )
    if run.evidence_status != "consistent":
        st.warning(
            "Saved evidence is incomplete or inconsistent. Retained captures remain inspectable; "
            "the declared outcome does not establish that all evidence is valid."
        )
    st.caption(
        "Read-only saved evidence. Fingerprints check consistency with the supplied manifest, "
        "not authenticity. Opening this directory starts no experiment."
    )
    left, right = st.columns(2)
    with left:
        st.markdown("**Requested settings**")
        requested = run.requested
        st.dataframe(
            [
                {"Setting": label, "Requested value": str(requested.get(key, "Unavailable"))}
                for label, key in (
                    ("Scenario", "scenario"),
                    ("Source", "source"),
                    ("Measurement duration (s)", "duration_s"),
                    ("Blackout start (s from measurement)", "blackout_at_s"),
                    ("Blackout duration (s)", "blackout_duration_s"),
                )
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("All requested settings"):
            st.json(dict(requested))
    with right:
        st.markdown("**Applied forwarding interruption**")
        if run.gate_intervals:
            st.dataframe(
                [
                    {
                        "Disabled at run (s)": _seconds(gate.start.elapsed_ns),
                        "Enabled at run (s)": _seconds(gate.end.elapsed_ns)
                        if gate.end
                        else "Unknown",
                        "Applied duration (s)": _seconds(gate.duration_ns),
                        "Start reference": f"actions.jsonl:{gate.start.line_number}",
                        "End reference": f"actions.jsonl:{gate.end.line_number}"
                        if gate.end
                        else "Missing",
                        "End reason": gate.end.data.get("reason") if gate.end else None,
                    }
                    for gate in run.gate_intervals
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.info("No consistent forwarding-disabled action is available.")
        st.caption("Actual transition records; an open interval has no established duration.")
    st.markdown("**Observed at the two capture points**")
    st.dataframe(
        [
            {
                "Point": POINT_LABELS[point],
                "Captured records": len(run.captures[point].records)
                if point in run.captures
                else None,
                "Observation references": sum(event.point == point for event in run.observations),
                "Consistent references": sum(
                    event.point == point and event.valid for event in run.observations
                ),
                "Capture traversal": run.captures[point].traversal
                if point in run.captures
                else "Missing",
            }
            for point in POINT_LABELS
        ],
        hide_index=True,
        width="stretch",
    )
    st.subheader("Run timeline")
    if any(event.valid for event in run.observations):
        with st.container(key="run_activity_plot"):
            st.plotly_chart(
                run_activity_chart(run),
                width="stretch",
                theme=None,
                config={"displaylogo": False, "scrollZoom": False},
                key=f"saved_run_timeline_{run.identity}",
            )
    else:
        st.info("No observations with consistent capture and clock references to plot.")
    measurement_elapsed = (
        run.measurement_monotonic_ns - run.origin_monotonic_ns
        if run.measurement_monotonic_ns is not None and run.origin_monotonic_ns is not None
        else None
    )
    st.caption(
        f"Measurement starts at run elapsed {_seconds(measurement_elapsed)} s. "
        "Gray marks startup; red marks closed, applied gate intervals. An open gate interval "
        "has only a start line. All consistent observation references are counted in at most "
        "200 bins. This timeline is independent of the capture filters below."
    )
    st.caption(
        "Capture Unix timestamps and MAVLink boot time remain separate. The capture inspector "
        "uses seconds from that file's first record; it is not aligned to this run timeline. "
        "An absence of observations does not establish vehicle behavior or physical packet loss."
    )
    with st.expander(f"Evidence issues ({len(run.issues)})"):
        if run.issues:
            counts = Counter(issue.code for issue in run.issues)
            st.text(" · ".join(f"{code}: {count}" for code, count in counts.items()))
            pages = math.ceil(len(run.issues) / TRACE_PAGE_SIZE)
            page = st.number_input(
                "Evidence issues page", 1, pages, 1, key=f"saved_run_issues_{run.identity}"
            )
            st.dataframe(
                [
                    {
                        "File": issue.file_name,
                        "Line": issue.line_number,
                        "Severity": issue.severity,
                        "Issue": issue.code,
                        "Details": issue.message,
                    }
                    for issue in run.issues[(page - 1) * TRACE_PAGE_SIZE : page * TRACE_PAGE_SIZE]
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.text("No evidence consistency issues detected.")
    with st.expander("Execution trace references"):
        kind = st.radio(
            "Trace",
            ["Actions", "Observations"],
            horizontal=True,
            key=f"saved_run_trace_{run.identity}",
        )
        events = run.actions if kind == "Actions" else run.observations
        if events:
            pages = math.ceil(len(events) / TRACE_PAGE_SIZE)
            page = st.number_input(
                "Trace page", 1, pages, 1, key=f"saved_run_trace_page_{run.identity}_{kind}"
            )
            st.dataframe(
                _trace_rows(run, kind, (page - 1) * TRACE_PAGE_SIZE),
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "JSONL line numbers are one-based; capture record indices and byte offsets "
                "are zero-based."
            )
    with st.expander("Run provenance and clocks"):
        st.json(
            {
                key: run.manifest.get(key)
                for key in (
                    "schema",
                    "environment",
                    "topology",
                    "clocks",
                    "origin_monotonic_ns",
                    "measurement_start",
                    "start",
                    "end",
                    "error",
                )
            }
        )
        st.dataframe(
            [
                {"File": name, "Bytes": len(item.raw_bytes), "SHA-256": item.sha256}
                for name, item in run.files.items()
            ],
            hide_index=True,
            width="stretch",
        )
