# Provider 路由配置化设计

日期：2026-09-20
状态：待实施
范围：后端配置、Provider 路由、协议白名单、PAC、HTTP/SSE/NDJSON/WebSocket 捕获

## 1. 背景

ContextSpy 当前通过 `contextspy/proxy/addon.py` 中的 `_HOST_PROVIDER` 静态表识别云端 LLM Provider，并通过请求 path 选择协议 Adapter。当前 fork 还把 `aiagent.lakala.com` 直接加入了该静态表。

这种方式能够工作，但存在以下问题：

- 新增或更换公司网关域名必须修改 Python 代码并重新构建。
- `Settings.extra_hosts` 虽然能读取配置，但没有参与 Provider 识别或 PAC 生成。
- Provider Host 列表同时被 Addon 和 PAC 依赖，后续容易形成不一致的数据源。
- 当前 `request()` 会先读取所有经过代理的请求正文，之后才判断是否属于支持的 LLM 流量。
- `provider_override` 容易被误解为协议解析器选择，但它实际上只是反向代理模式下保存的 Provider 标签。

本设计把“哪些 Host 可以采集、采集后归为哪个 Provider”配置化，同时保留现有的 Adapter 协议解析架构。

## 2. 设计目标

- 自定义云端网关只需修改 `~/.contextspy/config.toml` 即可接入。
- Host/Port 只负责识别 Provider，path 继续负责选择协议 Adapter。
- 自定义 Provider 可通过可选白名单限制允许采集的协议。
- Addon 与 PAC 使用同一份生效路由表。
- 未匹配或未授权的流量继续正常转发，但不读取、不保存正文。
- 保持现有官方 Provider、Ollama、本地反向代理及 WebSocket 行为兼容。
- 不引入数据库迁移或前端改动。

## 3. 非目标

第一阶段不包含：

- 前端 Provider 配置界面。
- 配置热加载。
- 非标准 path 到现有 Adapter 的自定义映射。
- 使用 TOML 描述新的 JSON 或流式协议解析规则。
- API Key、Authorization 或证书等凭证管理。
- 历史请求的 Provider 重标记。
- 覆盖或修改官方内置 Provider 路由。

## 4. 概念边界

本设计明确区分以下概念：

- **Provider**：请求发往的服务或网关，也是数据库和筛选界面使用的来源标签，例如 `openai`、`copilot`、`lakala_gateway`。
- **Provider protocol**：请求和响应采用的语义格式，例如 `openai_chat`、`openai_responses`、`anthropic`、`ollama`。
- **Transport**：响应传输方式，例如 JSON、SSE、NDJSON、WebSocket。
- **Agent**：发起请求的客户端，例如 Codex、Claude Code、Copilot 或 OpenAI SDK。

一个 Provider 可以支持多个协议；同一个协议也可以由多个 Provider 提供。

## 5. 用户配置

新增顶层 TOML 数组 `[[provider_routes]]`：

```toml
[[provider_routes]]
host = "aiagent.lakala.com"
provider = "lakala_gateway"
include_subdomains = true
allowed_protocols = [
  "openai_chat",
  "openai_responses",
  "anthropic",
]
```

字段定义：

| 字段 | 必填 | 默认值 | 含义 |
|---|---:|---|---|
| `host` | 是 | 无 | 纯主机名，不包含 scheme、端口、path 或通配符 |
| `provider` | 是 | 无 | 保存到请求记录中的稳定 Provider 标识 |
| `include_subdomains` | 否 | `false` | 是否匹配该 Host 的任意子域名 |
| `allowed_protocols` | 否 | 所有已注册协议 | 允许采集的 Provider protocol 白名单 |

`allowed_protocols` 的有效值来自 Adapter Registry 的 `format_id`。当前值为：

- `anthropic`
- `openai_chat`
- `openai_responses`
- `ollama`

缺失 `allowed_protocols` 表示允许所有已注册协议。显式空数组无实际用途且容易造成误解，因此视为配置错误。

## 6. 配置数据模型

