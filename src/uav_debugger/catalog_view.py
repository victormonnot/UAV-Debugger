"""Read-only discovery of saved local experiments, independent of execution."""

import os
from pathlib import Path

import streamlit as st

from .catalog import load_catalog_entry, scan_catalog


def refresh_catalog() -> None:
    st.session_state.pop("catalog_result", None)
    st.session_state.catalog_generation = st.session_state.get("catalog_generation", 0) + 1


def _assign(role: str, key: str) -> None:
    st.session_state[f"catalog_{role}"] = key


def _open(root: Path, kind: str, keys: dict[str, str]) -> None:
    try:
        runs = {role: load_catalog_entry(root, key) for role, key in keys.items()}
    except (OSError, ValueError) as error:
        st.session_state.catalog_error = f"Cannot open saved evidence: {error}"
        refresh_catalog()
        return
    st.session_state.analyze_handoff = {"kind": kind, "runs": runs, "origin": "local catalog"}
    st.session_state.analyze_input = kind
    for key in list(st.session_state):
        if key.startswith(("analysis_", "saved_run_", "compare_")) or key in (
            "import_result",
            "import_token",
        ):
            del st.session_state[key]


def present_catalog() -> None:
    st.subheader("Local experiments")
    root = Path(
        os.path.abspath(os.environ.get("UAV_DEBUGGER_EXPERIMENT_ROOT", "local/experiments"))
    )
    st.text(f"Catalog directory: {root}")
    st.caption(
        "Browse saved runs on this server, including runs retained after restart. "
        "Browsing and opening evidence never start or resume an experiment."
    )
    st.button("Refresh catalog", on_click=refresh_catalog)
    catalog = st.session_state.get("catalog_result")
    if catalog is None or catalog.root != root:
        catalog = scan_catalog(root)
        st.session_state.catalog_result = catalog
    error = st.session_state.pop("catalog_error", None)
    if error:
        st.error(error)
    for issue in catalog.issues:
        st.warning(issue)
    if catalog.truncated:
        st.warning("This catalog is partial. The configured scan limit was reached.")
    st.caption(
        "Manifest summaries only; captures and traces are validated when opened. "
        "Refresh to discover external changes. Controller history is separate from this catalog."
    )
    by_key = {entry.key: entry for entry in catalog.entries}
    for role in ("baseline", "blackout"):
        assigned = st.session_state.get(f"catalog_{role}")
        if assigned is not None and (
            assigned not in by_key
            or not by_key[assigned].openable
            or by_key[assigned].scenario != role
        ):
            st.session_state.pop(f"catalog_{role}", None)
            st.warning(
                f"The selected {role} is no longer available in this catalog. Select it again."
            )
    if not by_key:
        st.info("No saved experiments were found in this catalog.")
        return
    st.dataframe(
        [
            {
                "Run directory": entry.key,
                "Source": entry.source or "Unavailable",
                "Scenario": entry.scenario or "Unavailable",
                "Declared outcome": entry.outcome or "Unavailable",
                "Requested duration (s)": entry.duration_s,
                "Manifest": entry.issue or "Available; evidence not yet validated",
            }
            for entry in catalog.entries
        ],
        hide_index=True,
        width="stretch",
        height=240,
    )
    keys = list(by_key)
    selected = st.session_state.get("catalog_selected")
    if selected not in by_key:
        selected = keys[0]
    # Keep the durable selection independent of widgets removed by an Analyze
    # handoff. A refresh gets a fresh widget identity, including removed runs.
    widget_key = f"catalog_choice_{st.session_state.get('catalog_generation', 0)}"
    if widget_key not in st.session_state:
        st.session_state[widget_key] = selected
    key = st.selectbox(
        "Saved run",
        keys,
        format_func=lambda value: (
            f"{by_key[value].scenario or 'Unknown scenario'} · "
            f"{by_key[value].outcome or 'Unavailable outcome'} · {value}"
        ),
        key=widget_key,
    )
    st.session_state.catalog_selected = key
    entry = by_key[key]
    st.text(f"Evidence directory: {entry.directory}")
    st.caption(
        f"Recorded start (host Unix microseconds): {entry.start_unix_us}"
        if entry.start_unix_us is not None
        else "Recorded start (host Unix microseconds): Unavailable"
    )
    st.caption(
        "Recorded wall time is a manifest declaration, not file modification time or an "
        "ordering of actions across runs. No process liveness is inferred from saved files."
    )
    if entry.issue:
        st.warning(entry.issue)
    st.button(
        "Open in Analyze",
        type="primary",
        disabled=not entry.openable,
        on_click=_open,
        args=(root, "Saved experiment", {"saved": key}),
    )
    if entry.scenario in ("baseline", "blackout"):
        st.button(
            f"Use as {entry.scenario}",
            disabled=not entry.openable,
            on_click=_assign,
            args=(entry.scenario, key),
        )
    st.subheader("Compare saved evidence")
    pair = {}
    for role in ("baseline", "blackout"):
        assigned = st.session_state.get(f"catalog_{role}")
        if assigned is not None:
            pair[role] = assigned
        st.caption(f"Selected {role}: {assigned or 'none'}")
    st.button(
        "Compare selected runs",
        disabled=len(pair) != 2,
        on_click=_open,
        args=(root, "Compare experiments", pair),
    )
    st.caption(
        "Opening rereads the selected evidence. Assignment does not establish compatibility; "
        "interrupted, incomplete or incompatible evidence produces explicit blocked comparisons."
    )
