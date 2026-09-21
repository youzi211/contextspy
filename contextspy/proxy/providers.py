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
"""Provider routing registry.

Single source of truth for the ``host + port -> provider`` map and the
``provider + protocol -> allowed`` policy used by every capture boundary:

* forward mitmproxy (``start_proxy``)
* reverse mitmproxy (``start_local_proxies`` — each target gets a fixed route)
* PAC generation (``/proxy.pac``)
* HTTP request admission (``addon.request``)
* WebSocket admission (``addon.websocket_start``)

Adding a new built-in provider means adding one ``ProviderRoute`` to
``_BUILTIN_ROUTES``. Adding a new user-facing wire format (or a new
``format_id``) means adding one adapter — the registry only consumes the
adapter's ``format_id``, not its other internals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from contextspy.analysis.adapters import REGISTRY
from contextspy.config import ProviderRouteSettings

_PROVIDER_RE = re.compile(r"[a-z][a-z0-9_]*\Z")
_HOST_LABEL_RE = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")
_REGISTERED_PROTOCOLS = frozenset(adapter.format_id for adapter in REGISTRY)


@dataclass(frozen=True)
class ProviderRoute:
    """One immutable routing rule. Built once at startup by the registry.

    ``host=None`` and ``port`` set together identifies a fixed route for a
    reverse-proxy listener (the upstream host doesn't identify the provider,
    so we accept all hosts on that port and tag traffic with the route's
    provider).
    """

    host: str | None
    provider: str
    include_subdomains: bool
    allowed_protocols: frozenset[str] | None
    port: int | None = None
    source: str = "config"


# Built-in routes — match the historical suffix table that used to live in
# ``proxy/addon.py``. Order here is irrelevant; the registry sorts by suffix
# length at build time.
_BUILTIN_ROUTES: tuple[ProviderRoute, ...] = (
    ProviderRoute("api.openai.com", "openai", True, None, source="builtin"),
    ProviderRoute("openai.azure.com", "openai_azure", True, None, source="builtin"),
    ProviderRoute("api.anthropic.com", "anthropic", True, None, source="builtin"),
    ProviderRoute(
        "copilot-proxy.githubusercontent.com", "copilot", True, None, source="builtin",
    ),
    ProviderRoute("githubcopilot.com", "copilot", True, None, source="builtin"),
    ProviderRoute("opencode.ai", "opencode_zen", True, None, source="builtin"),
    ProviderRoute("chatgpt.com", "openai_chatgpt", True, None, source="builtin"),
    ProviderRoute(None, "ollama", False, None, port=11434, source="builtin"),
)


class ProviderRegistry:
    """Immutable lookup table for ``host + port`` and protocol allowlists."""

    def __init__(self, routes: tuple[ProviderRoute, ...]) -> None:
        self._routes = routes
        self._ports: Mapping[int, ProviderRoute] = MappingProxyType({
            route.port: route for route in routes if route.port is not None
        })
        self._exact: Mapping[str, ProviderRoute] = MappingProxyType({
            route.host: route for route in routes if route.host is not None
        })
        self._suffix: tuple[ProviderRoute, ...] = tuple(sorted(
            (
                route for route in routes
                if route.host is not None and route.include_subdomains
            ),
            key=lambda route: len(route.host or ""),
            reverse=True,
        ))

    def match(self, host: str, port: int) -> ProviderRoute | None:
        """Return the route matching ``host`` + ``port`` (port rule wins)."""
        port_route = self._ports.get(port)
        if port_route is not None:
            return port_route
        normalized = host.lower().removesuffix(".")
        exact = self._exact.get(normalized)
        if exact is not None:
            return exact
        for route in self._suffix:
            if normalized.endswith("." + (route.host or "")):
                return route
        return None

    def is_protocol_allowed(self, route: ProviderRoute, protocol: str) -> bool:
        """``None`` allowlist = all registered protocols are accepted."""
        return route.allowed_protocols is None or protocol in route.allowed_protocols

    def pac_routes(self) -> tuple[ProviderRoute, ...]:
        """Routes that participate in PAC generation (host-only, no port)."""
        return tuple(route for route in self._routes if route.host is not None)


# ---------------------------------------------------------------------------
# Builder / validation
# ---------------------------------------------------------------------------


def _invalid(index: int, field: str, value: object, fix: str) -> ValueError:
    return ValueError(
        f"provider_routes[{index}].{field}={value!r} is invalid. {fix}"
    )


def _normalize_config_host(raw: str, index: int) -> str:
    if raw != raw.strip():
        raise _invalid(
            index, "host", raw, "Remove leading or trailing whitespace.",
        )
    normalized = raw.lower().removesuffix(".")
    if not normalized or len(normalized) > 253:
        raise _invalid(
            index, "host", raw,
            "Use a non-empty hostname up to 253 characters.",
        )
    labels = normalized.split(".")
    if any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        raise _invalid(
            index, "host", raw,
            "Use a bare hostname without scheme, port, path, wildcard, "
            "or empty labels.",
        )
    return normalized


def build_provider_registry(
    settings_routes: Sequence[ProviderRouteSettings],
) -> ProviderRegistry:
    """Validate ``[[provider_routes]]`` settings and merge with built-ins."""
    builtin_hosts = frozenset(
        route.host for route in _BUILTIN_ROUTES if route.host is not None
    )
    seen: dict[str, int] = {}
    configured: list[ProviderRoute] = []

    for index, settings in enumerate(settings_routes):
        host = _normalize_config_host(settings.host, index)
        if host in seen:
            raise _invalid(
                index, "host", settings.host,
                f"Remove the duplicate of provider_routes[{seen[host]}].host.",
            )
        if host in builtin_hosts:
            raise _invalid(
                index, "host", host,
                "Choose a host that does not exactly duplicate a built-in route.",
            )
        if _PROVIDER_RE.fullmatch(settings.provider) is None:
            raise _invalid(
                index, "provider", settings.provider,
                "Use [a-z][a-z0-9_]*, for example enterprise_gateway.",
            )

        allowed: frozenset[str] | None = None
        if settings.allowed_protocols is not None:
            if not settings.allowed_protocols:
                raise _invalid(
                    index, "allowed_protocols", settings.allowed_protocols,
                    "Set a non-empty list or omit allowed_protocols.",
                )
            unknown = sorted(set(settings.allowed_protocols) - _REGISTERED_PROTOCOLS)
            if unknown:
                raise _invalid(
                    index, "allowed_protocols", unknown,
                    f"Choose only from {sorted(_REGISTERED_PROTOCOLS)}.",
                )
            allowed = frozenset(settings.allowed_protocols)

        seen[host] = index
        configured.append(ProviderRoute(
            host=host,
            provider=settings.provider,
            include_subdomains=settings.include_subdomains,
            allowed_protocols=allowed,
            source="config",
        ))

    return ProviderRegistry((*_BUILTIN_ROUTES, *configured))