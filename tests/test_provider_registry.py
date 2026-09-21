# Copyright 2026 Rimantas Zukaitis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

from pathlib import Path

import pytest

from contextspy.config import ProviderRouteSettings, Settings


def route(host: str, provider: str, **kwargs) -> ProviderRouteSettings:
    return ProviderRouteSettings(host=host, provider=provider, **kwargs)


# ---------------------------------------------------------------------------
# Settings: TOML shape + legacy `extra_hosts` rejection
# ---------------------------------------------------------------------------


def test_settings_loads_missing_minimal_and_complete_provider_routes(tmp_path: Path):
    missing = tmp_path / "missing.toml"
    assert Settings.load(missing).provider_routes == []

    minimal = tmp_path / "minimal.toml"
    minimal.write_text(
        '[[provider_routes]]\nhost = "gateway.example.com"\nprovider = "enterprise"\n',
        encoding="utf-8",
    )
    assert Settings.load(minimal).provider_routes == [
        ProviderRouteSettings(host="gateway.example.com", provider="enterprise")
    ]

    complete = tmp_path / "complete.toml"
    complete.write_text(
        """
[[provider_routes]]
host = "ai.example.com"
provider = "enterprise_ai"
include_subdomains = true
allowed_protocols = ["openai_chat", "anthropic"]
""".strip(),
        encoding="utf-8",
    )
    assert Settings.load(complete).provider_routes == [
        ProviderRouteSettings(
            host="ai.example.com",
            provider="enterprise_ai",
            include_subdomains=True,
            allowed_protocols=("openai_chat", "anthropic"),
        )
    ]


def test_settings_rejects_non_empty_legacy_extra_hosts(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[intercepted_hosts]\nextra_hosts = ["gateway.example.com"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"intercepted_hosts.*provider_routes"):
        Settings.load(config)


@pytest.mark.parametrize(
    ("toml_text", "message"),
    [
        ("provider_routes = {}", "provider_routes must be an array of tables"),
        ("[[provider_routes]]\nprovider = 'x'", r"provider_routes\[0\]\.host is required"),
        ("[[provider_routes]]\nhost = 'x.example'", r"provider_routes\[0\]\.provider is required"),
        (
            "[[provider_routes]]\nhost = 'x.example'\nprovider = 'x'\ninclude_subdomains = 'yes'",
            r"provider_routes\[0\]\.include_subdomains must be a boolean",
        ),
        (
            "[intercepted_hosts]\nextra_hosts = 'gateway.example.com'",
            r"intercepted_hosts.*extra_hosts must be an array of strings",
        ),
    ],
)
def test_settings_rejects_invalid_toml_shapes(tmp_path: Path, toml_text: str, message: str):
    config = tmp_path / "config.toml"
    config.write_text(toml_text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        Settings.load(config)


# ---------------------------------------------------------------------------
# ProviderRegistry: built-ins, normalization, precedence, allowlist, diagnostics
# ---------------------------------------------------------------------------


def test_builtin_routes_preserve_provider_labels():
    from contextspy.proxy.providers import build_provider_registry

    registry = build_provider_registry([])
    assert registry.match("api.openai.com", 443).provider == "openai"
    assert registry.match("tenant.openai.azure.com", 443).provider == "openai_azure"
    assert registry.match("api.anthropic.com", 443).provider == "anthropic"
    assert registry.match("api.githubcopilot.com", 443).provider == "copilot"
    assert registry.match("api.opencode.ai", 443).provider == "opencode_zen"
    assert registry.match("chatgpt.com", 443).provider == "openai_chatgpt"
    assert registry.match("localhost", 11434).provider == "ollama"
    assert registry.match("aiagent.lakala.com", 443) is None


def test_match_normalizes_case_and_trailing_root_dot():
    from contextspy.proxy.providers import build_provider_registry

    registry = build_provider_registry([])
    assert registry.match("API.OPENAI.COM.", 443).provider == "openai"


def test_single_label_internal_hostname_is_valid():
    from contextspy.proxy.providers import build_provider_registry

    registry = build_provider_registry([route("gateway", "internal")])
    assert registry.match("GATEWAY.", 443).provider == "internal"


def test_match_precedence_port_exact_then_longest_suffix():
    from contextspy.proxy.providers import build_provider_registry

    registry = build_provider_registry([
        route("example.com", "parent", include_subdomains=True),
        route("ai.example.com", "child", include_subdomains=True),
        route("exact.ai.example.com", "exact"),
        route("localhost", "configured_local"),
    ])
    assert registry.match("deep.ai.example.com", 443).provider == "child"
    assert registry.match("exact.ai.example.com", 443).provider == "exact"
    assert registry.match("localhost", 11434).provider == "ollama"


def test_protocol_allowlist_and_default_all_protocols():
    from contextspy.proxy.providers import build_provider_registry

    registry = build_provider_registry([
        route("all.example.com", "all"),
        route(
            "limited.example.com",
            "limited",
            allowed_protocols=("openai_chat",),
        ),
    ])
    all_route = registry.match("all.example.com", 443)
    limited = registry.match("limited.example.com", 443)
    assert registry.is_protocol_allowed(all_route, "anthropic") is True
    assert registry.is_protocol_allowed(limited, "openai_chat") is True
    assert registry.is_protocol_allowed(limited, "openai_responses") is False


@pytest.mark.parametrize(
    ("settings_route", "field", "value"),
    [
        (route("https://bad.example", "x"), "host", "https://bad.example"),
        (route("bad.example:443", "x"), "host", "bad.example:443"),
        (route("*.bad.example", "x"), "host", "*.bad.example"),
        (route("bad.example/path", "x"), "host", "bad.example/path"),
        (route("bad.example", "OpenAI-Bad"), "provider", "OpenAI-Bad"),
        (
            route("bad.example", "x", allowed_protocols=("unknown",)),
            "allowed_protocols",
            "unknown",
        ),
        (route("bad.example", "x", allowed_protocols=()), "allowed_protocols", "()"),
    ],
)
def test_registry_errors_name_index_field_value_and_fix(
    settings_route: ProviderRouteSettings, field: str, value: str,
):
    from contextspy.proxy.providers import build_provider_registry

    with pytest.raises(ValueError) as exc_info:
        build_provider_registry([settings_route])
    message = str(exc_info.value)
    assert "provider_routes[0]" in message
    assert field in message
    assert value in message
    assert "Use " in message or "Set " in message or "Choose " in message


def test_rejects_duplicate_hosts_after_normalization():
    from contextspy.proxy.providers import build_provider_registry

    with pytest.raises(
        ValueError,
        match=r"provider_routes\[1\].*duplicate.*provider_routes\[0\]",
    ):
        build_provider_registry([
            route("Gateway.Example.com", "one"),
            route("gateway.example.com.", "two"),
        ])


def test_rejects_exact_builtin_host_conflict_but_allows_specific_child():
    from contextspy.proxy.providers import build_provider_registry

    with pytest.raises(
        ValueError,
        match=r"provider_routes\[0\].*api\.openai\.com.*built-in",
    ):
        build_provider_registry([route("API.OPENAI.COM.", "custom")])
    registry = build_provider_registry([route("tenant.chatgpt.com", "tenant")])
    assert registry.match("tenant.chatgpt.com", 443).provider == "tenant"


# ---------------------------------------------------------------------------
# App state, runner plumbing, PAC
# ---------------------------------------------------------------------------


def test_create_app_builds_one_registry_and_exposes_it_on_state(tmp_path: Path):
    from contextspy.api.main import create_app

    settings = Settings(config_dir=tmp_path)
    settings.provider_routes = [route("gateway.example.com", "gateway")]
    app = create_app(settings)
    matched = app.state.provider_registry.match("gateway.example.com", 443)
    assert matched.provider == "gateway"


def test_proxy_start_passes_the_app_registry(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    from contextspy.api.routers.proxy import proxy_start
    from contextspy.proxy.providers import build_provider_registry

    settings = Settings(config_dir=tmp_path)
    registry = build_provider_registry([])
    ws_manager = object()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=settings,
        provider_registry=registry,
        ws_manager=ws_manager,
    )))
    captured: dict = {}
    monkeypatch.setattr("contextspy.api.routers.proxy.runner.is_running", lambda: False)
    monkeypatch.setattr(
        "contextspy.api.routers.proxy.runner.start_proxy",
        lambda actual_settings, actual_registry, actual_ws: captured.update(
            settings=actual_settings, registry=actual_registry, ws=actual_ws,
        ),
    )
    assert proxy_start(request) == {"status": "started"}
    assert captured == {"settings": settings, "registry": registry, "ws": ws_manager}


