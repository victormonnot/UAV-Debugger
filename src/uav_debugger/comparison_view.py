"""Two saved runs in Analyze, with explicit measurement-relative comparison."""

import streamlit as st

from .comparison import RunComparison, available_selections, compare_runs
from .comparison_report import build_comparison_markdown_report
from .comparison_ui import comparison_activity_chart, window_nanoseconds
from .run_view import POINT_LABELS, _seconds, uploaded_run_files
from .saved_run import SavedRun, load_run_files

ROLES = {"baseline": "Baseline", "blackout": "Blackout"}


def _clear_role(role: str) -> None:
    handoff = st.session_state.get("analyze_handoff")
    if handoff is not None and handoff["kind"] == "Compare experiments":
        handoff["runs"].pop(role, None)
    for key in list(st.session_state):
        if key.startswith(f"compare_{role}_"):
            del st.session_state[key]
    key = f"pair_upload_{role}_generation"
    st.session_state[key] = st.session_state.get(key, 0) + 1


def _clear_pair() -> None:
    for role in ROLES:
        _clear_role(role)
    for key in list(st.session_state):
        if key.startswith("compare_"):
            del st.session_state[key]


def _open_run(role: str) -> SavedRun | None:
    handoff = st.session_state.get("analyze_handoff")
    if handoff is not None and handoff["kind"] == "Compare experiments":
        run = handoff["runs"].get(role)
        if run is not None:
            with st.sidebar:
                st.caption(
                    f"{ROLES[role]} evidence opened from {handoff.get('origin', 'Experiment')}."
                )
                st.button(f"Clear {role}", on_click=_clear_role, args=(role,), width="stretch")
            return run
    with st.sidebar:
        uploaded = st.file_uploader(
            f"Open {role} experiment",
            accept_multiple_files="directory",
            key=f"pair_upload_{role}_{st.session_state.get(f'pair_upload_{role}_generation', 0)}",
            max_upload_size=35,
            help="Select a saved run directory. Opening evidence does not start an experiment.",
        )
        if uploaded:
            st.button(f"Clear {role}", on_click=_clear_role, args=(role,), width="stretch")
    prefix = f"compare_{role}_"
    token = tuple((item.name, item.size, item.file_id) for item in uploaded)
    if not uploaded or st.session_state.get(prefix + "token") != token:
        for key in list(st.session_state):
            if key.startswith(prefix):
                del st.session_state[key]
        if not uploaded:
            return None
        try:
            contents, name, ignored = uploaded_run_files(uploaded)
            run = load_run_files(contents, source_name=name)
        except ValueError as error:
            st.error(f"{ROLES[role]}: {error}")
            return None
        st.session_state[prefix + "run"] = run
        st.session_state[prefix + "token"] = token
        st.session_state[prefix + "ignored"] = ignored
    return st.session_state[prefix + "run"]


def _summary(role: str, run: SavedRun) -> None:
    st.subheader(ROLES[role])
    st.text(run.source_name)
    st.caption(f"{ROLES[role]} fingerprint: {run.identity}")
    st.write(f"Declared outcome: **{run.declared_outcome}** · Evidence: **{run.evidence_status}**")
    st.caption(f"Measurement origin (host monotonic ns): {run.measurement_monotonic_ns}")
    with st.expander(f"{ROLES[role]} settings and evidence"):
        st.json(dict(run.requested), expanded=False)
        st.dataframe(
            [
                {"File": name, "Bytes": item.size_bytes, "SHA256": item.sha256}
                for name, item in run.files.items()
            ],
            hide_index=True,
        )
        for issue in run.issues[:100]:
            st.text(f"{issue.severity}: {issue.code} · {issue.file_name}: {issue.message[:256]}")
        if len(run.issues) > 100:
            st.caption(f"{len(run.issues) - 100} further issues; inspect the saved run separately.")
        ignored = st.session_state.get(f"compare_{role}_ignored", ())
        if ignored:
            st.caption("Other files excluded from analysis:")
            for name in ignored:
                st.text(name)


def _metrics(comparison: RunComparison) -> None:
    rows, deltas, references = [], [], []
    for role, points in comparison.metrics.items():
        for point, metric in points.items():
            interval = metric.longest_interval
            rows.append(
                {
                    "Run": ROLES[role],
                    "Point": POINT_LABELS[point],
                    "Count": metric.count,
                    "Observed rate (Hz)": metric.rate_hz,
                    "Longest interval (s)": None
                    if interval is None
                    else interval.duration_ns / 1e9,
                }
            )
            selected = [metric.first, metric.last]
            if interval:
                selected.extend((interval.previous, interval.current))
            seen = set()
            for item in selected:
                if item is None or item.record.index in seen:
                    continue
                seen.add(item.record.index)
                references.append(
                    {
                        "Run": ROLES[role],
                        "Point": POINT_LABELS[point],
                        "Record index": item.record.index,
                        "Observation line": item.observation.line_number,
                        "Measurement seconds": _seconds(item.measurement_ns),
                        "Monotonic ns": str(item.observation.monotonic_ns),
                        "Capture Unix µs": str(item.record.timestamp_us),
                        "Byte offset": item.record.offset,
                    }
                )
    st.subheader("Observed comparison")
    st.dataframe(rows, hide_index=True, width="stretch")
    for point in POINT_LABELS:
        before, after = [comparison.metrics[role][point] for role in ROLES]
        deltas.append(
            {
                "Point": POINT_LABELS[point],
                "Count difference": after.count - before.count,
                "Rate difference (Hz)": after.rate_hz - before.rate_hz,
                "Longest interval difference (s)": (
                    (after.longest_interval.duration_ns - before.longest_interval.duration_ns) / 1e9
                    if before.longest_interval is not None and after.longest_interval is not None
                    else None
                ),
            }
        )
    st.caption("Differences: blackout minus baseline. Empty interval cells mean unavailable.")
    st.dataframe(deltas, hide_index=True, width="stretch")
    st.caption(
        "Rate = selected observations / window duration. Longest interval is between consecutive "
        "selected observations within the window; uncovered edges are not measured intervals."
    )
    with st.expander("First, last and longest-interval observation references"):
        st.dataframe(references, hide_index=True, width="stretch")
        st.caption("Record indices and byte offsets are zero-based; JSONL lines are one-based.")


