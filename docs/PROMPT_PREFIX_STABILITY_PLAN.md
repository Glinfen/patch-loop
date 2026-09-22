# Prompt 前缀稳定性修改方案

调研日期：2026-09-13。状态：PPS-01～07 已完成；PPS-08 验收工具与离线套件已实现，服务器真实试跑已执行但未通过发布门禁；PPS-09 入口及回退已准备，默认仍为 legacy。任务前缀：PPS。

2026-09-16 后续开发：按 [append_only 开销优化方案](APPEND_ONLY_OPTIMIZATION_PLAN.md) 完成 AOP-01～08。优化实现、离线 L0 和有预算真实入口已经交付；本轮未启动真实模型试跑。历史 PPS 结构与发布门禁保留，AOP 前置通过只允许后续显式进入 L1，不等于默认切换或第二阶段完成。

本方案遵循 [PLANNING_GUIDE.md](PLANNING_GUIDE.md)，基于当前工作区实际代码（含尚未提交的 Provider 契约改动）。不把其他计划中的接口当作已经完成的实现。

## 1. Problem

### Current Problem

当前默认 `legacy` 会把动态记忆写入第一条 system 消息；已有 `stable` 虽然冻结初始消息，却把不断增长的记忆发布流放在旧对话历史之前。两条路径都不能保证上一轮请求完整成为下一轮请求的前缀。

实际 Runtime + FakeProvider 三轮复现结果：

| 模式 | 第 1 轮消息数 | 第 2 轮相同完整前导消息 | 第 3 轮相同完整前导消息 |
| --- | --- | --- | --- |
| legacy | 2 | 0 / 上轮 2 条，system 已变化 | 0 / 上轮 4 条，system 已变化 |
| stable | 3 | 3 / 上轮 3 条 | 4 / 上轮 6 条；新增 delta 插到旧 assistant/tool 前 |

这里的“0 条”不代表 0 个缓存 token：第一条消息内部仍可能有相同文本。复现测的是完整消息前缀，不冒充供应商 tokenizer 或命中量。

另外存在以下问题：

- `ContextEngine.build` 每轮按预算重新选历史，过期值通过字符串替换改写旧内容。
- `_compress_epoch` 在裁剪后的普通请求已经完成后才调用；它拿到逻辑历史，没有完整复用先前插入的记忆发布消息。
- `CacheEpoch.rollover` 不断向已有冻结前缀追加摘要，长期运行会积累旧摘要。
- 当前 diagnostics 对脱敏、排序后的 JSON 求 LCP；这不是供应商最终 token 前缀。工具排序还会掩盖实际工具顺序变化。
- `MemoryDeltaPublisher` 按所有历史 delta 的内容全局去重；A→B→A→B 时最后一次相同 delta 可能被错误跳过。

仓库的 [真实历史基线](../benchmarks/results/pco00_cache_baseline.json) 报告 83,061 个输入 token、3,584 个命中 token，命中率 4.31%。这是特定历史任务，不能直接解释用户网页上的全部用量，也不能用作当前模型的严格 A/B 对照。

### Desired Behavior

同一 Task、同一 epoch、同一 Provider 配置内：上一轮已发送的完整消息序列和工具定义保持不变，新增输入、assistant/tool 结果、记忆更新只进入末尾。只有显式记录的压缩边界允许替换历史；安全要求优先于缓存。

### Scope

请求排列、一次性规范化、记忆发布、压缩时机与摘要生命周期、Checkpoint 恢复、实际消息前缀诊断、新任务默认配置和验收。

### Non-Goals

- 不实现服务端 KV cache、响应结果缓存、保活空请求或人工延时刷命中率。
- 不增加供应商、不改检索排序算法、不重构 Session/Effect 所有权模型。
- 不保证跨 Task 共用长历史缓存；新 Task 仍采用现有 Session 可见 Turn 继承语义。
- 不把提高命中率等同于必然降低总费用；压缩输出、重试和任务质量都计入验证。
- 不在本方案内重复实现 Provider Gateway 的传输、请求日志、续接协议。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `src/patchloop/domain.py::PromptCacheLayout/TaskExecutionConfig` | legacy/stable，默认 legacy | 增加 append_only；验证后新 Task 默认切换，历史任务保持原模式 |
| `src/patchloop/cli.py::run_task/start_session_task` | run 默认 legacy；Session 创建 Task 未传缓存布局 | 两入口显式支持同一选项，resume 不覆盖原配置 |
| `src/patchloop/prompt_cache/layout.py::PromptLayout` | 初始 system/项目快照/goal，冻结工具 | 复用初始布局和工具冻结；新模式单独提供固定记忆解释规则 |
| `src/patchloop/prompt_cache/coordinator.py::materialize_messages/prepare_request` | epoch prefix + publication + logical tail；prepare 再次 materialize | 新模式不再拼装旧 publication；读取已规范化消息流，候选发布与提交分开 |
| `src/patchloop/prompt_cache/publication.py::MemoryDeltaPublisher` | 快照和 delta，全局内容去重，V1 system 消息 | 新模式 V2 有序 user 数据消息，事务式候选状态；修复循环转换去重 |
| `src/patchloop/prompt_cache/epoch.py::CacheEpoch` | 冻结初始 spine；压缩 source 来自 logical messages；摘要累积 | 根前缀单独计数；摘要替换；压缩 source 明确为上次实际提交的请求 |
| `src/patchloop/context/engine.py::build/_normalize_messages/_exclude_history_values` | 每轮规范化、选组、替换过期值 | 保留旧模式；新模式提供新消息规范化与完整窗口校验，不裁剪已发内容 |
| `src/patchloop/runtime.py::_run_owned/_advance_once` | logical messages 与请求窗口不同 | append_only 下 checkpoint.messages 即规范化请求历史；内存候选原子提交 |
| `src/patchloop/runtime.py::_consume_pending_inputs/_supersede_response_for_inputs` | 追加 Turn；在新输入出现时取消未领取 Effect | 保持逻辑；先补齐工具结果，再追加输入/记忆；新增内容仅规范化一次 |
| `src/patchloop/runtime.py::_compress_epoch` | Step 结束后调用，错误时返回旧历史 | 普通请求前检查阈值，复用已发送 source；成功响应持久化后才切 epoch |
| `src/patchloop/memory/retrieval.py::LayeredMemoryContext.provider_projection` | 确定性正文，排除排序分数等审计字段 | 继续复用，不使用 rendered；不让 recent_history_tokens 驱动新模式裁剪 |
| `src/patchloop/memory/manager.py::inactive_context_values` | 返回已失效且无 active 同值记录的值 | 作为 V2 发布状态的一部分，追加失效声明，不回改旧历史 |
| `src/patchloop/persistence.py::RuntimeCheckpoint/SQLiteStore` | messages、cache epoch、publication、usage 分散投影 | 可空追加状态、已发消息边界、原子恢复；旧 JSON 加载保留 legacy |
| `src/patchloop/session/models.py::SessionCheckpoint` | 部分缓存状态投影，extra=forbid | 增加对应可空字段，所有双向投影显式覆盖 |
| `src/patchloop/prompt_cache/diagnostics.py::fingerprint_request/CacheDiagnostics` | tools 排序、JSON LCP、恢复后退化为 section 估计 | 新消息级摘要指标可跨进程比较；保留旧指标并标注估计性质 |
| `src/patchloop/evaluation/cache.py::CacheBenchmarkRunner/build_cache_fixture` | 手工合成 fixture，未强制经过实际 Runtime | 加实际 Runtime 录制路径，旧模拟只保留为结构单测 |
| `src/patchloop/evaluation/gates.py::CacheAcceptanceEvaluator/CacheRolloutPolicy` | PCO 固定基线和 stable rollout | 增加 PPS gate profile，按同批 baseline/candidate 比较 |
| `src/patchloop/providers/base.py::ProviderRequest/ProviderContinuation` | 当前已出现契约；Runtime 仍直接 complete | PPS 消息保留 continuation；请求日志接 PGW-07/08，不复制其状态机 |

当前调用链与测试约束已实际阅读。`tests/unit/test_prompt_cache_dependencies.py` 禁止 memory/providers 依赖 prompt_cache，也禁止 prompt_cache 依赖 Runtime/Persistence；新实现继续满足。

### Current Flow

```text
Task config → initial system/project/goal → Session visible history
→ Runtime logical messages → retrieve memory
→ legacy: 动态记忆写入 system
  stable: frozen prefix + 所有 publication messages + history
→ ContextEngine 每轮规范化、筛选、替换
→ Coordinator 再次 materialize → Provider.complete
→ assistant/tool 加入 logical messages
→ 若已发生 dropped_steps，才请求 epoch compression
→ checkpoint（保存的 logical messages 不等于实际请求）
```

## 3. External Research

