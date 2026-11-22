"""
Configuration A: the basic prompt (the evaluation baseline).

A deliberately minimal instruction: no role, no rules, no schema context and
no examples. It shows what an LLM does when it is simply asked to "convert
English to SQL". The structured-output contract is still enforced, because
the evaluator needs a machine-readable answer to score every configuration
the same way.
"""

from __future__ import annotations

from prompts.system_prompt import dialect_display_name

_BASELINE = "Convert the user's question into a {dialect_name} SQL query."


def baseline_prompt(dialect: str) -> str:
    return _BASELINE.format(dialect_name=dialect_display_name(dialect))
