"""
Controlled error-correction prompt.

When validation, verification or execution fails, the pipeline asks the
model to repair the query. It sends the same system prompt as generation
(so the cached prefix is reused), plus a <failed_attempt> block with:

* the SQL that failed,
* the sanitised error (a database message, validator finding or verifier
  issue; never credentials or stack traces),
* the attempt number and the hard maximum, so the model knows the budget is
  limited and should change strategy rather than repeat itself.

The pipeline enforces the budget in code (``max_correction_attempts`` <= 3);
the prompt only states it.
"""

from __future__ import annotations

CORRECTION_INSTRUCTIONS = """\
The previous SQL for this question failed. Write a corrected query.
- Read the error and the reviewer findings carefully and fix their cause; do not repeat the same \
query or the same mistake.
- Use only tables, columns and stored values from <database_schema>.
- If the failure shows that the question cannot be answered from this schema, set intent to \
"out_of_scope" with sql null instead of guessing.
- The query must still be a single read-only SELECT that answers the original question.
- In "assumptions", briefly note what you changed and why."""


def failed_attempt_block(*, sql: str | None, problems: list[str], attempt: int, max_attempts: int) -> str:
    """Render the failed attempt; ``problems`` must already be sanitised."""
    listed = "\n".join(f"- {p}" for p in problems) if problems else "- (no details available)"
    return (
        f'<failed_attempt number="{attempt}" of="{max_attempts}">\n'
        f"<failed_sql>\n{sql or '(no SQL was produced)'}\n</failed_sql>\n"
        f"<problems>\n{listed}\n</problems>\n"
        f"</failed_attempt>"
    )
