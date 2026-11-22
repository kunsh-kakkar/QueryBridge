"""Tests for backend/chart_recommender.py: form selection and design rules."""

from __future__ import annotations

import pytest

from backend.chart_recommender import (
    CATEGORICAL_DARK,
    CATEGORICAL_LIGHT,
    MAX_SERIES,
    available_chart_types,
    build_figure,
    recommend_charts,
)
from backend.result_formatter import format_result
from backend.sql_executor import ExecutionResult


def formatted(columns, rows):
    return format_result(ExecutionResult(sql="", columns=columns, rows=rows, truncated=False,
                                         execution_time_ms=1.0, max_rows=1000))


def types(result) -> list[str]:
    return [s.chart_type for s in recommend_charts(result)]


def test_single_number_is_a_stat_tile_not_a_chart():
    assert types(formatted(["total_sales"], [(1234.5,)])) == ["kpi"]


def test_empty_result_has_no_chart():
    assert recommend_charts(formatted(["a", "b"], [])) == []


def test_category_with_measure_is_a_ranked_horizontal_bar():
    result = formatted(["category", "revenue"], [("A", 10.0), ("B", 30.0), ("C", 20.0)])
    spec = recommend_charts(result)[0]
    assert (spec.chart_type, spec.orientation, spec.x, spec.y) == ("bar", "h", "category", "revenue")
    fig = build_figure(result.dataframe, spec)
    bar = fig.data[0]
    assert list(bar.x) == [10.0, 20.0, 30.0]       # ranked: largest at the top
    assert bar.marker.color == CATEGORICAL_LIGHT[0]  # one hue: no value-ramp on nominal categories
    assert fig.layout.showlegend is False            # single series: the title names it
    assert fig.layout.barcornerradius == 4


def test_repeated_categories_do_not_get_a_misleading_bar():
    rows = [(f"Person {i}", ["Sales", "Ops"][i % 2], 1000.0 + i) for i in range(40)]  # names are free text
    assert "bar" not in types(formatted(["name", "department", "salary"], rows))


def test_one_group_per_category_is_not_a_grouped_bar():
    rows = [(f"Person {i}", ["Sales", "Ops"][i % 2], 1000.0 + i) for i in range(12)]  # each name has 1 department
    specs = recommend_charts(formatted(["name", "department", "salary"], rows))
    assert all(s.color is None for s in specs if s.chart_type == "bar")


def test_time_series_is_a_line_with_crosshair():
    result = formatted(["month", "revenue"], [(f"2025-{m:02d}", float(m)) for m in range(1, 13)])
    specs = recommend_charts(result)
    assert [s.chart_type for s in specs][:2] == ["line", "area"]
    fig = build_figure(result.dataframe, specs[0])
    assert fig.layout.hovermode == "x unified"
    assert fig.data[0].line.width == 2 and fig.data[0].marker.size >= 8


def test_line_with_groups_has_legend_and_fixed_hue_order():
    rows = [(f"2025-{m:02d}", seg, float(m * (i + 1))) for m in range(1, 7) for i, seg in enumerate(["A", "B", "C"])]
    result = formatted(["month", "segment", "revenue"], rows)
    spec = recommend_charts(result)[0]
    assert spec.chart_type == "line" and spec.color == "segment"
    fig = build_figure(result.dataframe, spec)
    assert fig.layout.showlegend is True
    assert [t.line.color for t in fig.data] == CATEGORICAL_LIGHT[:3]


def test_more_than_eight_series_fold_into_other():
    rows = [(f"2025-{m:02d}", f"G{g}", float(g * 10 + m)) for m in range(1, 4) for g in range(12)]
    result = formatted(["month", "group", "value"], rows)
    spec = recommend_charts(result)[0]
    spec = spec.model_copy(update={"color": "group"})
    fig = build_figure(result.dataframe, spec)
    names = [t.name for t in fig.data]
    assert len(names) == MAX_SERIES and "Other" in names


def test_two_measures_scatter_caps_groups_at_three():
    rows = [(f"P{i}", ["a", "b", "c", "d"][i % 4], float(i), float(i * 2)) for i in range(20)]
    result = formatted(["name", "kind", "price", "units"], rows)
    scatter = next(s for s in recommend_charts(result) if s.chart_type == "scatter")
    assert scatter.color is None  # 4 groups > 3: all-pairs form gets no colour encoding
    fig = build_figure(result.dataframe, scatter)
    assert len(fig.data) == 1


def test_histogram_for_a_single_measure():
    result = formatted(["salary"], [(float(30000 + i * 1000),) for i in range(50)])
    assert types(result) == ["histogram"]
    fig = build_figure(result.dataframe, recommend_charts(result)[0])
    assert fig.layout.xaxis.title.text == "Salary"


def test_grouped_bar_for_two_categories():
    rows = [(d, s, float(i)) for i, (d, s) in enumerate([(d, s) for d in ["Eng", "Ops"] for s in ["Q1", "Q2"]])]
    result = formatted(["department", "quarter", "spend"], rows)
    spec = next(s for s in recommend_charts(result) if s.chart_type == "bar")
    assert spec.color == "quarter"
    fig = build_figure(result.dataframe, spec)
    assert fig.layout.barmode == "group" and fig.layout.showlegend is True


def test_never_a_dual_axis():
    result = formatted(["month", "revenue", "orders"], [(f"2025-{m:02d}", m * 100.0, m * 3.0) for m in range(1, 7)])
    for spec in recommend_charts(result):
        fig = build_figure(result.dataframe, spec)
        assert "yaxis2" not in fig.layout.to_plotly_json()


def test_dark_mode_uses_dark_palette_and_surface():
    result = formatted(["category", "revenue"], [("A", 1.0), ("B", 2.0)])
    fig = build_figure(result.dataframe, recommend_charts(result)[0], dark=True)
    assert fig.data[0].marker.color == CATEGORICAL_DARK[0]
    assert fig.layout.paper_bgcolor == "#1a1a19"


def test_measure_can_be_switched_and_large_values_use_compact_ticks():
    result = formatted(["category", "revenue", "units"], [("A", 120000.0, 3.0), ("B", 240000.0, 5.0)])
    spec = recommend_charts(result)[0]
    assert spec.measures == ["revenue", "units"]
    assert build_figure(result.dataframe, spec).layout.xaxis.tickformat == "~s"
    assert build_figure(result.dataframe, spec, y="units").layout.xaxis.tickformat == ","


def test_horizontal_bar_height_keeps_bars_thin():
    result = formatted(["name", "value"], [(f"Item {i}", float(i)) for i in range(30)])
    fig = build_figure(result.dataframe, recommend_charts(result)[0])
    assert fig.layout.height >= 30 * 30  # ~30px per category -> bars stay under 24px


def test_available_types_and_unknown_type():
    result = formatted(["month", "revenue"], [("2025-01", 1.0), ("2025-02", 2.0)])
    assert available_chart_types(result) == ["line", "area"]
    with pytest.raises(ValueError):
        build_figure(result.dataframe, recommend_charts(formatted(["n"], [(1.0,)]))[0])
