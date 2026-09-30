"""Explicit local Experiment controls; saved evidence still opens through Analyze."""

import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from .experiment import ExperimentConfig
from .experiment_control import ExperimentController
from .saved_run import load_run_directory

_ACTIVE = {"starting", "running", "stopping"}
_controller_instance = None
_controller_lock = threading.Lock()


def _controller() -> ExperimentController:
    # Python module state outlives Streamlit reruns and browser sessions. Cache
    # eviction must not create a second controller while a worker is active.
    global _controller_instance
    with _controller_lock:
        if _controller_instance is None:
            root = Path(os.environ.get("UAV_DEBUGGER_EXPERIMENT_ROOT", "local/experiments"))
            _controller_instance = ExperimentController(root)
    return _controller_instance


def _handoff(controller: ExperimentController, kind: str, identifiers: dict[str, str]) -> None:
    try:
        runs = {}
        for role, identifier in identifiers.items():
            snapshot = controller.snapshot(identifier)
            if snapshot.state in _ACTIVE:
                raise ValueError("Wait for the worker to finish before opening its evidence.")
            runs[role] = load_run_directory(snapshot.output)
    except (OSError, ValueError, KeyError) as error:
        st.session_state.experiment_error = f"Cannot open saved evidence: {error}"
        return
    st.session_state.analyze_handoff = {"kind": kind, "runs": runs}
    st.session_state.mode = "Analyze"
    st.session_state.analyze_input = kind
    # Invalidate selections even if the target input kind has not changed.
    for key in list(st.session_state):
        if key.startswith(("analysis_", "saved_run_", "compare_")) or key in (
            "import_result",
            "import_token",
        ):
            del st.session_state[key]


def _assign(role: str, identifier: str) -> None:
    st.session_state[f"experiment_{role}_id"] = identifier


def _browse_saved() -> None:
    from .catalog_view import refresh_catalog

    refresh_catalog()
    st.session_state.mode = "Analyze"
    st.session_state.analyze_input = "Local experiments"


def _clocks(snapshot) -> None:
    with st.expander("Controller requests and clocks"):
        st.json(
            {
                "start_requested": asdict(snapshot.created),
                "stop_requested": None
                if snapshot.stop_requested is None
                else asdict(snapshot.stop_requested),
                "worker_finished": None if snapshot.finished is None else asdict(snapshot.finished),
                "control_file": str(snapshot.control_file),
            }
        )
        st.caption(
            "Controller times describe requests and worker lifecycle. The saved runner actions "
            "and capture observations retain their own timestamps; Stop requested is not proof "
            "that the producer has already stopped."
        )


def _status_metrics(snapshot) -> None:
    source = (
        "ArduCopter SITL" if snapshot.requested.get("source") == "arducopter-sitl" else "Synthetic"
    )
    st.caption(
        f"Requested run: {source} · {snapshot.requested['scenario']} · "
        f"{snapshot.requested['duration_s']} s measurement duration"
    )
    columns = st.columns(3)
    columns[0].metric("Process state", snapshot.state)
    columns[1].metric("Declared outcome", snapshot.declared_outcome or "Unavailable")
    end = snapshot.finished.monotonic_ns if snapshot.finished else time.monotonic_ns()
    columns[2].metric(
        "Controller elapsed", f"{max(0, end - snapshot.created.monotonic_ns) / 1e9:.1f} s"
    )
    st.caption(f"Run ID: {snapshot.run_id}")
    st.text(f"Evidence directory: {snapshot.output}")


@st.fragment(run_every=0.5)
def _watch_history(controller: ExperimentController, signature: tuple) -> None:
    # Idle tabs must discover another tab's launch without clicking Start.
    if tuple((item.run_id, item.state) for item in controller.history()) != signature:
        st.rerun()


