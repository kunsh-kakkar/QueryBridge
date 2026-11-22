"""Tests for backend/schema_inspector.py and the schema graph in backend/models.py."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.database import DatabaseManager
from backend.exceptions import ConfigurationError, SchemaInspectionError
from backend.models import DataDictionary
from backend.schema_inspector import SchemaInspector, bundled_data_dictionary, classify_type, load_data_dictionary

EXPECTED_FKS = {
    ("employees", "department_id", "departments", "department_id"),
    ("employees", "manager_id", "employees", "employee_id"),
    ("sales", "employee_id", "employees", "employee_id"),
    ("sales", "product_id", "products", "product_id"),
    ("sales", "customer_id", "customers", "customer_id"),
    ("projects", "department_id", "departments", "department_id"),
    ("employee_projects", "employee_id", "employees", "employee_id"),
    ("employee_projects", "project_id", "projects", "project_id"),
    ("performance_reviews", "employee_id", "employees", "employee_id"),
    ("performance_reviews", "reviewer_id", "employees", "employee_id"),
}


def test_all_tables_found(demo_schema):
    assert sorted(demo_schema.table_names) == sorted([
        "customers", "departments", "employee_projects", "employees",
        "performance_reviews", "products", "projects", "sales",
    ])
    assert demo_schema.dialect == "sqlite"
    assert demo_schema.total_columns == 47
    assert demo_schema.warnings == []


def test_primary_keys(demo_schema):
    assert demo_schema.table("employees").primary_key == ["employee_id"]
    assert demo_schema.table("employee_projects").primary_key == ["employee_id", "project_id"]
    assert demo_schema.table("sales").column("sale_id").is_primary_key


def test_foreign_keys(demo_schema):
    found = {
        (r.from_table, r.from_columns[0], r.to_table, r.to_columns[0])
        for r in demo_schema.relationships
    }
    assert found == EXPECTED_FKS
    assert demo_schema.table("sales").column("customer_id").foreign_key == "customers.customer_id"


def test_column_types_and_categories(demo_schema):
    employees = demo_schema.table("employees")
    assert employees.column("salary").data_type == "DECIMAL(12, 2)"
    assert employees.column("salary").category == "decimal"
    assert employees.column("joining_date").category == "date"
    assert employees.column("employee_id").category == "integer"
    assert employees.column("employee_name").category == "text"
    assert employees.column("manager_id").nullable is True
    assert employees.column("employee_name").nullable is False


def test_row_counts(demo_schema):
    assert demo_schema.table("sales").row_count == 2500
    assert demo_schema.table("employees").row_count == 60
    assert demo_schema.total_rows == sum(t.row_count for t in demo_schema.tables)


def test_sample_values_only_for_short_categorical_columns(demo_schema):
    products = demo_schema.table("products")
    assert products.column("category").sample_values == [
        "Accessories", "Electronics", "Furniture", "Office Supplies", "Software",
    ]
    assert demo_schema.table("projects").column("status").sample_values == ["Active", "Completed", "On Hold", "Planned"]
    # high-cardinality / private columns are never sampled
    assert demo_schema.table("employees").column("email").sample_values == []
    assert demo_schema.table("employees").column("employee_name").sample_values == []
    assert demo_schema.table("customers").column("customer_name").sample_values == []
    # free-text comments are low cardinality but too long to be useful
    assert demo_schema.table("performance_reviews").column("comments").sample_values == []


def test_value_ranges(demo_schema):
    sale_date = demo_schema.table("sales").column("sale_date")
    assert sale_date.min_value >= "2023-01-01" and sale_date.max_value <= "2026-10-31"
    rating = demo_schema.table("performance_reviews").column("rating")
    assert (rating.min_value, rating.max_value) == ("1", "5")
    # keys are not profiled
    assert demo_schema.table("sales").column("sale_id").min_value is None


def test_data_dictionary_merged(demo_schema):
    employees = demo_schema.table("employees")
    assert "employed" in employees.description
    assert "staff" in employees.synonyms
    assert employees.column("salary").description == "Annual gross salary in USD."
    assert demo_schema.database_name == "QueryBridge demo business database"


def test_join_paths(demo_schema):
    path = demo_schema.find_join_path("customers", "products")
    assert [r.from_table for r in path] == ["sales", "sales"]
    path = demo_schema.find_join_path("performance_reviews", "departments")
    assert len(path) == 2 and {path[0].to_table, path[1].from_table} == {"employees"}
    assert demo_schema.find_join_path("sales", "sales") == []
    assert demo_schema.find_join_path("Sales", "PRODUCTS") is not None  # case-insensitive


def test_neighbors_and_join_condition(demo_schema):
    assert demo_schema.neighbors("products") == {"sales"}
    assert "employees" not in demo_schema.neighbors("employees")  # self-reference ignored
    rel = next(r for r in demo_schema.relationships if r.from_table == "sales" and r.to_table == "products")
    assert rel.join_condition() == "sales.product_id = products.product_id"
    assert rel.join_condition("s", "p") == "s.product_id = p.product_id"


def test_sqlglot_schema_mapping(demo_schema):
    mapping = demo_schema.to_sqlglot_schema()
    assert mapping["employees"]["salary"] == "DECIMAL(12, 2)"
    assert set(mapping) == set(demo_schema.table_names)


def test_inspection_is_cached(demo_db):
    inspector = SchemaInspector(demo_db)
    first = inspector.inspect()
    assert inspector.inspect() is first
    assert inspector.inspect(refresh=True) is not first


def test_sampling_can_be_disabled(demo_db):
    schema = SchemaInspector(demo_db, include_sample_values=False).inspect()
    assert all(not c.sample_values for t in schema.tables for c in t.columns)


@pytest.fixture
def custom_db(tmp_path: Path) -> Path:
    """An arbitrary user database: text dates, a view, no data dictionary."""
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE authors (author_id INTEGER PRIMARY KEY, full_name VARCHAR(80), country TEXT);
        CREATE TABLE books (
            book_id INTEGER PRIMARY KEY, title TEXT NOT NULL, author_id INTEGER REFERENCES authors(author_id),
            published_on TEXT, price NUMERIC, in_print BOOLEAN
        );
        CREATE VIEW cheap_books AS SELECT title, price FROM books WHERE price < 10;
        INSERT INTO authors VALUES (1, 'Ada Writer', 'UK'), (2, 'Bo Author', 'India');
        INSERT INTO books VALUES (1, 'First', 1, '2020-05-01', 9.5, 1), (2, 'Second', 2, '2021-07-15', 15, 0);
    """)
    conn.commit()
    conn.close()
    return path