以下为 2026-09-13 访问的官方文档及项目源码。在线分支会变化；只借鉴已读出的具体行为，不声称完整项目都满足本方案的不变量。

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| [Claude Code：prompt caching](https://code.claude.com/docs/en/prompt-caching) | 固定层在前；普通对话末尾追加；压缩使用同 system/tools/history 加末尾指令，压缩后重建对话层 | 保留完整上一请求；压缩作为显式边界；项目规则在任务开始冻结 | 不照搬 Anthropic TTL、认证差异或产品环境快照 |
| [Qwen Code：ChatCompressionService](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/services/chatCompressionService.ts) | `supportsCompressionCacheSharing` 和压缩路径检查容量；可共享时沿用主请求 system/配置/历史并追加指令；验证摘要有效性 | 压缩前预留空间、复用已发请求、拒绝空摘要和工具调用响应 | 不照搬 20K 摘要、13K 缓冲等针对更大窗口的绝对值；不自动换廉价模型破坏 prefix |
| [OpenCode：provider/transform.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/provider/transform.ts) 与 [session/compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts) | `applyCaching` 在 Provider 层处理缓存标记；compaction 有历史保留逻辑，prune 会标记旧工具输出供裁减 | Provider 格式与 Runtime 历史职责分离；显式识别历史裁减边界 | 不把 provider-specific cache_control 塞进通用 DeepSeek payload；不在普通轮次照搬旧输出 prune |

供应商事实：DeepSeek 当前文档描述请求输入/输出边界、共同前缀检测和固定间隔的缓存落盘单位；完整复用已持久化单位才可命中。因此 `A+B → A+B+C` 比 `A+B → A+C` 更直接复用上一请求。缓存是 best-effort，构建需要时间，命中并非客户端能保证。真实统计使用 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/)

### Lessons for PatchLoop

1. 优化对象是整个已发送请求的连续前缀，不只是 system 或最前面几条消息。
2. 动态数据放末尾；缓存 marker 不能修复前缀中间插入或历史重写。
3. 压缩会损失部分历史缓存，是可控的代价；不能为缓存无限保留历史和摘要。
4. 本地结构校验和 Provider 实测必须分开，不能用字节估算宣称真实命中率。

## 4. Design

### Selected Approach

扩展现有缓存组件，新增布局值 `append_only`。保留 `legacy`、`stable` 原语义作为已有任务兼容和 A/B 对照；不复制一套 Runtime、检索器或请求日志。新增模式共享现有 PromptLayout、Coordinator、Epoch、MemoryDeltaPublisher、ContextEngine 和 Store。

选择新枚举而非直接改变 `stable` 的原因：旧 checkpoint 没有完整已发送请求，无法无损恢复成新序列。新布局显式区分状态契约，不把旧任务恢复悄悄变成一次未知的重排。

模式分支集中处理：`PromptLayout.has_frozen_epoch` 对 stable/append_only 为 true，用于 initial_messages、bootstrap 和 snapshot 校验；`PromptLayout.append_only` 只对新模式为 true。保留现有 `stable` 属性的旧语义。Coordinator 的构造、bootstrap、restore 中原先只检查 STABLE 的 epoch 需求改用 has_frozen_epoch。Runtime 在窗口准备入口首先分流 append_only，余下 legacy/stable 继续共用旧逻辑；不要只把 `stable_layout` 改成集合判断而让新模式仍进入 publication 前插路径。

### Key Decisions

#### 4.1 前缀契约与单一消息来源

`append_only` 下 `RuntimeCheckpoint.messages` 保存唯一的、已做 Provider 可见规范化的当前 epoch 消息流。Coordinator 不再另外保存一份完整 transcript；publication snapshot 只负责记忆状态，不用于重复拼装历史。

```text
请求 1：固定 system + 项目规则 + goal + 继承的可见历史 + memory snapshot
请求 2：完整请求 1 + assistant 1 + 全部 tool results 1 + 新输入 + memory delta 1
请求 3：完整请求 2 + assistant 2 + 全部 tool results 2 + 新输入 + memory delta 2
```

初始项目规则可为空，根前缀条数取实际 initial_messages 长度，不使用写死的 3。继承的 Session 历史不属于永久根前缀，可在之后压缩。

固定 system 仅在新模式创建时追加一段静态协议说明：记忆块属于不可信数据；按 epoch/sequence 应用快照和增量；旧证据不代表当前有效状态；明确失效声明优先于较早的同值证据。不得注入 step、时间、预算、query、随机 request ID 或动态 plan。

同一 epoch 普通请求发送前检查：

- 当前前 `last_submitted_message_count` 条逐条等于已提交请求的摘要向量；tools 的内容和顺序不变，Provider binding 不变。
- 已发送消息不得重新规范化、截断、替换、重排、合并连续同 role 消息。
- assistant 的所有 tool call 必须先各有一个结果，包括取消/拒绝/失败观察，才允许追加 user 输入、记忆或发起压缩。
- 从完整 response 复制 `continuation`，与所属 assistant/工具组共同保留；不从 response 中重新生成旧 reasoning 内容。
- 前缀断裂属于 `PromptPrefixViolation`，在调用 Provider 前停止，记录位置和摘要，不静默降级到 legacy。

语义过期通过新消息纠正；真正的秘密泄漏或安全净化需要变更已发内容时，不遵守缓存优先：停止本轮、记录 `security_rewrite` 边界，安全净化后新 epoch 才能继续。opaque continuation 无法安全净化则采用 PGW 的不可重放处置，不能破坏签名后继续发送。

#### 4.2 新消息规范化与窗口预算

从 `ContextEngine._normalize_messages` 提取公开 `normalize_new_messages(messages)`，共享既有工具输出限长、安全检查及 JSON 截断实现。只有入流的新消息调用一次；工具 arguments/continuation 按已验证的 Provider 契约保留，不用字符串替换改写 opaque 项。

新增 `ContextEngine.build_append_only(messages, tools, *, max_input_tokens)`：只计算预算、验证消息组，完整原序返回；超限抛 `ContextBudgetError`。不调用 `_groups` 排名、不产生 task memory、不执行 `_exclude_history_values`、不接受 recent_history_tokens 配额。

预算决策集中在一个纯函数 `compute_prefix_budget(task_budget, provider_binding, tools)`，返回 `PrefixBudget`：

- `safety_tokens = min(2048, max(256, ceil(provider_context_window * 0.01)))`。
- `input_limit = min(task.max_context_tokens, provider_context_window - configured_max_output_tokens - safety_tokens)`；这是含 tools 的总输入限制。小于固定根前缀和工具成本则在首个请求前报预算错误。
- `compression_directive_reserve = estimate_message(COMPRESSION_INSTRUCTION) + 64`。
- `ordinary_limit = input_limit - compression_directive_reserve`，保证最近已发请求还能追加压缩指令。main 与 compression 都检查实际配置的输出上限。
- `soft_limit = floor(ordinary_limit * 0.80)`。只有已有可压缩的已发历史且候选超过 soft_limit 才压缩。首请求或没有可压缩历史时，允许到 ordinary_limit。
- 新记忆单次发布目标预算为 `min(2048, floor(ordinary_limit * 0.10))`；小于 64 则作为小窗口配置错误，不静默去掉约束。
- tools、根前缀、未决新用户输入均计入；检索结果只占剩余空间，检索层 recent_history_tokens 不再反向删历史。

当前 tokenizer 是估算，Provider usage 可用于诊断估算偏差，不能保证所有语言绝不超服务端窗口。服务端 context overflow 进入显式压缩/暂停恢复路径，不通过每轮暗中裁剪纠错。FakeProvider 的离线 budget 使用 Task 的输入限制；真实新模式必须有冻结的能力/输出配置，不从模型名字猜窗口。

#### 4.3 记忆发布 V2：在末尾按转换顺序追加

扩展 `MemoryDeltaPublisher`，V1 兼容读取、旧布局继续使用；V2 只用于 append_only。

```python
class MemoryPublicationUpdate(BaseModel):
    next_state: MemoryPublicationSnapshot
    messages: list[ModelMessage]  # 本次新增块，通常 0 或 1，可拆分为多个有序块

MemoryDeltaPublisher.preview(
    epoch_id: str,
    provider_projection: str,
    *,
    invalidated_values: list[str],
    max_message_tokens: int,
) -> MemoryPublicationUpdate
```

preview 不修改 publisher；Runtime 将新 messages 放进候选尾部，预算通过后，与 `next_state` 在同一 checkpoint 提交，再更新内存状态。prepare_request 不再调用 publish 第二次。失败/重试只复用该提交版本。

`MemoryPublicationSnapshot.schema_version` 明确允许 1.0/2.0，V2 加 `invalidated_values: list[str]`；`current_fingerprint` 覆盖正文 payload 与失效值集合。V1 保持原 hash 算法及校验，V2 校验连续 sequence、base/result 链和最后重放状态，不将两版 hash 混用。V2 的 `delta_count` 等于快照之后的发布块数；初始 snapshot 为一条消息，其大小受检索预算及普通请求总预算共同约束。单条消息上限的分块规则只用于 delta，snapshot 过大时不能靠分块绕过总预算。

V2 snapshot/delta 用 `role=user`，正文固定不可信数据前缀 + 确定性 JSON；不得提升成动态 system。字段固定为 `epoch_id`、`sequence`、`base_fingerprint`、`result_fingerprint`、`payload`。payload 是 snapshot 或 added/removed，以及当前 `invalidated_values` 的 added/removed。epoch ID 来自稳定状态，不能每次生成随机值。

- 保留 `provider_projection` 的稳定正文排序；不发布检索分数、查询文本、耗时、事件时间、访问计数。
- 排名变化但正文不变不发布；一轮检索失败不解释为“所有事实被删除”：projection=None 表示保留，成功返回空集合才是清空当前检索视图。
- “不在本次检索集合”与“事实已失效”分开：removed 只撤出当前视图，invalidated_values 明确当前不可使用的旧值。复用 manager 现有 active 同值过滤规则。
- 去重只依据当前有效状态是否相同，不按历史 delta 文本全局去重。A→B→A→B 必须四个状态都可重放；sequence 递增，base/result 链严格校验。
- 重复准备同一请求不能增加 sequence；已提交的 publication state 与 transcript 对应消息必须一致。
- 超长增量按确定性 category/key 顺序拆为有序块，每块有自身 base/result 链，整批提交；单条事实装不下时不切坏 JSON，不静默省略约束，返回 `MemoryDeltaTooLarge`。Runtime 先尝试新 epoch 的有界 snapshot，仍不适配则预算错误。
- invalidated_values 是数据，不会在旧 transcript 做 replace；评估失效事实使用率，不以“旧文本是否仍存在”判断陈旧事实回归。

