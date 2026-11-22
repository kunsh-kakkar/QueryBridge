"""
Database connectivity with read-only access enforced by the database engine.

``DatabaseManager`` gives the rest of the system two SQLAlchemy engines:

* ``query_engine``: runs validated, LLM-generated SQL under a strict policy;
* ``inspection_engine``: reads the schema (catalog tables and introspection pragmas).

SQLite (the default, fully tested) enforces read-only access three ways, so
writes are refused even if the SQL validator had a bug:

1. the file is opened with ``mode=ro`` (the OS-level file handle is read-only);
2. ``PRAGMA query_only = ON`` (SQLite refuses every change);
3. an **authorizer callback** (``sqlite3.Connection.set_authorizer``) that
   allows only SELECT, column reads and functions. Strict mode also blocks
   reads of internal ``sqlite_*`` tables and all PRAGMAs. ATTACH, DDL, DML,
   temp tables and ``load_extension`` are denied.

PostgreSQL and MySQL (EXPERIMENTAL, not tested in this project) use
read-only sessions with server-side statement timeouts. Use a read-only
database account as well.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.pool import NullPool

from backend.exceptions import DatabaseConnectionError
from backend.models import ConnectionStatus
from config import Settings, parse_database_url
from utils.helpers import Timer, collapse_whitespace, truncate_text
from utils.logger import get_logger, redact, register_secret

logger = get_logger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"
DEFAULT_DRIVERS = {"postgresql": "postgresql+psycopg", "mysql": "mysql+pymysql"}
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306}

# --- SQLite authorizer policy ------------------------------------------------
_SQLITE_SELECT = sqlite3.SQLITE_SELECT
_SQLITE_READ = sqlite3.SQLITE_READ
_SQLITE_FUNCTION = sqlite3.SQLITE_FUNCTION
_SQLITE_PRAGMA = sqlite3.SQLITE_PRAGMA
_SQLITE_TRANSACTION = sqlite3.SQLITE_TRANSACTION
_SQLITE_RECURSIVE = getattr(sqlite3, "SQLITE_RECURSIVE", 33)  # recursive CTEs

INTROSPECTION_PRAGMAS = frozenset(
    {"table_info", "table_xinfo", "foreign_key_list", "index_list", "index_info",
     "index_xinfo", "database_list", "integrity_check", "foreign_key_check"}
)
# Status pragmas SQLAlchemy itself reads on connect (isolation-level detection).
# Allowed in strict mode only as a read with no argument, never as an assignment.
# Generated SQL can never reach them: the SQL validator rejects every PRAGMA.
STRICT_READABLE_PRAGMAS = frozenset({"read_uncommitted"})
BLOCKED_FUNCTIONS = frozenset(
    {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "sqlite_offset"}
)


def make_sqlite_authorizer(strict: bool) -> Callable[[int, str | None, str | None, str | None, str | None], int]:
    """Build an allow-list authorizer for SQLite connections.

    Args:
        strict: True for the query path (LLM SQL). Blocks every PRAGMA and any
            read of ``sqlite_*`` internal tables. False for schema inspection,
            which may use read-only introspection pragmas and the catalog.
    """

    def authorizer(action: int, arg1: str | None, arg2: str | None,
                   db_name: str | None, source: str | None) -> int:
        if action in (_SQLITE_SELECT, _SQLITE_RECURSIVE, _SQLITE_TRANSACTION):
            return sqlite3.SQLITE_OK
        if action == _SQLITE_READ:
            if strict and arg1 and arg1.lower().startswith("sqlite_"):
                return sqlite3.SQLITE_DENY
            if db_name not in (None, "main"):  # e.g. temp or attached schemas
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        if action == _SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in BLOCKED_FUNCTIONS else sqlite3.SQLITE_OK
        if action == _SQLITE_PRAGMA:
            pragma = (arg1 or "").lower()
            if pragma in STRICT_READABLE_PRAGMAS and arg2 is None:
                return sqlite3.SQLITE_OK
            if not strict and pragma in INTROSPECTION_PRAGMAS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    return authorizer


def open_sqlite_readonly(path: Path, *, strict: bool = True) -> sqlite3.Connection:
    """Open a SQLite file with all three read-only layers applied."""
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                           check_same_thread=False, timeout=5.0)
    try:
        if hasattr(conn, "enable_load_extension"):
            conn.enable_load_extension(False)
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(make_sqlite_authorizer(strict))
    except sqlite3.Error:
        conn.close()
        raise
    return conn


# --- error sanitising ---------------------------------------------------------
_SQL_BLOCK = re.compile(r"\[SQL:.*?\](\s*\[parameters:.*?\])?", re.DOTALL)
_BACKGROUND = re.compile(r"\(Background on this error at:[^)]*\)")
_SQLA_PREFIX = re.compile(r"^\((?:[\w.]+)\)\s*")


def sanitize_db_error(exc: BaseException, max_chars: int = 400) -> str:
    """Turn a driver/SQLAlchemy exception into a short, secret-free message.

    Drops the echoed SQL, parameters, SQLAlchemy boilerplate and anything
    that looks like a credential.
    """
    if isinstance(exc, DBAPIError) and exc.orig is not None:
        message = str(exc.orig)
    else:
        message = str(exc)
    message = _BACKGROUND.sub("", _SQL_BLOCK.sub("", message))
    message = _SQLA_PREFIX.sub("", message.strip())
    message = collapse_whitespace(redact(message)) or exc.__class__.__name__
    return truncate_text(message, max_chars)


# --- URL construction -----------------------------------------------------------
def build_database_url(
    dialect: str,
    *,
    database: str,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> URL:
    """Build a URL from form fields. Special characters in the password are
    escaped correctly, and the password is registered for log redaction."""
    dialect = dialect.lower().strip()
    if not database or not database.strip():
        raise ValueError("A database name (or SQLite file path) is required.")
    if dialect == "sqlite":
        return URL.create("sqlite", database=str(Path(database).expanduser()))
    if dialect not in DEFAULT_DRIVERS:
        raise ValueError(f"Unsupported dialect '{dialect}'. Use sqlite, postgresql or mysql.")
    if not host:
        raise ValueError("A host is required for server databases.")
    register_secret(password)
    return URL.create(
        DEFAULT_DRIVERS[dialect],
        username=username or None,
        password=password or None,
        host=host.strip(),
        port=port or DEFAULT_PORTS[dialect],
        database=database.strip(),
    )


# --- manager ----------------------------------------------------------------------
class DatabaseManager:
    """Owns the read-only engines for one target database."""

    def __init__(self, url: str | URL, *, query_timeout_seconds: float = 10.0,
                 label: str | None = None) -> None:
        if isinstance(url, str):
            try:
                url = parse_database_url(url)
            except ValueError as exc:
                raise DatabaseConnectionError(str(exc)) from None
        backend = url.get_backend_name()
        if backend not in ("sqlite", *DEFAULT_DRIVERS):
            raise DatabaseConnectionError(f"Unsupported database backend '{backend}'.")
        if backend in DEFAULT_DRIVERS and "+" not in url.drivername:
            url = url.set(drivername=DEFAULT_DRIVERS[backend])  # pick an installed driver
        register_secret(url.password if isinstance(url.password, str) else None)

        self._url = url
        self.dialect: str = backend
        self.query_timeout_seconds = float(query_timeout_seconds)
        self._sqlite_path: Path | None = self._validate_sqlite_path(url) if backend == "sqlite" else None
        self.label = label or self._default_label()
        self._query_engine: Engine | None = None
        self._inspection_engine: Engine | None = None

    # --- construction helpers ------------------------------------------------
    @classmethod
    def from_settings(cls, settings: Settings) -> "DatabaseManager":
        return cls(
            settings.database_url_value(),
            query_timeout_seconds=settings.query_timeout_seconds,
            label="QueryBridge demo database" if settings.is_demo_database else None,
        )

    @classmethod
    def for_sqlite_file(cls, path: str | Path, **kwargs) -> "DatabaseManager":
        return cls(URL.create("sqlite", database=str(Path(path))), **kwargs)

    @staticmethod
    def _validate_sqlite_path(url: URL) -> Path:
        database = url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            raise DatabaseConnectionError(
                "Please provide the path to a SQLite database file (in-memory databases are not supported)."
            )
        path = Path(database).expanduser().resolve()
        if not path.is_file():
            raise DatabaseConnectionError(f"SQLite database file not found: {path.name}")
        try:
            with open(path, "rb") as handle:
                header = handle.read(len(SQLITE_MAGIC))
        except OSError:
            raise DatabaseConnectionError(f"SQLite database file is not readable: {path.name}") from None
        if header != SQLITE_MAGIC:
            raise DatabaseConnectionError(f"{path.name} is not a SQLite database file.")
        return path

    def _default_label(self) -> str:
        if self._sqlite_path is not None:
            return self._sqlite_path.name
        return f"{self.dialect}://{self._url.host}/{self._url.database}"

    # --- properties ------------------------------------------------------------
    @property
    def masked_url(self) -> str:
        return self._url.render_as_string(hide_password=True)

    @property
    def database_name(self) -> str:
        if self._sqlite_path is not None:
            return self._sqlite_path.stem
        return self._url.database or self.dialect

    @property
    def sqlite_path(self) -> Path | None:
        return self._sqlite_path

    @property
    def is_sqlite(self) -> bool:
        return self.dialect == "sqlite"

    # --- engines -----------------------------------------------------------------
    @property
    def query_engine(self) -> Engine:
        if self._query_engine is None:
            self._query_engine = self._build_engine(strict=True)
        return self._query_engine

    @property
    def inspection_engine(self) -> Engine:
        if self._inspection_engine is None:
            self._inspection_engine = (
                self._build_engine(strict=False) if self.is_sqlite else self.query_engine
            )
        return self._inspection_engine

    def _build_engine(self, *, strict: bool) -> Engine:
        if self.is_sqlite:
            path = self._sqlite_path
            assert path is not None
            # NullPool: every checkout gets a fresh connection with the
            # read-only layers installed; nothing is shared between threads.
            return create_engine(
                "sqlite+pysqlite://",
                creator=lambda: open_sqlite_readonly(path, strict=strict),
                poolclass=NullPool,
            )
        return create_engine(
            self._url,
            connect_args=self.server_connect_args(),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
            pool_recycle=1800,
        )

    def server_connect_args(self) -> dict[str, object]:
        """Driver arguments that make PostgreSQL/MySQL sessions read-only
        and time-limited (EXPERIMENTAL)."""
        timeout_ms = int(self.query_timeout_seconds * 1000)
        if self.dialect == "postgresql":
            return {
                "options": f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}",
                "connect_timeout": 10,
            }
        if self.dialect == "mysql":
            return {
                "init_command": (
                    "SET SESSION transaction_read_only = ON, "
                    f"SESSION max_execution_time = {timeout_ms}"
                ),
                "connect_timeout": 10,
                "read_timeout": int(self.query_timeout_seconds) + 5,
            }
        return {}

    # --- connections ---------------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[Connection]:
        """Connection for executing validated SQL (strict policy)."""
        with self._open(self.query_engine) as conn:
            yield conn

    @contextmanager
    def inspection_connection(self) -> Iterator[Connection]:
        """Connection for schema inspection (introspection allowed)."""
        with self._open(self.inspection_engine) as conn:
            yield conn

    @contextmanager
    def _open(self, engine: Engine) -> Iterator[Connection]:
        try:
            conn = engine.connect()
        except (SQLAlchemyError, sqlite3.Error, OSError) as exc:
            message = sanitize_db_error(exc)
            logger.warning("Database connection failed (%s): %s", self.dialect, message)
            raise DatabaseConnectionError(
                "Could not connect to the database. Check the connection settings.",
                detail=message,
            ) from None
        try:
            yield conn
        finally:
            conn.close()

    def test_connection(self) -> ConnectionStatus:
        """Run ``SELECT 1`` and report latency, server version and read-only status."""
        timer = Timer().start()
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1")).scalar_one()
                version = (
                    sqlite3.sqlite_version if self.is_sqlite
                    else ".".join(str(p) for p in (conn.dialect.server_version_info or ()))
                )
        except DatabaseConnectionError as exc:
            return ConnectionStatus(ok=False, dialect=self.dialect,
                                    message=f"{exc.user_message} ({exc.detail})")
        except SQLAlchemyError as exc:
            return ConnectionStatus(ok=False, dialect=self.dialect, message=sanitize_db_error(exc))
        return ConnectionStatus(
            ok=True,
            dialect=self.dialect,
            message=f"Connected to {self.label}",
            latency_ms=timer.stop(),
            server_version=version or None,
            read_only_enforced=True,
        )

    def dispose(self) -> None:
        """Close pooled connections (call when switching databases)."""
        for engine in (self._query_engine, self._inspection_engine):
            if engine is not None:
                engine.dispose()
        self._query_engine = None
        self._inspection_engine = None

    def __repr__(self) -> str:  # never shows the password
        return f"DatabaseManager(dialect={self.dialect!r}, url={self.masked_url!r})"
