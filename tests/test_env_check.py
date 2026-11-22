"""Tests for utils/env_check.py."""

from __future__ import annotations

from pathlib import Path

from utils import env_check


def test_read_pins(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# comment\ngoogle-genai==3.6.1\npsycopg[binary]==3.3.9  # driver\n-r other.txt\nloose>=1\n",
        encoding="utf-8",
    )
    assert env_check.read_pins(req) == {"google-genai": "3.6.1", "psycopg": "3.3.9"}


def test_read_pins_missing_file(tmp_path: Path):
    assert env_check.read_pins(tmp_path / "missing.txt") == {}


def test_check_package_reports_missing():
    found, detail = env_check.check_package("definitely-not-installed", "definitely_not_installed_xyz", None)
    assert not found and detail == "NOT INSTALLED"


def test_pinned_requirements_match_installed():
    pins = env_check.read_pins(env_check.PROJECT_ROOT / "requirements.txt")
    for dist, module in env_check.REQUIRED_PACKAGES.items():
        found, detail = env_check.check_package(dist, module, pins.get(dist.lower()))
        assert found, f"{dist} is not installed"
        assert "differs" not in detail, f"{dist}: {detail}"


def test_main_succeeds(isolated_env, monkeypatch, capsys):
    import config

    monkeypatch.setattr(config.Settings, "model_config", {**config.Settings.model_config, "env_file": None})
    assert env_check.main() == 0
    out = capsys.readouterr().out
    assert "RESULT: READY" in out
    assert "No GEMINI_API_KEY set" in out and "free from Google AI Studio" in out