#### 4.4 状态模型与职责

新增类型放在 `prompt_cache/coordinator.py`，不新建持久化子系统：

```python
class AppendOnlyPromptState(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    root_prefix_message_count: int
    last_submitted_message_count: int = 0
    last_submitted_message_fingerprints: list[str] = []
    last_submitted_tool_fingerprint: str | None = None
    last_submitted_binding_fingerprint: str | None = None
    last_submitted_request_id: str | None = None
    epoch_generation: int = 0
    compression_source_request_id: str | None = None
    compression_source_message_count: int | None = None
    compression_request_id: str | None = None
    deferred_compression_fingerprint: str | None = None


class PrefixBudget(BaseModel):
    input_limit: int
    ordinary_limit: int
    soft_limit: int
    memory_message_limit: int
    summary_limit: int
```

所有 count 非负并有上界关联验证；root 至少 2；last_submitted count 必须等于对应摘要向量长度，不能超过 checkpoint.messages；fingerprint 固定 SHA-256。列表实际实现用 Field(default_factory=list)，模型 frozen/extra=forbid。

`append_only_state: AppendOnlyPromptState | None = None` 加到 RuntimeCheckpoint、SessionCheckpoint、CoordinatorSnapshot 和现有 checkpoint_fields 投影；restore 对 append_only 缺状态报 `CheckpointSchemaError`，不猜测 prefix。无此字段的 legacy/stable 仍可读取。

内存候选可包含尚未发送的 suffix，但“已经规范化”不等于“已经发送”；last_submitted_message_count 明确划线。消息正文只在现有 checkpoint/messages 中保存，诊断只存摘要与长度。

#### 4.5 压缩在普通请求之前发生

不再等待 dropped_steps。append_only 完整候选建立后、创建普通 ProviderRequest 前判断预算；普通轮次的 dropped_steps 恒为空。

压缩分两阶段，复用 CacheEpoch：

1. Source 是 `messages[:last_submitted_message_count]`，即上次已提交普通请求的完整消息（含当时的记忆），而不是当前逻辑历史或本轮重算窗口。source + 固定末尾压缩指令形成独立 `EPOCH_COMPRESSION` 请求，沿用 tools/model/thinking。
2. 尚未发出的 suffix（上次 assistant、其完整工具结果、本次新输入）暂存原顺序，不能被摘要吞掉。本轮尚未提交的记忆 candidate 不并入 source；压缩后从当前有效状态重新生成一次 snapshot。
3. 输出为现有 `constraints/paths/decisions/failures/tests/unfinished/next_step` JSON 对象；禁止 tool_calls、空输出、非对象、无法解析 JSON。经现有安全处理后验证非空。`summary_limit = min(2048, max(128, floor(ordinary_limit * 0.125)))`；压缩请求输出上限取该值与原配置上限的较小值，仍满足 thinking 协议约束，否则使用原配置输出上限并在接收后验证摘要长度。
4. 新 epoch = 永久 root + 一条新 summary + 未发送 suffix + 当前 memory snapshot。旧 summary 是本次 source 的一部分，应被新 summary 吸收；不把所有历史 summary 永久叠加。generation 增长，ID 从固定根 epoch ID + generation 生成，避免 `.g1.g2...` 无限增长。
5. 成功响应先通过 PGW 持久化，再验证新窗口；要求新窗口不超过 ordinary_limit 且小于压缩前候选，否则视为 `compression_no_gain`。有效新 epoch 和 publication reset 一次 checkpoint 提交后才发送普通请求。
6. 压缩指令/压缩 assistant 响应不追加到普通 transcript；压缩响应只用于生成 summary。压缩 usage 单独按请求计入总费用。

失败处理固定如下：

| 情况 | 行为 |
| --- | --- |
| provider error、无效摘要、no_gain，原候选仍 <= ordinary_limit | 保留旧 epoch；同一 source fingerprint 不再次自动压缩，普通请求可以继续；下次 source 变化才可重试 |
| 失败且原候选 > ordinary_limit | 保留历史并暂停，标记 context_budget_exceeded/压缩原因；不发超限普通请求，不静默丢组 |
| 首轮根前缀/继承历史/新输入已经超限且无已发 source | 明确预算错误；不临时构造未经设计的另一条摘要调用 |
| 取消、lease 丢失 | 立即服从 PGW/SRF 控制；不得吞异常继续 Provider 或落库 |
| 结果已保存、epoch checkpoint 未提交即崩溃 | 恢复读取同一 compression request 响应，重新完成确定性的 epoch 提交；不二次计费、不重复摘要 |
| source 响应不明 | PGW 标记 unknown usage/interrupted；由其恢复策略管理，不由缓存模块重复发送 |

压缩期间新到达的用户 Turn 保留在 Store；下一普通请求前按 consumed_input_sequence 追加一次。不要把新 Turn 偷塞进已创建的 compression request，也不要复用其 request ID 表示新输入。

#### 4.6 持久化与 Provider Gateway 衔接

现有 [Provider Gateway 方案](PROVIDER_GATEWAY_DEVELOPMENT_PLAN.md) 的 PGW-07/08 已承担 ProviderRequest journal、原子响应提交、取消/重试、压缩恢复和按 request/attempt 用量去重。PPS 直接接这些接口：

- ProviderRequest 使用既有 task/purpose/step/epoch_generation/input_revision 身份；新增字段属于 prompt checkpoint，不另建请求表。
- `begin_provider_request` 之前持久化 messages、publication、AppendOnlyPromptState，last_submitted 指向该不可变输入。实际调用失败也保留该尝试记录；“submitted”不代表 Provider 已成功缓存。
- Provider 请求恢复先查 journal：已完成响应先恢复，不先检索记忆/重建请求；未完成请求按保存输入处理。新 input_revision 需要新身份，不能只更改旧请求内容。
- 恢复 Step/Effect 时，assistant 由已保存响应还原；已有 tool result 用原 effect/call ID 幂等补入，保持原输出顺序，沿用原审批、取消和 unknown 处置。
- 缓存统计使用 PGW 请求去重；不能在每次 resume 再 `observe_response` 累加同一已保存 usage。
- PPS 不修改 PGW continuation 数据结构，只保证复制和组边界；最终 adapter 若重写前序消息，PPS 的 transport contract 测试必须检出。

PGW-07/08 未完成时，PPS 纯组件和 FakeProvider 验证可先开发；Runtime 持久化接入与默认切换不能标记完成。不能为绕开依赖再建一套临时 compression journal。

#### 4.7 诊断：测真实发送排列，不把 JSON LCP 当 KV 命中

扩展 CacheDiagnosticsSnapshot/CacheLayoutTrace：

```text
previous_message_count
common_prefix_message_count
previous_request_is_prefix: bool | None
first_changed_message_index: int | None
common_prefix_estimated_tokens
tools_unchanged: bool | None
binding_unchanged: bool | None
comparison_kind: cold_start | ordinary | compression | epoch_boundary
prefix_break_reason: memory_insert | history_rewrite | tools_change | binding_change | security_rewrite | unknown | None
metric_basis: normalized_messages_v1
```

按实际顺序为每条规范化 message 序列化后哈希，保留 tool_calls、tool_call_id、continuation；tools 保留列表顺序，不为诊断单独排序。Checkpoint 保存上一请求摘要向量和每条估算长度，恢复后仍能判断整个上一请求是否为前缀。压缩请求与普通请求分开比较，压缩完成后明确 epoch_boundary，不把允许的重建计为普通轮次失败。

既有 longest_common_prefix_bytes/tokens 保留用于旧报告兼容，文案改为 canonical JSON 估计，不能进入 PPS 发布硬门禁。实际供应商格式测试通过 Fake transport 捕获序列化消息/items，逐项比较；HTTP JSON 外层逗号、数组闭括号、request_id 不是 token prefix，不对整个 HTTP body 简单 startswith。

CacheUsageAccumulator 继续只接受 Provider 报告的 hit/miss；缺失或不一致标为 unavailable，不补模拟值。总命中率用总 hit / 总 (hit+miss)，不平均每轮百分比。费用包含普通请求、压缩和可知的付费 attempt；未知费用单列。

#### 4.8 默认配置与兼容

- 开发期间 append_only 显式 opt-in；PPS 验收通过后 `TaskExecutionConfig`、run 和 session start 的新任务默认统一为 append_only。
- 所有持久化 Task JSON 加载通过 `persistence.py::_decode_task_payload`：仅当旧 JSON 缺 execution.prompt_cache_layout 时补 legacy；明确 legacy/stable/append_only 原样保持。替换该模块所有 Task.model_validate_json(row/payload) 直接解析点，避免遗漏事务内部路径。
- resume 不接受缓存布局覆盖；需要回退时，新 Task 明确 `--prompt-cache-layout legacy`。保留旧 stable 至少一个兼容周期。
- 新 append_only 数据不能保证由旧版本程序继续执行；回退是新任务配置回退，不承诺跨版本二进制无损降级。
- 不新增 SQL 表或单独迁移；新增 JSON 可空字段沿用现有版本兼容机制，PPS schema 由新增状态自己标识。若 PGW 已提升外层 checkpoint 版本，则接其适配函数，不覆盖回旧版本。

### Target Flow

