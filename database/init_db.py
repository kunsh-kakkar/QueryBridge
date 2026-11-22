"""
Build the QueryBridge demo database.

    python -m database.init_db              # build if missing (safe to re-run)
    python -m database.init_db --force      # rebuild from scratch
    python -m database.init_db --verify     # check an existing database
    python -m database.init_db --path other.db --seed 7

The database is built into a temporary file, checked (foreign keys and
integrity), and only then moved into place with an atomic rename, so a
half-built file is never used. ``logical_fingerprint`` hashes the schema and
every row; the evaluation uses it to prove that no query changed any data.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from database.sample_data import DEFAULT_SEED, TABLE_COLUMNS, generate_sample_data
from utils.helpers import Timer, format_duration_ms

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True)
class BuildReport:
    path: Path
    seed: int
    row_counts: dict[str, int]
    fingerprint: str
    elapsed_ms: float

    @property
    def total_rows(self) -> int:
        return sum(self.row_counts.values())


class DatabaseBuildError(RuntimeError):
    """The generated database failed a consistency check."""


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def logical_fingerprint(path: str | Path) -> str:
    """SHA-256 over the schema and every row, in a fixed order.

    Unlike a file hash, this depends only on the content, not on how SQLite
    laid the pages out on disk.
    """
    path = Path(path)
    digest = hashlib.sha256()
    conn = _open_read_only(path)
    try:
        objects = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        for obj in objects:
            digest.update(repr(obj).encode("utf-8"))
        tables = [name for kind, name, _ in objects if kind == "table"]
        for table in tables:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")]
            order = ", ".join(str(i) for i in range(1, len(columns) + 1))
            digest.update(f"#{table}:{columns}".encode("utf-8"))
            for row in conn.execute(f"SELECT * FROM {_quote(table)} ORDER BY {order}"):
                digest.update(repr(row).encode("utf-8"))
    finally:
        conn.close()
    return digest.hexdigest()


def table_row_counts(path: str | Path) -> dict[str, int]:
    """Row count per user table (read-only)."""
    conn = _open_read_only(Path(path))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {_quote(t)}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def is_sqlite_file(path: str | Path) -> bool:
    """True if ``path`` is an existing file with the SQLite header."""
    path = Path(path)
    if not path.is_file():
        return False
    with open(path, "rb") as handle:
        return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC


def build_database(path: str | Path, *, seed: int = DEFAULT_SEED, overwrite: bool = False) -> BuildReport:
    """Create the demo database at ``path`` and return a build report.

    Raises:
        FileExistsError: ``path`` exists and ``overwrite`` is False.
        DatabaseBuildError: a foreign-key or integrity check failed.
    """
    path = Path(path).resolve()
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists (use overwrite=True / --force)")
    path.parent.mkdir(parents=True, exist_ok=True)

    timer = Timer().start()
    data = generate_sample_data(seed)
    temp_path = path.with_name(path.name + ".building")
    temp_path.unlink(missing_ok=True)

    conn = sqlite3.connect(temp_path)
    try:
        conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:  # one transaction for all inserts
            for table, columns in TABLE_COLUMNS.items():
                placeholders = ", ".join("?" for _ in columns)
                conn.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    data.tables[table],
                )
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise DatabaseBuildError(f"Foreign-key violations: {violations[:5]}")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise DatabaseBuildError(f"Integrity check failed: {integrity}")
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("VACUUM")
    except BaseException:
        conn.close()
        temp_path.unlink(missing_ok=True)
        raise
    conn.close()

    os.replace(temp_path, path)  # atomic on the same filesystem
    return BuildReport(
        path=path,
        seed=seed,
        row_counts=data.row_counts(),
        fingerprint=logical_fingerprint(path),
        elapsed_ms=timer.stop(),
    )


def ensure_database(path: str | Path, *, seed: int = DEFAULT_SEED) -> Path:
    """Build the demo database only if it does not exist yet; return its path."""
    path = Path(path).resolve()
    if not path.exists():
        build_database(path, seed=seed)
    elif not is_sqlite_file(path):
        raise DatabaseBuildError(f"{path} exists but is not a SQLite database")
    return path


def verify_database(path: str | Path) -> tuple[bool, str]:
    """Run integrity and foreign-key checks on an existing database."""
    path = Path(path)
    if not is_sqlite_file(path):
        return False, "not a SQLite database file"
    conn = _open_read_only(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if integrity != "ok":
        return False, f"integrity check failed: {integrity}"
    if violations:
        return False, f"{len(violations)} foreign-key violations"
    return True, "integrity ok, no foreign-key violations"


def _print_counts(counts: dict[str, int]) -> None:
    width = max(len(name) for name in counts)
    for name, count in counts.items():
        print(f"  {name:<{width}}  {count:>6,}")
    print(f"  {'TOTAL':<{width}}  {sum(counts.values()):>6,}")


def main(argv: list[str] | None = None) -> int:
    from config import get_settings

    parser = argparse.ArgumentParser(description="Build the QueryBridge demo database.")
    parser.add_argument("--path", type=Path, help="target file (default: QB_DATA_DIR/querybridge_demo.db)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"random seed (default {DEFAULT_SEED})")
    parser.add_argument("--force", action="store_true", help="rebuild even if the file exists")
    parser.add_argument("--verify", action="store_true", help="only verify an existing database")
    args = parser.parse_args(argv)

    path = (args.path or get_settings().demo_db_path).resolve()

    if args.verify:
        ok, message = verify_database(path) if path.exists() else (False, "file not found")
        print(f"[{'OK' if ok else 'FAIL'}] {path}: {message}")
        if ok:
            _print_counts(table_row_counts(path))
            print(f"  fingerprint: {logical_fingerprint(path)}")
        return 0 if ok else 1

    if path.exists() and not args.force:
        print(f"[OK] Demo database already exists: {path}")
        print("     Use --force to rebuild it, or --verify to check it.")
        return 0

    try:
        report = build_database(path, seed=args.seed, overwrite=True)
    except (DatabaseBuildError, sqlite3.Error, OSError) as exc:
        print(f"[FAIL] Could not build database: {exc}", file=sys.stderr)
        return 1

    print(f"[OK] Built {report.path} (seed {report.seed}) in {format_duration_ms(report.elapsed_ms)}")
    _print_counts(report.row_counts)
    print(f"  fingerprint: {report.fingerprint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
