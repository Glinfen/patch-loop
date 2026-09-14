# PatchLoop Provider Gateway 开发计划

## 1. Problem

**状态：** PGW-01～PGW-06 已完成，下一任务为 **PGW-07**。调查日期：2026-09-08；下文新增能力和验收命令均为开发要求。

**Current Problem：** SRF 已交付 Session、审批、所有权和恢复主线，但 CLI 仍固定创建 DeepSeek Provider，构造器只接受一个模型；请求非流式，Runtime 直接读取供应商配置，Task 未绑定模型配置，协议续接信息也未完整保存。这是 [第二阶段目标](PHASE_2_PROJECT_GOALS.md) S2-G2 的直接缺口。

**Desired Behavior：** 相同 Session/Effect/Approval 流程可接入 DeepSeek、通用 Chat Completions、OpenAI Responses 和本地兼容服务；支持取消与增量展示，只有完整响应进入工具提交；重启沿用原配置和协议历史，不重复副作用、不扩大权限。

**Scope：** 两个协议族（Chat Completions、Responses），显式 Provider profile、能力检查、JSON/SSE 传输、请求级重试/取消、reasoning 续接、用量、持久化和 CLI。复用同步 Runtime、Pydantic、SQLite 与现有工具。

**Non-Goals：** Anthropic Messages、OAuth、自动模型路由/故障切换、活动 Task 中更换 Provider、多模态、供应商托管工具、WebSocket、完整 TUI、自动价格抓取、Skills/Git/Sandbox 功能扩展。Session 的后续新 Task 可以显式选择不同 Provider。

**SRF 交接：** [SRF 计划](SESSION_RUNTIME_FOUNDATION_PLAN.md) 已记录 SRF-00～06 完成，SRF-07 实现及本机质量门禁通过；[总验收报告](../benchmarks/results/srf07_acceptance_summary.json) 仍为 unverified，真实 Docker 尚未验收。2026-09-13 使用 DeepSeek 官方实时模型目录中的 `deepseek-flash` 完成基础请求和工具调用探测；兼容适配后的三次 Session 试用均实际调用模型，其中两次完成审批和新进程恢复，但三次都在固定 20 步预算耗尽且没有产生代码变更。这不阻塞本模块的离线开发，原未验收项继续保留；安全/恢复测试出现回归时必须先修复，不能靠增加 Provider 掩盖问题。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `src/patchloop/providers/base.py::ModelProvider/ModelMessage/ModelResponse` | 只有 name/complete；缺请求身份、能力和续接字段 | 保留 complete 兼容接口，扩展 Gateway 契约 |
| `providers/deepseek.py::DeepSeekProvider` | 已兼容当前 `deepseek-flash` 和历史 `deepseek-v4-flash`；仍为 stream=False，解析器丢弃 reasoning_content | 迁入 Chat Adapter 的显式 DeepSeek 方言并解除静态模型集合限制 |
| `providers/deepseek.py::UrllibJsonTransport/_request_with_retry` | 完整读取 HTTP body；Provider 内部指数重试，最后退化成 RuntimeError | 共用可取消传输；Gateway 独占重试生命周期 |
| `cli.py::_provider_from_env/_session_runtime_service` | 每次从环境重新构造 DeepSeek | 新任务解析并保存配置快照，恢复从 Task binding 构造 |
| `domain.py::TaskExecutionConfig` | 权限、Sandbox、缓存配置，没有 Provider | 增加可空 ProviderBinding，兼容旧数据 |
| `runtime.py::_execute/_compress_epoch` | 两处直接 complete；普通响应以 Effect 批次落库，压缩有独立错误和用量路径 | 两处统一请求生命周期；压缩结果可恢复，不创建伪工具 Step |
| `runtime.py::_provider_model/_provider_thinking` | getattr(provider.config, ...) 读取供应商内部字段 | 改读已冻结的 binding/capabilities |
| `execution/effects.py::persist_model_response_batch/response_from_step` | 完整 ModelResponse 保存到 AgentStep，再产生稳定 Effect | 保持提交边界，拒绝半截流触发工具，保存续接 |
| `persistence.py::prepare_effect_batch/RuntimeCheckpoint` | Runtime schema 当前为 4；已有 pending_model_request_step 和累计步骤 | 增加 request/attempt 存储、原子响应提交与兼容游标 |
| `context/engine.py::ContextEngine` | model_dump 后通用脱敏，按消息组裁剪 | 续接与原 assistant/工具结果成组，不误改 opaque 项 |
| `prompt_cache/usage.py::CacheUsageAccumulator` | 缓存缺失值保留 None，普通用量缺精度信息 | 保留原含义，补 request/attempt 去重与未知费用 |
| `observability.py::TaskMetrics/ProviderUsageRecord` | 从 Trace 汇总模型用量 | 区分逻辑请求、HTTP attempt、用量来源与未知消耗 |
| `tests/unit/test_deepseek_provider.py`、Session/Effect 恢复测试 | 注入 JsonTransport、缓存/重试/参数错误及 SRF 恢复断言 | 复用断言，补真 HTTP/SSE fixture 与跨协议同组契约 |

未写完整前缀的源码路径均位于 `src/patchloop/`。本次实际阅读了上述实现、SRF 验收记录和机器报告；没有把历史测试结果视为本次重新执行的结果。

### Current Flow

```text
CLI → DeepSeekProvider → SessionService → Runtime
→ complete → urllib/内部重试 → ModelResponse
→ prepare_effect_batch → Effect/Approval/ToolGateway
→ checkpoint + Memory/Prompt Cache + Trace
```

## 3. External Research