```text
恢复已保存请求/Effect（优先）
→ 规范化并追加新消息/完整工具组
→ 检索稳定记忆投影 → preview delta → 候选末尾追加
→ 完整窗口检查
→ 必要时：上次已发请求 + 压缩指令 → PGW 持久化响应 → 显式新 epoch
→ 前缀契约验证 → 原子提交 transcript + publication + request 边界
→ PGW ProviderRequest → 完整响应持久化
→ 原有审批/Effect 执行 → checkpoint
```

## 5. Change Map

| File | Symbol | Change |
| --- | --- | --- |
| `src/patchloop/domain.py` | PromptCacheLayout/TaskExecutionConfig | append_only；最终默认切换 |
| `src/patchloop/prompt_cache/layout.py` | PromptLayout | 新模式共享根布局、固定记忆协议说明 |
| `src/patchloop/prompt_cache/coordinator.py` | Coordinator/AppendOnlyPromptState/PrefixBudget | 候选状态、前缀校验、消息边界、预算决策 |
| `src/patchloop/prompt_cache/publication.py` | MemoryDeltaPublisher/Snapshot/Update | V2 有序发布、循环转换、失效声明、分块 |
| `src/patchloop/prompt_cache/epoch.py` | CacheEpoch/Snapshot | root count、source 明确化、替换摘要 |
| `src/patchloop/prompt_cache/diagnostics.py` | Diagnostics/Snapshot/Trace | 消息级可恢复前缀指标 |
| `src/patchloop/prompt_cache/__init__.py` | 导出 | 新契约；旧兼容模块继续 re-export |
| `src/patchloop/context/engine.py` | normalize_new_messages/build_append_only | 入流一次处理、完整校验 |
| `src/patchloop/runtime.py` | _run_owned/_advance_once/_compress_epoch/恢复及输入路径 | 单一 transcript、预检压缩、PGW 生命周期整合 |
| `src/patchloop/persistence.py`、`session/models.py` | Checkpoint/投影/_decode_task_payload | 增量状态与兼容加载 |
| `src/patchloop/cli.py` | run_task/start_session_task/benchmark_cache/validate_cache_gates | 布局选项、PPS 验收选项 |
| `src/patchloop/evaluation/cache.py`、`gates.py` | Runtime fixture/collector/PPS gate profile | 实际请求录制、按当批对照比较 |
| `tests/unit/test_prompt_prefix_stability.py`（新增） | 前缀/预算契约 | 多轮、篡改、压缩 source |
| `tests/unit/test_memory_publication.py`、`test_context.py`、`test_cache_epoch.py` | 现有覆盖扩展 | V1/V2、一次规范化、摘要长度有界 |
| `tests/unit/test_cache.py`、`test_cache_evaluation.py`、`test_cache_gates.py` | 指标/门禁 | 模拟不能充当实测 |
| `tests/integration/test_prompt_prefix_recovery.py`（新增） | 跨进程恢复 | 请求/压缩/Effect/输入故障点 |
| `tests/e2e/test_prompt_prefix_runtime.py`（新增） | 实际 Runtime 场景 | 长输出、检索变化、失效事实、预算压缩 |
| `tests/unit/test_deepseek_provider.py` 及 PGW adapter 测试 | capture transport | 最终 payload 的完整旧消息不变 |
| `README.md` | 配置和诊断示例 | 验收后更新新默认与回退方法 |

## 6. Implementation Tasks

### PPS-01：建立实际 Runtime 回归样本（已完成）

**Goal**

把已发现的前缀破坏转成可重复的失败基线，避免只验证手工拼接的缓存 fixture。

**Files / Symbols**

```text
tests/unit/test_prompt_prefix_stability.py（新增）
tests/e2e/test_prompt_prefix_runtime.py（新增）
src/patchloop/evaluation/cache.py::CacheBenchmarkRunner
```

**Implementation**

1. FakeProvider 返回至少 6 个工具轮次，使用实际 AgentRuntime、MemoryManager 和 ToolGateway，录制请求。
2. 覆盖相同检索、变化检索、A→B→A→B、长 tool output、待处理用户 Turn、forced compaction。
3. legacy/stable 基线断言其已观察到的行为；append_only 的期望断言随对应任务启用，不在主分支留下常驻失败测试。
4. 比较完整 message 数据及 tools，测试辅助函数只存在 tests，不建立生产缓存实现。

**Interface Changes**

`CacheEvaluationVariant.APPEND_ONLY`；Runtime fixture runner 返回现有 CacheRunReport 并标记 source=deterministic。

**Tests**

- 重现 legacy system 变化、stable 历史前插 delta。
- fixture 真正调用 Runtime；禁止用 build_cache_fixture 的手工数组冒充。

**Acceptance Criteria**

同一输入可稳定重现缺陷；记录可支持修复前后相同工具响应比较。

**Implementation Record**

- `CacheBenchmarkRunner.run_runtime_fixture` 通过 AgentRuntime、MemoryManager、ToolGateway 与 FakeProvider 执行六轮工具调用，并返回 `source=deterministic` 的 `CacheRunReport`。
- Runtime 回归覆盖 legacy system 改写、stable 记忆前插、同一记忆视图、A→B→A→B、长工具输出、强制压缩和运行中新增 Session Turn；比较完整消息数据和工具定义。
- 增加 `CacheEvaluationVariant.APPEND_ONLY` 标识。append_only Runtime 断言及行为仍由后续任务启用；旧手工仿真矩阵暂不把它作为已支持变体。
- 验证：PPS-01 定向测试 13 项通过，扩展相关回归集合 54 项通过；`ruff check src tests`、`mypy src/patchloop` 通过。全量测试在新增最后一项单元测试前运行，结果为 551 项通过、1 项跳过、1 项失败；唯一失败为 Windows `test_takeover_terminates_verified_live_command_before_new_writer` 无权终止子进程（错误码 5），与 PPS-01 改动无关。

### PPS-02：追加状态和兼容读取（已完成）

**Goal**

为新模式定义可恢复的单一 transcript 契约，保持旧任务行为。

**Files / Symbols**

```text
src/patchloop/domain.py::PromptCacheLayout
src/patchloop/prompt_cache/coordinator.py::AppendOnlyPromptState/CoordinatorSnapshot
src/patchloop/persistence.py::RuntimeCheckpoint/_decode_task_payload
src/patchloop/session/models.py::SessionCheckpoint
src/patchloop/prompt_cache/__init__.py
```

**Implementation**

1. 增加 append_only，暂不切默认；实现 4.4 的状态和关联校验。
2. 所有 checkpoint 双向投影、restore、save 路径传递可空 append_only_state，保持旧字段。
3. 统一持久化 Task 解码，旧缺字段补 legacy，显式模式不覆盖。
4. Coordinator 只持有状态，不导入 Store/Runtime；损坏新状态抛 CheckpointSchemaError 由 Runtime 边界处理。

**Interface Changes**

`AppendOnlyPromptState`；各 checkpoint 增加 `append_only_state=None`；新增 `_decode_task_payload(payload_json) -> Task`。

**Tests**

- 旧无字段、legacy、stable round trip；新状态 round trip。
- count/hash 不匹配、append_only 缺状态拒绝；Session/Runtime 投影等价。
- test_prompt_cache_dependencies 和现有 session migration 测试。

**Acceptance Criteria**

旧任务不被新默认改变；新状态可跨进程还原且不复制第二份正文。

**Implementation Record**

- `PromptCacheLayout` 增加 `APPEND_ONLY`，`TaskExecutionConfig` 默认保持 legacy；CLI `run` 显式限定 legacy/stable，避免在 PPS-06 完成前运行未实现的新模式。
- `AppendOnlyPromptState`（frozen、extra=forbid）实现 4.4 全部字段：count 非负、root ≥ 2、last_submitted count 等于摘要向量长度、指纹固定 SHA-256、compression source 不超过已提交边界；`validate_message_boundaries` 提供与 checkpoint.messages 的跨模型上界校验。
- Coordinator 构造/snapshot/from_legacy_state/from_snapshot 均持有并传递可空 `append_only_state`；append_only 缺状态或缺 epoch 快照一律拒绝，不从当前消息猜测 prefix；legacy/stable 携带新状态被拒绝；stable 与 append_only 同属冻结 epoch 布局（`_FROZEN_EPOCH_LAYOUTS`），append_only 恢复时同样还原 publication 状态。
- `RuntimeCheckpoint`、`SessionCheckpoint`、`PromptCacheCoordinatorSnapshot`、`PromptCacheCheckpointFields` 均增加 `append_only_state=None`；两个 checkpoint 模型用同一 `validate_message_boundaries` 校验消息边界，保存路径经 `checkpoint_fields()` 自动投影，不需要第二份正文。
- `_adapt_checkpoint_json` 将 pydantic ValidationError 包装为 `CheckpointSchemaError`；Runtime 新增 `_restore_prompt_cache` 边界（合并 resume/advance 两处重复恢复代码），append_only 恢复失败转换为 `CheckpointSchemaError`，legacy/stable 错误类型不变。
- 新增 `_decode_task_payload`：仅当 JSON 缺 `execution.prompt_cache_layout` 时补 legacy，显式 legacy/stable/append_only 原样保持，结构非法的 execution 仍然校验失败；替换 persistence.py 内全部三处 `Task.model_validate_json` 直接解析点（v0 迁移、`get_task`、事务内 `_task_row`）。
- 验证：新增定向测试 19 项（coordinator 8、storage 8、session models 2、runtime 边界 1，含旧无字段/legacy/stable round trip、count/hash 不匹配、缺状态拒绝、Session/Runtime 投影等价、Task 解码钉住）；定向集合 73 项、集成/session 集合 136 项通过；全量 pytest 572 passed、1 skipped（Windows 符号链接）、0 failed；`ruff check src tests`、`mypy src/patchloop` 通过。全量运行使用仓库外 basetemp（默认 pytest 临时目录在本机报 WinError 5，仓库内 basetemp 会污染检索基准索引）。
- 独立复核：PPS-02 相关测试集合 71 项、CLI 与 Session CLI 31 项通过；`ruff check src tests` 和 `mypy src/patchloop` 再次通过。

