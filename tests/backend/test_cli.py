# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mailintel import cli
from mailintel.settings import AppSettings


def test_python_module_cli_executes_init_db():
    database_url = os.environ.get("MAILINTEL_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MAILINTEL_TEST_DATABASE_URL is required for PostgreSQL CLI integration test")
    env = os.environ.copy()
    env["MAILINTEL_DATABASE_URL"] = database_url

    result = subprocess.run(
        [sys.executable, "-m", "mailintel.cli", "init-db"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Initialized database" in result.stdout
    assert database_url not in result.stdout


def test_serve_paused_sets_startup_environment(monkeypatch):
    calls = {}

    monkeypatch.delenv("MAILINTEL_LLM_PAUSED", raising=False)
    monkeypatch.setattr(cli, "get_settings", lambda: AppSettings(database_url="postgresql://test/mailintel"))
    monkeypatch.setattr(cli, "connect_database", lambda _url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "init_database", lambda _connection: None)
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: calls.update({"args": args, "kwargs": kwargs})),
    )

    assert cli.main(["serve", "--paused"]) == 0
    assert os.environ["MAILINTEL_LLM_PAUSED"] == "1"
    assert calls["args"] == ("mailintel.app:create_app",)
    assert calls["kwargs"]["factory"] is True
