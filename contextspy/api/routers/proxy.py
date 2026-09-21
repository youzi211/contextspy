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

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from contextspy.proxy import runner
from contextspy.proxy.cert import cert_exists, install_cert

router = APIRouter(tags=["proxy"])


@router.get("/proxy/status")
def proxy_status(request: Request) -> dict:
    settings = request.app.state.settings
    return {
        "running": runner.is_running(),
        "port": settings.proxy.port,
        "cert_installed": cert_exists(),
    }


@router.post("/proxy/start")
def proxy_start(request: Request) -> dict:
    if runner.is_running():
        return {"status": "already_running"}
    runner.start_proxy(
        request.app.state.settings,
        request.app.state.provider_registry,
        request.app.state.ws_manager,
    )
    return {"status": "started"}


@router.post("/proxy/stop")
def proxy_stop() -> dict:
    runner.stop_proxy()
    return {"status": "stopped"}


@router.post("/proxy/install-cert")
def proxy_install_cert() -> dict:
    success, message = install_cert()
    return {"success": success, "message": message}


@router.get("/proxy.pac", response_class=PlainTextResponse)
def proxy_pac(request: Request) -> PlainTextResponse:
    settings = request.app.state.settings
    proxy_host_port = f"127.0.0.1:{settings.proxy.port}"

    lines: list[str] = []
    for route in request.app.state.provider_registry.pac_routes():
        exact = f'shExpMatch(host, "{route.host}")'
        if route.include_subdomains:
            condition = (
                f'{exact} || shExpMatch(host, "*.{route.host}")'
            )
        else:
            condition = exact
        lines.append(
            f'    if ({condition}) return "PROXY {proxy_host_port}";'
        )
    body = "\n".join(lines)
    content = (
        f'function FindProxyForURL(url, host) {{\n{body}\n    return "DIRECT";\n}}\n'
    )
    return PlainTextResponse(content, media_type="application/x-ns-proxy-autoconfig")