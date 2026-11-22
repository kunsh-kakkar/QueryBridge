"""
Relevant-schema selection.

For every question, ``SchemaRetriever`` scores each table using transparent
lexical evidence. Every point is explained in ``SchemaContext.reasons`` and
shown in the Prompt Lab.

| Evidence                                            | Points            |
|-----------------------------------------------------|-------------------|
| table-name token in the question (share of tokens)  | up to 3.0         |
| data-dictionary synonym ("staff" -> employees)      | 2.5               |
| non-key column-name token ("salary")                | 1.5 each, cap 4.5 |
| a sampled value in the question ("London", "On Hold")| 2.0 each, cap 4.0 |
| description words                                   | 0.3 each, cap 1.0 |
| table used in the previous turn (follow-ups)        | 2.0, always kept  |

Words that come from the generic business lexicon (e.g. "revenue" -> sale)
count 0.8x. Foreign-key and primary-key columns are not scored, because their
tokens name *other* tables (``sales.employee_id`` is not evidence for
``sales``).

Selection modes:

* ``full``: the schema is small (<= ``full_context_max_tables``), so all tables
  are sent in fixed order (stable and cacheable); the ranking is a hint only.
* ``retrieved``: tables above ``min_score`` (max ``max_tables``), plus
  **bridge** tables on the shortest FK path between selected tables (e.g.
  ``sales`` for "customers who bought Electronics"), plus **lookup** tables
  they reference, while room remains.
* ``fallback``: nothing matched confidently; the highest-scoring and
  best-connected tables are used.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal

from backend.models import SchemaContext, SchemaModel, TableInfo

_WORD = re.compile(r"[a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

STOPWORDS = frozenset("""
a an the of in on at for with by to from and or not no all any each every some what which who whom
whose how many much is are was were be been being am do does did have has had show list give find get
display fetch return tell me us i we our my you your their them they it its this that these those there
here than then more less most least top bottom highest lowest high low greater fewer number total
average avg sum count max min maximum minimum per please now also only just between during over under
above below as into out up down about can could would should will want need see sort sorted order
ordered group grouped arrange rank ranked descending ascending desc asc first last next previous year
years month months day days week weeks quarter quarters date dates time today yesterday since before
after ago recent recently new old current same different whether if where when why one two three five
ten
""".split())

# Column tokens that say little on their own ("employee_name" -> "name").
GENERIC_COLUMN_TOKENS = frozenset({"id", "name", "date", "code", "type", "key", "no", "num", "number", "value"})

# Domain-agnostic business vocabulary: question word -> schema words it suggests.
GENERIC_SYNONYMS: dict[str, tuple[str, ...]] = {
    "staff": ("employee",), "worker": ("employee",), "personnel": ("employee",),
    "people": ("employee",), "colleague": ("employee",), "headcount": ("employee",),
    "hire": ("employee", "joining"), "hired": ("employee", "joining"), "joined": ("joining",),
    "earning": ("salary",), "earn": ("salary",), "paid": ("salary",), "pay": ("salary",),
    "wage": ("salary",), "income": ("salary",), "compensation": ("salary",), "payroll": ("salary",),
    "revenue": ("sale", "amount"), "sold": ("sale",), "sell": ("sale",), "selling": ("sale",),
    "seller": ("sale", "employee"), "purchase": ("sale",), "purchased": ("sale",), "bought": ("sale",),
    "buy": ("sale",), "transaction": ("sale",), "deal": ("sale",), "spent": ("sale", "amount"),
    "spend": ("sale", "amount"), "client": ("customer",), "buyer": ("customer",),
    "item": ("product",), "goods": ("product",), "merchandise": ("product",),
    "inventory": ("stock", "product"), "stock": ("stock", "product"),
    "boss": ("manager",), "supervisor": ("manager",), "report": ("manager",),
    "rated": ("rating",), "score": ("rating",), "appraisal": ("review",), "rating": ("review",),
    "performance": ("review", "rating", "sale", "project"),
    "performing": ("review", "rating", "sale", "project"),
    "performer": ("review", "rating", "sale", "project"),
    "dept": ("department",), "division": ("department",), "team": ("department", "employee"),
    "assigned": ("project",), "assignment": ("project",),
    "located": ("city", "location", "country"), "based": ("city", "location", "country"),
    "cost": ("price", "budget"), "expensive": ("price",), "cheap": ("price",), "cheapest": ("price",),
    "registered": ("registration",), "signup": ("registration",),
}
SYNONYM_WEIGHT = 0.8

W_NAME, W_SYNONYM, W_COLUMN, W_COLUMN_GENERIC = 3.0, 2.5, 1.5, 0.25
W_VALUE, W_DESCRIPTION, W_PREVIOUS = 2.0, 0.3, 2.0
CAP_COLUMNS, CAP_VALUES, CAP_DESCRIPTION = 4.5, 4.0, 1.0


def singularize(word: str) -> str:
    """Light English singulariser for matching ('salaries' -> 'salary')."""
    if len(word) <= 3 or word.endswith(("ss", "us", "is")):
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def tokenize_identifier(name: str) -> list[str]:
    """'employee_projects' / 'saleAmount' -> ['employee', 'project'] / ['sale', 'amount']."""
    spaced = _CAMEL.sub(" ", name).lower()
    return [singularize(tok) for tok in _WORD.findall(spaced)]


def tokenize_text(text: str) -> list[str]:
    """Question/description tokens: lower-cased, singularised, no stopwords or numbers."""
    return [
        singularize(tok) for tok in _WORD.findall(text.lower())
        if tok not in STOPWORDS and not tok.isdigit()
    ]


_SYNONYM_INDEX = {singularize(key): value for key, value in GENERIC_SYNONYMS.items()}


def expand_question_tokens(question: str) -> dict[str, float]:
    """Question tokens with weights: 1.0 for literal words, 0.8 for lexicon expansions."""
    weights: dict[str, float] = {}
    for token in tokenize_text(question):
        weights[token] = 1.0
        for extra in _SYNONYM_INDEX.get(token, ()):
            weights.setdefault(extra, SYNONYM_WEIGHT)
    return weights


@dataclass
class _TableIndex:
    table: TableInfo
    name_tokens: tuple[str, ...]
    synonym_tokens: set[str] = field(default_factory=set)
    synonym_phrases: list[str] = field(default_factory=list)
    column_tokens: dict[str, set[str]] = field(default_factory=dict)
    description_tokens: set[str] = field(default_factory=set)
    values: dict[str, list[str]] = field(default_factory=dict)


class SchemaRetriever:
    """Select the tables relevant to a question, and explain why."""

    def __init__(
        self,
        schema: SchemaModel,
        *,
        full_context_max_tables: int = 12,
        max_tables: int = 6,
        bridge_allowance: int = 2,
        min_score: float = 2.0,
    ) -> None:
        self.schema = schema
        self.full_context_max_tables = full_context_max_tables
        self.max_tables = max_tables
        self.bridge_allowance = bridge_allowance
        self.min_score = min_score
        self._indexes = [self._build_index(t) for t in schema.tables]

    @classmethod
    def from_settings(cls, schema: SchemaModel, settings) -> "SchemaRetriever":
        return cls(
            schema,
            full_context_max_tables=settings.schema_full_context_max_tables,
            max_tables=settings.schema_max_tables,
        )

    # --------------------------------------------------------------- indexing
    @staticmethod
    def _build_index(table: TableInfo) -> _TableIndex:
        index = _TableIndex(table=table, name_tokens=tuple(dict.fromkeys(tokenize_identifier(table.name))))
        own_name = set(index.name_tokens)
        for synonym in table.synonyms:
            tokens = tokenize_text(synonym)
            if len(tokens) == 1 and tokens[0] not in own_name:  # name matches are scored already
                index.synonym_tokens.add(tokens[0])
            elif len(tokens) > 1:
                index.synonym_phrases.append(" ".join(tokens))
        descriptions = [table.description or ""]
        for column in table.columns:
            descriptions.append(column.description or "")
            if column.is_primary_key or column.foreign_key:
                continue
            # 'product_name' in products: the 'product' prefix repeats the table
            # name and is not extra evidence; only 'name' remains (generic).
            index.column_tokens[column.name] = set(tokenize_identifier(column.name)) - own_name
            long_enough = [v.lower() for v in column.sample_values if len(v) >= 3]
            if long_enough:
                index.values[column.name] = long_enough
        index.description_tokens = set(tokenize_text(" ".join(descriptions)))
        return index

    # ---------------------------------------------------------------- scoring
    def _score(self, index: _TableIndex, tokens: dict[str, float], question_text: str,
               question_lower: str, previous: set[str]) -> tuple[float, list[str]]:
        score = 0.0
        reasons: list[str] = []

        hits = [tokens[t] for t in index.name_tokens if t in tokens]
        if hits:
            points = W_NAME * sum(hits) / len(index.name_tokens)
            score += points
            reasons.append(f"table name matches the question (+{points:.1f})")

        synonym_hits = [tokens[t] for t in index.synonym_tokens if t in tokens]
        phrase_hit = any(phrase in question_text for phrase in index.synonym_phrases)
        if synonym_hits or phrase_hit:
            points = W_SYNONYM * max(synonym_hits or [1.0])
            score += points
            reasons.append(f"business synonym matches (+{points:.1f})")

        column_points = 0.0
        matched_columns: list[str] = []
        for column, column_tokens in index.column_tokens.items():
            meaningful = column_tokens - GENERIC_COLUMN_TOKENS
            meaningful_hits = [tokens[t] for t in meaningful if t in tokens]
            if meaningful_hits:
                column_points += W_COLUMN * max(meaningful_hits)
                matched_columns.append(column)
            elif column_tokens & tokens.keys():
                column_points += W_COLUMN_GENERIC
        if column_points:
            column_points = min(column_points, CAP_COLUMNS)
            score += column_points
            if matched_columns:
                reasons.append(f"columns {', '.join(matched_columns)} match (+{column_points:.1f})")

        value_points = 0.0
        matched_values: list[str] = []
        for column, values in index.values.items():
            for value in values:
                if re.search(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", question_lower):
                    value_points += W_VALUE
                    matched_values.append(f"{column}='{value}'")
                    break
        if value_points:
            value_points = min(value_points, CAP_VALUES)
            score += value_points
            reasons.append(f"question mentions stored value {', '.join(matched_values)} (+{value_points:.1f})")

        description_hits = len(index.description_tokens & tokens.keys())
        if description_hits:
            points = min(description_hits * W_DESCRIPTION, CAP_DESCRIPTION)
            score += points

        if index.table.name.lower() in previous:
            score += W_PREVIOUS
            reasons.append(f"used in the previous question (+{W_PREVIOUS:.1f})")

        return round(score, 2), reasons

    def score_tables(self, question: str, previous_tables: Iterable[str] = ()) -> dict[str, tuple[float, list[str]]]:
        """Score every table for ``question`` -> {table: (score, reasons)}."""
        tokens = expand_question_tokens(question)
        question_lower = question.lower()
        question_text = " ".join(tokenize_text(question))
        previous = {t.lower() for t in previous_tables}
        return {
            idx.table.name: self._score(idx, tokens, question_text, question_lower, previous)
            for idx in self._indexes
        }

    # -------------------------------------------------------------- selection
    def retrieve(
        self,
        question: str,
        *,
        previous_tables: Iterable[str] = (),
        mode: Literal["auto", "full", "retrieved"] = "auto",
    ) -> SchemaContext:
        previous_tables = list(previous_tables)  # iterated twice below
        scored = self.score_tables(question, previous_tables)
        scores = {name: s for name, (s, _) in scored.items()}
        reasons = {name: list(r) for name, (_, r) in scored.items() if r}
        ranked = sorted((n for n in scores if scores[n] > 0), key=lambda n: (-scores[n], n))
        confident = [n for n in ranked if scores[n] >= self.min_score]

        use_full = mode == "full" or (mode == "auto" and len(self.schema.tables) <= self.full_context_max_tables)
        if use_full:
            return SchemaContext(
                tables=list(self.schema.tables),
                relationships=self.schema.relationships,
                selection_mode="full",
                ranked_tables=confident,
                scores=scores,
                reasons=reasons,
            )

        selected: list[str] = confident[: self.max_tables]
        selection_mode: Literal["retrieved", "fallback"] = "retrieved"
        if not selected:
            selection_mode = "fallback"
            by_connectivity = sorted(
                self.schema.table_names,
                key=lambda n: (-scores[n], -len(self.schema.neighbors(n)), n),
            )
            selected = by_connectivity[: self.max_tables]
            for name in selected:
                reasons.setdefault(name, []).append("fallback: no confident match, well-connected table")

        # Follow-ups refine the previous query, so its tables are always kept.
        for name in previous_tables:
            table = self.schema.table(name)
            if table is not None and table.name not in selected:
                selected.append(table.name)
                reasons.setdefault(table.name, []).append("kept from the previous question")

        self._add_bridges(selected, reasons)
        self._add_lookups(selected, reasons)

        chosen = {n.lower() for n in selected}
        tables = [t for t in self.schema.tables if t.name.lower() in chosen]  # stable schema order
        return SchemaContext(
            tables=tables,
            relationships=self.schema.relationships_among(chosen),
            selection_mode=selection_mode,
            ranked_tables=confident,
            scores=scores,
            reasons={n: r for n, r in reasons.items() if n.lower() in chosen},
            omitted_tables=[t.name for t in self.schema.tables if t.name.lower() not in chosen],
        )

    def _add_bridges(self, selected: list[str], reasons: dict[str, list[str]]) -> None:
        """Add the intermediate tables needed to join every pair of selected tables."""
        limit = self.max_tables + self.bridge_allowance
        for a, b in combinations(list(selected), 2):
            path = self.schema.find_join_path(a, b)
            if not path or len(path) < 2:
                continue
            current = a
            for rel in path[:-1]:
                current = rel.other(current)
                if current not in selected and len(selected) < limit:
                    selected.append(current)
                    reasons.setdefault(current, []).append(f"bridge table joining {a} and {b}")

    def _add_lookups(self, selected: list[str], reasons: dict[str, list[str]]) -> None:
        """While room remains, add tables the selected ones reference (names, labels)."""
        for name in list(selected):
            table = self.schema.table(name)
            if table is None:
                continue
            for fk in table.foreign_keys:
                parent = self.schema.table(fk.referred_table)
                if parent is None or parent.name in selected or len(selected) >= self.max_tables:
                    continue
                selected.append(parent.name)
                reasons.setdefault(parent.name, []).append(f"lookup table referenced by {name}")
