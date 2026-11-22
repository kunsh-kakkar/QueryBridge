"""
SQL security validator: the hard gate before any generated SQL can run.

Every candidate query from the LLM goes through these layers. The first
security violation blocks it, and a blocked query is **never** sent back for
regeneration:

1. **Text checks**: no SQL, NUL bytes, absurd length.
2. **Token allow-list** (SQLGlot tokenizer; works even when parsing fails):
   the first real token, after comments, must be SELECT, WITH or "(", and
   no further statement may follow a semicolon (no stacked queries). This
   catches statements the parser cannot read, such as MySQL ``LOAD DATA``.
3. **Parse** in the target dialect. A syntax error is *correctable* (it goes
   to the correction loop), not a security violation.
4. **Root statement** must be a SELECT or a set operation (UNION/INTERSECT/
   EXCEPT); CTEs are part of a SELECT.
5. **AST walk** rejects any data-changing or session-changing node anywhere
   in the tree: INSERT/UPDATE/DELETE/MERGE inside CTEs, DDL, TRUNCATE,
   ATTACH/DETACH, PRAGMA, SET, GRANT/REVOKE, COPY, LOAD DATA,
   SELECT ... INTO, row locks (FOR UPDATE), transaction control, and any
   statement SQLGlot only understands as an opaque ``Command`` (VACUUM,
   REPLACE INTO, EXPLAIN...).
6. **Function deny-list**: file/extension access, sleeps and DoS helpers,
   server administration, and SQLite ``pragma_*`` table functions.
7. **System catalogs** (sqlite_master, information_schema, pg_catalog, ...)
   and cross-database references are rejected.
8. **Row cap**: a missing LIMIT is added and an oversized one reduced. The
   cap is ``max_rows + 1``, so the executor can tell "exactly max_rows" from
   "truncated".
9. **Re-rendering**: the SQL that runs is regenerated from the validated
   AST (comments dropped). What was checked is exactly what executes; a
   comment or tokenisation trick cannot hide a second meaning.

This module is the policy; the database engine enforces read-only access
independently (``backend/database.py``).
"""

from __future__ import annotations

import logging
import re
from typing import Literal

import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType

from utils.helpers import truncate_text

logging.getLogger("sqlglot").setLevel(logging.ERROR)  # silence "falling back to Command" chatter

SQLGLOT_DIALECTS = {"sqlite": "sqlite", "postgresql": "postgres", "mysql": "mysql"}
MAX_SQL_CHARS = 20_000

ALLOWED_FIRST_TOKENS = frozenset({TokenType.SELECT, TokenType.WITH, TokenType.L_PAREN})
# INTO never belongs in a read-only SELECT (SELECT INTO, INTO OUTFILE/DUMPFILE, INSERT INTO).
ALWAYS_BLOCKED_TOKENS = frozenset({TokenType.INTO})
# If the parser cannot read the SQL but it contains one of these keywords, treat it as an attack,
# not a typo. (Only on parse failure: REPLACE is also an ordinary string function.)
WRITE_TOKENS = frozenset(getattr(TokenType, name) for name in (
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "MERGE", "GRANT", "REVOKE",
    "ATTACH", "DETACH", "PRAGMA", "COMMAND", "REPLACE", "SET", "COPY", "EXECUTE", "LOAD", "USE",
    "COMMIT", "ROLLBACK", "BEGIN") if hasattr(TokenType, name))

FORBIDDEN_NODES: dict[type[exp.Expression], str] = {
    exp.Insert: "INSERT", exp.Update: "UPDATE", exp.Delete: "DELETE", exp.Merge: "MERGE",
    exp.Drop: "DROP", exp.Create: "CREATE", exp.Alter: "ALTER", exp.TruncateTable: "TRUNCATE",
    exp.Command: "a non-query command", exp.Pragma: "PRAGMA", exp.Attach: "ATTACH", exp.Detach: "DETACH",
    exp.Grant: "GRANT", exp.Revoke: "REVOKE", exp.Set: "SET", exp.Use: "USE",
    exp.Transaction: "transaction control", exp.Commit: "COMMIT", exp.Rollback: "ROLLBACK",
    exp.Copy: "COPY", exp.LoadData: "LOAD DATA", exp.Into: "SELECT ... INTO (creates a table)",
    exp.Lock: "row locking (FOR UPDATE / FOR SHARE)", exp.Describe: "DESCRIBE", exp.Analyze: "ANALYZE",
    exp.Kill: "KILL",
}

BLOCKED_FUNCTIONS = frozenset({
    # file system / extensions
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "load_file",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export",
    "lo_get", "lo_put",
    # sleeps and denial of service
    "sleep", "pg_sleep", "pg_sleep_for", "pg_sleep_until", "benchmark", "randomblob", "zeroblob",
    # server administration / remote execution
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "pg_rotate_logfile",
    "set_config", "dblink", "dblink_exec", "sys_exec", "sys_eval", "xp_cmdshell",
    "get_lock", "release_lock", "master_pos_wait",
})
BLOCKED_FUNCTION_PREFIXES = ("pragma_", "pg_read", "pg_ls_", "lo_", "dblink")
SYSTEM_SCHEMAS = frozenset({"information_schema", "pg_catalog", "pg_toast", "mysql", "performance_schema",
                            "sys", "temp"})
