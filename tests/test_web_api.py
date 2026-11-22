"""Web API tests: the FastAPI server over the real demo database, with a scripted FakeLLM (no network)."""

from __future__ import annotations

import json
import shutil
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.exceptions import LLMRequestError
from tests.fakes import FakeLLM, clarification, generation, sql_explanation, verdict
from web import server
from web.app_state import AppState
from web.server import create_app

SESSION = "test-session-0123456789abcdef"
OVER_50K = "SELECT e.employee_name, e.salary FROM employees AS e WHERE e.salary > 50000"
SORTED = ("SELECT e.employee_name, e.salary, d.department_name FROM employees AS e JOIN departments AS d "
          "ON d.department_id = e.department_id WHERE e.salary > 50000 ORDER BY d.department_name")
BY_CATEGORY = ("SELECT p.category, ROUND(SUM(s.sale_amount), 2) AS total_sales FROM sales AS s "
               "JOIN products AS p ON p.product_id = s.product_id GROUP BY p.category ORDER BY total_sales DESC")
TOP_SELLERS = ("SELECT e.employee_name, SUM(s.sale_amount) AS total_sales FROM employees e JOIN sales s "
               "ON s.employee_id = e.employee_id GROUP BY e.employee_name ORDER BY total_sales DESC LIMIT 5")


@pytest.fixture
def env(tmp_path, demo_db_path, make_settings):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(demo_db_path, data / "querybridge_demo.db")
    settings = make_settings(data_dir=data, reference_date=date(2026, 11, 15))
    llm = FakeLLM()
    state = AppState(settings, llm=llm)
    client = TestClient(create_app(state))
    client.headers.update({"X-Session-Id": SESSION, "X-QueryBridge": "1"})
    yield client, llm, state
    client.close()
    if state.connection is not None:
        state.connection.db.dispose()


def ask(client: TestClient, question: str = "", **extra) -> tuple[list[str], dict]:
    """POST /api/ask and parse the NDJSON stream into (step labels, final event)."""
    response = client.post("/api/ask", json={"question": question, **extra})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    steps = [e["label"] for e in events if e["type"] == "step"]
    final = events[-1]
    assert final["type"] in ("result", "error")
    return steps, final


# ---------------------------------------------------------------- pages and security
def test_index_and_assets_are_served_with_security_headers(env):
    client, _, _ = env
    page = client.get("/")
    assert page.status_code == 200 and "<title>QueryBridge</title>" in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    for path in ("/static/css/app.css", "/static/js/app.js", "/vendor/plotly.min.js", "/favicon.svg"):
        assert client.get(path).status_code == 200, path


def test_page_has_no_inline_scripts_or_handlers(env):
    client, _, _ = env
    html = client.get("/").text
    assert "<script>" not in html and " onclick=" not in html and "javascript:" not in html


def test_state_changing_requests_need_the_custom_header(env):
    client, _, _ = env
    response = client.post("/api/session/reset", headers={"X-QueryBridge": ""})
    assert response.status_code == 403 and response.json()["error"]["code"] == "csrf"
    assert client.post("/api/session/reset").status_code == 200


def test_unknown_host_is_rejected(env):
    client, _, _ = env
    assert client.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 400


def test_public_host_name_is_accepted_when_deployed(env):
    import app

    _, _, state = env
    allowed = app.allowed_hosts_for("0.0.0.0", ["querybridge.onrender.com"])
    assert "127.0.0.1" in allowed and "querybridge.onrender.com" in allowed
    with TestClient(create_app(state, allowed_hosts=allowed)) as client:
        assert client.get("/", headers={"Host": "querybridge.onrender.com"}).status_code == 200
        assert client.get("/", headers={"Host": "evil.example.com"}).status_code == 400