### PPS-03：记忆 V2 按序追加

**Goal**

使记忆变化只产生末尾新增内容，同时保留语义失效处理。

**Files / Symbols**

```text
src/patchloop/prompt_cache/publication.py::MemoryDeltaPublisher/MemoryPublicationSnapshot
src/patchloop/prompt_cache/layout.py::PromptLayout
tests/unit/test_memory_publication.py
```

**Implementation**

1. 实现 4.3 preview/update、V2 user envelope、确定性分块和链验证，旧 V1 路径保留。
2. 去重按当前状态；V2 snapshot 校验不再要求历史正文 hash 唯一。
3. 纳入 invalidated_values，区分 projection=None 与空集合；新增固定协议说明只影响新模式 root。
4. 发布失败不改变原 state；多块提交必须全有或全无。

**Interface Changes**

`MemoryPublicationUpdate`、`MemoryDeltaPublisher.preview(...)`；V2 schema 分支。

**Tests**

- A→B→A→B 重放等于最终 B；相同状态不发布。
- 检索排名变化不发布；失效/重新有效更新可重放。
- 失败重试 sequence 不增长；拆块重放正确；单项超限报错。

**Acceptance Criteria**

每个已提交变更准确发布一次；当前状态与 transcript 的 publication 链一致。

**Implementation Record**

- 保留 `publish()` 的 V1 行为；新增 `MemoryDeltaPublisher.preview()` 与 `MemoryPublicationUpdate`，append_only 的 V2 snapshot/delta 使用 user 消息、固定 envelope、连续 sequence 及 base/result fingerprint。V1 和 V2 使用独立的 fingerprint 算法；V2 checkpoint 校验消息摘要、链连续性及完整重放结果。
- V2 按当前 payload 和失效集合去重，支持 A→B→A→B；`provider_projection=None` 保留当前检索视图，成功的空 projection 清空视图。失效值作为有序数据追加，不改写历史消息。
- 增量按固定 category/key 顺序分块，逐块检查预算；快照或单项超限抛出 `MemoryDeltaTooLarge`。预览失败不改变原 publisher，多块候选只有完整构造后才返回。
- `PromptLayout` 为 append_only root 添加固定记忆处理协议；Coordinator bootstrap 初始化冻结 epoch 和 root 状态。Runtime 将 preview 候选与请求历史原子提交的调用路径按计划留给 PPS-06；stable/legacy 的根提示及 V1 发布保持原行为。
- 新增 10 个用例覆盖状态循环、排名变化、失效值、检索失败与清空、V1 状态升级、重试原子性、分块预算、append_only root 及 Coordinator bootstrap。

### PPS-04：完整窗口与前缀校验

**Goal**

让新模式只规范化新增内容，普通窗口不重写已发历史。

**Files / Symbols**

```text
src/patchloop/context/engine.py::normalize_new_messages/build_append_only
src/patchloop/prompt_cache/coordinator.py::PrefixBudget/prepare_request
tests/unit/test_context.py
tests/unit/test_prompt_prefix_stability.py
```

**Implementation**

1. 提取现有规范化代码；追加输出限长一次执行，保留原工具/续接组约束。
2. 实现 4.2 预算函数与 build_append_only；新模式不执行筛选/字符串失效替换。
3. prepare_request 对已发向量、tools、binding 检查，不 materialize 全部 publication。
4. 引入 `PromptPrefixViolation`，普通请求前抛出，诊断不含正文。

**Interface Changes**

`normalize_new_messages(messages)`、`build_append_only(messages, tools, *, max_input_tokens)`、`compute_prefix_budget(...)`、`PromptPrefixViolation`。

**Tests**

- 历史超过 recent_history 配额但没超过总预算时仍完整保留。
- 单字改动、插入、tool 顺序变化被检测；正常尾部追加通过。
- 多工具结果未配齐拒绝；opaque continuation 不变。
- 中英文长输出规范化稳定；各种小窗口/输出预算组合无负预算。

**Acceptance Criteria**

同 epoch 普通请求结构不变量可在 Provider 前强制检查，旧 ContextEngine.build 测试保持通过。

**Implementation Record**

- 从 `ContextEngine._normalize_messages` 提取共享单消息规范化；`normalize_new_messages` 只脱敏/限长 `content`，工具 arguments 与 Provider continuation 保持原样，legacy `build` 的整体脱敏和原历史选择逻辑不变。
- 新增 `build_append_only`：校验 assistant tool call 与 tool result 完整且顺序匹配，按全量 messages + tools 估算窗口，超预算抛 `ContextBudgetError`，通过时按原序深拷贝返回，不做历史筛选、压缩或失效值替换。
- 新增纯函数 `compute_prefix_budget` 与 `PrefixBudget`，按任务/Provider 声明窗口、输出预算、安全余量、压缩 reserve 计算 ordinary/soft/memory/summary 上限；过小配置显式报错，离线模式按任务上下文预算计算。
- append_only 的 `prepare_request` 跳过 epoch materialize 与 V1 publication 注入，逐消息检查冻结 epoch 前缀、上一请求指纹向量、工具定义顺序和 Provider binding；违规在诊断及 Provider 调用前抛 `PromptPrefixViolation`，错误只包含原因、位置和截断摘要，不包含消息正文。已准备请求的边界以 SHA-256 指纹记录。
- 新增测试覆盖完整历史预算、工具组缺项、opaque continuation、英文/CJK 限长、正常追加、单字符修改、插入消息、工具排序、Provider binding 变更及小窗口预算。
- 验证：PPS-04 相关集合（context、prefix stability、coordinator、epoch、memory publication）51 项通过；全量 pytest 为 590 passed、1 skipped、3 failed。3 项失败均在 `tests/integration/test_session_cli.py`，隔离重跑可复现：两项 Click 测试访问未单独捕获的 stderr，一项交互中断预期退出码 130 而实际为 2；`ruff check src tests` 与 `mypy src/patchloop` 通过。

### PPS-05：压缩 source 和有界 epoch

**Goal**

在完整普通请求超阈值前复用旧请求压缩，避免摘要累积。

**Files / Symbols**

```text
src/patchloop/prompt_cache/epoch.py::CacheEpoch/CacheEpochSnapshot
src/patchloop/prompt_cache/coordinator.py::prepare_compression/complete_compression
tests/unit/test_cache_epoch.py
```

**Implementation**

1. 为新模式保存 root count；旧模式 rollover 保持原行为，新模式只保留 root+最新 summary。
2. prepare_compression 显式接 source request 消息与未发 suffix 边界，不重新检索、不重建 memory。
3. 严格 JSON/内容/预算验证；先构造候选新 epoch，验证通过才 commit。
4. 实现 soft/hard 失败决策、same-source defer；generation ID 有界。

**Interface Changes**

`CacheEpoch.rollover(..., replace_summary: bool = False, root_prefix_message_count: int | None = None)`；新增摘要验证函数；Coordinator compression preparation 带 source request ID/count。

**Tests**

- source 包含原 memory snapshot/delta，完整旧请求是压缩请求前缀。
- 连续 10 次压缩只有一条当前 summary；root 完全相同。
- 未发用户输入/工具结果完整保留；无效/no_gain 不更改 state。

**Acceptance Criteria**

不依赖 dropped_steps 触发；单元测试证明压缩不会丢失未发送 suffix，且输出可预检。

### PPS-06：Runtime 与恢复整合

**Goal**

在真实执行、审批、新输入和崩溃恢复中执行同一追加协议。

**Files / Symbols**

```text
src/patchloop/runtime.py::_run_owned/_advance_once/_compress_epoch
src/patchloop/runtime.py::_consume_pending_inputs/_supersede_response_for_inputs/_checkpoint
src/patchloop/persistence.py::checkpoint 投影与 PGW 请求接口
tests/integration/test_prompt_prefix_recovery.py（新增）
tests/e2e/test_prompt_prefix_runtime.py
```

**Implementation**

1. 前置依赖：PGW-07/08 的持久化及 Runtime 两条请求路径接入完成；按 4.6 接口操作，不另写 HTTP/request journal。
2. 重排 _advance_once：先恢复已保存响应，后生成新候选；各消息 append 入口调用同一规范化辅助函数。
3. candidate 预算通过后，把实际 request messages、publication 与提交边界一起 checkpoint；request 创建失败不发 Provider。
4. 将新模式压缩移到普通请求前；删除新模式下 step 末尾 dropped_steps 分支，旧布局继续原路径。
5. assistant 从完整 response 复制 continuation；所有 tool 结果齐备后才追加 Turn/记忆。复用已有 call/effect 和 consumed sequence 幂等键。
6. 压缩和普通 usage 均沿用 PGW request 去重，恢复不会累计两遍。

**Interface Changes**

Runtime 新增内部 `_append_prompt_messages(...)`、`_prepare_append_only_window(...)`；其余调用 PGW 已定义的 `_request_model` 和 Store request 接口。

**Tests**

- 请求前 checkpoint 后崩溃；响应落库后 checkpoint 前崩溃；恢复请求不重复发布记忆。
- 两个工具中第一个完成后崩溃；恢复第二个，结果顺序和配对正确。
- 等待审批、拒绝、新输入取消未领取 Effect，各路径不夹断工具组。
- 压缩响应已存但 epoch 未提交；压缩期间新输入；lease 丢失。
- 重放 usage 只计一次，未知 attempt 保持未知。