def test_proxy_pac_uses_registry_and_respects_subdomain_flag(tmp_path: Path):
    from types import SimpleNamespace

    from contextspy.api.routers.proxy import proxy_pac
    from contextspy.proxy.providers import build_provider_registry

    settings = Settings(config_dir=tmp_path)
    settings.proxy.port = 9999
    registry = build_provider_registry([
        route("exact.example.com", "exact"),
        route("tree.example.com", "tree", include_subdomains=True),
    ])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=settings, provider_registry=registry,
    )))
    body = proxy_pac(request).body.decode()
    assert 'shExpMatch(host, "exact.example.com")' in body
    assert 'shExpMatch(host, "*.exact.example.com")' not in body
    assert 'shExpMatch(host, "tree.example.com")' in body
    assert 'shExpMatch(host, "*.tree.example.com")' in body
    assert 'shExpMatch(host, "api.openai.com")' in body
    assert "11434" not in body
    assert 'return "PROXY 127.0.0.1:9999"' in body


# ---------------------------------------------------------------------------
# Default config template (Task 6)
# ---------------------------------------------------------------------------


def test_default_config_documents_provider_routes_and_removes_legacy_section(tmp_path: Path):
    settings = Settings(config_dir=tmp_path)
    settings.storage.db_path = tmp_path / "contextspy.db"
    settings.write_defaults()
    text = (tmp_path / "config.toml").read_text(encoding="utf-8")
    assert "# [[provider_routes]]" in text
    assert '# host = "gateway.example.com"' in text
    assert '# provider = "enterprise_gateway"' in text
    assert "# include_subdomains = false" in text
    assert "# allowed_protocols = [" in text
    assert "[intercepted_hosts]" not in text
    assert "extra_hosts" not in text
    assert Settings.load(tmp_path / "config.toml").provider_routes == []