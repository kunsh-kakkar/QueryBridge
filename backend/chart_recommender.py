"""
Automatic chart recommendation and Plotly rendering.

The form is chosen by the data's job (what the reader must do), using the
column profiles from ``result_formatter``:

| result shape                                   | form                                          |
|------------------------------------------------|-----------------------------------------------|
| one number (1 row x 1 numeric column)          | **stat tile**: shown as a number, not a chart |
| time column + measure                          | **line** (area for a single series)           |
| category/label + measure                       | **ranked horizontal bar**, one hue            |
| two categories + measure                       | **grouped bar**, categorical hues in order    |
| two or more measures                           | **scatter** (colour only for <= 3 groups)     |
| one measure, many rows                         | **histogram**                                 |

Design rules followed (validated palette, see the docs): one y-axis only (a
dual axis is never offered); no pie/donut (part-to-whole uses ranked bars);
categorical hues are assigned in fixed order and never cycled, so past 8
series the tail is folded into "Other"; bars are thin with rounded data
ends; lines are 2px; gridlines are solid hairlines; text uses ink colours,
never series colours; a legend appears only for 2+ series; light and dark
palettes are each validated against their own surface. Every chart sits next
to the table view, which is the accessible equivalent.
"""

from __future__ import annotations

from typing import Literal

import pandas as pd
import plotly.graph_objects as go
from pydantic import BaseModel, Field

from backend.result_formatter import FormattedResult

ChartType = Literal["kpi", "bar", "line", "area", "scatter", "histogram"]

# Validated reference palette (light / dark), fixed slot order.
CATEGORICAL_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"]
THEMES = {
    False: {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
            "grid": "#e1e0d9", "axis": "#c3c2b7", "series": CATEGORICAL_LIGHT},
    True: {"surface": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
           "grid": "#2c2c2a", "axis": "#383835", "series": CATEGORICAL_DARK},
}
FONT = "system-ui, -apple-system, 'Segoe UI', sans-serif"
MAX_SERIES = 8            # categorical hue ceiling; the tail folds into "Other"
SCATTER_MAX_GROUPS = 3    # all-pairs forms: only the first three slots validate
MAX_BAR_CATEGORIES = 40
BAR_ROW_PX = 30           # ~18px bar + air per category (bars stay <= 24px thick)


class ChartSpec(BaseModel):
    chart_type: ChartType
    x: str | None = None
    y: str | None = None
    color: str | None = None
    orientation: Literal["v", "h"] = "v"
    title: str
    reason: str
    measures: list[str] = Field(default_factory=list, description="Alternative measures the user can pick")


