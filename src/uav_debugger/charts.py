"""Bounded activity and attitude displays tied to the original observations."""

import math

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .analysis import timestamp_to_seconds
from .model import Record
from .telemetry import ATTITUDE_FIELDS, AttitudeView


def activity_chart(records: tuple[Record, ...], *, origin_us: int) -> go.Figure:
    """Aggregate all observations into at most 200 integer-time bins.

    Floating-point seconds are only display coordinates. Filtering and bin
    membership use the original integer timestamps, including at large epochs.
    """
    if not records:
        return go.Figure()
    low = min(record.timestamp_us for record in records)
    high = max(record.timestamp_us for record in records)
    bins = min(200, high - low + 1)
    width_us = (high - low + 1 + bins - 1) // bins
    bins = (high - low) // width_us + 1
    counts = [0] * bins
    for record in records:
        counts[(record.timestamp_us - low) // width_us] += 1
    starts = [low + index * width_us for index in range(bins)]
    intervals = [
        [
            timestamp_to_seconds(start, origin_us),
            timestamp_to_seconds(min(high, start + width_us - 1), origin_us),
        ]
        for start in starts
    ]
    figure = go.Figure(
        go.Bar(
            x=[(start - origin_us + width_us / 2) / 1_000_000 for start in starts],
            y=counts,
            width=width_us / 1_000_000,
            customdata=intervals,
            name="Selected observations",
            marker_color="#007f6d",
            hovertemplate="%{customdata[0]} to %{customdata[1]} s"
            "<br>%{y} observations<extra></extra>",
        )
    )
    figure.update_layout(
        height=320,
        margin=dict(l=65, r=20, t=15, b=65),
        xaxis_title="Capture time (s)",
        yaxis_title="Messages",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color="#18313c"),
        bargap=0,
        showlegend=False,
    )
    figure.update_xaxes(automargin=True, title_standoff=15)
    figure.update_yaxes(
        rangemode="tozero",
        tickformat=",d",
        gridcolor="#e7edef",
        automargin=True,
        title_standoff=15,
        dtick=1 if max(counts) <= 10 else None,
    )
    return figure


def attitude_chart(view: AttitudeView, *, origin_us: int) -> go.Figure:
    """Plot unchanged radians with explicit gaps and original-record point data.

    The line is only a display aid. No line crosses missing values, capture-clock
    discontinuities, an interval above the chosen maximum, or an angle jump above
    pi radians. Absolute timestamps remain strings in browser data to preserve
    microseconds beyond JavaScript's exact integer range.
    """
    if view.status != "ready":
        raise ValueError("Attitude plotting requires a ready single-source view.")

    figure = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.08)
    colors = ("#007f6d", "#375dc2", "#a45d18")
    for row, (field, color) in enumerate(zip(ATTITUDE_FIELDS, colors, strict=True), start=1):
        times = []
        values = []
        references = []
        previous = None
        previous_value = None
        for sample in view.samples:
            record = sample.record
            value = getattr(sample, field)
            if value is None:
                times.append(None)
                values.append(None)
                references.append(None)
            else:
                if previous is not None and previous_value is not None:
                    elapsed = record.timestamp_us - previous.record.timestamp_us
                    if (
                        sample.segment != previous.segment
                        or elapsed <= 0
                        or elapsed > view.max_gap_us
                        or abs(value - previous_value) > math.pi
                    ):
                        times.append(None)
                        values.append(None)
                        references.append(None)
                times.append((record.timestamp_us - origin_us) / 1_000_000)
                values.append(value)
                references.append(
                    [
                        record.index,
                        str(record.timestamp_us),
                        timestamp_to_seconds(record.timestamp_us, origin_us),
                        f"{record.system_id} / {record.component_id}",
                        record.offset,
                        record.frame_offset,
                        record.end_offset,
                    ]
                )
            previous = sample
            previous_value = value
        label = field.capitalize()
        figure.add_trace(
            go.Scatter(
                x=times,
                y=values,
                customdata=references,
                name=label,
                mode="lines+markers",
                connectgaps=False,
                line=dict(color=color, width=1.5, simplify=False),
                marker=dict(color=color, size=5),
                selected=dict(marker=dict(size=9)),
                unselected=dict(marker=dict(opacity=0.7)),
                hovertemplate=f"{label}: %{{y}} rad"
                "<br>Capture time: %{customdata[2]} s"
                "<br>Record #%{customdata[0]} · source %{customdata[3]}"
                "<br>Timestamp: %{customdata[1]} us"
                "<br>Record bytes: [%{customdata[4]}, %{customdata[6]})"
                "<br>Frame bytes: [%{customdata[5]}, %{customdata[6]})<extra></extra>",
            ),
            row=row,
            col=1,
        )
        figure.update_yaxes(title_text=f"{label} (rad)", row=row, col=1)
    figure.update_layout(
        height=620,
        margin=dict(l=70, r=20, t=20, b=65),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Arial, sans-serif", color="#18313c"),
        showlegend=False,
        hovermode="closest",
        clickmode="event+select",
        dragmode="zoom",
    )
    figure.update_xaxes(automargin=True, gridcolor="#e7edef")
    figure.update_xaxes(title_text="Capture time (s)", title_standoff=15, row=3, col=1)
    figure.update_yaxes(automargin=True, title_standoff=15, gridcolor="#e7edef")
    return figure