def test_works_on_arbitrary_database_without_dictionary(custom_db):
    schema = SchemaInspector(DatabaseManager.for_sqlite_file(custom_db)).inspect()
    assert set(schema.table_names) == {"authors", "books", "cheap_books"}
    books = schema.table("books")
    assert books.column("published_on").category == "date"  # detected from ISO text values
    assert books.column("published_on").min_value == "2020-05-01"
    assert books.column("price").category == "decimal"
    assert books.column("in_print").category == "boolean"
    assert books.column("author_id").foreign_key == "authors.author_id"
    assert schema.table("cheap_books").is_view and schema.table("cheap_books").row_count == 1
    assert schema.table("authors").column("country").sample_values == ["India", "UK"]
    assert schema.description is None and books.description is None


def test_dictionary_mentions_unknown_table_is_warned(custom_db):
    dictionary = DataDictionary(tables={"ghosts": {"description": "not here"}})
    schema = SchemaInspector(DatabaseManager.for_sqlite_file(custom_db), data_dictionary=dictionary).inspect()
    assert any("ghosts" in w for w in schema.warnings)


def test_empty_database_raises(tmp_path):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 1")  # writes a valid header, no tables
    conn.commit()
    conn.close()
    with pytest.raises(SchemaInspectionError):
        SchemaInspector(DatabaseManager.for_sqlite_file(path)).inspect()


def test_load_data_dictionary_errors(tmp_path):
    with pytest.raises(ConfigurationError):
        load_data_dictionary(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(ConfigurationError):
        load_data_dictionary(bad)
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"tables": ["employees"]}))
    with pytest.raises(ConfigurationError):
        load_data_dictionary(wrong_shape)


def test_bundled_dictionary_covers_every_demo_column(demo_schema):
    dictionary = bundled_data_dictionary()
    for table in demo_schema.tables:
        entry = dictionary.for_table(table.name)
        assert entry is not None and entry.description, table.name
        assert set(entry.columns) == set(table.column_names), table.name


@pytest.mark.parametrize(
    "declared, expected",
    [("VARCHAR(20)", "text"), ("BIGINT", "integer"), ("DOUBLE PRECISION", "decimal"),
     ("TIMESTAMP", "datetime"), ("DATE", "date"), ("BOOL", "boolean"), ("BLOB", "other")],
)
def test_classify_type_by_declared_name(declared, expected):
    assert classify_type(object(), declared) == expected