`contextspy/config.py` 新增只负责反序列化的配置对象：

```python
@dataclass(frozen=True)
class ProviderRouteSettings:
    host: str
    provider: str
    include_subdomains: bool = False
    allowed_protocols: tuple[str, ...] | None = None
```

`Settings` 新增：

```python
provider_routes: list[ProviderRouteSettings]
```

`config.py` 只解析 TOML 类型和必填字段。涉及内置路由、已注册 Adapter、冲突和标准化的校验由 Provider Registry 构建阶段完成，以避免配置模块依赖代理或分析模块。

## 7. Provider Registry

新增 `contextspy/proxy/providers.py`，作为 Provider 路由的唯一运行时实现。

### 7.1 运行时路由对象

```python
@dataclass(frozen=True)
class ProviderRoute:
    host: str | None
    provider: str
    include_subdomains: bool
    allowed_protocols: frozenset[str] | None
    port: int | None = None
    source: str = "config"
```

`host=None` 仅用于端口规则或反向代理固定路由。`source` 用于诊断，可取 `builtin`、`config` 或 `reverse_target`。

### 7.2 Registry 接口

```python
class ProviderRegistry:
    def match(self, host: str, port: int) -> ProviderRoute | None: ...

    def is_protocol_allowed(
        self,
        route: ProviderRoute,
        protocol: str,
    ) -> bool: ...

    def pac_routes(self) -> tuple[ProviderRoute, ...]: ...
```

Registry 在启动时构建一次，内部只保存不可变集合，供 FastAPI 和 mitmproxy 线程并发读取。

### 7.3 内置路由

官方内置路由从 `addon.py` 移入 `providers.py`：

- `api.openai.com`
- `openai.azure.com` 及其子域名
- `api.anthropic.com` 及其子域名
- `copilot-proxy.githubusercontent.com`
- `githubcopilot.com` 及其子域名
- `opencode.ai` 及其子域名
- `chatgpt.com`
- 端口 `11434` 的 Ollama 规则

`aiagent.lakala.com` 不作为通用内置路由，改由部署配置声明为 `lakala_gateway`。

### 7.4 匹配顺序

1. 内置端口规则，例如 Ollama `11434`。
2. 精确 Host。
3. `include_subdomains=true` 的路由，按 Host 长度从长到短匹配。
4. 无匹配时返回 `None`。

Host 在构建和匹配时统一转为小写并移除末尾的根域点。用户配置不接受 scheme、端口、path 或 `*`；子域名匹配必须通过 `include_subdomains` 显式开启。

允许父域名规则和更具体的子域名规则同时存在，精确和更长 Host 优先。完全相同的 Host 不能重复。

## 8. 校验与冲突规则

Registry 构建阶段执行以下校验：

- `host` 必须是合法纯主机名。
- `provider` 必须匹配稳定标识格式，例如 `[a-z][a-z0-9_]*`。
- `allowed_protocols` 中每个值都必须对应已注册 Adapter。
- `allowed_protocols=[]` 无效。
- 用户配置中相同 Host 不能重复。
- 用户 Host 不得与内置 Host 完全相同。
- 父域名和更具体子域名可以共存。

错误应包含配置块索引、字段、当前值和修复建议。任何配置错误都应阻止应用启动，不能静默忽略或退回默认值。

第一阶段不提供 `override_builtin`，因为当前需求仅为接入自定义企业网关。需要覆盖官方 Host 时应作为后续独立需求设计。

## 9. 应用启动与依赖传递

启动链路改为：

```text
Settings.load()
    -> build_provider_registry(settings.provider_routes)
    -> app.state.provider_registry
    -> runner.start_proxy(settings, registry, ws_manager)
    -> ContextSpyAddon(provider_registry=registry)
```

`/api/proxy/start` 复用 `app.state.provider_registry`。`/api/proxy.pac` 也从相同的 Registry 生成内容，不再导入 `_HOST_PROVIDER`。

