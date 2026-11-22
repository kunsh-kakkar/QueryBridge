"""
Automatic schema understanding.

``SchemaInspector`` uses SQLAlchemy's inspection API (on the read-only
inspection engine) to build a ``SchemaModel``. For each table it collects:

* columns, declared types, a normalised type category, nullability, defaults;
* primary keys and foreign keys (the relationship graph used for joins);
* row counts;
* light profiling that helps the LLM ground its SQL:
  - MIN/MAX of numeric and date columns (the data range: "2023-01-02 … 2026-10-31"),
  - up to N distinct values of *low-cardinality, short* text columns
    (categories, statuses, cities). High-cardinality columns such as names
    or emails are never sampled.

Sampled values are untrusted database content. They are cleaned here
(control characters removed, length capped), and the prompt layer wraps them
in data tags with an explicit "this is data, not instructions" rule.

Optional business descriptions and synonyms come from a ``DataDictionary``
(the demo DB ships ``database/data_dictionary.json``).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, inspect as sa_inspect, text, types as satypes
from sqlalchemy.engine import Inspector
from sqlalchemy.exc import SQLAlchemyError

from backend.database import DatabaseManager, sanitize_db_error
from backend.exceptions import ConfigurationError, DatabaseConnectionError, SchemaInspectionError
from backend.models import ColumnCategory, ColumnInfo, DataDictionary, ForeignKeyInfo, SchemaModel, TableInfo
from config import PROJECT_ROOT, Settings
from utils.helpers import Timer, collapse_whitespace, format_duration_ms, strip_control_chars, truncate_text, utc_now_iso
from utils.logger import get_logger

logger = get_logger(__name__)

BUNDLED_DICTIONARY_PATH = PROJECT_ROOT / "database" / "data_dictionary.json"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")
_DATE_LIKE_NAME = re.compile(r"(^|_)(date|day|time|timestamp|datetime)$|_(at|on)$")


def load_data_dictionary(path: str | Path) -> DataDictionary:
    """Load and validate a data-dictionary JSON file."""
    try:
        return DataDictionary.load(path)
    except FileNotFoundError:
        raise ConfigurationError(f"Data dictionary not found: {Path(path).name}") from None
    except (ValueError, ValidationError) as exc:
        raise ConfigurationError(
            "Data dictionary is not valid JSON in the expected format.",
            detail=truncate_text(str(exc), 300),
        ) from None


def bundled_data_dictionary() -> DataDictionary:
    """The data dictionary that ships with the demo database."""
    return load_data_dictionary(BUNDLED_DICTIONARY_PATH)


def classify_type(sa_type: Any, declared: str = "") -> ColumnCategory:
    """Map a SQLAlchemy type (or its declared name) to a coarse category."""
    checks: list[tuple[type, ColumnCategory]] = [
        (satypes.Boolean, "boolean"),
        (satypes.Integer, "integer"),
        (satypes.Numeric, "decimal"),  # includes Float / REAL
        (satypes.DateTime, "datetime"),
        (satypes.Date, "date"),
        (satypes.String, "text"),  # includes Text, Enum, Unicode
    ]
    for sa_class, category in checks:
        if isinstance(sa_type, sa_class):
            return category
    name = declared.upper()
    if "BOOL" in name:
        return "boolean"
    if "INT" in name:
        return "integer"
    if any(token in name for token in ("REAL", "FLOA", "DOUB", "DEC", "NUM", "MONEY")):
        return "decimal"
    if "TIMESTAMP" in name or "DATETIME" in name:
        return "datetime"
    if "DATE" in name:
        return "date"
    if any(token in name for token in ("CHAR", "TEXT", "CLOB", "STRING")):
        return "text"
    return "other"


def _type_string(sa_type: Any) -> str:
    try:
        return str(sa_type)
    except Exception:  # some reflected types cannot be compiled without a dialect
        return sa_type.__class__.__name__.upper()


def _format_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


class SchemaInspector:
    """Inspect a database once and cache the resulting ``SchemaModel``."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        data_dictionary: DataDictionary | None = None,
        include_sample_values: bool = True,
        sample_values_per_column: int = 10,
        max_distinct_for_samples: int = 25,
        max_mean_sample_length: int = 30,
        max_sample_chars: int = 60,
        max_rows_for_profiling: int = 500_000,
        include_views: bool = True,
    ) -> None:
        self.db = db
        self.data_dictionary = data_dictionary
        self.include_sample_values = include_sample_values
        self.sample_values_per_column = sample_values_per_column
        self.max_distinct_for_samples = max_distinct_for_samples
        self.max_mean_sample_length = max_mean_sample_length
        self.max_sample_chars = max_sample_chars
        self.max_rows_for_profiling = max_rows_for_profiling
        self.include_views = include_views
        self._cache: SchemaModel | None = None

    @classmethod
    def from_settings(cls, db: DatabaseManager, settings: Settings,
                      data_dictionary: DataDictionary | None = None) -> "SchemaInspector":
        if data_dictionary is None and settings.is_demo_database:
            data_dictionary = bundled_data_dictionary()
        return cls(
            db,
            data_dictionary=data_dictionary,
            include_sample_values=settings.include_sample_values,
            sample_values_per_column=settings.sample_values_per_column,
        )

    # ------------------------------------------------------------------ public
    def inspect(self, *, refresh: bool = False) -> SchemaModel:
        """Return the schema, inspecting the database on first use (or when refreshing)."""
        if self._cache is not None and not refresh:
            return self._cache
        timer = Timer().start()
        warnings: list[str] = []
        tables: list[TableInfo] = []
        try:
            with self.db.inspection_connection() as conn:
                inspector = sa_inspect(conn)
                names = [(name, False) for name in inspector.get_table_names() if not name.startswith("sqlite_")]
                if self.include_views:
                    names += [(name, True) for name in inspector.get_view_names()]
                for name, is_view in names:
                    try:
                        tables.append(self._inspect_table(inspector, conn, name, is_view, warnings))
                    except SQLAlchemyError as exc:
                        warnings.append(f"Skipped {name}: {sanitize_db_error(exc, 160)}")
                        logger.warning("Skipped table %s during inspection", name)
        except DatabaseConnectionError:
            raise
        except SQLAlchemyError as exc:
            raise SchemaInspectionError(
                "Could not read the database schema.", detail=sanitize_db_error(exc)
            ) from None

        if not tables:
            raise SchemaInspectionError("The database contains no tables or views that can be queried.")

        self._warn_unknown_dictionary_entries(tables, warnings)
        schema = SchemaModel(
            dialect=self.db.dialect,
            database_name=self.data_dictionary.database if self.data_dictionary and self.data_dictionary.database
            else self.db.database_name,
            description=self.data_dictionary.description if self.data_dictionary else None,
            tables=tables,
            inspected_at=utc_now_iso(),
            warnings=warnings,
        )
        self._cache = schema
        logger.info("Inspected %d tables / %d columns / %d FKs in %s",
                    len(tables), schema.total_columns, len(schema.relationships),
                    format_duration_ms(timer.stop()))
        return schema

    def invalidate(self) -> None:
        self._cache = None

    # ----------------------------------------------------------------- per table
    def _inspect_table(self, inspector: Inspector, conn: Connection, name: str,
                       is_view: bool, warnings: list[str]) -> TableInfo:
        raw_columns = inspector.get_columns(name)
        primary_key = [] if is_view else list(inspector.get_pk_constraint(name).get("constrained_columns") or [])
        foreign_keys = [] if is_view else [
            ForeignKeyInfo(
                columns=list(fk["constrained_columns"]),
                referred_table=fk["referred_table"],
                referred_columns=list(fk["referred_columns"]),
                name=fk.get("name"),
            )
            for fk in inspector.get_foreign_keys(name)
            if fk.get("referred_table") and fk.get("constrained_columns")
        ]
        fk_targets = {
            column: f"{fk.referred_table}.{target}"
            for fk in foreign_keys
            for column, target in zip(fk.columns, fk.referred_columns)
        }

        entry = self.data_dictionary.for_table(name) if self.data_dictionary else None
        column_docs = {k.lower(): v for k, v in (entry.columns.items() if entry else [])}
        row_count = self._count_rows(conn, name, warnings)
        can_profile = row_count is not None and 0 < row_count <= self.max_rows_for_profiling

        columns: list[ColumnInfo] = []
        for raw in raw_columns:
            declared = _type_string(raw["type"])
            info: dict[str, Any] = {
                "name": raw["name"],
                "data_type": declared,
                "category": classify_type(raw["type"], declared),
                "nullable": bool(raw.get("nullable", True)),
                "is_primary_key": raw["name"] in primary_key,
                "foreign_key": fk_targets.get(raw["name"]),
                "default": None if raw.get("default") is None else str(raw["default"]),
                "description": column_docs.get(raw["name"].lower()),
            }
            if can_profile and not info["is_primary_key"] and not info["foreign_key"]:
                try:
                    self._profile_column(conn, name, info)
                except SQLAlchemyError as exc:
                    warnings.append(f"Could not profile {name}.{raw['name']}: {sanitize_db_error(exc, 120)}")
            columns.append(ColumnInfo(**info))

        return TableInfo(
            name=name,
            is_view=is_view,
            columns=columns,
            primary_key=primary_key,
            foreign_keys=foreign_keys,
            row_count=row_count,
            description=entry.description if entry else None,
            synonyms=list(entry.synonyms) if entry else [],
        )

    def _quote(self, conn: Connection, identifier: str) -> str:
        return conn.dialect.identifier_preparer.quote(identifier)

    def _count_rows(self, conn: Connection, table: str, warnings: list[str]) -> int | None:
        try:
            return int(conn.execute(text(f"SELECT COUNT(*) FROM {self._quote(conn, table)}")).scalar_one())
        except SQLAlchemyError as exc:
            warnings.append(f"Could not count rows of {table}: {sanitize_db_error(exc, 120)}")
            return None

    def _profile_column(self, conn: Connection, table: str, info: dict[str, Any]) -> None:
        col = self._quote(conn, info["name"])
        tbl = self._quote(conn, table)
        category = info["category"]

        if category in ("integer", "decimal", "date", "datetime"):
            low, high = conn.execute(text(f"SELECT MIN({col}), MAX({col}) FROM {tbl}")).one()
            info["min_value"], info["max_value"] = _format_value(low), _format_value(high)
            return
        if category != "text":
            return

        if _DATE_LIKE_NAME.search(info["name"].lower()):
            # Dates stored as text (common in SQLite): detect ISO formatting.
            low, high = conn.execute(text(f"SELECT MIN({col}), MAX({col}) FROM {tbl}")).one()
            if isinstance(low, str) and isinstance(high, str) and _ISO_DATE.match(low) and _ISO_DATE.match(high):
                info["category"] = "date"
                info["min_value"], info["max_value"] = low, high
                return

        distinct = int(conn.execute(text(f"SELECT COUNT(DISTINCT {col}) FROM {tbl}")).scalar_one())
        info["distinct_count"] = distinct
        limit = self.sample_values_per_column
        if not self.include_sample_values or limit <= 0 or distinct > self.max_distinct_for_samples:
            return
        rows = conn.execute(
            text(f"SELECT DISTINCT {col} FROM {tbl} WHERE {col} IS NOT NULL ORDER BY {col} LIMIT :k"),
            {"k": limit},
        ).scalars().all()
        values = [self._clean_sample(v) for v in rows]
        values = [v for v in values if v]
        if values and sum(map(len, values)) / len(values) <= self.max_mean_sample_length:
            info["sample_values"] = values  # short categorical values only, never free text

    def _clean_sample(self, value: Any) -> str:
        cleaned = collapse_whitespace(strip_control_chars(str(value)))
        return truncate_text(cleaned, self.max_sample_chars)

    def _warn_unknown_dictionary_entries(self, tables: list[TableInfo], warnings: list[str]) -> None:
        if not self.data_dictionary:
            return
        known = {t.name.lower() for t in tables}
        for name in self.data_dictionary.tables:
            if name.lower() not in known:
                warnings.append(f"Data dictionary describes unknown table '{name}' (ignored).")