def test_hosted_mode_refuses_database_changes(tmp_path, demo_db_path, make_settings):
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(demo_db_path, data / "querybridge_demo.db")
    state = AppState(make_settings(data_dir=data, hosted=True), llm=FakeLLM())
    with TestClient(create_app(state)) as client:
        client.headers.update({"X-Session-Id": SESSION, "X-QueryBridge": "1"})
        assert client.get("/api/status").json()["hosted"] is True
        attempts = {
            "upload": {"files": {"file": ("other.db", b"SQLite format 3\x00" + b"0" * 200)}},
            "path": {"data": {"path": "/etc/passwd"}},
            "server": {"data": {"engine": "postgresql", "database": "x", "host": "169.254.169.254"}},
            "reset": {},
        }
        for path, kwargs in attempts.items():
            response = client.post(f"/api/connection/{path}", **kwargs)
            assert response.status_code == 403 and response.json()["error"]["code"] == "hosted", path
        assert client.get("/api/connection").json()["database"]["is_demo"] is True  # reading still works
        run = client.post("/api/sql/run", json={"sql": "SELECT COUNT(*) AS n FROM employees"}).json()
        assert run["status"] == "success" and run["kpi"]["value"] == 60
    state.connection.db.dispose()


def test_session_id_is_required_and_validated(env):
    client, _, _ = env
    assert client.get("/api/status", headers={"X-Session-Id": ""}).status_code == 400
    assert client.get("/api/status", headers={"X-Session-Id": "bad id with spaces!"}).status_code == 400


def test_validation_errors_never_echo_the_request(env):
    client, _, _ = env
    response = client.post("/api/connection/server", data={"engine": "oracle", "password": "hunter2-secret"})
    assert response.status_code == 422
    assert "hunter2" not in response.text and "engine" in response.json()["error"]["message"]


# ---------------------------------------------------------------- status and sessions
def test_status_describes_database_model_and_examples(env):
    client, _, _ = env
    body = client.get("/api/status").json()
    assert body["database"]["tables"] == 8 and body["database"]["rows"] == 3092
    assert body["database"]["is_demo"] is True
    assert body["llm"]["ready"] is True and body["config"]["name"] == "C"
    assert [c["name"] for c in body["configs"]] == ["A", "B", "D", "C"]
    assert "Which customers have never made a purchase?" in body["examples"]
    assert "AIza" not in json.dumps(body)


def test_config_switch_is_per_session(env):
    client, _, _ = env
    assert client.post("/api/session/config", json={"name": "B"}).json()["config"]["name"] == "B"
    assert client.get("/api/status").json()["config"]["name"] == "B"
    other = client.get("/api/status", headers={"X-Session-Id": "another-session-0123456789"}).json()
    assert other["config"]["name"] == "C"
    assert client.post("/api/session/config", json={"name": "Z"}).status_code == 400


def test_missing_api_key_is_explained(tmp_path, demo_db_path, make_settings):
    variable = "GEMINI_API_KEY"
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(demo_db_path, data / "querybridge_demo.db")
    state = AppState(make_settings(data_dir=data))  # real Gemini client, no key
    with TestClient(create_app(state)) as client:
        client.headers.update({"X-Session-Id": SESSION, "X-QueryBridge": "1"})
        status = client.get("/api/status").json()
        assert status["llm"]["ready"] is False and variable in status["llm"]["message"]
        assert status["llm"]["provider"] == "gemini" and status["llm"]["key_variable"] == variable
        response = client.post("/api/ask", json={"question": "How many employees are there?"})
        assert response.status_code == 400 and response.json()["error"]["code"] == "no_api_key"
        assert variable in response.json()["error"]["message"]
        # The prompt lab previews the exact request without calling the API
        preview = client.post("/api/prompt-preview", json={"question": "Top 5 salaries", "config": "C"}).json()
        assert preview["request"]["model"] == "gemini-3.8-flash" and preview["request"]["contents"]
        assert preview["request"]["config"]["thinking_config"] == {"thinking_level": "MEDIUM"}
        assert "$defs" not in json.dumps(preview["output_schema"]["response_json_schema"])
        # SQL tools still work without a key
        run = client.post("/api/sql/run", json={"sql": "SELECT COUNT(*) AS n FROM employees"}).json()
        assert run["status"] == "success" and run["kpi"]["value"] == 60
    state.connection.db.dispose()


