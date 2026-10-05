"""Framework-independent directory upload and saved-run timeline presentation."""

from pathlib import PurePosixPath

import plotly.graph_objects as go

from .saved_run import KNOWN_FILES, SavedRun

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
