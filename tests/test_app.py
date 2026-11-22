"""Tests for app.py: the command line used locally and on a host such as Render."""

from __future__ import annotations

import app
from web.server import LOCAL_HOSTS


def test_port_defaults_to_the_port_variable(monkeypatch):
    monkeypatch.setenv("PORT", "10000")
    assert app._parse_args([]).port == 10000
    assert app._parse_args(["--port", "8600"]).port == 8600
    monkeypatch.setenv("PORT", "not-a-number")
    assert app._parse_args([]).port == 8501
    monkeypatch.delenv("PORT")
    assert app._parse_args([]).port == 8501


def test_allowed_hosts():
    assert app.allowed_hosts_for("127.0.0.1", []) == LOCAL_HOSTS
    public = app.allowed_hosts_for("0.0.0.0", ["querybridge.onrender.com", "localhost"])
    assert public[: len(LOCAL_HOSTS)] == LOCAL_HOSTS and public.count("localhost") == 1
    assert "querybridge.onrender.com" in public
    assert "192.168.1.20" in app.allowed_hosts_for("192.168.1.20", [])