# ---------------------------------------------------------------- asking
def test_ask_streams_progress_then_a_result(env):
    client, llm, _ = env
    llm.queue(generation(OVER_50K, summary="Employees earning over 50,000."), verdict(True))
    steps, final = ask(client, "Show employees earning more than 50,000")
    assert steps[0] == "Checking your question…" and "Generating SQL with AI…" in steps
    result = final["result"]
    assert result["status"] == "success" and result["status_text"] == "Answered"
    assert result["table"]["row_count"] == 52 and result["message"] == "Employees earning over 50,000."
    assert [c["name"] for c in result["table"]["columns"]] == ["employee_name", "salary"]
    assert "LIMIT" in result["sql"] and result["executed_sql"]
    assert result["confidence_label"] in ("High", "Medium", "Low")
    assert result["translations"]  # PostgreSQL / MySQL versions for reference
    assert result["row_limit_added"] is True
    assert not any("reduced" in w for w in result["warnings"])  # our own row cap is not reported as a reduction


def test_follow_up_uses_conversation_memory(env):
    client, llm, _ = env
    llm.queue(generation(OVER_50K), verdict(True),
              generation(SORTED, intent="follow_up_modification"), verdict(True))
    ask(client, "Show employees earning more than 50,000")
    _, final = ask(client, "Now sort them by department")
    assert final["result"]["is_follow_up"] is True
    assert "department_name" in [c["name"] for c in final["result"]["table"]["columns"]]
    assert client.get("/api/status").json()["conversation"]["turns"] == 2
    client.post("/api/session/reset")
    assert client.get("/api/status").json()["conversation"]["turns"] == 0


def test_clarification_round_trip(env):
    client, llm, _ = env
    llm.queue(clarification("How should performance be measured?", ["Total sales amount", "Average rating"]),
              generation(TOP_SELLERS), verdict(True))
    _, first = ask(client, "Show me the highest-performing employees")
    assert first["result"]["status"] == "clarification"
    assert first["result"]["clarification"]["options"] == ["Total sales amount", "Average rating"]
    assert client.get("/api/status").json()["conversation"]["pending_clarification"] is True
    _, second = ask(client, clarification_answer="Total sales amount")
    assert second["result"]["status"] == "success" and second["result"]["table"]["row_count"] == 5
    assert "Total sales amount" in llm.prompts[-2].user_text
    latest = client.get("/api/history").json()["entries"][0]
    assert latest["question"] == "Show me the highest-performing employees"
    assert latest["clarified_with"] == "Total sales amount"


def test_clarification_without_pending_question_is_rejected(env):
    client, _, _ = env
    response = client.post("/api/ask", json={"clarification_answer": "Total sales"})
    assert response.status_code == 409


def test_empty_question_is_rejected(env):
    client, _, _ = env
    assert client.post("/api/ask", json={"question": "   "}).status_code == 400


def test_write_request_is_refused_without_calling_the_model(env):
    client, llm, _ = env
    _, final = ask(client, "Delete all sales from 2023")
    assert final["result"]["status"] == "refused" and llm.calls == []


def test_model_errors_become_friendly_messages(env):
    client, llm, _ = env
    llm.queue(LLMRequestError("The AI service rejected the request.", detail="HTTP 400: invalid argument"))
    _, final = ask(client, "How many employees are there?")
    assert final["result"]["status"] == "error"
    assert final["result"]["message"] == "The AI service rejected the request."
    assert "invalid argument" not in final["result"]["message"]


def test_unexpected_errors_end_the_stream_cleanly(env):
    client, llm, _ = env
    llm.queue(RuntimeError("internal detail that must not leak"))
    _, final = ask(client, "How many employees are there?")
    assert final["type"] == "error" and "internal detail" not in final["message"]
    llm.queue(generation("SELECT COUNT(*) AS n FROM employees"), verdict(True))
    _, again = ask(client, "How many employees are there?")  # the session lock was released
    assert again["result"]["kpi"]["value"] == 60


def test_busy_session_is_reported(env):
    client, _, _ = env
    session = client.app.state.sessions.get(SESSION)
    session.lock.acquire()
    try:
        response = client.post("/api/ask", json={"question": "How many employees are there?"})
        assert response.status_code == 409 and response.json()["error"]["code"] == "busy"
    finally:
        session.lock.release()