@st.fragment(run_every=0.5)
def _active_status(controller: ExperimentController, identifier: str) -> None:
    snapshot = controller.snapshot(identifier)
    if snapshot.state not in _ACTIVE:
        st.rerun()
    _status_metrics(snapshot)
    st.caption(
        "Waiting for saved evidence. Controller elapsed includes launch and cleanup; "
        "it is not the measured telemetry duration."
    )
    if st.button("Stop experiment", disabled=snapshot.state == "stopping", type="primary"):
        controller.stop(identifier)
        st.rerun()
    if snapshot.state == "stopping":
        st.info("Stop requested. Waiting for the runner to close its captures and exit.")
    with st.expander("Active run settings"):
        st.json(dict(snapshot.requested))
    _clocks(snapshot)


def _finished_results(controller: ExperimentController, history) -> None:
    identifiers = [snapshot.run_id for snapshot in history]
    by_id = {snapshot.run_id: snapshot for snapshot in history}
    selected = st.session_state.get("experiment_selected_run")
    newest = identifiers[0]
    if st.session_state.get("experiment_history_head") != newest or selected not in by_id:
        selected = newest
        st.session_state.experiment_history_head = newest
    # The selector disappears during execution and Analyze handoffs. A reused
    # frontend widget can retain an older choice even after its session value
    # changes. New history gets a new widget identity; the separate selection
    # survives widget cleanup when the user opens Analyze and returns.
    widget_key = f"experiment_run_choice_{newest}"
    if widget_key not in st.session_state:
        st.session_state[widget_key] = selected
    identifier = st.selectbox(
        "Run",
        identifiers,
        format_func=lambda value: (
            f"{by_id[value].requested['scenario']} · "
            f"{by_id[value].declared_outcome or by_id[value].state} · {value}"
        ),
        key=widget_key,
    )
    st.session_state.experiment_selected_run = identifier
    snapshot = by_id[identifier]
    _status_metrics(snapshot)
    if snapshot.error:
        st.error(snapshot.error)
    if snapshot.diagnostics:
        with st.expander("Worker diagnostics"):
            st.text(snapshot.diagnostics)
    if snapshot.state == "failed":
        st.warning("Worker or controller failed. Inspect retained evidence and diagnostics.")
    elif snapshot.declared_outcome == "completed":
        st.success("Experiment completed. Saved evidence is ready to inspect.")
    elif snapshot.declared_outcome == "interrupted":
        st.info("Experiment interrupted. The observations captured before shutdown are retained.")
    else:
        st.warning(
            "The experiment did not complete successfully. "
            "Inspect available evidence and diagnostics."
        )
    with st.expander("Requested settings"):
        st.json(dict(snapshot.requested))
    _clocks(snapshot)
    available = (snapshot.output / "run.json").is_file()
    st.button(
        "Open in Analyze",
        disabled=not available,
        type="primary",
        on_click=_handoff,
        args=(controller, "Saved experiment", {"saved": identifier}),
    )
    st.caption(
        "Analyze validates the finished files before inspection. Worker state and the runner's "
        "declared outcome remain separate from evidence consistency."
    )
    role = snapshot.requested["scenario"]
    st.button(
        f"Use as {role}",
        disabled=not available,
        on_click=_assign,
        args=(role, identifier),
    )
    st.subheader("Compare saved evidence")
    pair = {}
    for role in ("baseline", "blackout"):
        assigned = st.session_state.get(f"experiment_{role}_id")
        if assigned in by_id:
            pair[role] = assigned
            st.caption(f"Selected {role}: {assigned}")
        else:
            st.caption(f"Selected {role}: none")
    st.button(
        "Compare selected runs",
        disabled=len(pair) != 2,
        on_click=_handoff,
        args=(controller, "Compare experiments", pair),
    )
    st.caption(
        "Comparison checks profile, configuration and evidence compatibility. "
        "Interrupted or incomplete runs produce explicit blocked results."
    )


