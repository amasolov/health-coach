"""
Plotly chart generators for the Chainlit chat UI.

Each function takes tool result data and returns a plotly Figure (or None
if the data is insufficient).  The chat app attaches these as inline
cl.Plotly elements.
"""

from __future__ import annotations

from typing import Any

import plotly.graph_objects as go

# Consistent dark-friendly palette
_COLORS = {
    "ctl": "#2196F3",
    "atl": "#FF5722",
    "tsb": "#4CAF50",
    "tss": "#9E9E9E",
    "weight": "#2196F3",
    "body_fat": "#FF9800",
    "muscle": "#4CAF50",
    "resting_hr": "#F44336",
    "hrv": "#2196F3",
    "sleep": "#9C27B0",
}

_LAYOUT_DEFAULTS = dict(
    template="plotly_dark",
    margin=dict(l=40, r=20, t=40, b=30),
    height=350,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)


def chart_training_load(data: list[dict]) -> go.Figure | None:
    """CTL / ATL / TSB line chart with TSS bars from get_training_load."""
    calculated = [d for d in data if d.get("source") != "projected"]
    if len(calculated) < 2:
        return None

    dates = [d["time"] for d in calculated]
    fig = go.Figure()

    tss_vals = [d.get("tss") for d in calculated]
    if any(v for v in tss_vals):
        fig.add_trace(go.Bar(
            x=dates, y=tss_vals, name="TSS",
            marker_color=_COLORS["tss"], opacity=0.3, yaxis="y2",
        ))

    fig.add_trace(go.Scatter(
        x=dates, y=[d.get("ctl") for d in calculated],
        name="CTL (fitness)", line=dict(color=_COLORS["ctl"], width=2),
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=[d.get("atl") for d in calculated],
        name="ATL (fatigue)", line=dict(color=_COLORS["atl"], width=2),
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=[d.get("tsb") for d in calculated],
        name="TSB (form)", line=dict(color=_COLORS["tsb"], width=2),
        fill="tozeroy", fillcolor="rgba(76,175,80,0.1)",
    ))

    projected = [d for d in data if d.get("source") == "projected"]
    if projected:
        fig.add_trace(go.Scatter(
            x=[d["time"] for d in projected],
            y=[d.get("ctl") for d in projected],
            name="CTL (projected)", line=dict(color=_COLORS["ctl"], width=2, dash="dot"),
        ))

    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title="Training Load (PMC)",
        yaxis=dict(title="CTL / ATL / TSB"),
        yaxis2=dict(title="TSS", overlaying="y", side="right", showgrid=False),
    )
    return fig


def chart_body_composition(data: list[dict]) -> go.Figure | None:
    """Weight and body fat % dual-axis chart from get_body_composition."""
    if len(data) < 2:
        return None

    dates = [d["time"] for d in data]
    fig = go.Figure()

    weights = [d.get("weight_kg") for d in data]
    if any(w for w in weights):
        fig.add_trace(go.Scatter(
            x=dates, y=weights,
            name="Weight (kg)", line=dict(color=_COLORS["weight"], width=2),
            mode="lines+markers", marker=dict(size=4),
        ))

    body_fat = [d.get("body_fat_pct") for d in data]
    if any(bf for bf in body_fat):
        fig.add_trace(go.Scatter(
            x=dates, y=body_fat,
            name="Body fat %", line=dict(color=_COLORS["body_fat"], width=2),
            yaxis="y2", mode="lines+markers", marker=dict(size=4),
        ))

    muscle = [d.get("muscle_mass_kg") for d in data]
    if any(m for m in muscle):
        fig.add_trace(go.Scatter(
            x=dates, y=muscle,
            name="Muscle (kg)", line=dict(color=_COLORS["muscle"], width=2),
            mode="lines+markers", marker=dict(size=4),
        ))

    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title="Body Composition",
        yaxis=dict(title="kg"),
        yaxis2=dict(title="Body fat %", overlaying="y", side="right", showgrid=False),
    )
    return fig


def chart_vitals(data: list[dict]) -> go.Figure | None:
    """Resting HR, HRV, and sleep score from get_vitals."""
    if len(data) < 2:
        return None

    dates = [d["time"] for d in data]
    fig = go.Figure()

    rhr = [d.get("resting_hr") for d in data]
    if any(v for v in rhr):
        fig.add_trace(go.Scatter(
            x=dates, y=rhr,
            name="Resting HR", line=dict(color=_COLORS["resting_hr"], width=2),
        ))

    hrv = [d.get("hrv_ms") for d in data]
    if any(v for v in hrv):
        fig.add_trace(go.Scatter(
            x=dates, y=hrv,
            name="HRV (ms)", line=dict(color=_COLORS["hrv"], width=2),
            yaxis="y2",
        ))

    sleep = [d.get("sleep_score") for d in data]
    if any(v for v in sleep):
        fig.add_trace(go.Scatter(
            x=dates, y=sleep,
            name="Sleep score", line=dict(color=_COLORS["sleep"], width=2),
            yaxis="y2",
        ))

    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title="Vitals",
        yaxis=dict(title="Resting HR (bpm)"),
        yaxis2=dict(title="HRV (ms) / Sleep", overlaying="y", side="right", showgrid=False),
    )
    return fig


def chart_activities(data: list[dict]) -> go.Figure | None:
    """TSS bar chart grouped by activity type from get_activities."""
    with_tss = [d for d in data if d.get("tss")]
    if not with_tss:
        return None

    sport_types = sorted({d.get("activity_type", "other") for d in with_tss})
    fig = go.Figure()

    for sport in sport_types:
        subset = [d for d in with_tss if d.get("activity_type") == sport]
        fig.add_trace(go.Bar(
            x=[d["time"] for d in subset],
            y=[d.get("tss") for d in subset],
            name=sport.replace("_", " ").title(),
        ))

    fig.update_layout(
        **_LAYOUT_DEFAULTS,
        title="Activity TSS",
        barmode="stack",
        yaxis=dict(title="TSS"),
    )
    return fig


# ---------------------------------------------------------------------------
# Auto-detect which chart to generate based on tool name
# ---------------------------------------------------------------------------

TOOL_CHART_MAP: dict[str, Any] = {
    "get_training_load": chart_training_load,
    "get_body_composition": chart_body_composition,
    "get_vitals": chart_vitals,
    "get_activities": chart_activities,
}


def maybe_chart(tool_name: str, result: Any) -> go.Figure | None:
    """If the tool result is chart-worthy, return a Plotly figure."""
    chart_fn = TOOL_CHART_MAP.get(tool_name)
    if not chart_fn:
        return None
    if not isinstance(result, list) or len(result) < 2:
        return None
    try:
        return chart_fn(result)
    except Exception:
        return None