**Acceptance Criteria**

连续运行与故障恢复的对应普通请求前缀一致；不重复工具副作用；PGW/SRF 相关测试无回归。

**Implementation Record**

- Runtime 的 append_only 入口使用完整已规范化 transcript；根前缀、继承历史、assistant/continuation、工具观察和新 Turn 在入流时处理，普通请求不再经过旧布局的历史重建与裁剪路径。
- 记忆 V2 preview 与实际请求消息、已提交消息边界一起保存；恢复先识别已准备请求并复用输入，不重新检索或重复发布。checkpoint 已写但 request 尚未创建也能恢复，不将尚未创建的请求误计为未知用量。
- 请求前通过 PPS-05 接口压缩，source 精确来自最近一次普通请求，未发 suffix 与本轮候选记忆分别处理。压缩身份使用现有 Provider journal，响应先落库，再提交 root + 最新 summary + suffix + snapshot；没有新增请求表或第二份 transcript。
- 压缩期间到达的 Turn 在 epoch 提交后消费一次，并同步当前 Step 的 input revision；无效摘要按 soft/hard 预算继续或暂停，相同 source 的失败不会重复自动压缩；lease 丢失与 PGW 中断立即传播。
- Coordinator 支持按已记账 request 跳过 usage 累加，普通 usage 与 cache usage 共用 checkpoint 边界；压缩 usage 也使用既有 request/attempt 幂等键。工具恢复继续沿用 Effect/call ID，审批、拒绝和新输入取消路径补齐工具组后才追加 user 消息。
- 开发时检查确认 PGW-07/08 所需的 `_request_model`、`begin_provider_request`、`commit_provider_response` 和 Effect batch 接口已存在，本任务直接复用。

### PPS-07：前缀诊断和最终 payload 验证

**Goal**

能定位“哪一条旧消息改变”，并验证 Provider 编码没有撤销 Runtime 的保证。

**Files / Symbols**

```text
src/patchloop/prompt_cache/diagnostics.py::CacheDiagnostics/CacheLayoutTrace/Snapshot
src/patchloop/evaluation/cache.py::RealProviderCacheCollector
tests/unit/test_cache.py
tests/unit/test_deepseek_provider.py / PGW adapter 契约测试
```

**Implementation**

1. 实现 4.7 消息摘要向量、tools 原顺序比较、ordinary/compression/epoch 分类。
2. 区分旧 JSON LCP、规范化消息级指标、供应商真实 hit/miss，恢复不伪造精确 token LCP。
3. transport fixture 捕获 Chat messages/Responses items，验证旧片段逐项相同，包括 reasoning、call IDs 和工具定义。
4. 同一输入经 checkpoint round trip 后参数字符串序列化稳定；不要为获得指标重新排序实际消息。

**Interface Changes**

4.7 所列 Trace/Snapshot 可空兼容字段；旧报告缺字段时 PPS 状态为 unavailable。

**Tests**

- 工具列表调序不能被 canonical sorting 掩盖。
- 跨进程相同请求、追加请求、单条篡改定位准确。
- adapter 正常扩展通过；故意合并/改写旧消息的 fake adapter 被测试检出。

**Acceptance Criteria**

结构诊断恢复前后一致，日志无原始正文/秘密；模拟指标不会写进真实 usage。

### PPS-08：A/B 验收门禁

**Goal**

用任务质量、整体费用和供应商数据决定是否发布默认值。

**Files / Symbols**

```text
src/patchloop/evaluation/cache.py::Runtime fixture/RealProviderCacheCollector
src/patchloop/evaluation/gates.py::CacheAcceptanceEvaluator
src/patchloop/cli.py::benchmark_cache/validate_cache_gates
tests/unit/test_cache_evaluation.py/test_cache_gates.py
benchmarks/results/pps_prefix_acceptance.json（实施时生成，不手填）
```

**Implementation**

1. 给现有 benchmark-cache 增加 `--suite prefix-runtime`，validate-cache-gates 增加 `--profile pps`；PCO 旧接口保留。
2. 结构套件走实际 Runtime，不新增生产缓存模拟器；输出 cold/ordinary/compression/epoch/restore 的样本数和指标。
3. 真实 collector 按 request_id 关联 usage 与布局，不按事件位置猜场景；记录模型/endpoint 指纹、输入预算、价格版本和开始时间。
4. 按 8 节固定门禁评估；证据不足是 unverified，不能当 pass；不沿用硬编码历史 0.362 baseline。

**Interface Changes**

Cache report 增加 PPS 指标字段及 schema 标识；`evaluate(..., profile="pps", baseline_report=...)`，老默认 profile 保持。

**Tests**

- 模拟数据拒绝实测门禁；缺 cache usage/未知费用不通过费用验收。
- 加权汇总正确；压缩 token/费用和未知 attempt 不遗漏。
- 质量回退即阻止 default rollout，即使命中率很高。

**Acceptance Criteria**

报告能独立解释结构正确性、真实收益和未验证项，不用单一 hit rate 掩盖成本或质量。

### PPS-09：新任务默认切换与回退

**Goal**

确保用户正常启动的任务实际使用已验证的新模式。

**Files / Symbols**

```text
src/patchloop/domain.py::TaskExecutionConfig
src/patchloop/cli.py::run_task/start_session_task
src/patchloop/evaluation/gates.py::CacheRolloutPolicy
tests/integration/test_cli.py/test_session_cli.py
tests/unit/test_prompt_layout.py
README.md
```

**Implementation**

1. PPS-08 发布门禁通过后统一新 Task 默认 append_only；CLI 两入口提供 `--prompt-cache-layout append_only|stable|legacy`。
2. PPS rollout candidate 显式为 append_only，旧 PCO candidate 不被无关修改。
3. 更新默认断言和说明；resume 继续用原 Task 配置，不能切布局。
4. 文档给出新任务回退 legacy 命令及真实/估算指标区别。

**Interface Changes**

Session start 增加布局选项；Task 新建默认值变化。旧 Task 解码不变化。

**Tests**

- run 与 session start 默认一致；显式 legacy/stable 生效。
- 缺布局旧 JSON 恢复仍 legacy；活动新任务恢复不读当前默认。

**Acceptance Criteria**

通过 CLI 创建的任务报告 layout=append_only；回退不依赖数据库迁移，不虚称旧二进制可读新任务。

## 7. Implementation Order

```text
PPS-01 → PPS-02 → PPS-03 → PPS-04 → PPS-05
                              ↓
                   PGW-07/08 完成 → PPS-06
PPS-02 → PPS-07（契约部分可提前，adapter 集成等待 PGW）
PPS-06 + PPS-07 → PPS-08 → PPS-09
```

PPS-03 与 PPS-07 的 diagnostics 部分可以在 PPS-02 后并行；PPS-04 与 PPS-05 都涉及 coordinator，实施时顺序执行以减少冲突。并行描述是开发依赖信息，不要求启动多 Agent。

## 8. Verification

### PPS-06 实施验证（2026-09-15）

- 新增 `tests/integration/test_prompt_prefix_recovery.py`，覆盖请求创建前、响应落库后、usage checkpoint 后、首个工具提交后的故障恢复，continuation 保留、未知 attempt、无效摘要 soft/hard 分支及 same-source defer，以及压缩响应落库后的崩溃/lease 丢失与压缩期间新输入。
- 实际 Runtime 连续六个工具轮次覆盖 A→B→A→B→C 检索变化，逐条验证前序请求及工具定义保持相同；扩展 Session 新输入和审批/拒绝测试至 append_only。
- 最终定向集合：PPS Runtime/recovery、PGW runtime/persistence、Session migration/runtime、Effect/approval/security、coordinator/epoch/prefix/dependency，共 110 项通过。`ruff check src tests`、`mypy src/patchloop`、`git diff --check` 通过。
- 本轮全量 pytest 在追加最后几个定向案例前执行，结果 712 passed、2 skipped、1 failed。失败为未修改的 `tests/integration/test_provider_transport.py::test_transport_settings_are_explicit_and_http_urls_are_restricted`；单独重跑复现。本机 `ssl.get_default_verify_paths().cafile` 为 `None`，transport 使用 `verify=True`，该测试断言必须为 `SSLContext`。跳过项为未配置真实 Provider 验收和 Windows 符号链接不可用。
- 未调用真实付费 Provider；默认布局和 PPS-07～09 门禁保持原状态。

### PPS-05 实施验证（2026-09-14）

- 新增 append-only 压缩路径：压缩 source 精确复用最近一次已提交请求（含当时 memory snapshot/delta），未发工具组和用户输入作为 suffix 原序保留；候选 memory 状态不进入 source，并在新 epoch 中生成单条 V2 snapshot。
- 新 epoch 只保留原 root 和最新严格 JSON 摘要。摘要校验覆盖精确字段、字段类型、安全过滤和 token 上限；候选窗口只有满足 ordinary 上限且严格小于压缩前窗口时才提交。
- source request ID、消息数、epoch generation 和消息指纹在准备、完成及失败处理时交叉校验。相同 source 的失败会被 defer；普通预算内可继续旧 epoch，超 hard limit 则要求暂停。旧 stable rollover 路径保持原行为。
- 定向回归集合（context、cache/diagnostics、epoch、coordinator、memory publication、prompt-cache import/dependency/usage）65 项通过；`ruff check src tests` 与 `mypy src/patchloop` 通过。
- 全量 pytest：597 passed、1 skipped、6 failed。3 项 `tests/integration/test_session_cli.py` 失败仍是 Click stderr 捕获和 Windows 交互退出码问题；另 3 项 coding benchmark 测试在首次验证失败后，fake agent 未先调用 `update_plan`，被 run_tests 权限门禁拒绝。上述失败路径不涉及 prompt-cache 组件。
- PPS-05 当前只交付纯 coordinator/epoch/publication 路径；Runtime 请求前压缩、崩溃恢复及真实请求关联仍属于 PPS-06。

