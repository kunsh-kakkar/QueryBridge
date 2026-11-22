"""
Two-stage SQL verification.

**Stage A: static checks (deterministic, no LLM).** These run on the
validator's re-rendered SQL:

| check          | severity | what it verifies                                              |
|----------------|----------|---------------------------------------------------------------|
| tables_exist   | error    | every base table exists (with "did you mean" suggestions)     |
| columns_exist  | error    | every column resolves (SQLGlot qualifier); ambiguity detected |
| dry_run        | error    | EXPLAIN compiles the query on the real engine (dialect check) |
| joins          | warning  | joins have conditions; conditions follow documented FKs       |
| answer_shape   | warning  | "top N" asks match LIMIT N; superlatives have ORDER BY        |
| select_star    | warning  | SELECT * in the outer query                                   |

Errors make the candidate fail and go to the correction loop. Warnings do
not block; they are passed to the semantic reviewer as facts and shown to
the user.

**Stage B: semantic self-verification (LLM).** This runs only after the
static checks pass (a broken query goes straight to correction, saving a
call). An independent reviewer prompt asks whether the query actually
answers the question. If the reviewer call itself fails (rate limit,
network), the static result is kept and a warning recorded: verification
degrades gracefully, it never blocks a statically valid query because of an
outage.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from typing import Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify

from backend.database import DatabaseManager, sanitize_db_error
from backend.example_selector import detect_question_tags
from backend.exceptions import DatabaseConnectionError, LLMError
from backend.llm_client import StructuredLLM
from backend.models import (
    BuiltPrompt,
    ConversationTurn,
    LLMCallRecord,
    PromptConfig,
    SchemaContext,
    SchemaModel,
    VerificationVerdict,
)
from backend.prompt_manager import PromptManager
from backend.sql_validator import SQLGLOT_DIALECTS, ValidationReport
from utils.logger import get_logger

logger = get_logger(__name__)

CheckSeverity = Literal["error", "warning", "info"]
_COLUMN_IN_ERROR = re.compile(r"Column '([^']+)'|Unknown column: ([\w$]+)")
_TOP_N = re.compile(r"\b(?:top|first|bottom|last)\s+(\d+)\b(?!\s+(?:day|week|month|quarter|year)s?\b)|"
                    r"\b(\d+)\s+(?:most|least|highest|lowest|largest|smallest|best|worst|biggest|cheapest)\b",
                    re.IGNORECASE)


class StaticCheck(BaseModel):
    name: str
    passed: bool
    severity: CheckSeverity
    message: str


class VerificationReport(BaseModel):
    checks: list[StaticCheck] = Field(default_factory=list)
    referenced_tables: list[str] = Field(default_factory=list)
    referenced_columns: list[str] = Field(default_factory=list)
    explain_plan: list[str] = Field(default_factory=list)
    semantic: VerificationVerdict | None = None
    semantic_call: LLMCallRecord | None = None
    semantic_prompt: BuiltPrompt | None = None
    semantic_skipped_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def errors(self) -> list[StaticCheck]:
        return [c for c in self.checks if not c.passed and c.severity == "error"]

    @property
    def static_passed(self) -> bool:
        return not self.errors

    @property
    def semantic_passed(self) -> bool:
        return self.semantic is None or not self.semantic.has_blocking_issue

    @property
    def passed(self) -> bool:
        return self.static_passed and self.semantic_passed

    @property
    def problems(self) -> list[str]:
        """Actionable problems for the correction prompt."""
        found = [c.message for c in self.errors]
        if self.semantic is not None and self.semantic.has_blocking_issue:
            issues = [f"[{i.severity}] {i.category}: {i.description}" for i in self.semantic.issues]
            found.extend(issues or [f"Reviewer: {self.semantic.summary}"])
        return found

    @property
    def findings(self) -> list[str]:
        """Human-readable results of the static checks, as passed to the semantic reviewer."""
        items = [c.message for c in self.checks if not c.passed]
        return items or ["all automated checks passed (tables, columns, joins, dry run)"]


class SQLVerifier:
    def __init__(
        self,
        schema: SchemaModel,
        *,
        db: DatabaseManager | None = None,
        llm: StructuredLLM | None = None,
        prompt_manager: PromptManager | None = None,
        dialect: str | None = None,
    ) -> None:
        self.schema = schema
        self.db = db
        self.llm = llm
        self.prompts = prompt_manager
        self.dialect = dialect or schema.dialect
        self.sqlglot_dialect = SQLGLOT_DIALECTS.get(self.dialect, self.dialect)
        self._sqlglot_schema = {t.name: {c.name: "TEXT" for c in t.columns} for t in schema.tables}
        self._fk_pairs = self._documented_pairs()

    # ================================================================ stage A
    def static_verify(self, validation: ValidationReport, *, question: str | None = None) -> VerificationReport:
        report = VerificationReport()
        if not validation.is_safe or not validation.safe_sql:
            report.checks.append(StaticCheck(name="validated", passed=False, severity="error",
                                             message="The SQL did not pass the security validator."))
            return report
        sql = validation.safe_sql
        tree = sqlglot.parse_one(sql, read=self.sqlglot_dialect)
        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        aliases = self._alias_map(tree, cte_names)

        tables_ok = self._check_tables(tree, cte_names, report)
        columns_ok = self._check_columns(tree, report, aliases, cte_names) if tables_ok else False
        self._check_joins(tree, aliases, report)
        self._check_answer_shape(tree, validation, question, report)
        self._check_select_star(tree, report)
        if tables_ok and columns_ok:
            self._dry_run(sql, report)
        return report

    def _alias_map(self, tree: exp.Expression, cte_names: set[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for table in tree.find_all(exp.Table):
            if table.name and table.name.lower() not in cte_names:
                real = self.schema.table(table.name)
                mapping[table.alias_or_name.lower()] = real.name if real else table.name
        return mapping

    def _check_tables(self, tree: exp.Expression, cte_names: set[str], report: VerificationReport) -> bool:
        referenced = []
        unknown = []
        for table in tree.find_all(exp.Table):
            if not table.name or table.name.lower() in cte_names:
                continue
            known = self.schema.table(table.name)
            if known is None:
                unknown.append(table.name)
            elif known.name not in referenced:
                referenced.append(known.name)
        report.referenced_tables = referenced
        if unknown:
            hints = []
            for name in dict.fromkeys(unknown):
                close = difflib.get_close_matches(name.lower(), [t.lower() for t in self.schema.table_names], n=1)
                hints.append(f"'{name}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
            report.checks.append(StaticCheck(
                name="tables_exist", passed=False, severity="error",
                message=f"Unknown table(s): {', '.join(hints)}. Available tables: {', '.join(self.schema.table_names)}."))
            return False
        report.checks.append(StaticCheck(name="tables_exist", passed=True, severity="info",
                                         message=f"Tables exist: {', '.join(referenced) or '(none)'}"))
        return True

    def _check_columns(self, tree: exp.Expression, report: VerificationReport, aliases: dict[str, str],
                       cte_names: set[str]) -> bool:
        try:
            qualified = qualify(tree.copy(), schema=self._sqlglot_schema, dialect=self.sqlglot_dialect,
                                validate_qualify_columns=True, quote_identifiers=False, identify=False)
        except OptimizeError as exc:
            report.checks.append(StaticCheck(name="columns_exist", passed=False, severity="error",
                                             message=self._explain_column_error(str(exc), report.referenced_tables)))
            return False
        except Exception as exc:  # qualifier limitation, not a user error: rely on the dry run
            report.checks.append(StaticCheck(name="columns_exist", passed=True, severity="warning",
                                             message=f"Columns could not be fully checked statically ({type(exc).__name__})."))
            return True
        columns: list[str] = []
        for column in qualified.find_all(exp.Column):
            table_ref = (column.table or "").lower()
            if not table_ref or table_ref in cte_names or table_ref not in aliases:
                continue
            name = f"{aliases[table_ref]}.{column.name}"
            if name not in columns:
                columns.append(name)
        report.referenced_columns = columns
        report.checks.append(StaticCheck(name="columns_exist", passed=True, severity="info",
                                         message=f"All {len(columns)} referenced columns exist."))
        return True

    def _explain_column_error(self, error: str, tables: Sequence[str]) -> str:
        match = _COLUMN_IN_ERROR.search(error)
        column = (match.group(1) or match.group(2)) if match else None
        if not column:
            return f"Column check failed: {error.splitlines()[0]}"
        bare = column.split(".")[-1]
        owners = [t for t in tables if (info := self.schema.table(t)) and info.column(bare)]
        if len(owners) > 1:
            return (f"Column '{bare}' is ambiguous: it exists in {', '.join(owners)}. "
                    f"Qualify it with a table alias.")
        candidates = [f"{t}.{c}" for t in tables if (info := self.schema.table(t)) for c in info.column_names]
        close = difflib.get_close_matches(bare.lower(), [c.split(".")[1].lower() for c in candidates], n=3, cutoff=0.6)
        suggestion = [c for c in candidates if c.split(".")[1].lower() in close]
        hint = f" Did you mean {', '.join(suggestion)}?" if suggestion else ""
        return f"Unknown column '{bare}' in tables {', '.join(tables) or '(none)'}.{hint}"

    def _documented_pairs(self) -> set[frozenset[tuple[str, str]]]:
        pairs = set()
        for rel in self.schema.relationships:
            for a, b in zip(rel.from_columns, rel.to_columns):
                pairs.add(frozenset({(rel.from_table.lower(), a.lower()), (rel.to_table.lower(), b.lower())}))
        return pairs

    def _check_joins(self, tree: exp.Expression, aliases: dict[str, str], report: VerificationReport) -> None:
        problems: list[str] = []
        for join in tree.find_all(exp.Join):
            # Comma joins parse as CROSS joins; either way an unlinked join is a cartesian product.
            condition = join.args.get("on")
            if condition is None and not join.args.get("using"):
                select = join.parent_select
                where = select.args.get("where") if select else None
                joined = join.this.alias_or_name.lower() if isinstance(join.this, exp.Table) else ""
                linked = where is not None and any(
                    isinstance(eq.left, exp.Column) and isinstance(eq.right, exp.Column)
                    and joined in {(eq.left.table or "").lower(), (eq.right.table or "").lower()}
                    for eq in where.find_all(exp.EQ))
                if not linked:
                    problems.append(f"The join with {join.this.sql()} has no join condition "
                                    "(this multiplies rows: a cartesian product).")
                continue
            if condition is None:
                continue
            for eq in condition.find_all(exp.EQ):
                left, right = eq.left, eq.right
                if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                    continue
                lt, rt = aliases.get((left.table or "").lower()), aliases.get((right.table or "").lower())
                if not lt or not rt:
                    continue
                pair = frozenset({(lt.lower(), left.name.lower()), (rt.lower(), right.name.lower())})
                if pair not in self._fk_pairs:
                    problems.append(f"Join condition {lt}.{left.name} = {rt}.{right.name} is not a documented "
                                    "relationship; check that it links the right rows.")
        if problems:
            for message in dict.fromkeys(problems):
                report.checks.append(StaticCheck(name="joins", passed=False, severity="warning", message=message))
        elif list(tree.find_all(exp.Join)):
            report.checks.append(StaticCheck(name="joins", passed=True, severity="info",
                                             message="Joins follow documented relationships."))

    def _check_answer_shape(self, tree: exp.Expression, validation: ValidationReport, question: str | None,
                            report: VerificationReport) -> None:
        if not question:
            return
        match = _TOP_N.search(question)
        if match:
            wanted = int(match.group(1) or match.group(2))
            if validation.original_limit is None:
                report.checks.append(StaticCheck(
                    name="answer_shape", passed=False, severity="warning",
                    message=f"The question asks for {wanted} results but the query has no LIMIT {wanted}."))
            elif validation.original_limit != wanted:
                report.checks.append(StaticCheck(
                    name="answer_shape", passed=False, severity="warning",
                    message=f"The question asks for {wanted} results but the query uses LIMIT "
                            f"{validation.original_limit}."))
        if "superlative" in detect_question_tags(question):
            root = tree
            has_order = root.args.get("order") is not None
            has_extreme = any(isinstance(f, (exp.Max, exp.Min)) for f in root.find_all(exp.AggFunc))
            has_window = any(True for _ in root.find_all(exp.Window))
            if not (has_order or has_extreme or has_window):
                report.checks.append(StaticCheck(
                    name="answer_shape", passed=False, severity="warning",
                    message="The question asks for the highest/lowest/most, but the query has no ORDER BY, "
                            "MIN/MAX or ranking."))

    @staticmethod
    def _check_select_star(tree: exp.Expression, report: VerificationReport) -> None:
        root = tree if isinstance(tree, exp.Select) else None
        if root is not None and any(isinstance(e, exp.Star) for e in root.expressions):
            report.checks.append(StaticCheck(name="select_star", passed=False, severity="warning",
                                             message="SELECT * returns every column; listing the needed columns "
                                                     "is clearer."))

    def _dry_run(self, sql: str, report: VerificationReport) -> None:
        if self.db is None:
            return
        statement = f"EXPLAIN QUERY PLAN {sql}" if self.dialect == "sqlite" else f"EXPLAIN {sql}"
        try:
            with self.db.connect() as conn:
                rows = conn.exec_driver_sql(statement).all()  # raw: '10:30' is not a bind parameter
        except DatabaseConnectionError as exc:
            report.checks.append(StaticCheck(name="dry_run", passed=True, severity="warning",
                                             message=f"Dry run skipped: {exc.user_message}"))
            return
        except (DBAPIError, SQLAlchemyError) as exc:
            report.checks.append(StaticCheck(name="dry_run", passed=False, severity="error",
                                             message=f"The database rejected the query: {sanitize_db_error(exc, 250)}"))
            return
        report.explain_plan = [str(row[-1]) for row in rows]
        report.checks.append(StaticCheck(name="dry_run", passed=True, severity="info",
                                         message="The database compiled the query successfully (EXPLAIN)."))

    # ================================================================ stage B
    def semantic_verify(
        self,
        report: VerificationReport,
        *,
        question: str,
        sql: str,
        config: PromptConfig,
        schema_context: SchemaContext | None = None,
        history: Sequence[ConversationTurn] = (),
    ) -> VerificationReport:
        if self.llm is None or self.prompts is None:
            report.semantic_skipped_reason = "no LLM configured for verification"
            return report
        if not report.static_passed:
            report.semantic_skipped_reason = "static checks failed; the query goes to correction first"
            return report
        prompt = self.prompts.build_verification_prompt(
            question, sql=sql, schema_context=schema_context, static_findings=report.findings,
            history=history, config=config)
        report.semantic_prompt = prompt
        try:
            response = self.llm.complete(prompt, VerificationVerdict)
        except LLMError as exc:
            report.semantic_skipped_reason = f"reviewer unavailable: {exc.user_message}"
            report.warnings.append("The AI reviewer could not be reached, so only automated checks were applied.")
            logger.warning("Semantic verification unavailable: %s", exc.user_message)
            return report
        report.semantic = response.output
        report.semantic_call = response.record
        if response.output.has_blocking_issue:
            logger.info("Reviewer flagged %d issue(s)", len(response.output.issues))
        return report
