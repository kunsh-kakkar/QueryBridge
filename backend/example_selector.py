"""
Context-aware few-shot example selection.

Instead of always showing the same examples, ``ExampleSelector`` picks the k
demonstrations whose SQL patterns best match the question:

1. **Pattern detection**: interpretable regex rules map the question to SQL
   pattern tags ("top 5" -> order_limit; "above the average" ->
   comparison_to_aggregate; "never" -> anti_join; "per month" -> date_grouping).
   Join tags come from the schema retriever's hint (how many tables look relevant).
2. **Scoring**: weighted tag overlap (rarer, harder patterns weigh more),
   minus a small penalty for extra patterns the question does not need, plus
   word-overlap similarity of the question wording.
3. **Diversity** (greedy, MMR-style): each pick is penalised by its tag
   overlap with examples already chosen, so k slots are not spent on near
   duplicates.
4. **Behaviour examples** (clarify / refuse / missing data / follow-up) are
   only eligible when their trigger is detected; at most one is used.
5. Remaining slots are filled with broad core patterns, so the prompt always
   shows k examples.

The selected examples are ordered with the most relevant last, right before
the question, where models pay the most attention.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from backend.models import FewShotExample, SelectedExample
from prompts.few_shot_examples import CORE_FILL_ORDER, EXAMPLES_BY_ID, FEW_SHOT_EXAMPLES

_F = re.IGNORECASE

_SUPERLATIVES = (r"most|least|highest|lowest|largest|smallest|biggest|best|worst|cheapest|"
                 r"top|maximum|minimum|longest|shortest|oldest|newest|latest|earliest|greatest|fewest")
_MONTHS = (r"january|february|march|april|may|june|july|august|september|october|november|december|"
           r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
_PERIOD = r"day|week|month|quarter|year"

# tag -> pattern (searched in the lower-cased question)
TAG_PATTERNS: dict[str, re.Pattern[str]] = {
    "superlative": re.compile(rf"\b({_SUPERLATIVES})\b", _F),
    # "last 90 days" is a date range, not a top-N request
    "order_limit": re.compile(rf"\b(top|bottom|first|last)\s+\d+\b(?!\s+({_PERIOD})s?\b)|"
                              rf"\b\d+\s+({_SUPERLATIVES})\b|\btop\b", _F),
    "order": re.compile(r"\b(sort|sorted|order(ed)? by|rank(ed)?|alphabetical(ly)?|ascending|descending|"
                        r"first|from (the )?(highest|lowest|most|least))\b", _F),
    "aggregate": re.compile(r"\b(how many|count|number of|total|sum|average|avg|mean|median|maximum|minimum|"
                            r"overall|headcount)\b", _F),
    "group_by": re.compile(r"\b(per|each|every|breakdown|broken down|grouped)\b|"
                           r"\b(total|count|number|average|sum|revenue|sales|salary|spend\w*|amount)\b"
                           r"(\s+\w+){0,3}\s+by\b", _F),
    "having": re.compile(r"\b(with|have|having|has|had)\s+(more|fewer|less|at least|at most|over|under|above|below)"
                         r"\s+(than\s+)?\d+", _F),
    "comparison": re.compile(r"\b(more|less|higher|lower|greater|fewer|bigger|smaller)\s+than\b", _F),
    "comparison_to_aggregate": re.compile(
        r"\b(above|below|over|under|more than|less than|higher than|lower than|greater than|exceed\w*)\s+"
        r"(the\s+)?(\w+\s+){0,2}(average|mean|median)\b|\bthan (the )?(overall )?(average|mean)\b", _F),
    "anti_join": re.compile(r"\b(never|without any|not (yet )?(placed|made|sold|bought|assigned|reviewed|ordered)|"
                            r"haven'?t|hasn'?t|didn'?t|have not|has not|did not)\b|\bno\s+(\w+\s+)?"
                            r"(orders?|sales?|purchases?|reviews?|projects?|assignments?)\b", _F),
    "top_per_group": re.compile(rf"\b({_SUPERLATIVES}|best[- ]selling)\b.*\b(in each|for each|each|every|per)\b|"
                                rf"\b(in each|for each|each|every|per)\b.*\b({_SUPERLATIVES}|best[- ]selling)\b", _F),
    "window": re.compile(r"\b(rank|ranking|running total|cumulative|moving average|percentile|row number|"
                         r"dense rank)\b", _F),
    "self_join": re.compile(r"\b(managers?|supervisors?|boss(es)?|reports? to|direct reports?)\b", _F),
    "ratio": re.compile(r"\b(percent|percentage|ratio|share|proportion|fraction)\b|%", _F),
    "comparison_over_time": re.compile(r"\b(compare|compared|comparison|versus|vs\.?|growth|grew|trend|"
                                       r"year over year|month over month)\b", _F),
    "bucketing": re.compile(r"\b(range|ranges|bucket\w*|bands?|brackets?|categori[sz]e|classify|tiers?)\b", _F),
    "date": re.compile(rf"\b(19|20)\d{{2}}\b|\b({_PERIOD})s?\b|\b(date|daily|weekly|monthly|quarterly|yearly|"
                       rf"annual|since|before|after|between|ago|recent|recently|past|today|yesterday|"
                       rf"q[1-4]|ytd|{_MONTHS})\b", _F),
    "relative_date": re.compile(rf"\b(last|past|previous|this|current|recent)\s+(\d+\s+)?({_PERIOD})s?\b|"
                                rf"\b(ago|today|yesterday|recently|year to date|ytd)\b", _F),
    "date_grouping": re.compile(rf"\b(per|by|each|every)\s+({_PERIOD})\b|"
                                rf"\b(daily|weekly|monthly|quarterly|yearly|annually)\b", _F),
    "date_range": re.compile(rf"\bbetween\b.*\b({_MONTHS}|(19|20)\d{{2}})\b|\bfrom\s+({_MONTHS})\b.*\bto\b", _F),
    "text_search": re.compile(r"\b(contain\w*|includ\w*|starting with|starts with|ending with|ends with|"
                              r"named|called|like)\b|'[^']+'\s+in\s+(the\s+)?\w+", _F),
    "null": re.compile(r"\b(missing|unknown|empty|null|blank|not set)\b|\b(has|have|with|without)\s+no\b|"
                       r"\bwithout an?\b", _F),
    "distinct": re.compile(r"\b(distinct|unique|different)\b", _F),
    "filter": re.compile(r"\b(more than|less than|greater than|over|under|above|below|at least|at most|"
                         r"exactly|only|whose|where|with a|equal to)\b|\$\s?\d|\d{3,}", _F),
    "ambiguous": re.compile(r"\b(best(?![- ]?sell)|worst|top[- ]?performing|highest[- ]?performing|"
                            r"best[- ]?performing|performance|perform\w*|good|great|popular|important|"
                            r"successful|valuable|productive|efficient|engaged|loyal)\b", _F),
    "unsafe": re.compile(r"\b(delete|remove|drop|truncate|insert|update|alter|grant|revoke|rename|erase|"
                         r"wipe|purge|overwrite)\b|\b(add|set|change|modify|give|raise|increase|reduce|"
                         r"lower|replace|create)\s+(a|an|the|all|every|everyone|everybody|their|his|her|its|"
                         r"new)\b", _F),
    "missing_data": re.compile(r"\b(phone|telephone|mobile|address|password|ssn|social security|"
                               r"birthday|date of birth|age|gender)\b", _F),
}

# Follow-up wording ("now sort them", "only those in 2025"). Only meaningful
# when there IS a previous turn, so callers combine it with conversation state
# and pass ``is_follow_up``; it is not part of ``detect_question_tags``.
FOLLOW_UP_PATTERN = re.compile(
    r"^\s*(now|and|also|then|instead|only|what about|how about|but|same)\b|"
    r"\b(them|those|these|they|the same|that list|those results|previous|above)\b", _F)


def looks_like_follow_up(question: str) -> bool:
    """Heuristic: does the wording refer back to an earlier result?"""
    return bool(FOLLOW_UP_PATTERN.search(question))


# Rarer, harder patterns carry more weight than ubiquitous ones.
TAG_WEIGHTS: dict[str, float] = {
    "unsafe": 4.0, "ambiguous": 3.0, "missing_data": 3.0, "follow_up": 3.0,
    "top_per_group": 3.0, "self_join": 3.0, "anti_join": 3.0, "comparison_to_aggregate": 3.0,
    "window": 2.5, "having": 2.5, "ratio": 2.5, "comparison_over_time": 2.5, "bucketing": 2.5,
    "relative_date": 2.5, "date_grouping": 2.5, "text_search": 2.5, "case": 2.0,
    "cte": 2.0, "conditional_aggregation": 2.0, "date_range": 2.0, "null": 2.0,
    "multi_join": 1.5, "order_limit": 1.5, "nested": 1.5,
    "join": 1.0, "aggregate": 1.0, "group_by": 1.0, "superlative": 1.0, "distinct": 1.0,
    "date": 1.0, "comparison": 1.0, "order": 0.5, "filter": 0.5,
}
EXTRA_TAG_PENALTY = 0.15
LEXICAL_WEIGHT = 1.5
DIVERSITY_PENALTY = 1.0

_WORD = re.compile(r"[a-z0-9$%']+")
_LEXICAL_STOPWORDS = frozenset(
    "a an the of in on at for to from and or is are was were be do does did me my our we you your i "
    "it its this that these those show list give find get display what which who whose".split()
)


def detect_question_tags(question: str, *, is_follow_up: bool = False, likely_table_count: int = 0) -> set[str]:
    """Map a question to SQL-pattern tags using the rules in ``TAG_PATTERNS``."""
    tags = {tag for tag, pattern in TAG_PATTERNS.items() if pattern.search(question)}
    if is_follow_up:
        tags.add("follow_up")
    if likely_table_count >= 2:
        tags.add("join")
    if likely_table_count >= 3:
        tags.add("multi_join")
    # implied patterns
    if "top_per_group" in tags:
        tags |= {"window", "cte"}
    if "ratio" in tags or "comparison_over_time" in tags:
        tags.add("conditional_aggregation")
    if "bucketing" in tags:
        tags.add("case")
    if tags & {"anti_join", "comparison_to_aggregate"}:
        tags.add("nested")
    if "superlative" in tags:
        tags.add("order")
    return tags


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _LEXICAL_STOPWORDS}


def _jaccard(a: set[str], b: set[str] | frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class _Candidate:
    example: FewShotExample
    score: float
    matched: tuple[str, ...]


class ExampleSelector:
    """Pick the k most useful demonstrations for a question."""

    def __init__(self, examples: Sequence[FewShotExample] = FEW_SHOT_EXAMPLES,
                 core_fill_order: Sequence[str] = CORE_FILL_ORDER) -> None:
        self.examples = tuple(examples)
        self._by_id = {e.id: e for e in self.examples}
        self._position = {e.id: i for i, e in enumerate(self.examples)}  # deterministic tie-break
        self.core_fill_order = [i for i in core_fill_order if i in self._by_id]

    def score(self, example: FewShotExample, question_tags: set[str], question_words: set[str]) -> _Candidate:
        matched = sorted(example.tags & question_tags)
        extra = example.tags - question_tags
        score = (
            sum(TAG_WEIGHTS.get(t, 1.0) for t in matched)
            - EXTRA_TAG_PENALTY * sum(TAG_WEIGHTS.get(t, 1.0) for t in extra)
            + LEXICAL_WEIGHT * _jaccard(question_words, _words(example.question))
        )
        return _Candidate(example, round(score, 3), tuple(matched))

    def select(
        self,
        question: str,
        k: int = 4,
        *,
        strategy: str = "dynamic",
        is_follow_up: bool = False,
        likely_table_count: int = 0,
    ) -> list[SelectedExample]:
        if k <= 0:
            return []
        if strategy == "static":
            chosen = [self._by_id[i] for i in self.core_fill_order[:k]]
            return [SelectedExample(example=e, score=0.0, matched_tags=[], reason="static example set")
                    for e in chosen]

        tags = detect_question_tags(question, is_follow_up=is_follow_up, likely_table_count=likely_table_count)
        words = _words(question)
        candidates = [
            self.score(e, tags, words) for e in self.examples
            if not e.trigger_tags or (e.trigger_tags & tags)
        ]

        selected: list[_Candidate] = []
        remaining = [c for c in candidates if c.matched]
        behaviour_used = False
        while remaining and len(selected) < k:
            def adjusted(c: _Candidate) -> float:
                overlap = max((_jaccard(set(c.example.tags), s.example.tags) for s in selected), default=0.0)
                return c.score - DIVERSITY_PENALTY * overlap

            best = max(remaining, key=lambda c: (adjusted(c), -self._position[c.example.id]))
            remaining.remove(best)
            if best.example.trigger_tags:
                if behaviour_used:
                    continue
                behaviour_used = True
            if adjusted(best) <= 0 and selected:
                break
            selected.append(best)

        picked = [
            SelectedExample(
                example=c.example,
                score=c.score,
                matched_tags=list(c.matched),
                reason=f"matches {', '.join(c.matched)}" if c.matched else "core pattern",
            )
            for c in selected
        ]
        for example_id in self.core_fill_order:
            if len(picked) >= k:
                break
            if all(p.example.id != example_id for p in picked):
                picked.append(SelectedExample(example=self._by_id[example_id], score=0.0,
                                              matched_tags=[], reason="core pattern (fill)"))
        # most relevant last, i.e. closest to the question
        return sorted(picked, key=lambda p: p.score)


def get_example(example_id: str) -> FewShotExample:
    return EXAMPLES_BY_ID[example_id]