### PPS-01 实施验证（2026-09-13）

- `tests/unit/test_prompt_prefix_stability.py`、`tests/e2e/test_prompt_prefix_runtime.py` 和缓存评估测试验证实际 Runtime 请求基线；PPS-01 定向测试 13 项通过，扩展相关回归集合 54 项通过。
- 全量 pytest：551 passed、1 skipped、1 failed。失败是 `tests/integration/test_execution_takeover_recovery.py::test_takeover_terminates_verified_live_command_before_new_writer`，Windows `TerminateProcess` 返回拒绝访问（错误码 5）。
- `ruff check src tests` 与 `mypy src/patchloop` 均通过。

### PPS-02 实施验证（2026-09-13）

- 新增 append_only 状态契约测试 19 项通过；定向集合（prefix stability、layout、coordinator、publication、context、epoch、cache、evaluation、gates、dependencies、storage、session models）73 项通过；session 迁移/runtime/store 集合 136 项通过。
- 全量 pytest：571 passed、1 skipped（Windows 符号链接不可用）、0 failed；PPS-01 记录的 takeover 失败在本轮环境未复现。
- `ruff check src tests` 与 `mypy src/patchloop` 均通过。
- 本机默认 pytest 临时目录报 WinError 5；全量运行需要仓库外 basetemp（例如 `--basetemp=D:/codes/git/.pl_pytest_tmp`），仓库内 basetemp 会让 `test_committed_retrieval_report_is_reproducible` 检索到临时文件而失败。

### PPS-03 实施验证（2026-09-13）

- Prompt prefix stability、layout、coordinator、publication、context、epoch、cache、cache evaluation、gates、dependency/import 及 PCR baseline 测试共 75 项通过。
- Storage、Session checkpoint 和 Runtime 迁移/恢复测试另有 51 项通过。
- `ruff check src tests` 与 `mypy src/patchloop` 通过。

### 本次方案调研已执行

- 上一轮：prompt layout/coordinator/epoch/publication 相关测试 17 项通过。
- 本轮：context、prompt_cache dependencies、cache、cache evaluation、usage、PCR baseline 相关测试 33 项通过。
- 三轮 Runtime + FakeProvider 复现 legacy/stable 的消息前缀问题；未发真实付费模型请求。
- 本次只创建方案；这些结果是现状基线，不是 PPS 实现验收。

### 开发完成后的命令

在项目根目录 PowerShell 执行。新增测试和 CLI 选项由对应任务创建后才可运行。

```powershell
# targeted tests
.\.venv\Scripts\python.exe -m pytest tests/unit/test_prompt_prefix_stability.py tests/unit/test_prompt_layout.py tests/unit/test_prompt_cache_coordinator.py tests/unit/test_memory_publication.py tests/unit/test_context.py tests/unit/test_cache_epoch.py tests/unit/test_cache.py tests/unit/test_cache_evaluation.py tests/unit/test_cache_gates.py tests/unit/test_prompt_cache_dependencies.py -q

# Runtime、恢复与安全
.\.venv\Scripts\python.exe -m pytest tests/integration/test_prompt_prefix_recovery.py tests/e2e/test_prompt_prefix_runtime.py tests/integration/test_session_migration.py tests/integration/test_session_runtime.py tests/e2e/test_effect_recovery.py tests/e2e/test_approval_recovery.py tests/e2e/test_security_observability.py -q

# full regression
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src/patchloop

# 新增的实际 Runtime 离线验收入口
.\.venv\Scripts\python.exe -m patchloop benchmark-cache --suite prefix-runtime --output benchmarks/results/pps_prefix_runtime.json
```

PGW adapter 契约测试随其文件落地加入 targeted 集合，不用尚不存在的文件名假装已验证。

### 硬性结构与恢复门禁

- 至少 6 个连续工具轮次、3 种检索变化样本；所有同 epoch ordinary 请求 `previous_request_is_prefix=true`，tools/binding 不变，允许缺样本的比例为 0。
- 所有 compression 请求完整复用声明的 source；所有 epoch 重建都有原因和 request 关联。
- checkpoint/进程恢复与不停机运行使用同样 Provider 输入；请求、记忆发布、usage 无重复，工具副作用最多一次仍由 SRF Effect 保证。
- 10 次压缩后摘要数量为 1，所有普通请求 <= ordinary_limit；输入过大显式失败，不偷偷丢用户要求。
- 失效事实不再作为当前事实使用；秘密泄漏、越界修改、审批绕过均为 0。

### 真实 A/B 与发布门禁

使用现有 contract-migration 场景和至少一个长工具输出任务，在独立工作副本执行 legacy 与 append_only 各 3 次，固定模型、endpoint、工具定义、预算、任务内容和价格版本，交错顺序。每一对使用相同的固定任务前缀标识，不在每轮 Prompt 注入时间/随机数。报告标记不能保证供应商冷缓存隔离，不能将顺序优势冒充收益。

实测费用预算由任务启动者明确设置并记录；不得为本次“写方案”擅自执行付费 A/B。实施时使用正常任务调用和现有 Trace collector，禁止额外保活/预热请求。

发布 PPS-09 的门禁固定为：

| 项目 | 门禁 |
| --- | --- |
| 结构 | 上述普通请求前缀保持率 100%，缺失数据不通过 |
| 质量 | 两类任务全部 public/hidden 验证通过；成功率不低于当批 legacy；约束/关键事实召回不低于当批基线；失效事实使用、秘密泄漏、越界修改为 0 |
| 实测来源 | 每个请求可关联 Provider usage；cache hit/miss 完整且一致；压缩与普通请求都统计 |
| 缓存目标 | 排除每个 epoch 第一条普通请求后的加权 warm 命中率 >= 70%；全部请求加权命中率至少比当批 legacy 高 20 个百分点 |
| 费用目标 | 同任务总已知模型费用的三次运行中位数至少下降 20%；含压缩/attempt；任何 unknown 费用使费用 gate 为 unverified |
| 恢复 | 离线故障矩阵全部通过；至少一次实际任务 pause/resume 后普通前缀验证通过 |

70%/20 个百分点/20% 是本项目拟定的发布目标，不是 DeepSeek 的服务保证。结构正确但供应商收益未达标时保留 append_only opt-in，报告 unverified/未达标及 cold、epoch、服务端 best-effort 分解，不撤销结构测试、不伪造命中。新默认切换等待实测通过。

## 9. Blockers

**设计未决问题：None。** 请求排列、状态归属、预算、压缩失败行为、兼容与验收已在本方案确定。

**实施前置依赖（2026-09-15 更新）：** PGW-07/08 的请求持久化与 Runtime 恢复整合、PPS-07 诊断以及 PPS-08 验收工具已完成。PPS-09 默认切换仍等待真实 A/B 门禁；不另建 request journal。

**发布待验证：** 用户后续已配置本地代理并授权服务器真实测试。三个不同代码修订的试跑批次已完成，不能合并冒充同一修订各三次的正式配对验收。最新批次前缀结构通过，但 append_only 任务超时且隐藏测试未通过；缓存 miss 缺失、零价预算也无法证明收益目标。保留 legacy 默认和 append_only opt-in。

### PPS-07～09 开发记录（2026-09-15）

- PPS-07：Snapshot 保存规范化消息摘要向量，Trace 区分普通请求、压缩和 epoch 边界，记录完整消息前缀、有序工具及 binding 比较、请求/source 关联。旧 JSON LCP 及 canonical 指纹保持原语义，旧字段缺失时不伪造新指标。Chat/Responses transport 测试覆盖 continuation、原始参数字符串及 checkpoint round trip，并检出故意改写旧输入的 adapter。
- PPS-08：新增 `benchmark-cache --suite prefix-runtime` 和 `validate-cache-gates --profile pps`。离线套件运行实际 Runtime、三次重复及普通/长期压缩/恢复场景，禁止模拟缓存 usage。真实 collector 按 request/attempt 关联，检查缺失用量、未知费用与配对批次；按加权命中率、总费用、质量和恢复分别给出门禁结果。
- PPS-09：Task 和两个 CLI 入口共用默认常量，支持显式 append_only/stable/legacy；PPS rollout candidate 为 append_only、fallback 为 legacy。README 提供离线命令、后续真实 Trace manifest、质量证据及回退说明。默认常量仍为 legacy；缺少真实证据时，即使显式请求启用也不能通过发布门禁。
- 非 reasoning 压缩的输出上限使用 summary budget，并随压缩 request ID 持久化，保证恢复请求参数一致；reasoning 和旧 checkpoint 保持原输出参数。
- 离线报告由 CLI 生成于 `benchmarks/results/pps_prefix_runtime.json` 和 `benchmarks/results/pps_prefix_acceptance.json`；真实收益、费用及真实恢复证据保留 unverified，不执行付费调用。
- 最终验证：全量 pytest 为 746 passed、2 skipped、1 failed；失败为既有 transport CA 测试，本机 `ssl.get_default_verify_paths().cafile` 为 None。仅在该测试进程中用 certifi 设置 `SSL_CERT_FILE` 后复验 1 passed，未修改 transport 或系统配置。跳过项是真实 Provider 验收未配置和 Windows 符号链接不可用。`ruff check src tests`、`mypy src/patchloop` 及 `git diff --check` 通过。
- 离线三轮各包含 6 个工具轮次、3 种检索状态；每轮长期场景完成 17 次压缩，最多一条摘要，三轮 checkpoint 恢复均通过。验收报告的普通前缀、压缩 source、场景覆盖和摘要预算门禁通过；缺少独立质量/完整故障矩阵证据文件的汇总项与真实门禁仍保留 unverified，具体恢复测试由上述 pytest 覆盖。
- 收尾核对补充 `restored_request_count`，分别从实际 Runtime 恢复标记和真实 Trace 的恢复后请求统计，重复事件不重复计数。相关评估、PPS 门禁、旧基线和 CLI 回归 37 项通过，ruff/mypy 通过；离线报告已重新生成。

