"""Tests for config.py: defaults, env overrides, hard caps and secret handling."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

import config
from config import HARD_MAX_CORRECTION_ATTEMPTS, PROJECT_ROOT, Settings, parse_database_url


def test_defaults(make_settings):
    s = make_settings()
    assert s.model is None and s.effective_model == "gemini-3.8-flash"
    assert s.effective_verifier_model == s.effective_model
    assert s.max_correction_attempts == 2
    assert s.is_demo_database
    assert s.database_dialect == "sqlite"
    assert s.database_url_value().endswith("data/querybridge_demo.db")
    assert s.data_dir == PROJECT_ROOT / "data"
    assert s.has_llm_credentials is False
    assert s.effective_reference_date == date.today()


def test_environment_overrides(isolated_env):
    isolated_env.setenv("QB_MODEL", "gemini-3.6-flash")
    isolated_env.setenv("QB_MAX_RESULT_ROWS", "50")
    isolated_env.setenv("QB_ENABLE_SEMANTIC_VERIFICATION", "false")
    isolated_env.setenv("QB_GENERATION_EFFORT", "high")
    s = Settings(_env_file=None)
    assert s.model == "gemini-3.6-flash"
    assert s.max_result_rows == 50
    assert s.enable_semantic_verification is False
    assert s.generation_effort == "high"


def test_env_file_is_read(isolated_env, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("QB_MODEL=gemini-2.5-flash-lite\nQB_FEW_SHOT_K=2\n", encoding="utf-8")
    s = Settings(_env_file=env_file)
    assert s.model == "gemini-2.5-flash-lite"
    assert s.few_shot_k == 2


@pytest.mark.parametrize("attempts", [0, 1, HARD_MAX_CORRECTION_ATTEMPTS])
def test_correction_attempts_within_cap(make_settings, attempts):
    assert make_settings(max_correction_attempts=attempts).max_correction_attempts == attempts


@pytest.mark.parametrize("attempts", [-1, HARD_MAX_CORRECTION_ATTEMPTS + 1, 100])
def test_correction_attempts_above_cap_rejected(make_settings, attempts):
    with pytest.raises(ValidationError):
        make_settings(max_correction_attempts=attempts)


def test_correction_cap_enforced_from_environment(isolated_env):
    isolated_env.setenv("QB_MAX_CORRECTION_ATTEMPTS", "10")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "field, value",
    [
        ("generation_effort", "extreme"),
        ("max_result_rows", 0),
        ("query_timeout_seconds", 0),
        ("confidence_warning_threshold", 1.5),
        ("model", "gemini flash"),
        ("demo_db_filename", "../escape.db"),
    ],
)
def test_invalid_values_rejected(make_settings, field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("variable", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
def test_gemini_key_is_secret(isolated_env, variable):
    secret = "AIza" "SyTESTKEY1234567890abcdefghijklmno"
    isolated_env.setenv(variable, secret)
    s = Settings(_env_file=None)
    assert s.has_llm_credentials is True
    assert s.gemini_api_key.get_secret_value() == secret
    assert secret not in repr(s)
    assert secret not in str(s.public_summary()) and s.public_summary()["provider"] == "gemini"


def test_default_model_and_credentials(make_settings):
    assert make_settings(model="   ").effective_model == "gemini-3.8-flash"  # blank = default model
    assert make_settings(model="gemini-3.6-flash").effective_verifier_model == "gemini-3.6-flash"
    assert make_settings().has_llm_credentials is False
    assert make_settings(gemini_api_key="AIza-test").has_llm_credentials is True


def test_extra_allowed_hosts(isolated_env):
    isolated_env.setenv("QB_ALLOWED_HOSTS", "demo.example.com, other.example.com,")
    isolated_env.setenv("RENDER_EXTERNAL_HOSTNAME", "querybridge.onrender.com")
    s = Settings(_env_file=None)
    assert s.extra_allowed_hosts == ["demo.example.com", "other.example.com", "querybridge.onrender.com"]
    assert s.hosted is False and Settings(_env_file=None, hosted=True).public_summary()["hosted"] is True


def test_database_url_is_masked(make_settings):
    s = make_settings(database_url="postgresql+psycopg://reader:TopSecretPw@db.local:5432/sales")
    assert s.database_dialect == "postgresql"
    assert not s.is_demo_database
    assert "TopSecretPw" not in s.masked_database_url
    assert "***" in s.masked_database_url
    assert "TopSecretPw" not in repr(s)
    assert "TopSecretPw" not in str(s.public_summary())
    assert "TopSecretPw" in s.database_url_value()  # only the engine factory sees it


@pytest.mark.parametrize(
    "url",
    ["oracle://scott:TopSecretPw@host/orcl", "mssql+pyodbc://sa:TopSecretPw@host/db", "not a url"],
)
def test_unsupported_or_malformed_url_rejected_without_leaking(make_settings, url):
    with pytest.raises(ValidationError) as excinfo:
        make_settings(database_url=url)
    assert "TopSecretPw" not in str(excinfo.value)


def test_parse_database_url_accepts_supported_dialects():
    assert parse_database_url("sqlite:///x.db").get_backend_name() == "sqlite"
    assert parse_database_url("mysql+pymysql://u:p@h:3306/d").get_backend_name() == "mysql"
    assert parse_database_url("postgresql://u:p@h/d").get_backend_name() == "postgresql"


def test_empty_strings_mean_unset(isolated_env):
    for name in ("QB_REFERENCE_DATE", "QB_DATABASE_URL", "QB_MODEL", "QB_VERIFIER_MODEL", "QB_LOG_FILE",
                 "GEMINI_API_KEY"):
        isolated_env.setenv(name, "")
    s = Settings(_env_file=None)
    assert s.model is None and s.gemini_api_key is None
    assert s.reference_date is None
    assert s.database_url is None
    assert s.verifier_model is None
    assert s.log_file is None


def test_reference_date_and_verifier_model(make_settings):
    s = make_settings(reference_date="2026-11-01", verifier_model="gemini-3.6-flash")
    assert s.effective_reference_date == date(2026, 11, 1)
    assert s.effective_verifier_model == "gemini-3.6-flash"


def test_log_level_is_case_insensitive(make_settings):
    assert make_settings(log_level="debug").log_level == "DEBUG"


def test_relative_paths_resolve_against_project_root(make_settings):
    s = make_settings(data_dir="custom_data", log_file="logs/app.log")
    assert s.data_dir == (PROJECT_ROOT / "custom_data").resolve()
    assert s.demo_db_path == s.data_dir / "querybridge_demo.db"
    assert s.log_file == (PROJECT_ROOT / "logs" / "app.log").resolve()


def test_ensure_directories(make_settings, tmp_path: Path):
    s = make_settings(data_dir=tmp_path / "d", log_file=tmp_path / "l" / "q.log")
    s.ensure_directories()
    assert (tmp_path / "d").is_dir()
    assert (tmp_path / "l").is_dir()


def test_settings_are_immutable(make_settings):
    s = make_settings()
    with pytest.raises(ValidationError):
        s.model = "something-else"


def test_get_settings_cache_and_reload(isolated_env, monkeypatch):
    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    isolated_env.setenv("QB_MODEL", "first-model")
    first = config.get_settings()
    assert config.get_settings() is first
    isolated_env.setenv("QB_MODEL", "second-model")
    assert config.get_settings().model == "first-model"  # still cached
    assert config.reload_settings().model == "second-model"
