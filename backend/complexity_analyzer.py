"""
SQL complexity analysis from the AST.

Counts structural features (joins, subqueries, CTEs, aggregates, grouping,
HAVING, window functions, set operations, CASE expressions, predicates) and
turns them into a transparent weighted score and a level:

    Simple (< 3)  ·  Moderate (3-7)  ·  Complex (>= 8)

Used for the UI badge, for per-complexity breakdowns in the evaluation,
and as a signal in the composite confidence (complex queries are riskier).
"""

from __future__ import annotations

from typing import Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError

from backend.sql_validator import SQLGLOT_DIALECTS

ComplexityLevel = Literal["Simple", "Moderate", "Complex"]

WEIGHTS: dict[str, float] = {
    "joins": 2.0, "subqueries": 3.0, "ctes": 2.0, "aggregates": 1.0, "group_by": 1.0,
    "having": 2.0, "window_functions": 3.0, "set_operations": 2.0, "case_expressions": 1.0,
    "extra_predicates": 0.5, "distinct": 0.5, "self_joins": 1.0,
}
LABELS: dict[str, str] = {
    "joins": "join", "subqueries": "subquery", "ctes": "CTE", "aggregates": "aggregate function",
    "group_by": "GROUP BY", "having": "HAVING clause", "window_functions": "window function",
    "set_operations": "set operation", "case_expressions": "CASE expression",
    "extra_predicates": "additional filter condition", "distinct": "DISTINCT", "self_joins": "self-join",
}


class ComplexityReport(BaseModel):
    score: float
    level: ComplexityLevel
    features: dict[str, int] = Field(default_factory=dict)
    tables: int = 0
    reasons: list[str] = Field(default_factory=list)


def _level(score: float) -> ComplexityLevel:
    if score < 3:
        return "Simple"
    if score < 8:
        return "Moderate"
    return "Complex"


def _extra_conditions(tree: exp.Expression) -> int:
    """Conditions beyond the first in each WHERE/HAVING (each AND/OR adds one)."""
    total = 0
    for clause in list(tree.find_all(exp.Where)) + list(tree.find_all(exp.Having)):
        total += sum(1 for node in clause.this.walk() if isinstance(node, exp.Connector)
                     and node.parent_select is clause.parent_select)
    return total


def _self_joins(tree: exp.Expression) -> int:
    """Tables repeated within one SELECT's own FROM/JOIN list (e.g. employees joined to employees)."""
    repeats = 0
    for select in tree.find_all(exp.Select):
        names: list[str] = []
        source = select.args.get("from_") or select.args.get("from")
        if source is not None and isinstance(source.this, exp.Table):
            names.append(source.this.name.lower())
        for join in select.args.get("joins") or []:
            if isinstance(join.this, exp.Table):
                names.append(join.this.name.lower())
        repeats += len(names) - len(set(names))
    return repeats


def _count_subqueries(tree: exp.Expression) -> int:
    """SELECTs nested inside the query: not the main query, a top-level UNION branch or a CTE body."""
    top_level: set[int] = set()

    def mark(node: exp.Expression | None) -> None:
        if isinstance(node, exp.Select):
            top_level.add(id(node))
        elif isinstance(node, exp.SetOperation):
            mark(node.left)
            mark(node.right)
        elif isinstance(node, exp.Subquery):
            mark(node.this)

    mark(tree)
    for cte in tree.find_all(exp.CTE):
        mark(cte.this)
    return sum(1 for select in tree.find_all(exp.Select) if id(select) not in top_level)


def analyze_complexity(sql: str, dialect: str = "sqlite") -> ComplexityReport:
    """Score the structural complexity of a SELECT statement."""
    try:
        tree = sqlglot.parse_one(sql, read=SQLGLOT_DIALECTS.get(dialect, dialect))
    except ParseError:
        return ComplexityReport(score=0.0, level="Simple", reasons=["could not be parsed"])

    base_tables = [t for t in tree.find_all(exp.Table) if t.name]
    table_names = [t.name.lower() for t in base_tables]
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    real_tables = [n for n in table_names if n not in cte_names]

    features = {
        "joins": sum(1 for _ in tree.find_all(exp.Join)),
        "subqueries": _count_subqueries(tree),
        "ctes": len(cte_names),
        "aggregates": sum(1 for _ in tree.find_all(exp.AggFunc)),
        "group_by": sum(1 for _ in tree.find_all(exp.Group)),
        "having": sum(1 for _ in tree.find_all(exp.Having)),
        "window_functions": sum(1 for _ in tree.find_all(exp.Window)),
        "set_operations": sum(1 for _ in tree.find_all(exp.SetOperation)),
        "case_expressions": sum(1 for _ in tree.find_all(exp.Case)),
        "extra_predicates": _extra_conditions(tree),
        "distinct": sum(1 for _ in tree.find_all(exp.Distinct)),
        "self_joins": _self_joins(tree),
    }
    score = round(sum(WEIGHTS[k] * v for k, v in features.items()), 1)
    reasons = [f"{v} {LABELS[k]}{'s' if v != 1 else ''}" for k, v in features.items() if v]
    return ComplexityReport(score=score, level=_level(score), features=features,
                            tables=len(set(real_tables)), reasons=reasons)
