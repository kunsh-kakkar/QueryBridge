"""
Evaluation reporting: comparison chart, Markdown/CSV tables and an HTML report.

The chart follows the project's data-viz rules: one y-axis (percent), fixed
categorical hue order per configuration (A, B, D, C), direct value labels
(mandatory with four series), a hairline grid and a legend.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import plotly.graph_objects as go

from backend.chart_recommender import FONT, THEMES
from config import PROJECT_ROOT

SUMMARY_PATH = PROJECT_ROOT / "evaluation" / "results" / "summary.json"

METRIC_LABELS: dict[str, str] = {
    "overall_accuracy": "Overall accuracy",
    "result_accuracy": "Result-set accuracy",
    "execution_accuracy": "Execution accuracy",
    "exact_match": "Exact SQL match",
    "schema_compliance": "Schema compliance",
    "safety_accuracy": "Safety accuracy",
    "ambiguity_f1": "Ambiguity detection (F1)",
}
TABLE_METRICS = list(METRIC_LABELS) + ["ambiguity_precision", "ambiguity_recall", "decline_accuracy",
                                       "false_refusal_rate"]
EXTRA_LABELS = {"ambiguity_precision": "Ambiguity precision", "ambiguity_recall": "Ambiguity recall",
                "decline_accuracy": "Unanswerable handled", "false_refusal_rate": "False refusal rate"}


def load_summary(path: Path = SUMMARY_PATH) -> dict | None:
    """The last measured evaluation summary, or None if no real run has been saved yet."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def label(metric: str) -> str:
    return METRIC_LABELS.get(metric) or EXTRA_LABELS.get(metric, metric)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def comparison_figure(summary: dict[str, Any], *, dark: bool = False) -> go.Figure:
    theme = THEMES[dark]
    metrics = [m for m in METRIC_LABELS
               if all(c["metrics"].get(m) is not None for c in summary["configs"])]
    fig = go.Figure()
    for index, config in enumerate(summary["configs"]):
        values = [config["metrics"][m] for m in metrics]
        fig.add_trace(go.Bar(
            name=f"{config['name']}: {config['label']}", x=[METRIC_LABELS[m] for m in metrics], y=values,
            marker=dict(color=theme["series"][index], line=dict(width=0)),
            text=[f"{v:.0%}" for v in values], textposition="outside", textfont=dict(color=theme["ink2"], size=11),
            hovertemplate="%{x}<br>%{y:.1%}<extra>" + config["name"] + "</extra>", cliponaxis=False))
    fig.update_layout(
        barmode="group", bargap=0.28, bargroupgap=0.08, barcornerradius=4, height=460,
        margin=dict(l=8, r=8, t=64, b=8), paper_bgcolor=theme["surface"], plot_bgcolor=theme["surface"],
        font=dict(family=FONT, color=theme["ink"], size=13),
        legend=dict(orientation="h", yanchor="bottom", y=1.04, xanchor="left", x=0, font=dict(color=theme["ink2"])),
        hoverlabel=dict(bgcolor=theme["surface"], font=dict(color=theme["ink"])))
    fig.update_yaxes(range=[0, 1.12], tickformat=".0%", gridcolor=theme["grid"], linecolor=theme["axis"],
                     tickfont=dict(color=theme["muted"]), automargin=True)
    fig.update_xaxes(linecolor=theme["axis"], tickfont=dict(color=theme["ink2"]), showgrid=False, automargin=True)
    return fig


def markdown_table(summary: dict[str, Any]) -> str:
    configs = summary["configs"]
    header = "| Metric | " + " | ".join(f"{c['name']}: {c['label']}" for c in configs) + " |"
    lines = [header, "|" + "---|" * (len(configs) + 1)]
    for metric in TABLE_METRICS:
        lines.append(f"| {label(metric)} | " + " | ".join(_pct(c["metrics"].get(metric)) for c in configs) + " |")
    lines.append("| Avg. LLM calls / question | " + " | ".join(
        f"{c['operational']['avg_llm_calls']:.2f}" for c in configs) + " |")
    lines.append("| Avg. latency / question | " + " | ".join(
        f"{c['operational']['avg_latency_ms'] / 1000:.1f} s" for c in configs) + " |")
    lines.append("| Total cost (USD, est.) | " + " | ".join(
        f"${c['operational']['total_cost_usd']:.2f}" for c in configs) + " |")
    return "\n".join(lines)


def category_table(summary: dict[str, Any]) -> str:
    names = [c["name"] for c in summary["configs"]]
    lines = ["| Category | n | " + " | ".join(names) + " |", "|---|---|" + "---|" * len(names)]
    for category, row in summary["by_category"].items():
        lines.append(f"| {category} | {row['n']} | " + " | ".join(_pct(row.get(n)) for n in names) + " |")
    return "\n".join(lines)


def csv_text(summary: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["config", "label", *TABLE_METRICS, "avg_llm_calls", "avg_latency_ms", "total_cost_usd"])
    for c in summary["configs"]:
        writer.writerow([c["name"], c["label"], *[c["metrics"].get(m) for m in TABLE_METRICS],
                         c["operational"]["avg_llm_calls"], c["operational"]["avg_latency_ms"],
                         c["operational"]["total_cost_usd"]])
    return buffer.getvalue()


def write_reports(summary: dict[str, Any], directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = {
        "markdown": directory / "comparison.md",
        "csv": directory / "comparison.csv",
        "html": directory / "comparison.html",
    }
    meta = (f"Model: {summary['model']} · {summary['dataset']['items']} questions · reference date "
            f"{summary['reference_date']} · run {summary['generated_at']}")
    paths["markdown"].write_text(
        f"# QueryBridge evaluation\n\n{meta}\n\n## Metrics by configuration\n\n{markdown_table(summary)}\n\n"
        f"## Accuracy by category\n\n{category_table(summary)}\n\n"
        f"Database unchanged after the run: {'yes' if summary['database_unchanged'] else 'NO'} · "
        f"validator red-team: {summary['validator_red_team']['blocked']}/{summary['validator_red_team']['attacks']} "
        f"attacks blocked.\n", encoding="utf-8")
    paths["csv"].write_text(csv_text(summary), encoding="utf-8")
    comparison_figure(summary).write_html(paths["html"], include_plotlyjs="cdn", full_html=True,
                                          config={"displaylogo": False})
    return paths
