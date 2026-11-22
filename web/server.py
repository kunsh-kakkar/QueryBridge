"""
QueryBridge web server: a JSON API over the existing pipeline plus the
static HTML/CSS/JavaScript interface.

Security model (the app is meant to run on the user's own machine):

* Binds to 127.0.0.1 by default; requests whose Host header is not a local
  name are rejected (blocks DNS-rebinding attacks).
* Every state-changing request must carry the ``X-QueryBridge: 1`` header.
  Browsers cannot add a custom header to a cross-site request without a CORS
  preflight, which this server never approves, so other websites cannot drive
  it (CSRF).
* A strict Content-Security-Policy allows scripts only from this server;
  the interface renders model and database text with ``textContent`` only.
* Error responses never contain tracebacks, request bodies, credentials or
  the unmasked database URL.
* Every query still goes through the backend's validator and read-only
  executor; the web layer adds no way to run SQL around them.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import plotly
import plotly.io as pio
from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.chart_recommender import build_figure
from backend.database import DatabaseManager, build_database_url
from backend.exceptions import ConfigurationError, QueryBridgeError
from backend.gemini_client import gemini_schema
from backend.llm_client import ProviderLLM
from backend.models import DataDictionary, SQLGenerationResult, VerificationVerdict
from backend.pipeline import QueryPipeline
from backend.prompt_manager import EVALUATION_ORDER, PRESETS
from config import API_KEY_VARIABLE, LLM_PROVIDER, PROVIDER_NAME
from evaluation.report import TABLE_METRICS, comparison_figure, label, load_summary
from prompts.system_prompt import PROMPT_VERSION
from utils.helpers import file_sha256
from utils.logger import get_logger
from web import serializers as ser
from web.app_state import EXAMPLE_QUESTIONS, AppState
from web.sessions import Session, SessionStore

logger = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
PLOTLY_JS = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
LOCAL_HOSTS = ["localhost", "127.0.0.1", "[::1]", "::1", "testserver"]
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_DICTIONARY_BYTES = 2 * 1024 * 1024
SQLITE_MAGIC = b"SQLite format 3\x00"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
       "form-action 'self'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}

NO_DATABASE = "No database is connected. Open “Connect a database” in Settings to choose one."
HOSTED_DATABASE = ("Changing the database is turned off on this hosted copy of QueryBridge: it always uses the "
                   "sample company database. Run QueryBridge on your own computer to connect your own database.")
KEY_NEEDED = "Answering questions needs a Gemini API key (free from Google AI Studio)."


def no_api_key_message() -> str:
    return (f"{KEY_NEEDED} Add {API_KEY_VARIABLE} to the .env file in the QueryBridge folder "
            "and restart the app. Everything else (exploring your data, history, running saved SQL) works "
            "without one.")


# ====================================================================== request bodies
class AskBody(BaseModel):
    question: str = Field(default="", max_length=20_000)
    clarification_answer: str | None = Field(default=None, max_length=2_000)


class SQLBody(BaseModel):
    sql: str = Field(max_length=20_000)


class ExplainBody(SQLBody):
    level: Literal["beginner", "technical"] = "beginner"


class ConfigBody(BaseModel):
    name: str = Field(max_length=4)


class PromptPreviewBody(BaseModel):
    question: str = Field(max_length=5_000)
    config: str = Field(default="C", max_length=4)
    stage: Literal["generation", "verification", "explanation"] = "generation"
    mode: Literal["auto", "full", "retrieved"] = "auto"
    with_history: bool = False


# ====================================================================== helpers
def _error(status: int, message: str, code: str = "error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}}, status_code=status)


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _read_limited(upload: UploadFile, limit: int, what: str) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise APIError(413, f"The {what} is larger than {limit // (1024 * 1024)} MB.", "too_large")
    return data


def _dictionary(upload: UploadFile | None) -> tuple[DataDictionary | None, str | None]:
    if upload is None or not upload.filename:
        return None, None
    data = _read_limited(upload, MAX_DICTIONARY_BYTES, "data dictionary")
    try:
        return DataDictionary.model_validate(json.loads(data.decode("utf-8"))), Path(upload.filename).name
    except (ValueError, UnicodeDecodeError):
        raise ConfigurationError("The data dictionary must be JSON in the QueryBridge format "
                                 "(see database/data_dictionary.json).") from None


def _examples(state: AppState) -> list[str]:
    connection = state.connection
    if connection is None:
        return []
    if connection.dictionary_name == "demo data dictionary":
        return list(EXAMPLE_QUESTIONS)
    tables = sorted(connection.schema.tables, key=lambda t: -(t.row_count or 0))[:3]
    examples: list[str] = []
    for table in tables:
        name = table.name.replace("_", " ")
        examples.append(f"How many {name} are there?")
        examples.append(f"Show 10 {name}.")
    return examples[:6]


# ====================================================================== app factory
def create_app(state: AppState | None = None, *, sessions: SessionStore | None = None,
               allowed_hosts: list[str] | None = None) -> FastAPI:
    state = state or AppState()
    sessions = sessions or SessionStore(conversation_turns=state.settings.conversation_turns)
    app = FastAPI(title="QueryBridge", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.qb = state
    app.state.sessions = sessions

    # ---------------------------------------------------------------- middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in SAFE_METHODS:
            if request.headers.get("x-querybridge") != "1":
                response = _error(403, "This request did not come from the QueryBridge page.", "csrf")
                response.headers.update(SECURITY_HEADERS)
                return response
            # Public deployments share one database for every visitor, and the file-path and
            # server connectors would reach the host's files and network: refuse before reading the body.
            if state.settings.hosted and request.url.path.startswith("/api/connection/"):
                response = _error(403, HOSTED_DATABASE, "hosted")
                response.headers.update(SECURITY_HEADERS)
                return response
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOCAL_HOSTS)

    # ---------------------------------------------------------------- errors
    @app.exception_handler(APIError)
    async def api_error(_: Request, exc: APIError):
        return _error(exc.status, exc.message, exc.code)

    @app.exception_handler(QueryBridgeError)
    async def qb_error(_: Request, exc: QueryBridgeError):
        return _error(400, exc.user_message, "query_bridge")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Never echo the request body back (it may contain a password).
        fields = sorted({".".join(str(p) for p in e.get("loc", ())[1:]) or "body" for e in exc.errors()})
        return _error(422, "Some of the information sent was missing or invalid: " + ", ".join(fields) + ".",
                      "invalid_request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException):
        return _error(exc.status_code, str(exc.detail) if exc.status_code < 500 else "Server error.", "http")

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception):
        logger.error("Unhandled %s in web request", type(exc).__name__)
        return _error(500, "Something unexpected went wrong. Please try again.", "unexpected")

    # ---------------------------------------------------------------- dependencies
    def session_dep(request: Request) -> Session:
        try:
            return sessions.get(request.headers.get("x-session-id"))
        except ValueError:
            raise APIError(400, "Missing or invalid session id. Reload the page.", "session") from None

    def pipeline_or_503() -> QueryPipeline:
        pipeline = state.pipeline
        if pipeline is None:
            raise APIError(503, state.connection_error or NO_DATABASE, "no_database")
        return pipeline

    def dialect() -> str:
        return state.connection.schema.dialect if state.connection else "sqlite"

    def store(session: Session, result) -> dict[str, Any]:
        rid = sessions.add_result(session, result)
        return ser.result_json(result, rid, dialect=dialect())

    def find_result(session: Session, result_id: str):
        result = sessions.result(session, result_id)
        if result is None:
            raise APIError(404, "That answer is no longer available. Ask the question again.", "expired")
        return result

    # ---------------------------------------------------------------- pages and assets
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "img" / "logo_icon.svg", media_type="image/svg+xml")

    @app.get("/vendor/plotly.min.js", include_in_schema=False)
    def plotly_js() -> FileResponse:
        return FileResponse(PLOTLY_JS, media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=86400"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---------------------------------------------------------------- status and session
    @app.get("/api/status")
    def status(session: Session = Depends(session_dep)) -> dict[str, Any]:
        connection = state.connection
        cfg = state.settings
        database = None
        if connection is not None:
            schema = connection.schema
            database = {
                "label": connection.db.label, "dialect": ser.DIALECT_NAMES.get(schema.dialect, schema.dialect),
                "tables": len(schema.tables), "columns": schema.total_columns, "rows": schema.total_rows,
                "relationships": len(schema.relationships), "table_names": schema.table_names,
                "is_demo": connection.dictionary_name == "demo data dictionary",
            }
        usage = state.llm.tracker.summary() if isinstance(state.llm, ProviderLLM) else None
        return {
            "prompt_version": PROMPT_VERSION,
            "hosted": cfg.hosted,
            "database": database,
            "connection_error": state.connection_error,
            "llm": {"ready": state.llm_ready, "provider": LLM_PROVIDER, "provider_name": PROVIDER_NAME,
                    "key_variable": API_KEY_VARIABLE, "model": cfg.effective_model,
                    "message": None if state.llm_ready else no_api_key_message()},
            "config": ser.config_json(state.config(session.config_name)),
            "configs": [ser.config_json(state.config(name)) for name in EVALUATION_ORDER],
            "examples": _examples(state),
            "limits": {"max_question_chars": cfg.max_question_chars, "max_rows": cfg.max_result_rows,
                       "timeout_seconds": cfg.query_timeout_seconds},
            "conversation": {"turns": len(session.conversation.turns),
                             "pending_clarification": session.conversation.pending_clarification is not None},
            "usage": usage,
        }

    @app.post("/api/session/config")
    def set_config(body: ConfigBody, session: Session = Depends(session_dep)) -> dict[str, Any]:
        if body.name not in PRESETS:
            raise APIError(400, "Unknown configuration. Choose A, B, D or C.", "config")
        session.config_name = body.name
        return {"config": ser.config_json(state.config(body.name))}

    @app.post("/api/session/reset")
    def reset_session(session: Session = Depends(session_dep)) -> dict[str, Any]:
        session.conversation.clear()
        session.results.clear()
        return {"ok": True}

    # ---------------------------------------------------------------- asking
    @app.post("/api/ask")
    def ask(body: AskBody, session: Session = Depends(session_dep)) -> StreamingResponse:
        pipeline = pipeline_or_503()
        if not state.llm_ready:
            raise APIError(400, no_api_key_message(), "no_api_key")
        conversation = session.conversation
        question = body.question
        answer = body.clarification_answer.strip() if body.clarification_answer else None
        if answer is not None:
            pending = conversation.pending_clarification
            if pending is None:
                raise APIError(409, "That clarification has expired. Please ask your question again.", "expired")
            question = pending.original_question
        elif not question.strip():
            raise APIError(400, "Please type a question first.", "empty")
        if not session.lock.acquire(blocking=False):
            raise APIError(409, "QueryBridge is still working on your previous question.", "busy")

        events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        config = state.config(session.config_name)
        db_dialect = dialect()

        def work() -> None:
            try:
                result = pipeline.ask(question, conversation=conversation, config=config,
                                      clarification_answer=answer,
                                      on_step=lambda text: events.put({"type": "step", "label": text}))
                rid = sessions.add_result(session, result)
                events.put({"type": "result", "result": ser.result_json(result, rid, dialect=db_dialect)})
            except Exception as exc:  # noqa: BLE001 - the stream must always end with a message
                logger.error("Question failed with %s", type(exc).__name__)
                events.put({"type": "error", "message": "Something unexpected went wrong. Please try again."})
            finally:
                session.lock.release()
                events.put(None)

        threading.Thread(target=work, name="querybridge-ask", daemon=True).start()

        def stream() -> Iterator[bytes]:
            while (event := events.get()) is not None:
                yield (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"X-Accel-Buffering": "no"})

    @app.get("/api/results/{result_id}/chart")
    def chart(result_id: str, index: int = 0, measure: str | None = None, dark: bool = False,
              session: Session = Depends(session_dep)) -> Response:
        result = find_result(session, result_id)
        charts = [c for c in result.charts if c.chart_type != "kpi"]
        if result.formatted is None or not 0 <= index < len(charts):
            raise APIError(404, "No chart is available for this answer.", "no_chart")
        spec = charts[index]
        y = measure if measure and (measure == spec.y or measure in spec.measures) else spec.y
        figure = build_figure(result.formatted.dataframe, spec, dark=dark, y=y)
        title = spec.title
        if y != spec.y and spec.y:
            title = title.replace(spec.y.replace("_", " ").capitalize(), y.replace("_", " ").capitalize(), 1)
        body = '{"title": %s, "reason": %s, "figure": %s}' % (
            json.dumps(title), json.dumps(spec.reason), pio.to_json(figure, validate=False))
        return Response(body, media_type="application/json")

    @app.get("/api/results/{result_id}/details")
    def details(result_id: str, session: Session = Depends(session_dep)) -> dict[str, Any]:
        return ser.details_json(find_result(session, result_id))

    # ---------------------------------------------------------------- SQL tools
    @app.post("/api/sql/run")
    def run_sql(body: SQLBody, session: Session = Depends(session_dep)) -> dict[str, Any]:
        if not body.sql.strip():
            raise APIError(400, "Paste a SQL query first.", "empty")
        return store(session, pipeline_or_503().run_sql(body.sql, question="Your SQL query"))

    @app.post("/api/sql/explain")
    def explain_sql(body: ExplainBody, session: Session = Depends(session_dep)) -> dict[str, Any]:
        pipeline = pipeline_or_503()
        if not body.sql.strip():
            raise APIError(400, "Paste a SQL query first.", "empty")
        if not state.llm_ready:
            return {"validation": ser.validation_json(pipeline.validator.validate(body.sql)),
                    "explanation": None, "error": no_api_key_message()}
        explanation, validation, error = pipeline.explain_sql(body.sql, level=body.level)
        return {
            "validation": ser.validation_json(validation),
            "explanation": explanation.model_dump() if explanation is not None else None,
            "error": error,
        }

    @app.post("/api/sql/check")
    def check_sql(body: SQLBody, session: Session = Depends(session_dep)) -> dict[str, Any]:
        return {"validation": ser.validation_json(pipeline_or_503().validator.validate(body.sql))}

    # ---------------------------------------------------------------- history
    @app.get("/api/history")
    def history(search: str | None = None, status: str | None = None, limit: int = 200,
                session: Session = Depends(session_dep)) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        entries = state.history.list(limit=limit, status=status or None, search=(search or "").strip() or None)
        return {"entries": [ser.history_json(e) for e in entries], "stats": state.history.stats(),
                "statuses": ser.STATUS_TEXT}

    @app.post("/api/history/{entry_id}/rerun")
    def rerun(entry_id: int, session: Session = Depends(session_dep)) -> dict[str, Any]:
        entry = state.history.get(entry_id)
        if entry is None:
            raise APIError(404, "That history entry no longer exists.", "not_found")
        if not entry.sql:
            raise APIError(400, "This entry has no SQL to run. Use “Ask again” instead.", "no_sql")
        return store(session, pipeline_or_503().run_sql(entry.sql, question=entry.question))

    @app.delete("/api/history/{entry_id}")
    def delete_entry(entry_id: int, session: Session = Depends(session_dep)) -> dict[str, Any]:
        return {"deleted": bool(state.history.delete(entry_id))}

    @app.delete("/api/history")
    def clear_history(session: Session = Depends(session_dep)) -> dict[str, Any]:
        return {"removed": state.history.clear()}

    # ---------------------------------------------------------------- schema
    @app.get("/api/schema")
    def schema(session: Session = Depends(session_dep)) -> dict[str, Any]:
        connection = state.connection
        if connection is None:
            raise APIError(503, state.connection_error or NO_DATABASE, "no_database")
        return ser.schema_json(connection.schema, label=connection.db.label,
                               dictionary_name=connection.dictionary_name)

    @app.get("/api/schema/tables/{name}/preview")
    def preview(name: str, session: Session = Depends(session_dep)) -> dict[str, Any]:
        pipeline = pipeline_or_503()
        table = state.connection.schema.table(name) if state.connection else None
        if table is None:
            raise APIError(404, "There is no table with that name.", "not_found")
        quoted = pipeline.db.inspection_engine.dialect.identifier_preparer.quote(table.name)
        result = pipeline.run_sql(f"SELECT * FROM {quoted} LIMIT 10", question=f"Preview {table.name}")
        if not result.ok or result.formatted is None:
            raise APIError(400, result.message or "The preview could not be loaded.", "preview")
        frame = result.formatted.dataframe
        kinds = {p.name: p.kind for p in result.formatted.profiles}
        return {"columns": [{"name": str(c), "kind": kinds.get(str(c), "text")} for c in frame.columns],
                "rows": ser.frame_rows(frame), "row_count": result.formatted.row_count}

    # ---------------------------------------------------------------- prompt lab
    @app.post("/api/prompt-preview")
    def prompt_preview(body: PromptPreviewBody, session: Session = Depends(session_dep)) -> dict[str, Any]:
        pipeline = pipeline_or_503()
        if body.config not in PRESETS:
            raise APIError(400, "Unknown configuration. Choose A, B, D or C.", "config")
        question = body.question.strip() or EXAMPLE_QUESTIONS[0]
        config = state.config(body.config)
        conversation = session.conversation
        context = pipeline.retriever.retrieve(question, mode=body.mode,
                                              previous_tables=conversation.previous_tables())
        history = conversation.history() if body.with_history else []
        sample_sql = next((r.sql for r in reversed(session.results.values()) if r.sql),
                          "SELECT e.employee_name, e.salary FROM employees AS e ORDER BY e.salary DESC LIMIT 5")
        contract = None
        if body.stage == "generation":
            prompt = pipeline.prompts.build_generation_prompt(question, config=config, schema_context=context,
                                                              history=history)
            contract = SQLGenerationResult
        elif body.stage == "verification":
            prompt = pipeline.prompts.build_verification_prompt(
                question, sql=sample_sql, schema_context=context, static_findings=["all automated checks passed"],
                history=history, config=config)
            contract = VerificationVerdict
        else:
            prompt = pipeline.prompts.build_explanation_prompt(sample_sql, schema_context=context)

        request_preview = None
        schema_preview = None
        if isinstance(pipeline.llm, ProviderLLM):
            request_preview, schema_preview = pipeline.llm.preview_request(prompt, contract or SQLGenerationResult)
        elif contract is not None:
            schema_preview = {"response_mime_type": "application/json", "response_json_schema": gemini_schema(contract)}
        cacheable = sum(s.token_estimate for s in prompt.system_sections) \
            if any(s.cache_breakpoint for s in prompt.sections) else None
        return {
            "config": ser.config_json(config),
            "question": question,
            "prompt": ser.prompt_json(prompt),
            "cacheable_tokens": cacheable,
            "context": {
                "mode": context.selection_mode, "tables": context.table_names,
                "scores": [{"table": name, "score": round(context.scores.get(name, 0.0), 3),
                            "selected": name in context.table_names, "why": context.reasons.get(name, [])}
                           for name in sorted(context.scores, key=lambda n: -context.scores[n])],
            },
            "request": request_preview,
            "output_schema": schema_preview,
            "contract": contract.__name__ if contract is not None else None,
            "sample_sql": sample_sql if body.stage != "generation" else None,
            "prompt_version": PROMPT_VERSION,
        }

    # ---------------------------------------------------------------- evaluation
    @app.get("/api/evaluation")
    def evaluation(dark: bool = False, session: Session = Depends(session_dep)) -> Response:
        summary = load_summary()
        if summary is None or not summary.get("configs"):
            return JSONResponse({"summary": None})
        rows = []
        for config in summary["configs"]:
            operational = config.get("operational", {})
            rows.append({
                "configuration": f"{config['name']}: {config['label']}",
                "metrics": {label(m): config["metrics"].get(m) for m in TABLE_METRICS},
                "avg_llm_calls": operational.get("avg_llm_calls"),
                "avg_latency_s": round(operational.get("avg_latency_ms", 0) / 1000, 2),
                "cost_usd": operational.get("total_cost_usd"),
            })
        meta = {"questions": summary.get("dataset", {}).get("items"), "model": summary.get("model"),
                "generated_at": summary.get("generated_at"), "database_unchanged": summary.get("database_unchanged"),
                "red_team": summary.get("validator_red_team"), "notes": summary.get("notes", []),
                "by_category": summary.get("by_category")}
        body = '{"summary": %s, "rows": %s, "figure": %s}' % (
            json.dumps(meta, default=str), json.dumps(rows, default=str),
            pio.to_json(comparison_figure(summary, dark=dark), validate=False))
        return Response(body, media_type="application/json")

    # ---------------------------------------------------------------- connection
    def connection_info() -> dict[str, Any]:
        connection = state.connection
        info: dict[str, Any] = {"database": None, "error": state.connection_error,
                                "settings": state.settings.public_summary()}
        if connection is not None:
            status_ = connection.db.test_connection()
            info["database"] = {
                "label": connection.db.label, "masked_url": connection.db.masked_url,
                "dialect": ser.DIALECT_NAMES.get(connection.schema.dialect, connection.schema.dialect),
                "dictionary_name": connection.dictionary_name,
                "is_demo": connection.dictionary_name == "demo data dictionary",
                "ok": status_.ok, "message": status_.message, "read_only": status_.read_only_enforced,
                "latency_ms": status_.latency_ms, "server_version": status_.server_version,
                "tables": len(connection.schema.tables),
            }
        return info

    def connect(db: DatabaseManager, dictionary: DataDictionary | None, dictionary_name: str | None) -> dict:
        status_ = db.test_connection()
        if not status_.ok:
            db.dispose()
            raise APIError(400, status_.message, "connect_failed")
        schema_ = state.switch_database(db, dictionary, dictionary_name)
        sessions.reset_all_conversations()
        return {"connected": True, "label": db.label, "tables": len(schema_.tables), **connection_info()}

    @app.get("/api/connection")
    def get_connection(session: Session = Depends(session_dep)) -> dict[str, Any]:
        return connection_info()

    @app.post("/api/connection/upload")
    def connect_upload(file: UploadFile = File(...), dictionary: UploadFile | None = File(None),
                       session: Session = Depends(session_dep)) -> dict[str, Any]:
        data = _read_limited(file, MAX_UPLOAD_BYTES, "database file")
        if not data.startswith(SQLITE_MAGIC):
            raise APIError(400, "That file is not a SQLite database. Choose a .db, .sqlite or .sqlite3 file.",
                           "not_sqlite")
        parsed_dictionary, dictionary_name = _dictionary(dictionary)
        uploads = state.settings.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        target = uploads / f"{hashlib.sha256(data).hexdigest()[:16]}.db"  # never trust the uploaded file name
        if not target.exists() or file_sha256(target) != hashlib.sha256(data).hexdigest():
            target.write_bytes(data)
        label_ = Path(file.filename or "uploaded.db").name[:120]
        db = DatabaseManager.for_sqlite_file(target, label=label_,
                                             query_timeout_seconds=state.settings.query_timeout_seconds)
        return connect(db, parsed_dictionary, dictionary_name)

    @app.post("/api/connection/path")
    def connect_path(path: str = Form(..., max_length=4096), dictionary: UploadFile | None = File(None),
                     session: Session = Depends(session_dep)) -> dict[str, Any]:
        if not path.strip():
            raise APIError(400, "Type the path to a SQLite file.", "empty")
        parsed_dictionary, dictionary_name = _dictionary(dictionary)
        db = DatabaseManager.for_sqlite_file(Path(path.strip()).expanduser(),
                                             query_timeout_seconds=state.settings.query_timeout_seconds)
        return connect(db, parsed_dictionary, dictionary_name)

    @app.post("/api/connection/server")
    def connect_server(engine: Literal["postgresql", "mysql"] = Form(...), host: str = Form("", max_length=255),
                       port: int = Form(0, ge=0, le=65535), database: str = Form(..., max_length=255),
                       username: str = Form("", max_length=255), password: str = Form("", max_length=1024),
                       dictionary: UploadFile | None = File(None),
                       session: Session = Depends(session_dep)) -> dict[str, Any]:
        parsed_dictionary, dictionary_name = _dictionary(dictionary)
        try:
            url = build_database_url(engine, database=database, host=host or None, port=port or None,
                                     username=username or None, password=password or None)
        except ValueError as exc:
            raise APIError(400, str(exc), "invalid") from None
        db = DatabaseManager(url, query_timeout_seconds=state.settings.query_timeout_seconds)
        return connect(db, parsed_dictionary, dictionary_name)

    @app.post("/api/connection/reset")
    def connect_demo(session: Session = Depends(session_dep)) -> dict[str, Any]:
        schema_ = state.reset_to_demo()
        sessions.reset_all_conversations()
        return {"connected": True, "label": state.connection.db.label if state.connection else "",
                "tables": len(schema_.tables), **connection_info()}

    return app
