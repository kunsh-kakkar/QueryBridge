# Code

The application code is kept at the repository root so the hosting service (Render) can run it directly:

| Path | What it is |
|---|---|
| `app.py` | Starts the web app |
| `backend/` | The pipeline: schema inspection and retrieval, few-shot selection, prompt building, Gemini client, SQL validation, verification, correction, safe execution, charts |
| `prompts/*.py` | Prompt text |
| `web/` | Web server (FastAPI) and the browser interface |
| `database/` | Demo database builder and data dictionary |
| `evaluation/` | Evaluation dataset (48 questions with gold SQL) and harness |
| `tests/` | Automated tests |