SYSTEM_TABLE_PREFIXES = ("sqlite_", "pg_")

ViolationKind = Literal["security", "syntax"]


class Violation(BaseModel):
    code: str
    kind: ViolationKind
    message: str


class ValidationReport(BaseModel):
    original_sql: str | None
    is_safe: bool
    safe_sql: str | None = Field(default=None, description="Re-rendered SQL that may be executed")
    pretty_sql: str | None = None
    statement_type: str | None = None
    violations: list[Violation] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    referenced_tables: list[str] = Field(default_factory=list)
    cte_names: list[str] = Field(default_factory=list)
    original_limit: int | None = Field(default=None, description="LIMIT written in the candidate SQL")
    applied_limit: int | None = Field(default=None, description="LIMIT in the SQL that runs")
    limit_added: bool = False

    @property
    def is_blocked(self) -> bool:
        """A security violation: must not run and must not be regenerated."""
        return any(v.kind == "security" for v in self.violations)

    @property
    def is_correctable(self) -> bool:
        """Only syntax problems: the correction loop may try to fix them."""
        return not self.is_safe and not self.is_blocked

    @property
    def messages(self) -> list[str]:
        return [v.message for v in self.violations]


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    try:
        return node.sql_name().lower()
    except Exception:  # pragma: no cover - defensive
        return type(node).__name__.lower()


_FUNCTION_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_NOT_FUNCTIONS = frozenset({"IN", "AS", "AND", "OR", "NOT", "EXISTS", "OVER", "VALUES", "FROM", "JOIN", "ON",
                            "WHERE", "SELECT", "USING", "WITH", "FILTER", "WITHIN", "ALL", "ANY", "SOME"})


def _function_calls(sql: str) -> set[str]:
    """Upper-cased names written in call position (used to report dialect translations)."""
    without_strings = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return {m.upper() for m in _FUNCTION_CALL.findall(without_strings)} - _NOT_FUNCTIONS