def test_chart_and_details_for_an_answer(env):
    client, llm, _ = env
    llm.queue(generation(BY_CATEGORY), verdict(True))
    _, final = ask(client, "What is the total sales amount by product category?")
    result = final["result"]
    assert result["charts"] and result["charts"][0]["type"] == "bar"
    chart = client.get(f"/api/results/{result['id']}/chart", params={"index": 0, "dark": True}).json()
    assert chart["figure"]["data"][0]["type"] == "bar" and chart["title"]
    assert chart["figure"]["layout"]["paper_bgcolor"].lower() == "#1a1a19"
    details = client.get(f"/api/results/{result['id']}/details").json()
    assert [s["name"] for s in details["steps"]][0] == "Input check"
    assert details["prompts"][0]["stage"] == "generation" and details["prompts"][0]["sections"]
    assert details["verification"]["checks"]
    assert client.get("/api/results/unknown/details").status_code == 404
    assert client.get(f"/api/results/{result['id']}/chart", params={"index": 9}).status_code == 404


def test_results_belong_to_their_session(env):
    client, llm, _ = env
    llm.queue(generation(OVER_50K), verdict(True))
    _, final = ask(client, "Show employees earning more than 50,000")
    other = client.get(f"/api/results/{final['result']['id']}/details",
                       headers={"X-Session-Id": "another-session-0123456789"})
    assert other.status_code == 404


# ---------------------------------------------------------------- SQL tools
def test_run_sql_validates_and_blocks(env):
    client, llm, _ = env
    ok = client.post("/api/sql/run", json={"sql": "SELECT category, COUNT(*) AS n FROM products GROUP BY category"})
    assert ok.json()["status"] == "success" and ok.json()["table"]["row_count"] == 5
    blocked = client.post("/api/sql/run", json={"sql": "DROP TABLE employees"}).json()
    assert blocked["status"] == "blocked" and blocked["table"] is None
    check = client.post("/api/sql/check", json={"sql": "DELETE FROM sales"}).json()["validation"]
    assert check["is_safe"] is False and check["is_blocked"] is True
    assert llm.calls == []
    assert client.get("/api/status").json()["database"]["rows"] == 3092  # nothing changed


def test_explain_sql(env):
    client, llm, _ = env
    llm.queue(sql_explanation("Counts the employees."))
    body = client.post("/api/sql/explain", json={"sql": "SELECT COUNT(*) FROM employees", "level": "beginner"}).json()
    assert body["error"] is None and body["explanation"]["summary"] == "Counts the employees."
    assert body["validation"]["is_safe"] is True


# ---------------------------------------------------------------- history
def test_history_list_rerun_delete_and_clear(env):
    client, llm, _ = env
    llm.queue(generation(OVER_50K), verdict(True))
    ask(client, "Show employees earning more than 50,000")
    history = client.get("/api/history").json()
    assert history["stats"]["total"] == 1
    entry = history["entries"][0]
    assert entry["question"] == "Show employees earning more than 50,000" and entry["status_text"] == "Answered"
    assert client.get("/api/history", params={"search": "nothing-like-this"}).json()["entries"] == []
    rerun = client.post(f"/api/history/{entry['id']}/rerun").json()
    assert rerun["status"] == "success" and rerun["config_name"] == "direct" and len(llm.calls) == 2
    assert client.delete(f"/api/history/{entry['id']}").json()["deleted"] is True
    assert client.post(f"/api/history/{entry['id']}/rerun").status_code == 404
    client.post("/api/sql/run", json={"sql": "SELECT 1 AS one"})
    assert client.delete("/api/history").json()["removed"] >= 0
    assert client.get("/api/history").json()["stats"]["total"] == 0


# ---------------------------------------------------------------- schema
def test_schema_and_preview(env):
    client, _, _ = env
    schema = client.get("/api/schema").json()
    assert schema["totals"] == {"tables": 8, "columns": 47, "rows": 3092, "relationships": 10}
    employees = next(t for t in schema["tables"] if t["name"] == "employees")
    salary = next(c for c in employees["columns"] if c["name"] == "salary")
    assert salary["friendly_type"] in ("Number", "Whole number") and employees["links"]
    preview = client.get("/api/schema/tables/employees/preview").json()
    assert preview["row_count"] == 10 and preview["columns"][0]["name"] == "employee_id"
    assert client.get("/api/schema/tables/no_such_table/preview").status_code == 404


