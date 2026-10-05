"""Framework-independent exact windows and shared-bin comparison charts."""

from decimal import Decimal, InvalidOperation

import plotly.graph_objects as go

from .comparison import RunComparison
from .saved_run_ui import POINT_LABELS, _seconds

ROLES = {"baseline": "Baseline", "blackout": "Blackout"}


def window_nanoseconds(value: str) -> int:
    """Accept exact decimal seconds, with no more than nanosecond precision."""
    try:
        seconds = Decimal(value)
        if not seconds.is_finite() or not 0 <= seconds <= 60:
            raise ValueError("Window seconds must be finite and between 0 and 60.")
        ns = seconds * 1_000_000_000
        if ns != ns.to_integral_value():
            raise ValueError("Window seconds support at most nine decimal places.")
        integer_ns = int(ns)
        if seconds != Decimal(integer_ns) / 1_000_000_000:
            raise ValueError("Window seconds support at most nine decimal places.")
        return integer_ns
    except InvalidOperation as error:
        raise ValueError("Window seconds must be decimal numbers.") from error


def comparison_activity_chart(comparison: RunComparison, point: str) -> go.Figure:
    """Count selected references in shared bins; never resample telemetry values."""
    figure = go.Figure()
    if not comparison.comparable:
        return figure
    low, high = comparison.start_ns, comparison.end_ns
    span = high - low
    bins = min(200, max(1, (span + 99_999_999) // 100_000_000))
    width = (span + bins - 1) // bins
    count = (span + width - 1) // width
    for role, color in (("baseline", "#007f6d"), ("blackout", "#bd4f28")):
        counts = [0] * count
        for item in comparison.metrics[role][point].observations:
            counts[(item.measurement_ns - low) // width] += 1
        figure.add_trace(
            go.Scatter(
                name=ROLES[role],
                x=[(low + min(index * width + width / 2, span)) / 1e9 for index in range(count)],
                y=counts,
                mode="lines+markers",
                line={"color": color, "shape": "hv"},
                marker={"size": 4},
                customdata=[
                    [_seconds(low + index * width), _seconds(min(high, low + (index + 1) * width))]
                    for index in range(count)
                ],
                hovertemplate=(
                    "%{fullData.name}: %{y} observations<br>Measurement [%{customdata[0]}, "
                    "%{customdata[1]}) s<extra></extra>"
                ),
            )
        )
        for gate in comparison.gates[role]:
            if gate.end_ns is None:
                figure.add_vline(x=gate.start_ns / 1e9, line_color=color)
            else:
                figure.add_vrect(
                    x0=gate.start_ns / 1e9,
                    x1=gate.end_ns / 1e9,
                    fillcolor=color,
                    opacity=0.12,
                    line_width=0,
                )
    figure.update_layout(
        height=400,
        title={"text": f"{POINT_LABELS[point]} observation activity", "x": 0, "font": {"size": 16}},
        xaxis={
            "title": "Monotonic seconds since each run's measurement start",
            "range": [low / 1e9, high / 1e9],
        },
        yaxis_title="Selected observations per bin",
        legend={"orientation": "h", "x": 1, "xanchor": "right", "y": 1.05, "yanchor": "bottom"},
        margin={"l": 70, "r": 20, "t": 60, "b": 70},
    )
    return figure
