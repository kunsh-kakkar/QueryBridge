"""
Browser-test server for tests/browser/walkthrough.js.

Runs the real web app and the real demo database, but with a *scripted* stand-in
model (tests.fakes.FakeLLM), so the interface can be exercised without an API key
or network access. The SQL it returns is fixed per question and still goes through
the full validator, verifier and read-only executor. Never use this for real answers.

    python -m tests.browser.demo_server 8765
"""

import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from backend.models import SQLExplanationResult, SQLGenerationResult, VerificationVerdict  # noqa: E402
from config import Settings  # noqa: E402
from tests.fakes import FakeLLM, clarification, explanation, generation, verdict  # noqa: E402
from web.app_state import AppState  # noqa: E402
from web.server import create_app  # noqa: E402

ANSWERS = {
    "top 5 employees": ("SELECT e.employee_name, d.department_name, e.salary FROM employees AS e JOIN departments AS d "
                        "ON d.department_id = e.department_id ORDER BY e.salary DESC LIMIT 5",
                        "The five employees with the highest salaries, with their department."),
    "by product category": ("SELECT p.category, ROUND(SUM(s.sale_amount), 2) AS total_sales FROM sales AS s JOIN products AS p "
                            "ON p.product_id = s.product_id GROUP BY p.category ORDER BY total_sales DESC",
                            "Total sales amount for each product category, largest first."),
    "monthly revenue": ("SELECT strftime('%Y-%m', s.sale_date) AS month, ROUND(SUM(s.sale_amount), 2) AS revenue FROM sales AS s "
                        "WHERE s.sale_date >= '2025-01-01' AND s.sale_date < '2026-01-01' GROUP BY month ORDER BY month",
                        "Revenue for each month of 2025."),
    "how many employees": ("SELECT COUNT(*) AS employee_count FROM employees", "The number of employees."),
    "never made a purchase": ("SELECT c.customer_name, c.city FROM customers AS c WHERE NOT EXISTS (SELECT 1 FROM sales AS s "
                              "WHERE s.customer_id = c.customer_id) ORDER BY c.customer_name",
                              "Customers with no sales."),
    "total sales amount": ("SELECT e.employee_name, ROUND(SUM(s.sale_amount), 2) AS total_sales FROM employees AS e JOIN sales AS s "
                           "ON s.employee_id = e.employee_id GROUP BY e.employee_name ORDER BY total_sales DESC LIMIT 5",
                           "The five employees with the highest total sales amount."),
}


def respond(prompt, model):
    section = prompt.section("question")
    text = (section.text.split("</question>")[0] if section else prompt.user_text).lower()
    if model is VerificationVerdict:
        return verdict(True)
    if model is SQLExplanationResult:
        return SQLExplanationResult(summary="Lists each department's average salary, for departments with more than five employees.",
                                    steps=[], tables_used=["employees", "departments"], columns_used=[],
                                    potential_issues=[], modifies_data=False)
    assert model is SQLGenerationResult
    if "highest-performing" in text and "clarification -" not in text:
        return clarification("How should performance be measured?", ["Total sales amount", "Average review rating"])
    for key, (sql, summary) in ANSWERS.items():
        if key in text:
            result = generation(sql, summary=summary, assumptions=["Salaries are compared as stored, in one currency."]
                                if "employees" in key else [])
            return result.model_copy(update={"explanation": explanation(
                summary, tables_accessed=["employees", "departments"] if "employees" in key else ["sales"],
                ordering_and_limits="Sorted from highest to lowest, top 5 kept." if "top" in key else None)})
    return generation(None, intent="out_of_scope", summary="This question cannot be answered from this database.")


def main():
    data = Path(tempfile.mkdtemp(prefix="qb-ui-"))  # fresh history every run
    source = ROOT / "data" / "querybridge_demo.db"
    if source.exists():
        shutil.copyfile(source, data / "querybridge_demo.db")
    settings = Settings(_env_file=None, data_dir=data, reference_date=date(2026, 11, 15))
    state = AppState(settings, llm=FakeLLM(responder=respond))
    uvicorn.run(create_app(state), host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765, log_level="warning")


if __name__ == "__main__":
    main()