测试或嵌入式调用如果直接调用 `start_proxy()`，必须显式传入 Registry，避免不同入口构建出不一致的路由实例。

## 10. HTTP 请求解析流程

当前 `request()` 会先读取所有经过代理的请求正文。本设计将 Provider 和协议准入判断前移到 `request()`：

```text
Registry.match(host, port)
    -> get_adapter(path)
    -> registry.is_protocol_allowed(route, adapter.format_id)
    -> 通过后才捕获正文
```

通过准入后，把稳定解析结果写入 `flow.metadata`，包括：

- ProviderRoute
- provider 标签
- Adapter 或 provider protocol
- request capture 状态

`responseheaders()`、`response()` 和 `error()` 只复用 metadata，不重复解析 Host 或 path。

各类结果的行为：

| 条件 | 转发 | 捕获正文 | 入库 | 日志 |
|---|---:|---:|---:|---|
| Host 未匹配 | 是 | 否 | 否 | debug |
| Host 匹配但 path 无 Adapter | 是 | 否 | 否 | debug |
| 协议不在白名单 | 是 | 否 | 否 | info/warning |
| Host、协议均允许 | 是 | 是 | 是 | debug |

协议白名单只控制采集和分析，绝不阻止、篡改或重定向真实请求。

## 11. HTTP、SSE、NDJSON 与错误链路

- `responseheaders()` 仅对 metadata 中已准入的请求安装 SSE collector。
- `response()` 对已准入请求继续使用现有 JSON、SSE、NDJSON 和文本识别逻辑。
- `error()` 只记录已在 `request()` 阶段准入的失败调用。
- 已准入请求的 Adapter 在请求阶段确定，后续阶段不因响应状态或 Content-Type 改变 Provider protocol。
- 传输重建和 normalization pipeline 保持不变。

## 12. WebSocket

`WsProtocol` 增加明确的 `provider_protocol` 属性：

```python
class CodexResponsesProtocol:
    protocol_id = "codex_responses"
    provider_protocol = "openai_responses"
```

`websocket_start()` 依次执行：

1. Registry 匹配 Host。
2. WebSocket Registry 匹配 Host 和 path。
3. 用 `provider_protocol` 棳查 ProviderRoute 的协议白名单。
4. 通过后才创建 `WsSession` 和缓存消息。

被拒绝的 WebSocket 连接仍正常转发，但 ContextSpy 不创建状态、不缓存 frame、不保存请求。

## 13. 本地反向代理

将语义模糊的 `provider_override` 改为固定路由：

```python
ContextSpyAddon(
    fixed_route=ProviderRoute(
        host=None,
        provider=target.provider,
        include_subdomains=False,
        allowed_protocols=None,
        source="reverse_target",
    )
)
```

第一阶段不修改 `[[reverse_targets]]` 配置结构，也不增加本地模式协议白名单。固定路由允许所有已注册协议，具体 Adapter 仍由 path 决定。

## 14. PAC 生成

`/api/proxy.pac` 使用 `app.state.provider_registry.pac_routes()`：

- `include_subdomains=false` 只生成精确 Host 条件。
- `include_subdomains=true` 同时生成精确 Host 和 `*.host` 条件。
- 仅端口型路由不进入 PAC。
- 用户自定义 Host 自动进入 PAC。

由于 Host 已经过严格校验，PAC 生成不接受用户提供的任意脚本片段。

## 15. 旧配置迁移

旧字段：

```toml
[intercepted_hosts]
extra_hosts = []
```

当前版本会读取但不会使用该字段。迁移策略为：

- 字段缺失或数组为空：正常启动。
- 数组非空：启动失败，并提示转换为 `[[provider_routes]]`。
- 不自动推断 `provider`，避免错误标签或非预期正文采集。
- 默认配置模板删除误导性说明，改为结构化路由示例。

已有本地配置需要显式加入 `aiagent.lakala.com` 的 ProviderRoute。仓库默认模板仅给出通用注释示例，不把公司内部域名加入官方内置列表。

## 16. 日志与隐私

建议的诊断事件：

