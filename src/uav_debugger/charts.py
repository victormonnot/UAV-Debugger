"""Bounded activity plots that count every selected record without resampling it."""

import plotly.graph_objects as go

from .analysis import timestamp_to_seconds
from .model import Record


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