def _literal_int(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(str(node.this))
        except ValueError:
            return None
    return None


class SQLValidator:
    """Decide whether candidate SQL is safe to run, and produce the SQL that will run."""

    def __init__(self, dialect: str = "sqlite", *, max_rows: int = 1000, max_sql_chars: int = MAX_SQL_CHARS) -> None:
        self.dialect = dialect
        self.sqlglot_dialect = SQLGLOT_DIALECTS.get(dialect, dialect)
        self.max_rows = max_rows
        self.row_cap = max_rows + 1
        self.max_sql_chars = max_sql_chars

    # ------------------------------------------------------------------ API
    def validate(self, sql: str | None) -> ValidationReport:
        report = ValidationReport(original_sql=sql, is_safe=False)

        def fail(code: str, kind: ViolationKind, message: str) -> ValidationReport:
            report.violations.append(Violation(code=code, kind=kind, message=message))
            report.is_safe = False
            return report

        if sql is None or not sql.strip():
            return fail("empty", "syntax", "No SQL was produced.")
        if "\x00" in sql:
            return fail("nul_byte", "security", "The SQL contains a NUL byte.")
        if len(sql) > self.max_sql_chars:
            return fail("too_long", "security", f"The SQL is longer than {self.max_sql_chars:,} characters.")

        # --- token allow-list (independent of the parser) -----------------
        try:
            tokens = sqlglot.tokenize(sql, read=self.sqlglot_dialect)
        except TokenError as exc:
            return fail("tokenize", "syntax", f"SQL could not be tokenized: {truncate_text(str(exc), 200)}")
        if not tokens:
            return fail("empty", "syntax", "No SQL was produced.")
        if tokens[0].token_type not in ALLOWED_FIRST_TOKENS:
            return fail("not_select", "security",
                        f"Only read-only SELECT queries are allowed (the statement starts with "
                        f"'{tokens[0].text.upper()}').")
        for index, token in enumerate(tokens):
            if token.token_type == TokenType.SEMICOLON and any(
                t.token_type != TokenType.SEMICOLON for t in tokens[index + 1:]
            ):
                return fail("stacked", "security", "Multiple SQL statements are not allowed.")
        if any(t.token_type in ALWAYS_BLOCKED_TOKENS for t in tokens):
            return fail("forbidden_statement", "security",
                        "INTO is not allowed: QueryBridge never writes tables or files.")

        # --- parse ---------------------------------------------------------
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.sqlglot_dialect) if s is not None]
        except ParseError as exc:
            errors = getattr(exc, "errors", None) or [{"description": str(exc)}]
            first = errors[0]
            detail = first.get("description", str(exc))
            where = f" (line {first.get('line')}, column {first.get('col')})" if first.get("line") else ""
            write_words = sorted({t.text.upper() for t in tokens if t.token_type in WRITE_TOKENS})
            if write_words:
                return fail("unparseable_write", "security",
                            f"Unreadable SQL containing data-changing keywords ({', '.join(write_words)}) was rejected.")
            return fail("parse", "syntax", f"SQL syntax error{where}: {truncate_text(str(detail), 200)}")
        if len(statements) != 1:
            return fail("stacked", "security", "Exactly one SQL statement is required.")

        root = statements[0]
        while isinstance(root, exp.Subquery) and isinstance(root.this, (exp.Select, exp.SetOperation, exp.Subquery)):
            root = root.this  # "(SELECT ...)" -> SELECT ...
        report.statement_type = type(root).__name__.upper()
        if not isinstance(root, (exp.Select, exp.SetOperation)):
            return fail("not_select", "security",
                        f"Only SELECT queries are allowed (found {report.statement_type}).")

        # --- AST walk: forbidden statements anywhere ------------------------
        for node in root.walk():
            for node_type, label in FORBIDDEN_NODES.items():
                if isinstance(node, node_type):
                    return fail("forbidden_statement", "security", f"{label} is not allowed: QueryBridge is read-only.")

        # --- functions -------------------------------------------------------
        for func in root.find_all(exp.Func):
            name = _function_name(func)
            if name in BLOCKED_FUNCTIONS or name.startswith(BLOCKED_FUNCTION_PREFIXES):
                return fail("blocked_function", "security", f"The function {name}() is not allowed.")

        # --- tables: system catalogs and cross-database access ----------------
        cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
        tables: dict[str, str] = {}
        for table in root.find_all(exp.Table):
            name = str(table.name or "")
            lowered, schema = name.lower(), str(table.db or "").lower()
            if not name:
                continue  # table-valued function; functions were checked above
            if table.catalog:
                return fail("cross_database", "security", f"Cross-database references are not allowed ({table.sql()}).")
            if schema in SYSTEM_SCHEMAS or (lowered not in cte_names and lowered.startswith(SYSTEM_TABLE_PREFIXES)):
                return fail("system_catalog", "security", f"System catalog access is not allowed ({table.sql()}).")
            if lowered not in cte_names:
                tables.setdefault(lowered, name)
        report.referenced_tables = list(tables.values())
        report.cte_names = sorted(cte_names)

        # --- row cap ---------------------------------------------------------
        limit_problem = self._apply_row_cap(root, report)
        if limit_problem:
            return fail("limit", "syntax", limit_problem)

        # --- re-render from the AST and re-check the round trip ----------------
        safe_sql = root.sql(dialect=self.sqlglot_dialect, comments=False)
        try:
            reparsed = sqlglot.parse(safe_sql, read=self.sqlglot_dialect)
        except ParseError:
            return fail("render", "syntax", "The query could not be safely re-rendered.")
        if len(reparsed) != 1 or not isinstance(reparsed[0], (exp.Select, exp.SetOperation)):
            return fail("render", "security", "The re-rendered query is not a single SELECT.")

        translated = _function_calls(sql) - _function_calls(safe_sql)
        if translated:
            report.warnings.append(
                f"Translated to {self.dialect} equivalents: {', '.join(sorted(f + '()' for f in translated))}.")
        report.safe_sql = safe_sql
        report.pretty_sql = root.sql(dialect=self.sqlglot_dialect, pretty=True, comments=False)
        report.is_safe = True
        return report

    # ------------------------------------------------------------------ helpers
    def _apply_row_cap(self, root: exp.Expression, report: ValidationReport) -> str | None:
        limit = root.args.get("limit")
        fetch = root.args.get("fetch")
        if isinstance(limit, exp.Fetch):  # SQLGlot 30 stores FETCH FIRST under "limit"
            limit, fetch = None, limit
        if limit is None and fetch is None:
            root.set("limit", exp.Limit(expression=exp.Literal.number(self.row_cap)))
            report.applied_limit, report.limit_added = self.row_cap, True
            return None
        if limit is not None:
            value = _literal_int(limit.args.get("expression"))
            if value is None:
                return "LIMIT must be a plain number."
            report.original_limit = value
            if value > self.row_cap:
                limit.set("expression", exp.Literal.number(self.row_cap))
                report.applied_limit = self.row_cap
                report.warnings.append(f"LIMIT {value:,} was reduced to the {self.max_rows:,}-row display cap.")
            else:  # includes our own cap (max_rows + 1), so re-validating capped SQL adds no warning
                report.applied_limit = value
            return None
        value = _literal_int(fetch.args.get("count"))
        if value is None:
            return "FETCH FIRST must use a plain number."
        report.original_limit = value
        if value > self.row_cap:
            fetch.set("count", exp.Literal.number(self.row_cap))
            report.applied_limit = self.row_cap
            report.warnings.append(f"FETCH FIRST {value:,} was reduced to the {self.max_rows:,}-row display cap.")
        else:
            report.applied_limit = value
        return None