调查日期：2026-09-08。下面是官方源码/文档中的设计，在线 main 和文档会继续变化；只采用适合当前 Python/SQLite 架构的部分。

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| [Codex：ModelProviderInfo 源码](https://github.com/openai/codex/blob/main/codex-rs/model-provider-info/src/lib.rs) | 显式 wire API、endpoint、凭据环境引用、请求重试及流空闲超时 | 协议与供应商分开，传输配置集中 | 不照搬 Responses-only、OAuth、WebSocket、云平台认证 |
| [Qwen Code：Model Providers](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/model-providers/) | Provider ID 映射协议，模型配置包整体选择，与临时运行配置区分 | profile 原子解析和冻结，不混入旧模型生成参数 | 不照搬可能捕获实际密钥的临时快照、任意多层参数覆盖 |
| [OpenCode：Providers](https://opencode.ai/v2/docs/providers) | Provider ID、协议包、endpoint、模型目录分离，兼容端点可注册 | 一个 Chat Adapter 支持多个 profile，显示名与模型 ID 分开 | 不采用动态 npm/provider 插件和在线大模型目录 |

### Lessons for PatchLoop

- profile 负责“连接谁”，Adapter 负责协议，Gateway 负责一次请求，Runtime 负责持久化/权限；两个 base URL 不算两个独立协议族。
- 续接是协议状态。DeepSeek 工具请求需要回传 reasoning_content；Responses 的无服务端存储路径应保留有序输出项。依据：[DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)、[OpenAI Reasoning](https://developers.openai.com/api/docs/guides/reasoning)。
- 网络 EOF 不等于协议完成；Responses 有明确完成/失败事件。传输采用可显式关闭的异步流。依据：[Responses Streaming](https://developers.openai.com/api/docs/guides/streaming-responses)、[HTTPX Async](https://www.python-httpx.org/async/)。
- Responses 函数工具本轮显式 strict=false，保持当前可选参数语义；JSON Schema 最终输出是另一项显式请求能力。依据：[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)。

## 4. Design

### Selected Approach

扩展现有 providers 包。Gateway 同步入口内部以 `asyncio.run` 管理 HTTPX AsyncClient、读取任务及控制监督；不将 SessionService 改成 async，不增加常驻服务或另一套 Agent 框架。

### Key Decisions

| 决策 | 固定方案 |
| --- | --- |
| 协议 | protocol=chat_completions/responses；Chat dialect=standard/deepseek，显式声明，不通过 URL 猜测 |
| 配置来源 | 内置 DeepSeek + `~/.patchloop/providers.toml`；`--provider-config` 显式替换文件。首版不自动读取项目内 Provider 配置 |
| 解析优先级 | 新 Task 的 CLI profile > 文件 default > 内置；model 只能选择该 profile 已配置模型。凭据为指定环境变量 > 显式 env 文件；LLM_* 别名只用于旧 DeepSeek 路径 |
| Task 绑定 | TaskExecutionConfig.provider 保存非秘密配置、能力、价格版本、endpoint、fingerprint，凭据只存引用。恢复不重读当前默认 profile |
| 切换 | 只在新 Task 选择不同 Provider；活动 Task resume 不接受模型/协议/端点覆盖。同名凭据引用可轮换密钥，不改变 binding |
| 请求身份 | request ID 由 Task/purpose/Step index/epoch generation/input revision 稳定构造；purpose=agent_step/epoch_compression。每次 HTTP 尝试独立 attempt ID，不用作 Effect 去重键；不同输入不复用已完成请求 |
| 工具 | 仅客户端函数工具。delta 仅展示，完整响应落库后才调用原 prepare_effect_batch；能力不授予权限 |
| 传输 | `httpx>=0.28,<1`，验证 TLS，禁止自动重定向，trust_env=False；代理/CA 显式配置。HTTP 只允许 loopback；URL 禁止 userinfo/query 密钥 |
| 超时 | 默认 connect 10s、write 30s、idle 60s、含重试总期限 120s，受 Task 剩余执行预算限制。控制轮询 100ms；取消须关闭响应，关闭失败阻止后续请求 |
| 重试 | 最多额外 2 次；只重试连接建立失败及完整 429/502/503/504 错误响应。Retry-After 秒/日期上限 30s且受 deadline 限制；可能送达后的断流/read timeout、部分输出、解析错误、取消不自动重试 |
| 续接 | Message/Response 增加 ProviderContinuation；DeepSeek reasoning 文本，Responses 白名单有序 items。Responses store=false，不依赖 previous_response_id |
| 上下文 | assistant、续接及工具结果不可拆组；完整组结算后可压缩。新 Task 继承可见 Turn，不复制前 Task 供应商续接 |
| 用量 | 旧数值字段兼容；新完整性/费用状态区分未知。价格由 profile 显式配置，本地零价也要显式声明，不自动拉取现价 |

request 状态为 pending → completed/failed/cancelled/interrupted；completed 只表示完整模型响应落库。未结算 attempt 在恢复时记为 interrupted。Provider 中断与工具 Effect unknown 分开：是否重新请求先检查已提交响应/Effect，不能借 Provider 重试重新执行工具。

### Target Flow

```text
ProfileResolver → Task ProviderBinding → ProviderFactory
→ Runtime 创建并保存 request → Gateway 检查能力
→ Adapter 编码 → Transport JSON/SSE → 聚合/取消/有限重试
→ 完整响应 → 原子保存 request + Step/Effects（或压缩响应）
→ Runtime 用量/上下文 → 原 Policy/Approval/ToolGateway
```

## 5. Change Map

源码路径以下均相对 `src/patchloop/`；标记新增的文件在实施时创建。

| File | Symbol | Change |
| --- | --- | --- |
| `providers/contracts.py`（新增） | Binding/Capabilities/Continuation/Error | 无 domain/Store 依赖的基础类型 |
| `providers/base.py` | Message/Response/Usage/Request/Event | 扩展已有消息契约，请求类型定义在消息模型之后，避免循环导入 |
| `providers/config.py`、`factory.py`（新增） | Resolver/Factory | 解析配置、能力/端点校验、构造实例 |
| `providers/transport.py`、`sse.py`（新增） | HttpxTransport/SSEDecoder | 可取消 HTTP、增量帧解码 |
| `providers/gateway.py`（新增） | Gateway/LegacyProviderAdapter | 生命周期、重试、旧 complete 兼容 |
| `providers/chat.py`、`responses.py`（新增） | Adapter/StreamReducer | 无 Store/工具依赖的协议编解码 |
| `providers/deepseek.py`、`__init__.py` | DeepSeekProvider/Config | 保留公开门面，委托新实现 |
| `providers/continuation.py`、`usage.py`（新增） | Codec/UsageNormalizer | 协议状态存储/日志投影、用量映射 |
| `domain.py`、`persistence_contracts.py`、`persistence.py` | TaskExecutionConfig/Store/Checkpoint | binding、request/attempt、原子提交/迁移 |
| `runtime.py`、`execution/effects.py` | _execute/_compress_epoch/response_from_step | Gateway、控制、完整响应和用量回放 |
| `context/engine.py`、`prompt_cache/diagnostics.py`、`prompt_cache/usage.py` | 消息组/摘要/计数 | 续接保护与精度兼容 |
| `cli.py`、`observability.py` | profile/状态/事件/metrics | 显式选择、错误、流式展示及用量来源 |
| `evaluation/provider.py`（新增） | ProviderAcceptanceRunner | 协议/本地/真实试用机器报告 |
| 仓库根 `pyproject.toml`、`uv.lock`、`.env.example`、`README.md` | 依赖和使用说明 | HTTPX、配置示例、兼容行为；保留用户既有 lock 修改 |
| `tests/unit/test_provider_*.py`、`tests/integration/test_provider_*.py`、`tests/e2e/test_provider_session.py`（新增） | 契约与边界测试 | 默认离线，真实服务显式触发 |

## 6. Implementation Tasks

### PGW-01：请求、能力与兼容模型

**实施状态：已完成（2026-09-08）。** 契约、兼容序列化、循环依赖隔离和定向/全仓门禁已通过。

**Goal**

固定两个协议共用的类型和端口，后续任务无需重新决定对象边界。

**Files / Symbols**

```text
src/patchloop/providers/contracts.py（新增）：Binding/Capabilities/Continuation/Error
src/patchloop/providers/base.py：Message/Response/Usage/ProviderRequest/ProviderEvent
src/patchloop/providers/__init__.py：公共导出
tests/unit/test_provider_contracts.py（新增）
```

**Implementation**

1. contracts 只定义无 domain 依赖的类型。不可变 Binding 含 schema_version=1、profile_id、protocol、dialect、model、base_url、auth（bearer/none）、credential_env、capabilities、generation、transport、pricing、fingerprint。fingerprint 覆盖影响线上请求的非秘密配置，不含密钥值。
2. Capabilities 固定 tools、multiple_tool_calls、streaming、reasoning_transport（none/deepseek_text/responses_items）、structured_output、context_window_tokens、max_output_tokens、usage_supported、cache_usage_supported；自定义模型显式声明，不靠名称推测。
3. base 中在现有消息模型之后定义 ProviderRequest：request_id/task_id/step_index/purpose/epoch_generation/input_revision/messages/tools/output_schema/max_output_tokens；保持依赖 `contracts → 无状态类型`、`base → contracts/domain`，domain 只导入 contracts.Binding。
4. ModelMessage/Response 增加可空 continuation，Response 增加 request_id/finish_reason；ModelUsage 增加 input/output 是否 reported、cost_status、pricing_version。旧字段默认值可读，旧 FakeProvider 不要求立即改接口。
5. Event 固定 request_started/attempt_started/text_delta/reasoning_delta/tool_call_delta/usage/response_completed/request_failed/request_cancelled，带 request/attempt ID 和序号；只有 completed 携带完整响应。Control 回调返回 pause/cancel/lease_lost/None。
6. ProviderError 含 kind、safe_message、http_status、retry_after、request_sent、partial_output、retryable、usage_unknown；kind 包括配置/认证/限流/连接/超时/协议/截断/取消/能力错误，禁止原 headers/body。

**Interface Changes**

```text
Gateway.complete_request(request, *, control=None, on_event=None) -> ModelResponse
Adapter.encode(request, binding) -> EncodedRequest
Adapter.parse_json(body, binding) -> ModelResponse
Adapter.new_reducer(binding) -> StreamReducer
StreamReducer.feed(frame) -> list[ProviderEvent]; finish() -> ModelResponse
```

Protocol 定义在 base 的模型之后，旧 ModelProvider.complete(messages, tools) 保留；具体 HTTPX 类型不进入公开端口。

**Tests**

- 旧消息/响应/Task fixture；fingerprint 稳定、配置变化、秘密不进 dump/repr。
- 非法能力组合、超限字段、未知事件；请求/响应往返序列化。

**Acceptance Criteria**

- 无 providers/domain 循环依赖；两个 Adapter 能仅依赖公开契约。
- 未切换生产请求路径，现有 Fake/DeepSeek 单测与 mypy 通过。

### PGW-02：配置解析与 ProviderFactory

**实施状态：已完成（2026-09-08）。** Profile/Credential 解析、不可变 Task binding、安全 URL 校验和 fail-closed Factory 已完成；生产 Adapter 按计划由 PGW-04～06 注册。

**Goal**

为显式供应商/模型选择生成唯一可持久化的 Binding。

**Files / Symbols**

```text
src/patchloop/providers/config.py、factory.py（新增）：ProfileResolver/CredentialResolver/Factory
src/patchloop/providers/deepseek.py：DeepSeekConfig.from_env
src/patchloop/domain.py：TaskExecutionConfig.provider
tests/unit/test_provider_config.py（新增）
```

**Implementation**

1. tomllib 读取 schema_version=1、default_profile、profiles.<id>；profile 含 protocol/dialect/base_url/credential_env 和 models.<id> 的能力、生成参数、价格。未知 profile/model 返回可用 ID，不发网络。
2. 按第 4 节优先级整体选择 profile；--model 仅选该 profile 已定义项，不把其他 Provider 的 temperature/thinking 混入。新自定义模型先在配置文件声明能力，不提供任意 extra_body 注入。
3. base_url 只去尾斜杠，再追加 /chat/completions 或 /responses；不自动补删 /v1。拒绝 userinfo、query、fragment 和非 loopback HTTP；URL、代理/CA 配置在发送凭据前校验。
4. 凭据只在运行期解析为 SecretStr；内置 DeepSeek 保留 DEEPSEEK_*/LLM_* 与显式 env_file，通用/OpenAI profile 只读各自 credential_env。本地 auth=none 必须显式，不用假 API Key。
5. TaskExecutionConfig.provider 默认 None。新 Task 创建时保存解析结果；旧 None 的首次绑定由 PGW-08 在取得 guard 后执行。Factory 注册三条 protocol/dialect 路径，未实现的 Adapter 明确不支持，不回落 DeepSeek。

配置字段固定为：profile 增加 `auth`、`default_model`、`transport`；每个 models 项包含 `capabilities`、`generation`、`pricing`。generation 只允许 `max_output_tokens`、可空 `temperature`、`reasoning_enabled`、可空 `reasoning_effort`，Chat 另有 `token_limit_field=max_tokens/max_completion_tokens`（默认 max_tokens）；不支持字段拒绝，不能静默丢弃。pricing 包含 `version/input_per_million/output_per_million/cached_input_per_million`，最后一项可空。内置 DeepSeek 默认模型使用官方实时目录在 2026-09-13 返回的 `deepseek-flash`；模型/生成参数沿用现有配置，上下文按当前 TaskBudget 的 32000 保守声明、输出默认 16384；这些是产品默认限制，不宣称服务最大规格。历史报告中的旧模型 ID 不追溯改写。

**Interface Changes**

```text
ProfileResolver.resolve(profile_id=None, model=None, config_path=None, env_file=None) -> Binding
CredentialResolver.resolve(binding) -> SecretStr | None
ProviderFactory.create(binding, transport=None) -> Gateway
TaskExecutionConfig.provider: ProviderBinding | None = None
```

**Tests**

- 来源优先级、profile 原子性、旧别名、跨供应商凭据隔离和本地无认证。
- 根 URL、带 /v1 URL；恶意 URL/重定向不发送秘密。
- 环境默认值改变不影响已有 binding，同名密钥轮换不改变 fingerprint。

**Acceptance Criteria**

- 解析无网络 I/O，输出无秘密，不隐式读取项目 Provider 配置。
- 提供 DeepSeek、Responses、local 测试配置；能力/价格显式，配置存在不算真实连通验收。

### PGW-03：可取消传输与 SSE 解码

**实施状态：已完成（2026-09-14）。** 可取消 HTTPX 传输、分帧限额 SSE 解码、本地可控 HTTP fixture 和对应测试已实现；13 项 PGW-03 测试、Provider 定向测试、mypy 与 lint 通过。PGW-03 当时的全仓测试记录为 613 通过、3 项既有 Session CLI 失败、1 项因 Windows 符号链接不可用跳过；PGW-04 完成后的全仓回归中，先前 3 项 CLI 失败未复现，647 通过、1 项因同一符号链接限制跳过。PGW-03 当时的全仓格式检查报告 9 个未改动文件需要格式化。

**Goal**

提供协议无关、资源有界的 JSON/SSE I/O。

**Files / Symbols**

```text
src/patchloop/providers/transport.py、sse.py（新增）：HttpxTransport/SSEDecoder
pyproject.toml、uv.lock
tests/support/provider_http_server.py（新增）
tests/unit/test_provider_sse.py、tests/integration/test_provider_transport.py（新增）
```

**Implementation**

1. 加入 `httpx>=0.28,<1` 和用于显式结构化输出校验的 `jsonschema>=4.23,<5`，更新依赖锁。一次 complete_request 生命周期内创建 AsyncClient，重试复用、finally 关闭；底层 retries=0。open 返回 status、受限 headers 与 async bytes iterator，不持有 Store。
2. 分开 connect/write/read idle/总 deadline。读取与控制监督在同一 event loop，100ms 检查控制和总期限；取消任务后等待 response.aclose。关闭失败映射 transport_cleanup_failed，不继续发下一请求。
3. 同步入口后续使用 asyncio.run；已有 event loop 中调用同步入口时报明确用法错误，不偷偷创建常驻线程。异步代码不调用阻塞 sleep，时钟可注入。
4. SSEDecoder 处理 UTF-8 分字节、CRLF/LF、注释心跳、多行 data、event/id 字段；空行结算一帧，[DONE] 留给协议 reducer 判断，网络 EOF 不宣告成功。
5. 默认单帧 1 MiB、累计响应 16 MiB，可信 profile 可配置；超限关闭流并报 response_too_large。Fake server 使用临时 loopback 端口，支持 headers 前阻塞、分块、断流、重定向和连接关闭观测。

**Interface Changes**

```text
AsyncTransport.open(encoded, timeouts) -> AsyncContextManager[TransportResponse]
TransportResponse: status_code, headers, aiter_bytes()
SSEDecoder.feed(bytes) -> list[SSEFrame]; finish() -> list[SSEFrame]
SSEFrame: event, data, id
```

**Tests**

- 中文/emoji/CRLF 分块、多行帧、心跳、超限、EOF 残帧。
- headers 前/流中取消，server 确认断开；所有错误路径关闭 client/response。
- TLS、代理、重定向及连续请求不泄漏连接。

**Acceptance Criteria**

- 受控本地环境中，Control 返回取消后 2 秒内退出请求并关闭连接；不把 SQLite 锁等待计入网络关闭断言。
- 传输无工具/Store/协议专属依赖，无原始响应或 Authorization 日志。

### PGW-04：Gateway 生命周期、能力检查与重试

**实施状态：已完成（2026-09-14）。** 新增 ProviderGateway 与 LegacyProviderAdapter，统一能力预检、JSON/SSE 完整响应校验、请求级重试/取消、全局 deadline 和生命周期事件；StreamReducer 契约明确要求拒绝未终止或未结算的流。Provider 定向回归 78 通过，mypy 与改动文件 Ruff/格式检查通过；全仓回归 647 通过、1 项因 Windows 符号链接不可用跳过。ProviderFactory 的 Chat/DeepSeek 路由由 PGW-05 注册，Responses 路由继续 fail-closed，等待 PGW-06。

**Goal**

统一请求执行过程，仅将完整协议结果交回 Runtime。

**Files / Symbols**

```text
src/patchloop/providers/gateway.py（新增）：ProviderGateway/LegacyProviderAdapter
src/patchloop/providers/base.py：Adapter/StreamReducer Protocol
tests/unit/test_provider_gateway.py（新增）
```

**Implementation**

1. 请求前检查 tools/streaming/reasoning/structured_output 和模型限制。必需工具不支持时本地报错，禁止删掉 tools 后降级；streaming=false 使用 JSON 路径，仍发最终事件。本轮 output_schema 与非空 tools 互斥，Schema 先用 Draft202012Validator.check_schema 校验；完整响应后解析 content JSON 并本地验证，失败报 structured_output_invalid，不返回伪成功。
2. 按 Content-Type 使用 JSON parser 或 SSE reducer；终止成功、工具调用 ID 唯一、片段已结算才返回 ModelResponse。多次完成事件、未知输出结构或不支持工具类型返回协议错误。
3. 实现第 4 节重试白名单；默认指数等待 1s/2s，加可注入小幅 jitter，Retry-After 优先。每次 attempt 独立 ID 和 accumulator，取消优先于重试，不把失败尝试的文本/参数拼到下一次。
4. 区分完整响应中的非法工具 JSON与截断协议：前者保留 ToolCall.arguments_error，由现有工具反馈模型；缺 ID、重复 ID、未结算参数或无终止帧不得构造可执行调用。finish=length 不因已有文字而算成功。
5. on_event 接收临时 delta 和生命周期；Gateway 本身不写数据库。回调失败必须关闭流并返回 observer_error，不能跳过错误继续执行。全局 deadline 包含重试与 backoff。
6. LegacyProviderAdapter 包装旧 ModelProvider/FakeProvider，能力标记不可即时取消；仅用于旧注入兼容。生产 Factory 必须创建新 Gateway，不在两个层级同时重试。

**Interface Changes**

```text
ProviderGateway(binding, adapter, transport, clock, random_source)
  complete_request(request, *, control=None, on_event=None)
  complete(messages, tools)  # 兼容入口，创建临时请求 ID
LegacyProviderAdapter(provider).complete_request(...)
```

**Tests**

- 429 Retry-After 秒/日期、502 后成功、401 不重试、请求送达后断流不自动重试、总超时。
- 能力失败时网络计数为 0；backoff 期间取消后 attempt 不增加。
- 缺 ID/重复 ID/错误参数、重复终止、空响应和回调失败的关闭行为。

**Acceptance Criteria**

- 重试只有一处，每个 logical request 至多返回一个成功 ModelResponse。
- 失败/取消/半截流测试不会返回可供 Effect 提交的部分结果。

### PGW-05：Chat Completions 与 DeepSeek 适配

**Goal**

一个 Adapter 接入标准兼容服务，并替换 DeepSeek 单模型实现。

**Files / Symbols**

```text
src/patchloop/providers/chat.py（新增）：ChatCompletionsAdapter/ChatStreamReducer
src/patchloop/providers/deepseek.py：DeepSeekProvider/DeepSeekConfig/JsonTransport 兼容门面
tests/unit/test_deepseek_provider.py、tests/unit/test_provider_chat.py（新增）
tests/fixtures/providers/chat/（新增）
```

**Implementation**

1. 复用现有 assistant.tool_calls、tool.tool_call_id、函数 Schema 和 permission 描述映射；工具列表为空时省略 tools/tool_choice。Adapter 不修改传入消息和 Schema 对象。
2. standard 方言仅发送 profile 支持的生成字段，默认无 thinking/reasoning_effort；DeepSeek 方言发送显式 enabled/disabled 和已配置 effort。按 token_limit_field 映射输出限制。声明支持结构化输出时将 output_schema 映射为 response_format.json_schema（strict=true），否则 Gateway 本地拒绝；Schema 使用远端支持的子集，服务拒绝不得自动改写 Schema。移除模型必须等于 flash 的限制，模型是否受服务支持由显式配置与响应确认。
3. JSON 解析 content、tool_calls、finish_reason、usage、reasoning_content；stop/tool_calls 成功，length/content_filter/资源中断分别映射截断/拒绝/服务错误。ModelResponse 中非空 content 不是成功依据。
4. SSE 按 choice index=0 和 tool call index 聚合，ID/名称片段一致校验，arguments 结算后解析；允许 choices=[] 的 usage-only 尾帧。验证 finish_reason 和 [DONE]，缺用量标 unknown，缺终止标 truncated_response。
5. DeepSeek reasoning_content 放入 continuation，随原 assistant 回传，包括工具请求历史中没有 tool call 的 assistant。standard 不发送 DeepSeek 字段；旧历史缺必需续接时返回 continuation_unavailable，不补造 reasoning。
6. 保留 DeepSeekProvider/Config.from_env、name、旧 JsonTransport/sleeper 注入和 ProviderRequestError 导入。旧同步 JsonTransport 注入走显式 LegacyTransportBridge，只用于兼容测试且不承诺即时取消；默认生产路径委托新 Gateway，删除原第二层重试。
7. 将“拒绝非 flash”测试改为“接受显式模型配置”，这是有意行为变化。其他请求布局、工具语义和缓存字段保持兼容；新增 reasoning 引起的请求差异单独测试，不修改旧纯布局基线来掩盖回归。

**Interface Changes**

```text
ChatCompletionsAdapter(dialect='standard' | 'deepseek')
ProviderContinuation.deepseek_reasoning_content: str | None
DeepSeekProvider.complete(...) -> Gateway 的完整响应
```

**Tests**

- 原 DeepSeek 消息/缓存/脱敏/重试断言，standard 不出现 DeepSeek 字段。
- 两轮工具与最终消息的 reasoning 回传；JSON/SSE 归一化结果相同。
- 多 call 交错 delta、usage-only 帧、finish=length、缺 ID、坏 arguments、缺 [DONE]。

**Acceptance Criteria**

- 标准 loopback 服务可用自定义模型完成读取→工具反馈→回答，无 DeepSeek 凭据依赖。
- 默认 DeepSeek 已迁入同一 Gateway，续接保留有多轮证据，不只更换 base URL。

**实施状态：已完成（2026-09-14）。** 新增标准/DeepSeek Chat Completions Adapter 与 SSE reducer，Factory 已注册这两种方言；JSON 与 SSE 汇总结果、交错工具片段、usage-only 尾帧、完整终止、拒绝/截断/服务错误和 DeepSeek reasoning continuation 均有测试。DeepSeek 默认路径已委托可取消 HTTPX Gateway，旧同步 JsonTransport 与 sleeper 通过仅用于兼容测试的桥接保留，重试统一归 Gateway；自定义模型名不再受静态白名单限制。全仓 pytest **660 通过、1 跳过**（Windows 主机不支持符号链接）；mypy、Ruff 和改动文件格式检查通过。未调用真实外部模型服务。

### PGW-06：OpenAI Responses Adapter

**Goal**

用第二个独立协议族验证 Gateway 和相同工具契约。

**Files / Symbols**

```text
src/patchloop/providers/responses.py（新增）：ResponsesAdapter/ResponsesStreamReducer
tests/unit/test_provider_responses.py（新增）
tests/fixtures/providers/responses/（新增）
```

**Implementation**

1. 编码 /responses；system/developer/user/assistant 文本保留角色，工具调用映射 function_call、结果映射 function_call_output，使用 call_id 配对，不能误用 output item id。
2. 函数定义使用扁平 name/description/parameters，strict=false；multiple_tool_calls=false 时发送 parallel_tool_calls=false。本地工具执行仍由 SRF 串行处理，协议支持并行不改变执行权限。
3. 设置 store=false，不使用 previous_response_id。reasoning/output token 字段按 profile 白名单映射；显式 output_schema 经 text.format 请求 JSON Schema（strict=true），由 PGW-04 统一本地验证；采用 API 支持的 Schema 子集，服务拒绝不静默改写。它与函数工具 strict=false 是两种不同用途，不能混淆。
4. 解析有序 message/function_call/reasoning 输出项，可见 content/tool_calls 与原生续接同时保存。带续接的历史 assistant 只回放已校验原生项，不重复编码规范化正文/工具调用。仅允许客户端函数工具，拒绝托管联网/代码执行等类型。
5. reducer 支持 response.created、output_item.added/done、output_text.delta/done、function_call_arguments.delta/done、reasoning 相关项、completed/failed/incomplete。按 output_index/item_id 定位，增量与完成快照交叉校验；已知进度事件可忽略，未知 output item 报 unsupported_output。
6. 仅 response.completed 且最终 status=completed 返回完整结果；error/failed/incomplete/EOF 不提交工具。完整但坏参数沿用 arguments_error；refusal 转结构化拒绝原因，不能当“任务已完成”。

**Interface Changes**

```text
ProviderContinuation.responses_items: tuple[ValidatedResponseItem, ...]
ValidatedResponseItem: message | function_call | reasoning（白名单字段模型）
Responses call_id ↔ ToolCall.id；response.id 仅诊断，不代替 Effect ID
```

**Tests**

- 相同 ToolSpec 的 Chat/Responses wire 差异和结果配对；可选参数不被强制改写。
- 多 call、delta/最终快照、encrypted reasoning、store=false 续接。
- incomplete、refusal、未知托管工具、缺完成事件、结构化输出不符合 Schema。

**Acceptance Criteria**

- 与 Chat 共用规范化工具/错误契约测试，无 Chat 代理冒充 Responses。
- 不以服务端 response ID 或进程内缓存作为恢复前提。

**实施状态：已完成（2026-09-14）。** 新增独立 Responses Adapter/reducer 并注册 Factory 路由，按 call_id 配对函数调用与工具结果；store=false 下保存并重放通过白名单校验的 message/function_call/reasoning 项，encrypted reasoning 内容原样保留。JSON 与 SSE 共用规范化响应和错误契约，流结束必须收到 response.completed 且状态为 completed，增量与完成快照交叉校验；Gateway 继续负责工具权限、能力检查及结构化输出本地 Schema 校验。Responses 专属 fixture 与测试覆盖 Chat/Responses wire 差异、多个工具调用、续接、拒绝/截断/托管工具输出和结构化输出错误。全仓 pytest **676 通过、1 跳过**（Windows 主机不支持符号链接）；mypy、Ruff 全仓检查和改动文件格式检查通过。未调用真实外部模型服务。

### PGW-07：请求、Attempt 与续接持久化

**Goal**

跨进程区分完整响应、失败尝试和未知远端用量，保存可重放协议状态。

**Files / Symbols**

```text
src/patchloop/providers/continuation.py（新增）：ContinuationCodec
src/patchloop/persistence_contracts.py、persistence.py：ProviderRequestStore/Checkpoint/prepare_effect_batch
src/patchloop/context/engine.py、prompt_cache/diagnostics.py
tests/integration/test_provider_persistence.py（新增）
```

**Implementation**

1. 在当前 Runtime schema 4 后增加下一迁移版本，新增 provider_requests/provider_attempts。request 唯一 task/purpose/step/epoch generation/input revision，保存 binding fingerprint、输入摘要、状态、完整响应引用和 usage；attempt 保存 execution_id、序号、时间、错误/usage 状态及预算预留。外键级联，所有执行写入复用 lease/version guard。
2. 扩展 prepare_effect_batch(provider_request_id=None)，同事务完成 request 并写原 Step/Effects；重复相同提交幂等、响应不同报冲突。压缩通过 commit_provider_response 保存响应，不建伪 Step/Effect。没有 tool call 的普通响应也必须完成 request。
3. 实现 begin_request/begin_attempt/finish_attempt/get_request；journal 与状态同事务。保存完整响应时一并完成成功 attempt，失败/取消 attempt 独立落库；begin 前不能发送 HTTP。失去 lease 后禁止旧 owner 补写成功。
4. checkpoint 增加 pending request ID、accounted request/attempt IDs，保留旧 SRF 字段兼容读取。恢复先查完整响应；无终态 attempt 标 interrupted，新尝试创建新 attempt ID。请求不变但输入 revision 已变化时旧 pending request 作废，生成带新输入 revision 的逻辑 ID，不能与旧输入混用。
5. Codec 区分 storage/public/replay 投影：DeepSeek reasoning 文本继续脱敏，若发生改写则 replayable=false，后续需要它时明确阻塞；Responses 只保留白名单项，校验后的 encrypted_content 原字节存储，公开日志仅类型/长度/摘要。不能豁免整个原响应 body 的脱敏。
6. 对 ContextEngine 和 persistence 中新字段的通用 dump/redact 路径使用同一 Codec；assistant/续接/工具结果成组保留，不能截断 opaque 项。文本 reasoning 按现有 token 估算计入，opaque 字节按保守大小计入预算；超限在已结算组边界压缩或返回上下文错误。
7. 重放校验 protocol/fingerprint/完整性；续接缺失、损坏或跨 profile 时不静默删除后继续。标准 Chat/Fake 旧消息无续接仍可读。备份/回退复用 SRF 流程，不移动 persistence.py 包边界。

**Interface Changes**

```text
Store.begin_provider_request(record, *, lease_guard)
Store.begin_provider_attempt(record, *, lease_guard)
Store.finish_provider_attempt(id, outcome, *, lease_guard)
Store.commit_provider_response(id, response, *, lease_guard)
Store.get_provider_request(id) -> ProviderRequestRecord
ContinuationCodec.to_storage / to_public / validate_for_replay
```

**Tests**

- schema 4 升级/重复升级/故障回滚；旧 checkpoint 和已确认 SRF Effect 保留。
- 普通响应与 Effect 原子提交崩溃，压缩已存而 checkpoint 未推进，完整响应不重复请求。
- SQLite→checkpoint→Context→Adapter 往返；密文不变、日志无 reasoning 原文、损坏阻塞。
- stale owner 拒绝，未结束 attempt 转 interrupted，变更输入不能复用旧 request 内容。

**Acceptance Criteria**

- 普通/压缩两路径均可恢复，断流没有半个可执行响应。
- 新续接数据不会被现有脱敏器静默改坏后继续发送，也不会进入用户 Turn/公开 Trace。

### PGW-08：Runtime 两条调用路径接入

**Goal**

接入 Gateway 的取消/恢复语义，保持 SRF 权限、所有权和稳定 Effect 身份。

**Files / Symbols**

```text
src/patchloop/runtime.py：_execute/_compress_epoch/_provider_model/_provider_thinking/控制回调
src/patchloop/execution/effects.py：persist_model_response_batch/response_from_step
src/patchloop/session/service.py
tests/integration/test_provider_runtime.py、tests/e2e/test_provider_session.py（新增）
```

**Implementation**

1. Runtime 初始化时一次性适配旧 ModelProvider，两处模型调用都通过 _request_model：创建/读取 request、检查 binding、开始 attempt、调用 Gateway、提交结果。开始/失败/取消事件由 Runtime 适配到 Store，临时 delta 仅转发 UI；Gateway 的 response_completed 只代表内存聚合完成，成功 journal 必须等 PGW-07 响应/Effect 事务提交后生成。移除对供应商 config 的 getattr，缓存元信息改取 Binding。
2. 新 Task 创建时绑定；历史 provider=None 在取得 Execution 后条件更新。读取旧模型事件核对 legacy profile；无模型证据时要求显式 legacy profile，不能猜成当前默认。已有完整 Step 优先回放，不因 Provider 配置改变而重新请求。
3. 只有完整 response 才走原 _prepare_model_response/prepare_effect_batch，保持 input revision、Effect ID、一次性 Approval 认领。规范化 assistant 历史携带 continuation，新 Task 从可见 Turn 重建，不复制旧供应商原生项。
4. Control 复用 heartbeat.failure/pending ControlRequest；pause/cancel 的 ProviderCancelled 交原控制结算，不调用 _fail(PROVIDER_ERROR)；lease lost 走原 fencing，禁止保存成功。新增用户输入继续沿用 SRF 的“完整响应后取消陈旧动作”，不每条消息都自动重发请求。
5. 重试用尽的连接/限流/超时/截断，保持 outcome=active、runtime_condition=paused，保存 request 错误供显式 resume；配置/认证错误给可修复状态；不可恢复协议错误失败并报告类别。沿用现有两维 Task 状态，不新增供应商状态枚举。
6. _compress_epoch 使用同一 request journal、取消与用量路径。成功响应落库后才完成 epoch；取消不能被 except Exception 吞掉。普通压缩失败可保持旧 epoch，但必须记录错误与用量；恢复后复用已落库压缩响应。
7. 在所有响应返回后、持久化前再次核对 owner；Adapter 不重新排序/修改工具 Schema，不改变 Policy/Sandbox。网络取消不能宣称取消了已开始的工具动作，工具仍走原受管进程清理。

**Interface Changes**

```text
AgentRuntime._request_model(task, request, control) -> ModelResponse
AgentRuntime._provider_request_control() -> pause | cancel | lease_lost | None
RuntimeCheckpoint 使用 PGW-07 请求/用量游标
```

**Tests**

- 两协议均完成 read→审批写入→测试→回答，中断重启后 Effect ID 不变且写次数为 1。
- 普通/压缩请求在 headers 前和流中取消，lease lost 拒绝旧响应；无新工具执行。
- response 已存而 checkpoint 未推进时无新 HTTP；partial stream 无 Effect，显式 resume 新建 attempt。
- 新输入、预算、Memory/Cache、旧 Fake 和 CLI 路径回归。

**Acceptance Criteria**

- Runtime 无协议族分支、HTTP retry 循环、供应商字段猜测。
- 未授权动作、已确认副作用重复、取消/断流半响应工具执行均为 0。

### PGW-09：用量、缓存与费用精度

**Goal**

跨协议、重试和恢复如实累计消耗，缺字段不按免费处理。

**Files / Symbols**

```text
src/patchloop/providers/usage.py（新增）：UsageNormalizer/PriceSnapshot
src/patchloop/providers/base.py：ModelUsage
src/patchloop/runtime.py：_record_model_usage/_build_report/预算检查
src/patchloop/prompt_cache/usage.py、observability.py、evaluation/cache.py
src/patchloop/persistence.py：request/attempt 预算与累计游标
tests/unit/test_provider_usage.py（新增）
```

**Implementation**

1. 映射 Chat prompt/completion、Responses input/output/cached/reasoning token；reasoning 属于 output 子集，不重复相加。DeepSeek hit/miss 保留原值；Responses total 和合法 cached 齐全时推导 miss，来源标 derived。
2. input/output reported、cache 字段来源、cost_status（estimated/unknown/legacy）和 pricing_version 随 ModelUsage 保存。bool 不当整数；不一致缓存原值保留并计异常，缺缓存不补零当已报告。
3. PriceSnapshot 明确 input/cached_input/output 单价与版本，缺 cached 单价按普通 input 保守估算。DeepSeek 现有默认价格标 legacy 估算来源，不宣称最新；新增 profile 只读用户配置，本地零价显式声明。
4. 新 Task 有美元预算却无价格配置时 preflight 返回 pricing_required。每次 attempt 发送前预留估算费用：以编码请求 UTF-8 字节数加每条消息 64 token 开销作为保守输入估计，加 max_output_tokens，按未缓存单价计算。该预留仅用于客户端保守预算，不能宣称供应商账单严格上限。
5. attempt 有完整用量后用估算实际费用替换预留；确认在发出前失败可释放预留；已送达但用量未知保留预留并报告 cost_status=unknown。下一 attempt 的“已知累计 + 未知预留 + 新预留”超过预算则暂停，不把未知 attempt 当零费用。错误响应是否可能计费按 request_sent 保守处理。
6. 成功 request 和失败 attempt 各自按 ID 只计一次；不能从 Step/request/Trace 三处重复累计。旧 accounted_model_response_steps 映射为历史已计集合，补导已存在响应时不再增加旧费用。
7. TaskReport/checkpoint/metrics/replay 增加请求数、尝试数、未知用量次数、预算预留与费用状态。缓存 fingerprint 的旧空续接消息仍按旧 canonical 投影计算；新续接只加入非秘密摘要，不将原文写入诊断。

**Interface Changes**

```text
UsageNormalizer.normalize(protocol, raw_usage, pricing) -> ModelUsage
UsageNormalizer.reserve(encoded_request, max_output_tokens, pricing) -> float
Report: provider_requests, provider_attempts, unknown_usage_attempts, cost_status, reserved_cost_usd
Checkpoint: accounted_provider_request_ids / accounted_provider_attempt_ids
```

**Tests**

- 正常/缺失/不一致用量，cached/reasoning 子集不重复；无价格 preflight、显式零价。
- 429 重试、断流、完成后重启、压缩恢复，预留与最终计数正确。
- 未知消耗不放大可用预算；旧缓存/报告 fixture 和新增摘要兼容。

**Acceptance Criteria**

- 费用有来源与精度，未知不展示成已知 0 美元；预算含全部 attempt。
- request/attempt 去重与旧水位转换无双计，新能力不破坏旧布局基线。

### PGW-10：Provider CLI 与流式展示

**Goal**

用 CLI 选择/检查 Provider，展示准确的请求、完成与恢复状态。

**Files / Symbols**

```text
src/patchloop/cli.py：_provider_from_env/_session_runtime_service/session start/run/resume
README.md、.env.example
tests/integration/test_provider_cli.py（新增）
```

**Implementation**

1. 新增 provider list/show/check。list/show/check 默认仅验证本地配置和来源/能力；check --connect 才发送最小无仓库内容请求，报告模型/用量/错误，不自动枚举远端模型。
2. session start 和旧 run 增加 --provider/--model/--provider-config；Task 创建前解析验证 Binding。resume 不接受模型/协议/endpoint 覆盖，误用返回 configuration_mismatch 及新建 Task 命令。
3. _session_runtime_service 从 Task binding 创建 Gateway；_provider_from_env 保留旧 DeepSeek 兼容名称但委托 Resolver。旧无绑定 Task 支持显式 --legacy-provider 用于一次性核对绑定，不作为活动 Task 切换后门。
4. human 模式按 request ID 展示生成中、临时正文、失败和完成；delta 跨 chunk 脱敏，对不完整敏感片段缓存到可判断边界，无法保证时缓冲至完整响应再展示。reasoning 默认仅活动状态，opaque/原文不展示。
5. --json 保持 SRF 单对象结果，不混入 delta；显式 --events-jsonl 输出带 version/request/attempt/sequence 的事件行与最终结果事件。中断临时正文不保存成完成的 Assistant Turn。
6. session show/metrics/replay 增加绑定和请求错误；复用现有退出码，可恢复 Provider 错误映射 paused，配置错误映射用法错误。README 写四条接入配置、精确预授权与恢复限制；不修改权限为宽泛自动批准。

**Interface Changes**

```text
patchloop provider list/show/check --provider-config <file>
patchloop provider check <profile-id> --connect
patchloop session start <session-id> <goal> --provider <profile-id> --model <configured-id>
patchloop run <goal> --provider <profile-id>
patchloop resume <legacy-task-id> --legacy-provider <profile-id>
--json 与 --events-jsonl 互斥
```

**Tests**

- 新旧入口选模型、恢复不漂移、legacy 一次性绑定、local 无 key、跨 profile 凭据隔离。
- JSON 无混杂文本，临时输出不成为 Turn，秘密跨两个 delta 仍被隐藏。
- check 无 --connect 时网络数 0；配置失败不创建 Task，不发送仓库内容。

**Acceptance Criteria**

- 用户修改配置即可给新 Task 换 Provider，无需修改 Runtime；活动任务不漂移。
- CI/human 契约稳定，所有入口保持原审批/所有权/恢复路径。

### PGW-11：跨协议验收、本地服务与真实试用

**Goal**

证明两个协议族与实际本地兼容模型可完成相同 Session/工具闭环。

**Files / Symbols**

```text
src/patchloop/evaluation/provider.py（新增）：ProviderAcceptanceRunner
tests/support/provider_http_server.py
tests/e2e/test_provider_session.py
tests/fixtures/providers/acceptance.json（新增）
benchmarks/results/provider_gateway_acceptance.json（实施时生成）
```

**Implementation**

1. 固定同组场景：正文、同响应多工具、坏参数、权限拒绝、审批重启、确认结果回放、新约束、截断/断流/取消/lease lost、压缩、reasoning 往返、未知用量。Chat/Responses 分别执行，不用 Adapter 内 Fake 绕开 HTTP。
2. 默认 loopback server 离线运行；每项恢复/取消场景至少重复 3 次，交叉核对 server 请求数、独立工具审计、SQLite 和 Trace。完整响应前 Effect 数为 0；全部失败保留。
3. 另用用户已运行的真实本地 OpenAI-compatible 模型服务完成工具试用，记录实现/版本/model/tools 能力。Fake server 只算协议测试，不算本地推理验收；不自动下载模型或安装后台服务。
4. 显式凭据运行 DeepSeek Chat 与 OpenAI Responses 核心场景，各 3 个独立干净工作区；固定仓库 revision、验证命令、权限/工具，只更换 Provider。记录生成参数、公开/独立测试、diff、用量来源、时延及全部失败。
5. 模型、价格、端点由验收配置明确提供，不在测试默认值硬编码最新模型。缺服务/凭据为 unverified，不能 skip 后计通过；原 SRF-07 报告保留，新证据单独保存并链接。
6. 运行第 8 节门禁，生成按 protocol/profile 分组的报告；区分正常完成、正确拒绝、正确暂停、真实失败和环境未具备，不能从 fixture 成功率推断真实任务成功率。

**Interface Changes**

```text
ProviderAcceptanceRunner.run(manifest, *, real=False) -> ProviderAcceptanceReport
python -m patchloop.evaluation.provider --manifest <path> --output <path> [--real]
```

**Tests**

- runner 缺服务预检、分组、失败完整保存、凭据不进报告、重复次数不选最佳。
- 两协议同组 HTTP 测试、local profile 与真实进程 Session 恢复。

**Acceptance Criteria**

- 离线全通过；真实 Chat、Responses、本地服务三条证据完成后才声明模块完整验收。
- 未授权动作、确认副作用重复、半截流工具执行、秘密泄漏均为 0。

## 7. Implementation Order

```text
PGW-01 契约
→ PGW-02 配置
→ PGW-03 传输 → PGW-04 Gateway
→ PGW-05 Chat/DeepSeek → PGW-06 Responses
→ PGW-07 请求/续接持久化
→ PGW-08 Runtime → PGW-09 用量
→ PGW-10 CLI → PGW-11 验收
```

PGW-01 后，02 与 03 可独立开发；04 接口稳定后，05 与 06 可并行。07 可先实现 Store/迁移，但完整验收依赖两个 Adapter 的续接 fixture。生产 CLI 的最终切换必须等 07～09 通过，不能先开放无绑定恢复或无预算的多 Provider 入口。

首个提交为 **PGW-01 契约与兼容测试**；每项按自身验收标准结束，不先全仓搬文件或引入外部 Agent 框架。初估 20～30 个开发日，真实服务等待单列，进程取消/续接迁移存在失败时重新评估，不压缩验证。

## 8. Verification

以下命令用于实施阶段，新增文件完成后执行；不是本次文档修改已经获得的结果。

```text
python -m pytest -q tests/unit/test_provider_contracts.py tests/unit/test_provider_config.py
python -m pytest -q tests/unit/test_provider_sse.py tests/integration/test_provider_transport.py
python -m pytest -q tests/unit/test_provider_gateway.py tests/unit/test_provider_chat.py tests/unit/test_provider_responses.py
python -m pytest -q tests/unit/test_deepseek_provider.py tests/unit/test_provider_usage.py
python -m pytest -q tests/integration/test_provider_persistence.py tests/integration/test_provider_runtime.py tests/integration/test_provider_cli.py
python -m pytest -q tests/e2e/test_provider_session.py tests/e2e/test_effect_recovery.py tests/e2e/test_approval_recovery.py
python -m pytest -q tests/integration/test_session_runtime.py tests/integration/test_runtime_ownership.py tests/integration/test_session_cli.py
python -m patchloop.evaluation.provider --manifest tests/fixtures/providers/acceptance.json --output benchmarks/results/provider_gateway_acceptance.json
ruff check src tests
ruff format --check src tests
mypy src
python -m pytest -q
```

真实试用使用显式 profile/凭据/服务和独立输出文件，不覆盖离线或 SRF 历史报告。记录环境、revision、完整失败/跳过及原因，不删除既有失败测试以使 Provider 门禁通过。

## 9. Blockers

**实现设计：None。** 协议选择、配置来源、状态归属、生命周期、错误传播和测试路径已固定。

**完整验收外部条件：** DeepSeek/OpenAI API 凭据、显式模型和价格配置、实际运行的本地兼容模型服务。原 SRF 报告记录当次凭据未配置；本次未探测当前秘密或调用真实模型。条件缺失不阻塞 PGW-01～10 离线开发，但 PGW-11 对应真实路径须保持 unverified。SRF Docker 验收仍归原安全门禁，不以 Provider 协议测试替代。
