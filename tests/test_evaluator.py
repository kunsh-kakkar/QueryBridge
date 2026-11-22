"""End-to-end tests of the evaluation harness (offline).

An *oracle* fake model answers every question with the gold SQL (or the
expected behaviour). Every metric must then be 100%, which proves the
harness scores correctly. A *broken* fake must score 0% on answerable
questions. These runs only validate the harness: they are never written to
``evaluation/results``.
"""

from __future__ import annotations

import json
import re

import pytest

from backend.models import SQLGenerationResult, VerificationVerdict
from backend.prompt_manager import PRESETS
from evaluation import baseline_comparison
from evaluation.evaluator import Evaluator, load_dataset, summarize
from evaluation.report import load_summary
from tests.fakes import FakeLLM, clarification, generation, refusal, verdict

ITEMS = load_dataset()
BY_QUESTION = {i.question: i for i in ITEMS}
QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)


def oracle(prompt, output_model):
    if output_model is VerificationVerdict:
        return verdict(True)
    assert output_model is SQLGenerationResult
    asked = QUESTION.search(prompt.section("question").text).group(1)
    item = BY_QUESTION[asked]
    if item.expected_behavior == "execute":
        return generation(item.gold_sql, confidence=0.95)
    if item.expected_behavior == "clarify":
        return clarification()
    if item.expected_behavior == "refuse":
        return refusal("unsafe_request")
    return refusal("out_of_scope")


def broken(prompt, output_model):
    if output_model is VerificationVerdict:
        return verdict(True)
    return generation("SELECT 1 AS wrong")


@pytest.fixture(scope="module")
def oracle_pipeline(demo_db_path):
    return baseline_comparison.build_pipeline(FakeLLM(responder=oracle), db_path=demo_db_path)


@pytest.mark.parametrize("config_name", ["A", "C"])
def test_oracle_scores_100_percent(oracle_pipeline, config_name):
    config = PRESETS[config_name]
    results = Evaluator(oracle_pipeline).evaluate(ITEMS, config)
    failures = [(r.id, r.status, r.match_reason) for r in results if not r.correct]
    assert failures == []
    metrics = summarize(results, config)["metrics"]
    for name in ("overall_accuracy", "execution_accuracy", "result_accuracy", "exact_match", "schema_compliance",
                 "safety_accuracy", "ambiguity_precision", "ambiguity_recall", "ambiguity_f1", "decline_accuracy"):
        assert metrics[name] == 1.0, name
    assert metrics["false_refusal_rate"] == 0.0


def test_broken_model_scores_zero_on_answerable_questions(demo_db_path):
    pipeline = baseline_comparison.build_pipeline(FakeLLM(responder=broken), db_path=demo_db_path)
    results = Evaluator(pipeline).evaluate([i for i in ITEMS if i.expected_behavior == "execute"][:6], PRESETS["B"])
    summary = summarize(results, PRESETS["B"])
    assert summary["metrics"]["result_accuracy"] == 0.0
    assert summary["metrics"]["execution_accuracy"] == 1.0  # it runs, but answers the wrong thing
    assert all("row" in r.match_reason or "column" in r.match_reason for r in results)


def test_early_refusals_need_no_model(oracle_pipeline):
    item = next(i for i in ITEMS if i.id == "Q43")  # "Delete all sales from 2023."
    calls_before = len(oracle_pipeline.llm.calls)
    outcome = Evaluator(oracle_pipeline).evaluate_item(item, PRESETS["C"])
    assert outcome.correct and outcome.status == "refused"
    assert len(oracle_pipeline.llm.calls) == calls_before


def test_full_run_writes_reports_and_proves_db_unchanged(oracle_pipeline, tmp_path):
    subset = [i for i in ITEMS if i.id in {"Q01", "Q10", "Q37", "Q40", "Q44", "Q47"}]
    summary = baseline_comparison.run(["B", "C"], subset, pipeline=oracle_pipeline, output_dir=tmp_path,
                                      model="fake-model", verbose=False)
    assert summary["database_unchanged"] is True
    assert summary["validator_red_team"]["blocked"] == summary["validator_red_team"]["attacks"]
    assert [c["name"] for c in summary["configs"]] == ["B", "C"]
    for name in ("summary.json", "comparison.md", "comparison.csv", "comparison.html"):
        assert (tmp_path / name).exists(), name
    assert "| Overall accuracy |" in (tmp_path / "comparison.md").read_text()
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1 and (runs[0] / "items_C.jsonl").read_text().count("\n") == len(subset)
    loaded = load_summary(tmp_path / "summary.json")  # the web interface can read it
    assert loaded["configs"][1]["metrics"]["overall_accuracy"] == 1.0
    assert json.loads((tmp_path / "summary.json").read_text())["dataset"]["items"] == len(subset)


def test_estimate_uses_real_prompts(oracle_pipeline):
    rows = baseline_comparison.estimate(oracle_pipeline, ITEMS[:5], ["A", "C"])
    assert rows["A"]["avg_prompt_tokens"] < 200 < rows["C"]["avg_prompt_tokens"]
    assert rows["A"]["calls"] == 5 and rows["C"]["calls"] > rows["A"]["calls"]  # C adds the review call


def test_cli_estimate_needs_no_credentials(isolated_env, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    config.get_settings.cache_clear()
    assert baseline_comparison.main(["--estimate", "--limit", "3", "--configs", "B"]) == 0
    out = capsys.readouterr().out
    assert "Google Gemini" in out and "No charge on the Gemini free tier" in out and "$" not in out
    assert baseline_comparison.main(["--limit", "1", "--configs", "B", "--yes"]) == 2  # no key: refuses to run
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    config.get_settings.cache_clear()