### 服务器真实试跑（2026-09-15）

- 环境：Linux 服务器独立目录 `/root/autodl-tmp/pps-openai-20260915`，Python 3.12.14，通过 SSH 回环端口转发访问本机代理，模型 `gpt-5.6-luna`、推理强度 high、标准 Chat Completions。密钥经用户明确授权单独传输，未进入 Git。
- 三轮 pilot 各运行合同迁移和长工具输出两类场景的 legacy/append_only，共 12 个任务。这是三个修订的调试试跑，不是同一批次、同一修订各三次的正式收益验收。pilot-01 为 `d8adeb7`，pilot-02 为 `4dfc3ed`，pilot-03 为 `79bb981`。
- 实验发现并修复：标准 Chat 内部推理缺少网关能力声明；压缩保留工具定义但默认 tool_choice=auto，真实模型返回工具调用而非摘要；暂停任务在释放 lease 后写报告产物导致 LeaseLost。现已增加 chat_internal 模式，压缩请求使用 tool_choice=none（工具定义与 source 消息仍保持），活动任务的报告产物延后到结束时保存。相关回归包括 Provider/CLI 69 项、编码/恢复 45 项及 gate 16 项通过。
- pilot-03 每个任务输入预算 24,000、累计输入上限 500,000、累计输出上限 80,000、最多 40 步；Task 原有时间预算为 300 秒，驱动进程另有 900 秒超时。网关上下文声明 128,000 为暂定运行配置，不是已核实的上游模型规格。价格按用户确认的本地零价预算记录。

| 最新试跑场景 | legacy | append_only |
| --- | --- | --- |
| 合同迁移 | 完成；隐藏测试 5/5；188,307 输入 token | 第 30 步超时；隐藏测试 3/5；270,770 输入 token；9 次压缩 |
| 长工具输出 | 完成；隐藏测试 5/5；320,323 输入 token | 第 24 步超时；隐藏测试 0/5、尚无修改；246,733 输入 token；9 次压缩 |

- append_only 的 34 个同 epoch 普通请求比较全部保持前缀；18 次压缩 source 复用检查通过；真实中断/恢复及审批后恢复的前缀检查通过。合同迁移遗漏纯空白 order_id 的拒绝要求，表明结构正确尚不足以保证摘要后的任务质量。长输出的较低输入量来自任务未完成，不能宣称节省。
- 原始响应提供 input/output 和 cache hit，但未提供 cache miss；本批严格缓存命中率仍不可验证。没有非零成本基线，不能证明费用下降 20%。本批未提供完整质量/安全与离线故障矩阵证据文件，相关缺项保持 unverified；已证实的任务成功率退化明确为 fail。
- 结果文件：`benchmarks/results/pps_server_pilot01_*`、`pps_server_pilot02_*`、`pps_server_pilot03_*`。最新正式评估输出为 `pps_server_pilot03_acceptance.json`，passed=false、rollout=legacy。下一步应先改进摘要约束保真及压缩开销，再安排同一修订下的三次配对验收。

### PGW/PPS 联合修复与暂停点（2026-09-15）

- 修复提交 `c426f33`：V2 发布预算计入消息序列化开销；新工作记忆按字段和列表项发布增量，忽略 revision，保留旧 V2 消息体与指纹的兼容性。压缩指令保留实际约束、边界值和精确错误文本，并说明已完成探索不会因压缩重置。Trace 补充压缩前后的预算证据。真实质量和费用改善尚待验证。
- 标准 Chat 返回有效 `prompt_tokens_details.cached_tokens` 且与所选命中计数一致时，由输入量减去命中量得到 miss，标记为 derived。自定义计数、冲突或缺失输入仍为 unknown，不回填历史试跑。字段语义参考 [OpenAI Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)。零价格配置无法验证费用下降目标。
- 本地全量回归：766 passed、2 skipped、1 failed；唯一失败为沙箱禁止恢复测试终止子进程，同一测试在具备进程权限后单独复验通过。随后 Session 验收启动路径修复 `dbc7f04` 的相关回归 40 passed、1 skipped；CLI env-file 凭据状态修复回归 17 passed。ruff、mypy 和 diff 检查通过。
- 修复后的离线 Runtime 三次重复生成 `benchmarks/results/pps_joint_offline_runtime.json` 和 `pps_joint_offline_acceptance.json`：普通前缀、压缩 source、场景覆盖与有界 epoch 检查通过；未附完整离线恢复证据，真实配对、质量、恢复及收益仍为 unverified。离线结果不等于真实 A/B。
- 用户因 AutoDL 余额不足暂停服务器测试，待充值完成后通知恢复。当前配置 PGW 两次真实 Session 试用通过，第三次因 SSH 中断尚未取回结果；新 PPS 真实配对尚未开始。恢复后先读取已有结果，再补齐 PGW 复验，执行两场景各一对小规模试跑；质量通过后再进行同一修订各三次正式配对。默认布局保持 legacy。

### 充值恢复后的联合复验结果（2026-09-15）

- 服务器恢复后在同一代码修订 `8fecd4f` 完成当前 `gpt-5.6-luna` profile 三次真实 Session/工具往返，全部通过；完整 PGW 离线矩阵也通过。结果为 `pgw_configured_profile_r2.json`、`pgw_joint_offline_acceptance.json`。完整三 Provider 真实矩阵仍为 unverified。
- pilot-04 两场景各一对均完成，公开检查 4/4、隐藏测试 20/20 通过，仅修改 order_service.py。合同迁移 append_only 和长输出 legacy 曾停在只读 `git status --short` 审批，已按相同规则补充批准、恢复及复测；初始 results.json 和补充 supplemental-results.json 分开保留。试跑脚本现只增加该精确命令的自动审批，保留正常审批记录。

| 场景与布局 | 完成耗时（秒） | 输入 token | 压缩次数 | 重复证据读取 | 隐藏测试 |
| --- | ---: | ---: | ---: | ---: | --- |
| 合同迁移 legacy | 96.68 | 189494 | 0 | 0 | 5/5 |
| 合同迁移 append_only | 223.34 | 245830 | 5 | 0 | 5/5 |
| 长输出 legacy | 177.71 | 375211 | 0 | 5 | 5/5 |
| 长输出 append_only | 248.42 | 232714 | 6 | 0 | 5/5 |

- 34 次普通前缀比较、11 次压缩 source 检查和真实恢复前缀检查通过。候选压缩调用分别耗时 82.21、105.62 秒，质量改善尚未带来耗时改善。
- 完成请求的 hit/miss 全部完整，但主动 SIGINT 取消一次在途请求，缺少该 attempt 的 usage，严格缓存与费用门禁必须为 unverified。仅对已知普通请求计算的加权 warm 命中率为 58.82%，不含未知 attempt，不能替代正式指标，也低于 70% 目标。
- 结果保存为 `benchmarks/results/pps_server_pilot04_{cache,diagnosis,quality,acceptance,provenance}.json`。原始 trace、SQLite、审批与测试日志保存在服务器 `/root/autodl-tmp/pgw-pps-20260915-r2/pilot-04`，本地副本在忽略目录 `.patchloop/pgw-pps-r2/pilot-04`。
- 本批仅一对且含补充人工操作，不能视为正式三次配对。基于已知 warm 指标与压缩开销，暂不启动正式 12 任务批次。下一步降低 epoch 压缩开销并改善命中率；完整质量/安全、故障矩阵汇总及非零费用证据仍待补齐。默认继续 legacy。

### AOP 后续真实入口（2026-09-16）

- AOP-01～08 已完成。`append-only-overhead` 与 `aop` profile 负责 L0；`benchmarks/run_pps_server.py` 默认只进行报告、源码、fixture、证据摘要、场景和预算预检，输出模型请求数 0，且不加载凭据。
- 真实执行必须显式提供 `--execute-real` 和三项批次预算。runner 按剩余预算顺序启动，固定策略写入每个 trial manifest；中断、未知 usage、预算不足、真实 Task 状态未完成或验证失败都会保存 partial 原因并停止扩量。
- 正常成本样本只在无在途 Provider attempt 的静止边界进行持久化 pause/resume；`--inject-inflight-cancel` 是独立故障入口，产生的未知用量不得并入成本样本或通过补跑覆盖。
- 当前停在 L0 归档完成、L1 尚未执行的边界。进入 L1 前须在提交后的同一源码修订重新生成 readiness；L1 两场景各一对通过后才能进入 L2 三次正式配对，PPS 发布门禁通过后才允许评估默认切换。具体命令与恢复步骤见 README。
- L1 不再使用历史零价配置。`gpt-5.6-luna` 按 2026-09-16 OpenAI 公布价格锁定普通输入 0.20、缓存读取 0.02、缓存写入 0.25、输出 1.20 美元/百万 token；若 Chat 代理未透传 `cache_write_tokens`，费用与批次用量视为 unknown 并停批。24K 单次上下文不会进入官方 272K 长上下文加价档。