def _humanize(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def recommend_charts(result: FormattedResult) -> list[ChartSpec]:
    """Ranked chart suggestions for a result; empty when no chart would be meaningful."""
    if result.is_empty:
        return []
    measures = result.columns_of("numeric")
    dims = [c for c in result.columns_of("categorical") if (p := result.profile(c)) and p.distinct > 1]
    labels = result.columns_of("text")
    times = result.columns_of("datetime")
    specs: list[ChartSpec] = []

    if result.row_count == 1 and len(result.dataframe.columns) == 1 and measures:
        return [ChartSpec(chart_type="kpi", y=measures[0], title=_humanize(measures[0]),
                          reason="A single number reads best as a stat tile, not a one-bar chart.")]

    if times and measures:
        x, y = times[0], measures[0]
        color = next((d for d in dims if result.profile(d).distinct <= MAX_SERIES), None)
        specs.append(ChartSpec(chart_type="line", x=x, y=y, color=color, measures=measures,
                               title=f"{_humanize(y)} over {_humanize(x).lower()}",
                               reason="A date column with a measure shows a trend over time."))
        if color is None:
            specs.append(ChartSpec(chart_type="area", x=x, y=y, measures=measures,
                                   title=f"{_humanize(y)} over {_humanize(x).lower()}",
                                   reason="A single series over time can also be shown as an area."))

    category = dims[0] if dims else (labels[0] if labels else None)
    frame = result.dataframe
    if category and measures and result.row_count > 1:
        distinct = result.profile(category).distinct
        y = measures[0]
        # Bars draw exactly what the table shows: one row per bar (charts never silently aggregate).
        # Grouped bars need a real cross-tab: unique (x, group) pairs, several groups per x, <= 8 groups.
        cross_tab = (
            len(dims) >= 2
            and not frame.duplicated(subset=[dims[0], dims[1]]).any()
            and result.profile(dims[1]).distinct <= MAX_SERIES
            and frame.groupby(dims[0])[dims[1]].nunique().mean() > 1
        )
        if cross_tab:
            specs.append(ChartSpec(chart_type="bar", x=dims[0], y=y, color=dims[1], measures=measures,
                                   title=f"{_humanize(y)} by {_humanize(dims[0]).lower()} and "
                                         f"{_humanize(dims[1]).lower()}",
                                   reason="Two categories and a measure: grouped bars, one colour per group."))
        elif distinct == result.row_count and distinct <= MAX_BAR_CATEGORIES:
            specs.append(ChartSpec(chart_type="bar", x=category, y=y, orientation="h", measures=measures,
                                   title=f"{_humanize(y)} by {_humanize(category).lower()}",
                                   reason="A category with a measure: ranked bars compare magnitudes exactly."))

    if len(measures) >= 2 and result.row_count >= 3:
        group = next((d for d in dims if result.profile(d).distinct <= SCATTER_MAX_GROUPS), None)
        specs.append(ChartSpec(chart_type="scatter", x=measures[0], y=measures[1], color=group,
                               title=f"{_humanize(measures[1])} vs {_humanize(measures[0]).lower()}",
                               reason="Two measures: a scatter plot shows how they relate."))

    if measures and result.row_count >= 10 and not dims and not times:
        specs.append(ChartSpec(chart_type="histogram", x=measures[0], measures=measures,
                               title=f"Distribution of {_humanize(measures[0]).lower()}",
                               reason="Many values of one measure: a histogram shows their distribution."))
    return specs


def available_chart_types(result: FormattedResult) -> list[ChartType]:
    return list(dict.fromkeys(s.chart_type for s in recommend_charts(result)))


# =============================================================================
# Rendering
# =============================================================================
def _fold_series(frame: pd.DataFrame, color: str, value: str) -> pd.DataFrame:
    """Keep the 7 largest groups and fold the rest into 'Other' (never a 9th hue)."""
    totals = frame.groupby(color, dropna=False)[value].sum().sort_values(ascending=False)
    if len(totals) <= MAX_SERIES:
        return frame
    keep = set(totals.index[: MAX_SERIES - 1])
    folded = frame.copy()
    folded[color] = folded[color].where(folded[color].isin(keep), "Other")
    return folded


def _style(fig: go.Figure, theme: dict, *, legend: bool, height: int, value_axis: str,
           max_value: float | None) -> go.Figure:
    tickformat = "~s" if max_value is not None and abs(max_value) >= 10_000 else ","
    axis = dict(gridcolor=theme["grid"], gridwidth=1, linecolor=theme["axis"], zerolinecolor=theme["axis"],
                tickfont=dict(color=theme["muted"], size=12), title_font=dict(color=theme["ink2"], size=12),
                showline=True, linewidth=1, automargin=True, title_standoff=12)  # labels are never clipped
    fig.update_layout(
        height=height, margin=dict(l=8, r=16, t=48 if legend else 16, b=8),
        paper_bgcolor=theme["surface"], plot_bgcolor=theme["surface"],
        font=dict(family=FONT, color=theme["ink"], size=13),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    font=dict(color=theme["ink2"]), title_text=""),
        hoverlabel=dict(bgcolor=theme["surface"], bordercolor=theme["axis"], font=dict(color=theme["ink"], family=FONT)),
        bargap=0.35, bargroupgap=0.08, barcornerradius=4,
    )
    fig.update_xaxes(**axis, showgrid=value_axis == "x")
    fig.update_yaxes(**axis, showgrid=value_axis == "y")
    (fig.update_xaxes if value_axis == "x" else fig.update_yaxes)(tickformat=tickformat)
    return fig