def present_comparison(export_area) -> None:
    st.subheader("Compare experiments")
    st.write("Open a saved baseline and blackout from the same telemetry profile.")
    with st.sidebar:
        st.caption("Each run: 64 MiB total · 64 files · 10 MiB per capture. Two runs maximum.")
        st.button("Clear comparison", on_click=_clear_pair, width="stretch")
    runs = {role: _open_run(role) for role in ROLES}
    for column, (role, run) in zip(st.columns(2), runs.items(), strict=True):
        with column:
            if run is not None:
                _summary(role, run)
            else:
                st.info(f"Select the saved {role} experiment directory.")
    if any(run is None for run in runs.values()):
        return
    baseline, blackout = runs.values()
    identity = baseline.identity + blackout.identity
    if st.session_state.get("compare_pair") != identity:
        for key in list(st.session_state):
            if key.startswith("compare_control_"):
                del st.session_state[key]
        st.session_state.compare_pair = identity
    selections = available_selections(baseline, blackout)
    sources = list(selections) or [(1, 1)]
    source = st.selectbox(
        "Comparison source",
        sources,
        format_func=lambda value: f"{value[0]} / {value[1]}",
        index=sources.index((1, 1)) if (1, 1) in sources else 0,
        key=f"compare_control_source_{identity}",
    )
    types = list(selections.get(source, ())) or ["ATTITUDE"]
    message_type = st.selectbox(
        "Comparison message type",
        types,
        index=types.index("ATTITUDE") if "ATTITUDE" in types else 0,
        key=f"compare_control_type_{identity}_{source}",
    )
    initial = compare_runs(baseline, blackout, source=source, message_type=message_type)
    comparison = initial
    if initial.end_ns is not None:
        with st.form(f"comparison_window_{identity}"):
            left, right = st.columns(2)
            start = left.text_input(
                "Window start (s)", "0", key=f"compare_control_start_{identity}"
            )
            end = right.text_input(
                "Window end (s)",
                _seconds(initial.end_ns),
                key=f"compare_control_end_{identity}",
            )
            st.form_submit_button("Apply comparison window")
        try:
            comparison = compare_runs(
                baseline,
                blackout,
                source=source,
                message_type=message_type,
                start_ns=window_nanoseconds(start),
                end_ns=window_nanoseconds(end),
            )
        except ValueError as error:
            st.error(str(error))
            return
    st.caption(
        f"Comparison window: [{_seconds(comparison.start_ns)}, {_seconds(comparison.end_ns)}) s "
        "since each run's own measurement start. Startup and drain are excluded."
    )
    st.subheader("Configuration differences")
    if comparison.differences:
        st.dataframe(
            [
                {
                    "Setting": item.field,
                    "Baseline": str(item.baseline),
                    "Blackout": str(item.blackout),
                    "Blocks comparison": item.blocking,
                }
                for item in comparison.differences
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No relevant configuration differences recorded.")
    st.subheader("Applied forwarding gates")
    gates = [
        {
            "Run": ROLES[role],
            "Start (s)": _seconds(gate.start_ns),
            "End (s)": _seconds(gate.end_ns),
            "Start action line": gate.start_line,
            "End action line": gate.end_line,
        }
        for role in ROLES
        for gate in comparison.gates[role]
    ]
    if gates:
        st.dataframe(gates, hide_index=True, width="stretch")
    else:
        st.caption("No validated forwarding gate interval is available.")
    if comparison.comparable:
        st.success("Comparison available")
        _metrics(comparison)
        point = st.selectbox(
            "Comparison observation point",
            list(POINT_LABELS),
            format_func=POINT_LABELS.get,
            index=1,
            key=f"compare_control_point_{identity}",
        )
        with st.container(key="comparison_activity_plot"):
            st.plotly_chart(
                comparison_activity_chart(comparison, point), theme=None, width="stretch"
            )
        st.caption(
            "Shared bins count observations; shading shows actual gate timing. "
            "No messages are paired across runs or shifted to align a gap."
        )
    else:
        st.warning("Comparison unavailable")
        for issue in comparison.issues:
            st.text(f"{issue.run_role or 'Pair'} · {issue.code}: {issue.message}")
    st.caption(
        "An observed difference does not establish physical packet loss, autopilot behavior "
        "or a cause. Inspect each run in Saved experiment for full capture analysis."
    )
    with export_area.container():
        st.download_button(
            "Download report",
            build_comparison_markdown_report(comparison),
            file_name=f"uav-debugger-comparison-{baseline.identity[:8]}-{blackout.identity[:8]}.md",
            mime="text/markdown",
            on_click="ignore",
            type="primary",
            width="stretch",
        )