def present_experiment() -> None:
    st.title("Experiment")
    st.caption("Run a bounded local telemetry path, then inspect its saved evidence in Analyze.")
    st.button("Browse saved experiments", on_click=_browse_saved)
    controller = _controller()
    history = controller.history()
    active = next((item for item in history if item.state in _ACTIVE), None)
    st.info(
        "One experiment can run at a time on this server. All local tabs share its state and "
        "Stop control. Switching modes or closing a tab lets the bounded run continue."
    )
    error = st.session_state.pop("experiment_error", None)
    if error:
        st.error(error)
    if active is not None:
        _active_status(controller, active.run_id)
        st.divider()
    else:
        _watch_history(controller, tuple((item.run_id, item.state) for item in history))
    st.subheader("New run settings")
    source_column, scenario_column = st.columns(2)
    source = source_column.selectbox(
        "Experiment source", ["Synthetic", "ArduCopter SITL"], disabled=active is not None
    )
    scenario = scenario_column.selectbox(
        "Scenario",
        ["baseline", "blackout"],
        format_func=str.title,
        disabled=active is not None,
    )
    binary = os.environ.get("UAV_DEBUGGER_UI_SITL_BINARY") if source == "ArduCopter SITL" else None
    unavailable = source == "ArduCopter SITL" and not binary
    if unavailable:
        st.info(
            "Configure the pinned SITL executable when launching this server "
            "with --sitl-binary PATH."
        )
    elif binary:
        st.caption("ArduCopter 4.6.3 · disarmed telemetry · isolated local worker")
        st.text(f"SITL executable: {binary}")
    else:
        st.caption(
            "Synthetic ATTITUDE · source 1 / 1 · 20 Hz · sender → relay → receiver on loopback"
        )
    with st.form("experiment_configuration"):
        duration_column, blackout_column, length_column = st.columns(3)
        duration = duration_column.number_input(
            "Duration (s)",
            min_value=0.1,
            max_value=60.0,
            value=6.0,
            step=0.1,
            disabled=active is not None,
        )
        blackout_at = blackout_column.number_input(
            "Blackout start (s)",
            min_value=0.1,
            max_value=59.8,
            value=2.0,
            step=0.1,
            disabled=active is not None or scenario != "blackout",
            help="Requested seconds after measurement start. "
            "Keep at least 0.1 s before and after the interruption.",
        )
        blackout_duration = length_column.number_input(
            "Blackout duration (s)",
            min_value=0.1,
            max_value=59.8,
            value=2.0,
            step=0.1,
            disabled=active is not None or scenario != "blackout",
            help="Requested duration from actual forwarding disable. "
            "The full interval must fit inside the measurement duration with 0.1 s margins.",
        )
        startup = 15.0
        if source == "ArduCopter SITL":
            startup = st.number_input(
                "SITL startup timeout (s)",
                min_value=0.1,
                max_value=60.0,
                value=15.0,
                step=0.1,
                disabled=active is not None,
            )
        start = st.form_submit_button(
            "Start experiment",
            disabled=active is not None or unavailable,
            type="primary",
        )
    output_root = os.environ.get("UAV_DEBUGGER_EXPERIMENT_ROOT", "local/experiments")
    st.caption(
        f"New runs are saved under {output_root}. "
        "Each run gets a new directory; saved files are never replaced or automatically deleted."
    )
    if start:
        try:
            snapshot = controller.start(
                ExperimentConfig(
                    scenario=scenario,
                    duration_s=duration,
                    blackout_at_s=blackout_at if scenario == "blackout" else None,
                    sitl_binary=Path(binary) if binary else None,
                    startup_timeout_s=startup,
                    blackout_duration_s=blackout_duration if scenario == "blackout" else None,
                )
            )
        except (ValueError, RuntimeError, OSError) as error:
            st.error(str(error))
        else:
            st.session_state.experiment_selected_run = snapshot.run_id
            st.rerun()
    st.divider()
    if active is not None:
        return
    if history:
        _finished_results(controller, history)
        st.caption(
            "The latest 20 runs are retained in this server's history; "
            "older directories remain on disk."
        )
    else:
        st.metric("Process state", "idle")
        st.info("No experiment has been started in this server.")