def build_figure(frame: pd.DataFrame, spec: ChartSpec, *, dark: bool = False, y: str | None = None) -> go.Figure:
    """Render a ``ChartSpec`` (optionally with another measure ``y``) as a styled Plotly figure."""
    theme = THEMES[dark]
    palette = theme["series"]
    y = y or spec.y
    fig = go.Figure()

    if spec.chart_type == "bar" and spec.orientation == "h":
        data = frame[[spec.x, y]].dropna().sort_values(y, ascending=True)
        fig.add_trace(go.Bar(x=data[y], y=data[spec.x].astype(str), orientation="h",
                             marker=dict(color=palette[0], line=dict(width=0)),
                             hovertemplate=f"%{{y}}<br>{_humanize(y)}: %{{x:,.2f}}<extra></extra>"))
        height = max(220, BAR_ROW_PX * len(data) + 80)
        fig.update_xaxes(title_text=_humanize(y))
        fig.update_yaxes(title_text="", automargin=True)
        return _style(fig, theme, legend=False, height=height, value_axis="x", max_value=data[y].abs().max())

    if spec.chart_type == "bar":
        data = _fold_series(frame.dropna(subset=[y]), spec.color, y) if spec.color else frame
        groups = list(dict.fromkeys(data[spec.color].astype(str))) if spec.color else [None]
        for index, group in enumerate(groups):
            part = data[data[spec.color].astype(str) == group] if group is not None else data
            fig.add_trace(go.Bar(x=part[spec.x].astype(str), y=part[y], name=group or _humanize(y),
                                 marker=dict(color=palette[index % MAX_SERIES], line=dict(width=0)),
                                 hovertemplate=f"%{{x}}<br>{_humanize(y)}: %{{y:,.2f}}<extra>{group or ''}</extra>"))
        fig.update_layout(barmode="group")
        fig.update_yaxes(title_text=_humanize(y))
        return _style(fig, theme, legend=len(groups) > 1, height=380, value_axis="y", max_value=data[y].abs().max())

    if spec.chart_type in ("line", "area"):
        data = frame.dropna(subset=[spec.x, y]).sort_values(spec.x)
        if spec.color:
            data = _fold_series(data, spec.color, y)
        groups = list(dict.fromkeys(data[spec.color].astype(str))) if spec.color else [None]
        for index, group in enumerate(groups):
            part = data[data[spec.color].astype(str) == group] if group is not None else data
            color = palette[index % MAX_SERIES]
            fig.add_trace(go.Scatter(
                x=part[spec.x], y=part[y], name=group or _humanize(y),
                mode="lines+markers" if len(part) <= 24 else "lines",
                line=dict(color=color, width=2, shape="linear"),
                marker=dict(size=8, color=color, line=dict(width=2, color=theme["surface"])),
                fill="tozeroy" if spec.chart_type == "area" else None,
                fillcolor=_with_alpha(color, 0.10) if spec.chart_type == "area" else None,
                hovertemplate=f"%{{x}}<br>{_humanize(y)}: %{{y:,.2f}}<extra>{group or ''}</extra>",
            ))
        fig.update_layout(hovermode="x unified")
        fig.update_yaxes(title_text=_humanize(y))
        return _style(fig, theme, legend=len(groups) > 1, height=380, value_axis="y", max_value=data[y].abs().max())

    if spec.chart_type == "scatter":
        data = frame.dropna(subset=[spec.x, spec.y])
        groups = list(dict.fromkeys(data[spec.color].astype(str)))[:SCATTER_MAX_GROUPS] if spec.color else [None]
        for index, group in enumerate(groups):
            part = data[data[spec.color].astype(str) == group] if group is not None else data
            fig.add_trace(go.Scatter(
                x=part[spec.x], y=part[spec.y], name=group or "", mode="markers",
                marker=dict(size=9, color=palette[index], line=dict(width=2, color=theme["surface"])),
                hovertemplate=(f"{_humanize(spec.x)}: %{{x:,.2f}}<br>{_humanize(spec.y)}: %{{y:,.2f}}"
                               f"<extra>{group or ''}</extra>")))
        fig.update_xaxes(title_text=_humanize(spec.x))
        fig.update_yaxes(title_text=_humanize(spec.y))
        styled = _style(fig, theme, legend=len(groups) > 1, height=380, value_axis="y",
                        max_value=data[spec.y].abs().max())
        styled.update_xaxes(showgrid=True)
        return styled

    if spec.chart_type == "histogram":
        x = y or spec.x
        data = frame[x].dropna()
        fig.add_trace(go.Histogram(x=data, marker=dict(color=palette[0], line=dict(width=2, color=theme["surface"])),
                                   hovertemplate=f"{_humanize(x)}: %{{x}}<br>Rows: %{{y}}<extra></extra>"))
        fig.update_xaxes(title_text=_humanize(x))
        fig.update_yaxes(title_text="Rows")
        return _style(fig, theme, legend=False, height=340, value_axis="y", max_value=None)

    raise ValueError(f"Chart type '{spec.chart_type}' is not rendered as a figure")


def _with_alpha(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"
