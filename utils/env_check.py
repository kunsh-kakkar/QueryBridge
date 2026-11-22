"""
Environment self-check:  python -m utils.env_check

Checks the Python version, compares every pinned package against what is
installed, and prints a secret-free summary of the active configuration.
Exits with status 1 if anything required is missing or misconfigured.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

MIN_PYTHON = (3, 11)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# distribution name -> import name
REQUIRED_PACKAGES: dict[str, str] = {
    "google-genai": "google.genai",
    "pydantic": "pydantic",
    "pydantic-settings": "pydantic_settings",
    "SQLAlchemy": "sqlalchemy",
    "sqlglot": "sqlglot",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "python-multipart": "python_multipart",
    "plotly": "plotly",
    "pandas": "pandas",
}
OPTIONAL_PACKAGES: dict[str, str] = {
    "psycopg": "psycopg",
    "PyMySQL": "pymysql",
}

_PIN = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([^\s#;]+)")


def read_pins(requirements: Path) -> dict[str, str]:
    """Parse ``name==version`` lines from a requirements file (lower-cased names)."""
    pins: dict[str, str] = {}
    if not requirements.exists():
        return pins
    for line in requirements.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            pins[match.group(1).lower()] = match.group(2)
    return pins


def check_package(dist: str, module: str, pinned: str | None) -> tuple[bool, str]:
    """Import ``module`` and compare its installed version with the pin."""
    try:
        importlib.import_module(module)
        installed = metadata.version(dist)
    except (ImportError, metadata.PackageNotFoundError):
        return False, "NOT INSTALLED"
    if pinned and installed != pinned:
        return True, f"{installed}  (pinned {pinned} – version differs)"
    return True, installed


def main() -> int:
    ok = True
    print("QueryBridge environment check")
    print("=" * 60)

    py_ok = sys.version_info[:2] >= MIN_PYTHON
    ok &= py_ok
    print(f"[{'OK' if py_ok else 'FAIL'}] Python {sys.version.split()[0]} "
          f"(requires >= {'.'.join(map(str, MIN_PYTHON))})")

    pins = read_pins(PROJECT_ROOT / "requirements.txt")
    optional_pins = read_pins(PROJECT_ROOT / "requirements-optional.txt")

    print("\nRequired packages")
    for dist, module in REQUIRED_PACKAGES.items():
        found, detail = check_package(dist, module, pins.get(dist.lower()))
        ok &= found
        print(f"  [{'OK' if found else 'FAIL'}] {dist:<18} {detail}")

    print("\nOptional drivers (PostgreSQL / MySQL – experimental)")
    for dist, module in OPTIONAL_PACKAGES.items():
        found, detail = check_package(dist, module, optional_pins.get(dist.lower()))
        print(f"  [{'OK' if found else '--'}] {dist:<18} {detail if found else 'not installed (optional)'}")

    print("\nConfiguration")
    try:
        from pydantic import ValidationError

        from config import get_settings

        settings = get_settings()
    except ValidationError as exc:  # inputs are hidden by hide_input_in_errors
        print(f"  [FAIL] Invalid configuration:\n{exc}")
        return 1

    for key, value in settings.public_summary().items():
        print(f"  {key:<28} {value}")

    from config import API_KEY_VARIABLE, PROVIDER_NAME

    if settings.has_llm_credentials:
        print(f"\n[OK] {PROVIDER_NAME} API key configured – live SQL generation available.")
    else:
        print(f"\n[--] No {API_KEY_VARIABLE} set – offline tests still work; "
              "set the key (free from Google AI Studio) in .env for live use.")

    if settings.is_demo_database:
        exists = settings.demo_db_path.exists()
        print(f"[{'OK' if exists else '--'}] Demo database: "
              f"{'present' if exists else 'not built yet (run: python -m database.init_db)'}")

    print("=" * 60)
    print("RESULT:", "READY" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
