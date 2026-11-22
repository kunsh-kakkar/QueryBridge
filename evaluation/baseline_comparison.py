"""
Compare prompt configurations A, B, D and C on the evaluation dataset.

    python -m evaluation.baseline_comparison --estimate        # request estimate only, no API calls
    python -m evaluation.baseline_comparison                   # full run (asks for confirmation)
    python -m evaluation.baseline_comparison --configs B C --limit 10 --yes

Every number is measured by running the real pipeline against the real
database. Outputs (in ``evaluation/results/``):

* ``summary.json``: read by the Streamlit Evaluation page;
* ``comparison.md``, ``comparison.csv``, ``comparison.html``: report tables and chart;
* ``runs/<timestamp>/items_<config>.jsonl``: every item's outcome, for auditing.

The demo database's content fingerprint is recorded before and after the run,
to prove that no question changed any data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.conversation_manager import ConversationManager
from backend.database import DatabaseManager
from backend.llm_client import create_llm_client
from backend.models import ConversationTurn
from backend.pipeline import QueryPipeline
from backend.prompt_manager import EVALUATION_ORDER, PRESETS
from backend.schema_inspector import SchemaInspector, bundled_data_dictionary
from config import API_KEY_VARIABLE, PROJECT_ROOT, PROVIDER_NAME, get_settings
from database.init_db import ensure_database, logical_fingerprint
from evaluation.evaluator import EvalItem, Evaluator, ItemResult, load_dataset, summarize
from evaluation.red_team import run_red_team
from evaluation.report import write_reports
from utils.helpers import utc_now_iso

RESULTS_DIR = PROJECT_ROOT / "evaluation" / "results"
REFERENCE_DATE = date(2026, 11, 15)  # pinned: relative-date questions have stable gold answers
CALLS_PER_ITEM = {"A": 1.0, "B": 1.0, "D": 1.0, "C": 2.2}  # C: generation + review (+ occasional correction)


def build_pipeline(llm=None, *, db_path: Path | None = None) -> QueryPipeline:
    settings = get_settings()
    path = db_path or ensure_database(settings.demo_db_path)
    db = DatabaseManager.for_sqlite_file(path, label="QueryBridge demo database",
                                         query_timeout_seconds=settings.query_timeout_seconds)
    schema = SchemaInspector(db, data_dictionary=bundled_data_dictionary()).inspect()
    return QueryPipeline(db=db, schema=schema, llm=llm or create_llm_client(settings), config=PRESETS["C"],
                         reference_date=REFERENCE_DATE, max_rows=settings.max_result_rows,
                         query_timeout_seconds=settings.query_timeout_seconds, history=None)


def select_items(items: list[EvalItem], categories: list[str] | None, limit: int | None) -> list[EvalItem]:
    chosen = [i for i in items if not categories or i.category in categories]
    return chosen[:limit] if limit else chosen


def estimate(pipeline: QueryPipeline, items: list[EvalItem], configs: list[str]) -> dict[str, Any]:
    """Request and prompt-size estimate from the *actual* prompts each configuration would send."""
    rows = {}
    for name in configs:
        config = PRESETS[name]
        system_tokens = user_tokens = 0
        for item in items:
            history = ConversationManager(max_turns=5)
            for turn in item.turns:
                history.add_turn(ConversationTurn(question=turn.question, sql=turn.gold_sql, status="success"))
            context = pipeline.retriever.retrieve(item.question)
            prompt = pipeline.prompts.build_generation_prompt(item.question, config=config, schema_context=context,
                                                              history=history.history())
            system_tokens += sum(s.token_estimate for s in prompt.system_sections)
            user_tokens += sum(s.token_estimate for s in prompt.user_sections)
        calls = CALLS_PER_ITEM[name] * len(items)
        rows[name] = {"calls": round(calls),
                      "avg_prompt_tokens": round((system_tokens + user_tokens) / max(1, len(items)))}
    return rows


def run(configs: list[str], items: list[EvalItem], *, pipeline: QueryPipeline, output_dir: Path,
        model: str, verbose: bool = True) -> dict[str, Any]:
    db_path = pipeline.db.sqlite_path
    before = logical_fingerprint(db_path) if db_path else None
    evaluator = Evaluator(pipeline)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = output_dir / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    summaries = []
    all_results: dict[str, list[ItemResult]] = {}
    for name in configs:
        config = PRESETS[name]

        def progress(index: int, total: int, outcome: ItemResult, _name: str = name) -> None:
            if verbose:
                mark = "✓" if outcome.correct else "✗"
                print(f"  [{_name}] {index:>2}/{total} {outcome.id} {mark} {outcome.status:<13} "
                      f"{outcome.latency_ms / 1000:5.1f}s  {outcome.match_reason or ''}", flush=True)

        if verbose:
            print(f"\nConfiguration {name}: {config.label}")
        results = evaluator.evaluate(items, config, progress=progress)
        all_results[name] = results
        with open(run_dir / f"items_{name}.jsonl", "w", encoding="utf-8") as handle:
            for r in results:
                handle.write(r.model_dump_json() + "\n")
        summaries.append(summarize(results, config))

    after = logical_fingerprint(db_path) if db_path else None
    categories = sorted({i.category for i in items}, key=[i.category for i in items].index)
    by_category = {}
    for category in categories:
        row: dict[str, Any] = {"n": sum(1 for i in items if i.category == category)}
        for summary in summaries:
            row[summary["name"]] = summary["by_category"].get(category, {}).get("accuracy")
        by_category[category] = row

    summary = {
        "generated_at": utc_now_iso(),
        "model": model,
        "reference_date": REFERENCE_DATE.isoformat(),
        "dataset": {"items": len(items), "categories": {c: by_category[c]["n"] for c in categories}},
        "configs": summaries,
        "by_category": by_category,
        "database_unchanged": before == after,
        "fingerprint_before": before,
        "fingerprint_after": after,
        "validator_red_team": run_red_team(),
        "run_directory": str(run_dir.relative_to(PROJECT_ROOT)) if run_dir.is_relative_to(PROJECT_ROOT) else str(run_dir),
        "notes": [
            "Multi-turn items replay the gold previous turn as conversation history.",
            "Result-set accuracy ignores column order/names and allows extra columns; exact match is strict.",
            "Costs are estimates from token usage and list prices.",
        ],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_reports(summary, output_dir)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare QueryBridge prompt configurations.")
    parser.add_argument("--configs", nargs="+", default=list(EVALUATION_ORDER), choices=list(PRESETS))
    parser.add_argument("--categories", nargs="+", help="only these dataset categories")
    parser.add_argument("--limit", type=int, help="only the first N items")
    parser.add_argument("--estimate", action="store_true", help="print a request estimate and exit (no API calls)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    settings = get_settings()
    items = select_items(load_dataset(), args.categories, args.limit)
    configs = [c for c in EVALUATION_ORDER if c in args.configs]
    pipeline = build_pipeline(llm=None if settings.has_llm_credentials else _NoCallLLM())

    model = settings.effective_model
    print(f"{len(items)} questions × {len(configs)} configurations ({', '.join(configs)}) · "
          f"{PROVIDER_NAME} · model {model}")
    rows = estimate(pipeline, items, configs)
    for name, row in rows.items():
        print(f"  {name}: ~{row['calls']} calls · ~{row['avg_prompt_tokens']:,} prompt tokens each")
    total_calls = sum(r["calls"] for r in rows.values())
    print(f"  ~{total_calls} requests in total. No charge on the Gemini free tier, but free-tier requests are "
          "limited per model per day (see your limits in Google AI Studio): spread a full run over several "
          "days with --configs / --limit if needed.")
    if args.estimate:
        return 0
    if not settings.has_llm_credentials:
        print(f"\nNo {PROVIDER_NAME} API key configured. Set {API_KEY_VARIABLE} in .env "
              "to run the evaluation.", file=sys.stderr)
        return 2
    if not args.yes and input("Run now? This makes real API calls. [y/N] ").strip().lower() != "y":
        print("Cancelled.")
        return 1

    summary = run(configs, items, pipeline=pipeline, output_dir=args.output_dir, model=model)
    print("\nResults")
    for config in summary["configs"]:
        m = config["metrics"]
        print(f"  {config['name']}: overall {m['overall_accuracy']:.1%} · result-set {m['result_accuracy']:.1%} · "
              f"safety {m['safety_accuracy']:.1%} · ambiguity F1 {m['ambiguity_f1']:.1%}")
    print(f"Database unchanged: {summary['database_unchanged']} · reports in {args.output_dir}")
    return 0


class _NoCallLLM:
    """Placeholder so the estimate can build prompts without credentials; never called."""

    def complete(self, prompt, output_model):  # pragma: no cover - guarded by has_llm_credentials
        raise RuntimeError("No API key configured")


if __name__ == "__main__":
    raise SystemExit(main())