- `provider_route_matched`
- `provider_route_not_found`
- `provider_endpoint_not_supported`
- `provider_protocol_not_allowed`

日志可包含 Host、Path、Provider、protocol 和路由来源，但不得包含 Authorization、API Key、Cookie、请求正文或响应正文。

协议不允许时必须在读取正文前返回，而不是读取后丢弃。

## 17. 数据兼容

- 不修改数据库 schema。
- 新请求使用配置的 Provider 标签，例如 `lakala_gateway`。
- 历史 `aiagent.lakala.com` 请求保留原来的 `openai` 标签。
- 不对历史数据执行回填或批量迁移。
- REST API 和前端数据结构保持不变。

## 18. 文件改动范围

新增：

- `contextspy/proxy/providers.py`
- `tests/test_provider_registry.py`

修改：

- `contextspy/config.py`
- `contextspy/proxy/addon.py`
- `contextspy/proxy/runner.py`
- `contextspy/proxy/ws_protocols/base.py`
- `contextspy/proxy/ws_protocols/codex.py`
- `contextspy/api/main.py`
- `contextspy/api/routers/proxy.py`
- `tests/test_providers.py`
- `tests/test_ws_protocols.py`
- `README.md`
- `docs/cloud-mode.md`
- `docs/development.md`
- `SPEC.md`

不修改 UI 和数据库迁移文件。

## 19. 测试策略

### 19.1 Provider Registry

- 官方 Host 与当前行为兼容。
- 自定义 Host 精确匹配。
- 子域名开关有效。
- 精确和最长 Host 优先。
- Ollama 端口规则优先。
- Host 规范化。
- 非法 Host、Provider、协议和空白名单被拒绝。
- 重复用户 Host 和内置 Host 冲突被拒绝。
- `allowed_protocols=None` 允许所有已注册协议。

### 19.2 Addon

- 未知 Host 不捕获请求正文。
- 已知 Host、未知 path 不捕获正文。
- 已知 Host、允许协议正常分析和入库。
- 已知 Host、拒绝协议不捕获、不入库。
- JSON、SSE、NDJSON 和失败请求行为不回归。
- 企业网关请求保存为 `lakala_gateway`。

### 19.3 WebSocket

- 允许 `openai_responses` 时创建 Codex Session。
- 白名单拒绝时不创建状态、不缓存 frame。
- 原有 Codex WebSocket 重建测试继续通过。

### 19.4 PAC

- 官方路由继续存在。
- 自定义路由自动出现。
- `include_subdomains` 控制通配条件。
- 端口路由不进入 PAC。

### 19.5 配置

- 完整、最小和缺失 `provider_routes` 的配置都能加载。
- 非空 `extra_hosts` 给出迁移错误。
- 默认配置模板生成正确示例。

最终运行完整后端测试套件。因为第一阶段不修改 UI，不要求前端测试作为该改造的验收门槛。

## 20. 验收标准

- 新云端网关仅修改 TOML 即可接入。
- `aiagent.lakala.com` 的新请求保存为 `lakala_gateway`。
- 同一网关可以通过标准 path 自动识别多个协议。
- `allowed_protocols` 可选且能阻止未授权协议的正文捕获和入库。
- PAC 自动包含自定义 Host，并遵守子域名配置。
- 官方 Provider、Ollama、本地反向代理和 Codex WebSocket 不回归。
- 无数据库迁移和前端改动。
- 完整后端测试通过。

## 21. 实施顺序

1. 先为 Provider Registry 和配置校验编写失败测试。
2. 实现 `ProviderRoute`、`ProviderRegistry` 和配置解析。
3. 将 Registry 接入应用启动、runner 和 PAC。
4. 将 Addon 改为请求阶段单次准入，并覆盖 HTTP/SSE/NDJSON/error。
5. 为 WebSocket 增加 `provider_protocol` 和白名单检查。
6. 把反向代理从 `provider_override` 改为固定路由。
7. 更新默认配置模板和文档。
8. 运行重点测试和完整后端测试。
