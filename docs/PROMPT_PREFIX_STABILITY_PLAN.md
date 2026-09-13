# Prompt 前缀稳定性修改方案

调研日期：2026-09-13。状态：方案完成；PPS-01 已完成，后续任务尚未实施。任务前缀：PPS。

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

### PPS-02：追加状态和兼容读取

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

### PPS-01 实施验证（2026-09-13）

- `tests/unit/test_prompt_prefix_stability.py`、`tests/e2e/test_prompt_prefix_runtime.py` 和缓存评估测试验证实际 Runtime 请求基线；PPS-01 定向测试 13 项通过，扩展相关回归集合 54 项通过。
- 全量 pytest：551 passed、1 skipped、1 failed。失败是 `tests/integration/test_execution_takeover_recovery.py::test_takeover_terminates_verified_live_command_before_new_writer`，Windows `TerminateProcess` 返回拒绝访问（错误码 5）。
- `ruff check src tests` 与 `mypy src/patchloop` 均通过。

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

**实施前置依赖：** 当前工作区有 ProviderRequest/Binding/Continuation 契约，但 Runtime 仍直接 `provider.complete`；PGW-07/08 请求持久化和 Runtime 接入尚不能由当前已读代码确认完成。因此 PPS-06 完整恢复集成及 PPS-09 默认切换等待这两项完成；PPS-01～05 和 PPS-07 纯诊断部分不受阻。执行模型不要自行另造一套 request journal。

**发布待验证：** 尚无本次 append_only 的真实 A/B 数据，不能把历史基线或 FakeProvider 结果当作缓存收益验收。付费试验预算和执行属于后续实施阶段，不阻塞本方案交付。
