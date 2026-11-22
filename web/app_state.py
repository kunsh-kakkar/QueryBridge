"""
Application-wide state for the web server.

One ``AppState`` per running server holds the settings, the connected
database and its inspected schema, the query pipeline (with a single LLM
client and its usage tracker), and the persistent history store. Switching
databases rebuilds the pipeline under a lock, so concurrent requests always
see a consistent connection, schema and pipeline.

Per-browser data (conversation memory, recent results, the chosen technique
configuration) lives in ``web.sessions`` instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from backend.database import DatabaseManager
from backend.exceptions import QueryBridgeError
from backend.history_store import HistoryStore
from backend.llm_client import ProviderLLM, StructuredLLM, create_llm_client
from backend.models import DataDictionary, PromptConfig, SchemaModel
from backend.pipeline import QueryPipeline
from backend.prompt_manager import PRESETS, preset_from_settings
from backend.schema_inspector import SchemaInspector, bundled_data_dictionary
from config import Settings, get_settings
from database.init_db import ensure_database
from utils.logger import get_logger

logger = get_logger(__name__)

EXAMPLE_QUESTIONS: tuple[str, ...] = (
    "Show me the top 5 employees with the highest salaries.",
    "What is the total sales amount by product category?",
    "Which customers from London bought Electronics products?",
    "Show monthly revenue for 2025.",
    "Which employees earn more than their manager?",
    "Which departments have more than 5 employees?",
    "Which customers have never made a purchase?",
    "Show me the highest-performing employees.",
)


@dataclass
class Connection:
    db: DatabaseManager
    schema: SchemaModel
    dictionary_name: str | None = None


class AppState:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: StructuredLLM | None = None,
        connection: Connection | None = None,
        history: HistoryStore | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self._lock = threading.RLock()
        self.llm: StructuredLLM = llm or create_llm_client(self.settings)
        self.history = history or HistoryStore(self.settings.meta_db_path)
        self.connection: Connection | None = connection
        self.connection_error: str | None = None
        if self.connection is None:
            try:
                self.connection = self._default_connection()
            except QueryBridgeError as exc:
                self.connection_error = exc.user_message
                logger.warning("Initial database connection failed: %s", exc.user_message)
        self.pipeline: QueryPipeline | None = self._build_pipeline() if self.connection else None

    # ------------------------------------------------------------------ build
    def _inspect(self, db: DatabaseManager, dictionary: DataDictionary | None) -> SchemaModel:
        return SchemaInspector(db, data_dictionary=dictionary,
                               include_sample_values=self.settings.include_sample_values,
                               sample_values_per_column=self.settings.sample_values_per_column).inspect()

    def _default_connection(self) -> Connection:
        cfg = self.settings
        if cfg.is_demo_database:
            ensure_database(cfg.demo_db_path)
            db = DatabaseManager.for_sqlite_file(cfg.demo_db_path, label="QueryBridge demo database",
                                                 query_timeout_seconds=cfg.query_timeout_seconds)
            return Connection(db, self._inspect(db, bundled_data_dictionary()), "demo data dictionary")
        db = DatabaseManager.from_settings(cfg)
        return Connection(db, self._inspect(db, None), None)

    def _build_pipeline(self) -> QueryPipeline:
        assert self.connection is not None
        cfg = self.settings
        return QueryPipeline(
            db=self.connection.db, schema=self.connection.schema, llm=self.llm,
            config=preset_from_settings(cfg, "C"), reference_date=cfg.effective_reference_date,
            max_rows=cfg.max_result_rows, query_timeout_seconds=cfg.query_timeout_seconds,
            max_question_chars=cfg.max_question_chars, conversation_turns=cfg.conversation_turns,
            full_context_max_tables=cfg.schema_full_context_max_tables, schema_max_tables=cfg.schema_max_tables,
            confidence_threshold=cfg.confidence_warning_threshold, history=self.history,
        )

    # ------------------------------------------------------------------ queries
    @property
    def llm_ready(self) -> bool:
        return self.settings.has_llm_credentials or not isinstance(self.llm, ProviderLLM)

    def config(self, name: str | None) -> PromptConfig:
        return preset_from_settings(self.settings, name if name in PRESETS else "C")

    # ------------------------------------------------------------------ switching
    def switch_database(self, db: DatabaseManager, dictionary: DataDictionary | None = None,
                        dictionary_name: str | None = None) -> SchemaModel:
        """Inspect ``db`` and make it the active database (raises QueryBridgeError)."""
        schema = self._inspect(db, dictionary)
        with self._lock:
            old = self.connection
            self.connection = Connection(db, schema, dictionary_name)
            self.connection_error = None
            self.pipeline = self._build_pipeline()
        if old is not None and old.db is not db:
            old.db.dispose()
        logger.info("Switched database to %s (%d tables)", db.label, len(schema.tables))
        return schema

    def reset_to_demo(self) -> SchemaModel:
        cfg = self.settings
        ensure_database(cfg.demo_db_path)
        db = DatabaseManager.for_sqlite_file(cfg.demo_db_path, label="QueryBridge demo database",
                                             query_timeout_seconds=cfg.query_timeout_seconds)
        return self.switch_database(db, bundled_data_dictionary(), "demo data dictionary")
