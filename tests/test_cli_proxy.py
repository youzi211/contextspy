from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
from typer.testing import CliRunner

import contextspy.cli as cli
from contextspy.config import Settings


def test_runner_overrides_all_proxy_variants(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("all_proxy", "http://127.0.0.1:7890")

    env = cli._base_proxy_env(8888)

    assert env["ALL_PROXY"] == "http://127.0.0.1:8888"
    assert env["all_proxy"] == "http://127.0.0.1:8888"


def test_codex_dotenv_conflict_is_detected(tmp_path: Path):
    (tmp_path / ".env").write_text(
        "HTTPS_PROXY=http://127.0.0.1:7890\nOTHER_SETTING=kept\n",
        encoding="utf-8",
    )

    assert hasattr(cli, "_codex_dotenv_proxy_conflicts")
    assert cli._codex_dotenv_proxy_conflicts(tmp_path, "http://127.0.0.1:8888") == [
        "HTTPS_PROXY"
    ]


def test_proxy_upstream_url_loads_from_config(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[proxy]\nupstream_url = "http://127.0.0.1:7890"\n',
        encoding="utf-8",
    )

    assert Settings.load(config).proxy.upstream_url == "http://127.0.0.1:7890"


def test_run_creates_and_ends_session_when_none_is_active(monkeypatch):
    posts = []

    def fake_get(url, **kwargs):
        if url.endswith("/proxy/status"):
            return httpx.Response(200, json={"running": True, "port": 8888}, request=httpx.Request("GET", url))
        if url.endswith("/sessions"):
            return httpx.Response(200, json={"sessions": []}, request=httpx.Request("GET", url))
        raise AssertionError(url)

    def fake_post(url, **kwargs):
        posts.append(url)
        if url.endswith("/sessions"):
            return httpx.Response(200, json={"session": {"id": "test-session"}}, request=httpx.Request("POST", url))
        return httpx.Response(200, json={"session": {"id": "test-session"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(cli.httpx, "get", fake_get)
    monkeypatch.setattr(cli.httpx, "post", fake_post)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    result = CliRunner().invoke(cli.app, ["run", "echo", "hello"])

    assert result.exit_code == 0
    assert posts == [
        "http://127.0.0.1:5173/api/sessions",
        "http://127.0.0.1:5173/api/sessions/test-session/end",
    ]


def test_start_direct_overrides_configured_upstream(monkeypatch, tmp_path: Path):
    settings = Settings()
    settings.proxy.upstream_url = "http://127.0.0.1:7890"
    settings.config_dir = tmp_path
    settings.storage.db_path = tmp_path / "contextspy.db"
    monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
    monkeypatch.setattr(cli, "_abort_if_migrations_pending", lambda settings: None)
    monkeypatch.setattr("contextspy.proxy.cert.generate_cert", lambda: (True, "already exists"))
    seen = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: seen.append(app.state.settings.proxy.upstream_url))

    result = CliRunner().invoke(cli.app, ["start", "--direct", "--no-browser"])

    assert result.exit_code == 0
    assert seen == [None]