# ---------------------------------------------------------------- prompt lab and evaluation
@pytest.mark.parametrize("stage", ["generation", "verification", "explanation"])
def test_prompt_preview_makes_no_model_call(env, stage):
    client, llm, _ = env
    body = client.post("/api/prompt-preview", json={"question": "Top 5 salaries", "config": "C",
                                                    "stage": stage}).json()
    assert body["prompt"]["stage"] == stage and body["prompt"]["sections"]
    assert body["context"]["scores"] and llm.calls == []


def test_prompt_preview_baseline_config(env):
    client, _, _ = env
    body = client.post("/api/prompt-preview", json={"question": "Top 5 salaries", "config": "A"}).json()
    assert body["config"]["name"] == "A" and body["prompt"]["examples"] == []


def test_evaluation_without_results_reports_none(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(server, "load_summary", lambda: None)
    assert client.get("/api/evaluation").json() == {"summary": None}


def test_evaluation_with_results(env, monkeypatch):
    client, _, _ = env
    metrics = {"overall_accuracy": 0.5, "result_accuracy": 0.5, "execution_accuracy": 0.9, "exact_match": 0.1,
               "schema_compliance": 1.0, "safety_accuracy": 1.0, "ambiguity_f1": 0.5}
    summary = {"model": "test", "dataset": {"items": 48}, "configs": [
        {"name": "A", "label": "Baseline", "metrics": metrics, "operational": {"avg_llm_calls": 1.0}},
        {"name": "C", "label": "Full", "metrics": metrics, "operational": {"avg_llm_calls": 2.0}}]}
    monkeypatch.setattr(server, "load_summary", lambda: summary)
    body = client.get("/api/evaluation").json()
    assert body["summary"]["questions"] == 48 and len(body["rows"]) == 2 and body["figure"]["data"]


# ---------------------------------------------------------------- connection
def test_connection_info_hides_secrets(env):
    client, _, _ = env
    body = client.get("/api/connection").json()
    assert body["database"]["ok"] is True and body["database"]["read_only"] is True
    assert body["database"]["is_demo"] is True and "settings" in body


def test_upload_sqlite_and_reset_to_demo(env, demo_db_path):
    client, llm, state = env
    llm.queue(generation(OVER_50K), verdict(True))
    ask(client, "Show employees earning more than 50,000")
    with open(demo_db_path, "rb") as handle:
        response = client.post("/api/connection/upload", files={"file": ("my shop.db", handle,
                                                                         "application/octet-stream")})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is True and body["label"] == "my shop.db" and body["tables"] == 8
    assert body["database"]["is_demo"] is False
    assert client.get("/api/status").json()["conversation"]["turns"] == 0  # new database, new conversation
    uploads = list((state.settings.data_dir / "uploads").iterdir())
    assert len(uploads) == 1 and uploads[0].name != "my shop.db"
    examples = client.get("/api/status").json()["examples"]
    assert examples and all(e.startswith(("How many", "Show 10")) for e in examples)
    reset = client.post("/api/connection/reset").json()
    assert reset["database"]["is_demo"] is True


def test_upload_rejects_non_sqlite_files(env):
    client, _, _ = env
    response = client.post("/api/connection/upload", files={"file": ("notes.db", b"hello world", "text/plain")})
    assert response.status_code == 400 and "not a SQLite database" in response.json()["error"]["message"]


def test_bad_data_dictionary_is_explained(env, demo_db_path):
    client, _, _ = env
    with open(demo_db_path, "rb") as handle:
        response = client.post("/api/connection/upload", files={
            "file": ("shop.db", handle, "application/octet-stream"),
            "dictionary": ("dict.json", b"not json", "application/json")})
    assert response.status_code == 400 and "data dictionary" in response.json()["error"]["message"]


def test_connect_by_path_and_missing_file(env, demo_db_path):
    client, _, _ = env
    ok = client.post("/api/connection/path", data={"path": str(demo_db_path)})
    assert ok.status_code == 200 and ok.json()["tables"] == 8
    missing = client.post("/api/connection/path", data={"path": "/no/such/file.db"})
    assert missing.status_code == 400 and missing.json()["error"]["message"]
