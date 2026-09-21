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

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


_DEFAULT_DIR = Path.home() / ".contextspy"


@dataclass
class ProxySettings:
    port: int = 8888
    bind_addr: str = "127.0.0.1"


@dataclass
class WebSettings:
    port: int = 5173
    bind_addr: str = "127.0.0.1"


@dataclass
class StorageSettings:
    db_path: Path = field(default_factory=lambda: _DEFAULT_DIR / "contextspy.db")


@dataclass
class RetentionSettings:
    """How long raw bodies / block contents are kept before being purged.

    Purge only runs at server startup (not on a background timer) — see
    docs/development.md. 0 means keep forever.
    """
    raw_body_days: int = 7
    block_content_days: int = 7


@dataclass(frozen=True)
class ProviderRouteSettings:
    """One ``[[provider_routes]]`` entry from TOML — config-time only.

    Runtime uses the equivalent ``ProviderRoute`` (frozen, normalized, validated)
    built once at startup by ``contextspy.proxy.providers.build_provider_registry``.
    """

    host: str
    provider: str
    include_subdomains: bool = False
    allowed_protocols: tuple[str, ...] | None = None


@dataclass
class ReverseTarget:
    """A local LLM server to proxy in reverse mode."""
    name: str                   # human label, e.g. "llama-server"
    listen_port: int            # port contextspy listens on, e.g. 8889
    target_url: str             # upstream URL, e.g. "http://127.0.0.1:8080"
    provider: str = "openai"    # stored label; request path selects the adapter


@dataclass
class Settings:
    proxy: ProxySettings = field(default_factory=ProxySettings)
    web: WebSettings = field(default_factory=WebSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    provider_routes: list[ProviderRouteSettings] = field(default_factory=list)
    reverse_targets: list[ReverseTarget] = field(default_factory=list)
    config_dir: Path = field(default_factory=lambda: _DEFAULT_DIR)

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        settings = cls()
        path = config_path or (_DEFAULT_DIR / "config.toml")
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            if "proxy" in data:
                p = data["proxy"]
                settings.proxy.port = p.get("port", settings.proxy.port)
                settings.proxy.bind_addr = p.get("bind_addr", settings.proxy.bind_addr)
            if "web" in data:
                w = data["web"]
                settings.web.port = w.get("port", settings.web.port)
                settings.web.bind_addr = w.get("bind_addr", settings.web.bind_addr)
            if "storage" in data:
                s = data["storage"]
                if "db_path" in s:
                    settings.storage.db_path = Path(s["db_path"]).expanduser()
            if "retention" in data:
                r = data["retention"]
                settings.retention.raw_body_days = r.get("raw_body_days", settings.retention.raw_body_days)
                settings.retention.block_content_days = r.get(
                    "block_content_days", settings.retention.block_content_days
                )
            if "intercepted_hosts" in data:
                legacy = data["intercepted_hosts"]
                if not isinstance(legacy, dict):
                    raise ValueError("[intercepted_hosts] must be a TOML table")
                legacy_hosts = legacy.get("extra_hosts", [])
                if (
                    not isinstance(legacy_hosts, list)
                    or not all(isinstance(host, str) for host in legacy_hosts)
                ):
                    raise ValueError(
                        "[intercepted_hosts].extra_hosts must be an array of strings"
                    )
                if legacy_hosts:
                    raise ValueError(
                        "[intercepted_hosts].extra_hosts is non-empty; convert each "
                        "host to [[provider_routes]] and set an explicit provider value"
                    )
            if "provider_routes" in data:
                route_tables = data["provider_routes"]
                if not isinstance(route_tables, list):
                    raise ValueError("provider_routes must be an array of tables")
                for index, route in enumerate(route_tables):
                    if not isinstance(route, dict):
                        raise ValueError(
                            f"provider_routes[{index}] must be a TOML table"
                        )
                    for required in ("host", "provider"):
                        if required not in route:
                            raise ValueError(
                                f"provider_routes[{index}].{required} is required"
                            )
                        if not isinstance(route[required], str):
                            raise ValueError(
                                f"provider_routes[{index}].{required} must be a string"
                            )
                    include_subdomains = route.get("include_subdomains", False)
                    if not isinstance(include_subdomains, bool):
                        raise ValueError(
                            f"provider_routes[{index}].include_subdomains "
                            "must be a boolean"
                        )
                    raw_protocols = route.get("allowed_protocols")
                    if raw_protocols is not None and (
                        not isinstance(raw_protocols, list)
                        or not all(isinstance(value, str) for value in raw_protocols)
                    ):
                        raise ValueError(
                            f"provider_routes[{index}].allowed_protocols "
                            "must be an array of strings"
                        )
                    settings.provider_routes.append(ProviderRouteSettings(
                        host=route["host"],
                        provider=route["provider"],
                        include_subdomains=include_subdomains,
                        allowed_protocols=(
                            tuple(raw_protocols) if raw_protocols is not None else None
                        ),
                    ))
            if "reverse_targets" in data:
                for rt in data["reverse_targets"]:
                    settings.reverse_targets.append(
                        ReverseTarget(
                            name=rt["name"],
                            listen_port=int(rt["listen_port"]),
                            target_url=rt["target_url"],
                            provider=rt.get("provider", "openai"),
                        )
                    )
        return settings

    def ensure_dirs(self) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.storage.db_path.parent.mkdir(parents=True, exist_ok=True)

    def write_defaults(self) -> None:
        self.ensure_dirs()
        config_path = self.config_dir / "config.toml"
        if not config_path.exists():
            db_path_toml = str(self.storage.db_path).replace("\\", "/")
            config_path.write_text(
                f"""[proxy]
port = {self.proxy.port}
bind_addr = "{self.proxy.bind_addr}"

[web]
port = {self.web.port}
bind_addr = "{self.web.bind_addr}"

[storage]
db_path = "{db_path_toml}"

[retention]
# How many days to keep raw request/response bodies and block contents before
# purging (0 = keep forever). Purge only runs at server startup, not on a
# timer — see docs/development.md if contextspy runs for days without restart.
raw_body_days = {self.retention.raw_body_days}
block_content_days = {self.retention.block_content_days}

# Add a cloud gateway without rebuilding ContextSpy. Host must be a bare
# hostname (no scheme, port, path, or wildcard). Omit allowed_protocols to
# allow every registered protocol; set it to limit the captured wire formats.
# Built-in providers (api.openai.com, api.anthropic.com, chatgpt.com, …) are
# already covered — only add entries for custom / enterprise hosts.
# [[provider_routes]]
# host = "gateway.example.com"
# provider = "enterprise_gateway"
# include_subdomains = false
# allowed_protocols = [
#   "openai_chat",
#   "openai_responses",
#   "anthropic",
# ]

# Uncomment and edit to enable local reverse-proxy mode.
# Each [[reverse_targets]] block defines one local LLM server to intercept.
# [[reverse_targets]]
# name        = "llama-server"   # display label
# listen_port = 8889             # port contextspy listens on
# target_url  = "http://127.0.0.1:8080"  # where your server actually runs
# provider    = "openai"         # stored label; request path selects the adapter
""",
                encoding="utf-8",
            )
