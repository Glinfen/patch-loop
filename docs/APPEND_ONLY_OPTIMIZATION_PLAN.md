# append_only 开销优化开发方案

调研日期：2026-09-15。代码基线：`69738d2`；真实证据执行修订：`8fecd4f`。状态：方案已完成；AOP-01～08 已完成。离线优化阶段未执行真实模型评测，后续 L1 仍须显式授权启动。

本方案遵循 [PLANNING_GUIDE.md](PLANNING_GUIDE.md)，承接 [PPS 方案](PROMPT_PREFIX_STABILITY_PLAN.md)，服务于 [第二阶段总路线](PHASE_2_DEVELOPMENT_PLAN.md)。用户已明确：真实评测等 append_only 优化后再进行。

## 1. Problem

### Current Problem

pilot-04 已证明两个小型场景的代码质量恢复：legacy/append_only 四个任务均完成，隐藏测试 20/20 通过。它没有证明大型仓库实用性，也未通过 PPS 发布门禁。

| 场景 | legacy 秒数 / 输入 / 输出 | append_only 秒数 / 输入 / 输出 | 候选压缩调用 / 摘要秒数 |
| --- | --- | --- | --- |
| 合同迁移 | 96.68 / 189494 / 1595 | 223.34 / 245830 / 6685 | 5 / 82.21 |
| 长工具输出 | 177.71 / 375211 / 4207 | 248.42 / 232714 / 7483 | 6 / 105.62 |

完成请求的未缓存输入合计由 510945 降至 174416，但候选输出增多、耗时增加。已知普通请求的加权 warm 命中率为 58.82%；一次主动中断留下未知用量 attempt，因此正式缓存和费用指标仍不可验证。上述数字只描述这批样本。

本轮代码与持久化证据核对发现一个尚未修复的数据流问题：

1. `MemoryManager.retrieve` 同时持有完整 `WorkingMemorySnapshot` 和 `working.render()`。
2. `CrossLayerMemoryRetriever._direct_selection` 对 render 字符串执行 `_truncate_to_tokens`，追加 `[layer budget truncated]`。
3. `LayeredMemoryContext.provider_projection` 把这段截断字符串作为单个 working_state.text 发给 Publisher。
4. `_working_state_entries` 只拆分有效 JSON；被截断的 JSON 解析失败后按旧整段条目发布，revision 和近期事件继续引发整段替换。
5. pilot-04 最终 checkpoint 仍含这种条目。11 次压缩触发时的新增记忆通常为两条消息，合计约 2400～3000 个估算 token。先前 Publisher 单测覆盖了有效 JSON，未覆盖真实检索截断链路。

另有三个已确认的开销因素：80% 固定软阈值；长输出场景重建后仍占约 10000 个估算 token，随后两步又触发压缩；reasoning 模式下摘要请求继承主请求输出上限，当前没有更短的摘要目标。具体收益须在实施后测量，不能把这些因素都称为已证明的因果贡献。

### Desired Behavior

- 工作状态以完整、稳定标识的条目进入模型上下文，字符截断不再破坏结构或触发整段替换。
- 同 epoch 普通请求保持完整前缀，尽量增加一次压缩后的有效工作轮次。
- 摘要保留实际约束、已完成工作和精确事实，同时避免重复复制运行日志。
- 普通、压缩、失败 attempt 和未知用量分别可见；离线优化完成后才恢复有限真实验收。

### Scope

只调整 append_only 的记忆投影、压缩调度、摘要目标、诊断、恢复兼容和评测前置检查。复用现有 Task、Checkpoint、MemoryManager、Publisher、Coordinator、Provider journal、CLI 和 gate。

### Non-Goals

- 本轮不实现 Skills、Workspace、TUI；其模块顺序由总路线维护。
- 不切换默认布局，不更换用户的 Luna/high，不调低推理强度，不猜测模型上下文规格或真实价格。
- 不在已提交消息中删除旧工具输出，不引入独立摘要 Provider、后台摘要、预热或缓存保活调用。
- 不降低第二阶段 30 任务 / 5 仓库 / 每项 3 次，以及原 PPS 质量和收益发布目标。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `memory/working.py::WorkingMemorySnapshot, WorkingMemoryItem` | 已有稳定 key、kind、pinned、完整 items；render 含 revision 和 recent_events | 从 Snapshot 生成模型条目，避免反解析 render |
| `memory/manager.py::MemoryManager.retrieve` | 将 snapshot 和 render 传入检索器 | 传递固定的投影版本；存储错误继续沿用现有 fallback |
| `memory/retrieval.py::CrossLayerMemoryRetriever._direct_selection` | 字符截断 direct memory，provider_text 与审计文本共用 | 增加结构化投影分支；按完整条目适配预算 |
| `memory/retrieval.py::LayeredMemoryContext.provider_projection` | record_id=None 时嵌套 text | 输出显式 working_memory 条目，审计投影保持独立 |
| `prompt_cache/publication.py::MemoryDeltaPublisher.preview, _working_state_entries` | V2 增量和完整 replay 已实现；有效工作记忆 JSON 使用列表 index | 新格式使用 item key；旧 V1/V2 消息与指纹原样保留 |
| `prompt_cache/coordinator.py::compute_prefix_budget` | 普通输入预留压缩指令；软阈值 80%；摘要上限最多 2048 | 保留硬预算，按版本选择调度参数与摘要目标 |
| `runtime.py::AgentRuntime._prepare_append_only_window` | 获取记忆、判断压缩、调用 PGW、提交重建状态 | 调用统一纯决策接口；避免 Runtime 与 Coordinator 各自判断阈值 |
| `prompt_cache/coordinator.py::_prepare_append_only_compression, complete_append_only_compression` | 精确复用最后请求，保留未发送后缀；新 epoch 原子替换摘要 | 增加重建空间预检、收益诊断和失败原因；不破坏事务顺序 |
| `providers/base.py::ProviderRequest` | 已有 purpose 和可持久化 max_output_tokens | 复用；本轮不新增摘要模型或 reasoning 覆盖字段 |
| `domain.py::TaskExecutionConfig`、`coordinator.py::AppendOnlyPromptState` | Task 和 Checkpoint 保存布局与恢复状态 | 增加优化策略版本并固定到任务生命周期 |
| `evaluation/cache.py::CacheBenchmarkRunner, RealProviderCacheCollector` | Runtime 离线 fixture、真实 request/attempt 关联 | 增加开销回归场景和按 purpose 统计，未知数据不补零 |
| `evaluation/gates.py::CacheAcceptanceEvaluator._evaluate_prefix` | PPS 结构、质量、缓存、费用门禁 | 新增独立离线 AOP 前置 gate；PPS 发布门禁仍独立 |
| `benchmarks/run_pps_server.py::main` | 两场景交错试跑，SIGINT 可能取消在途请求 | 实跑需显式启动和前置报告；成本试跑使用静止边界暂停 |
| `tests/unit/test_memory_publication.py` | 覆盖完整工作记忆拆分和旧格式升级 | 补充完整 Retriever → Publisher 的截断复现 |
| `tests/e2e/test_prompt_prefix_runtime.py`、`tests/integration/test_prompt_prefix_recovery.py` | 实际 Runtime 和恢复前缀测试 | 增加新策略、真实记忆预算、待压缩请求恢复 |

表中源码路径均相对于 `src/patchloop/`，明确写出 `tests/`、`benchmarks/` 的除外。

### Current Flow

```text
WorkingMemorySnapshot → render → 字符截断 → 嵌套 text
→ Publisher 尝试解析，失败时整段替换
→ Runtime 判断 80% 阈值 → Coordinator 再校验阈值
→ PGW 摘要请求 → 校验摘要 → root + summary + 未发送后缀 + snapshot
→ Checkpoint → 下一普通请求
```

## 3. External Research

调研以 2026-09-15 可访问的官方文档和源码为依据。下列 main/dev 链接会变化，不把上游当前默认值当作 PatchLoop 的配置。

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| Claude Code | [官方缓存说明](https://code.claude.com/docs/en/prompt-caching)：稳定层在前，变化追加；摘要请求复用原工具与历史；压缩后重建会话缓存 | 保持压缩 source 精确复用；评估摘要生成与下一阶段缓存重建两种成本 | 不照搬 Anthropic TTL、价格、权限默认或模型切换行为 |
| Qwen Code | [官方命令](https://qwenlm.github.io/qwen-code-docs/en/users/features/commands/)区分模型摘要与无模型快速压缩；[chatCompressionService.ts](https://github.com/QwenLM/qwen-code/blob/main/packages/core/src/services/chatCompressionService.ts)显式限制摘要输出，并尝试缓存共享 | 将结构化整理与模型摘要分开；摘要有明确目标；保留缓存兼容参数 | 不采用专用模型回退或多次隐式重试；不直接删除 PatchLoop 已提交历史 |
| OpenCode | [配置](https://opencode.ai/docs/config/#compaction)区分 auto、prune、reserved；[compaction.ts](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts)保护近期内容、按收益条件清理旧工具输出 | 显式预留重建空间；保留未发送后缀；诊断重建后的可用空间 | 不在同 epoch 原地剪除旧工具结果，不照抄上游阈值 |

### Lessons for PatchLoop

先减少上下文进入链路中的冗余，再减少模型摘要次数；本项目的 append-only 不变量优先于通用 prune 技巧。稳定结构不等于供应商实际命中，必须分别验证。动态 Skill 和工作区事件以后也应追加在会话尾部，不改写已冻结 system/tool 前缀。

## 4. Design

### Selected Approach

采用版本化 `balanced_v1` 候选策略：结构化工作记忆 + 固定预算投影 + 95% 软阈值 + 摘要内容目标 + 统一开销证据。既有 `baseline_v1` 行为保留，默认布局仍为 legacy。

### Key Decisions

#### 4.1 策略版本与状态归属

- `TaskExecutionConfig.append_only_optimization: Literal["baseline_v1", "balanced_v1"] = "baseline_v1"`。旧 Task 缺字段等同 baseline_v1。
- `AppendOnlyPromptState.optimization_version` 同名值；`last_compression_attempt_step: int | None` 保存软压缩退避状态；`compression_instruction: str | None` 与 `compression_summary_target_tokens: int | None` 保存待压缩请求的最终指令和内容目标。缺字段旧快照使用 baseline_v1。
- 新 CLI 参数 `--append-only-optimization` 只用于 `run` 和 `session start` 创建 Task；balanced_v1 必须与 append_only 同时指定，其他组合报配置错误。
- 同一 Task 不允许切换策略。resume 以 Task 和 Checkpoint 为准，二者冲突抛 `CheckpointSchemaError`；不得重写 pending 请求。
- 参数由 `prompt_cache/coordinator.py::AppendOnlyOptimizationPolicy.for_version(version)` 返回冻结配置，不提供分散的任意参数开关。实验修改需新版本号。
- 不新增数据库表；使用现有版本化 JSON。新二进制读取旧快照已覆盖；旧二进制读取新策略不作兼容承诺，回退使用新代码上的 baseline_v1 新任务。

#### 4.2 工作记忆保持结构与稳定身份

在 `memory/working.py` 新增冻结模型 `WorkingMemoryProviderEntry(key: str, kind: WorkingMemoryItemKind, value: str, pinned: bool)` 和纯函数 `project_working_entries(snapshot: WorkingMemorySnapshot) -> list[WorkingMemoryProviderEntry]`。key 复用 WorkingMemoryItem.key，不用数组位置、revision、时间或分数生成身份；模型投影序列化为 `{type:"working_memory", key, field, value}`，field 沿用 `_context_payload` 的 kind→label 映射。pinned 只供预算和审计，不成为授权声明。

- goal 已在用户消息中，不重复复制；constraints、prohibitions、plan、read_files、changed_files、active_errors、questions、key_evidence 使用已有 item 内容。
- 不发布 revision、recent_events、调用时间、分数。recent_results 只选最近两项成功/失败观察，按原 item key 排序输出；完整日志仍在原存储。
- 字符串内容先经过现有 SecretRedactor/UntrustedContentGuard。关键值不截断，JSON 只由结构化序列化生成。
- `RetrievalSelection.provider_items` 增加可选条目列表，默认 None。`MemoryManager.retrieve` 与 `CrossLayerMemoryRetriever.retrieve` 增加 `projection_mode="legacy"|"structured_v1"`，由固定 Task 策略选择。legacy/stable/baseline_v1 保持原路径。
- structured_v1 的模型投影不从 `selection.text` 反解析；审计文本可保留旧 render。审计 `_fit_render` 不得为了自身元数据开销移除已选模型条目；在该分支先固定模型选择，再裁剪审计展示元数据。
- 使用 `ContextEngine.estimate_message` 检查最终 V2 snapshot/delta 的 envelope 开销。按完整条目裁减，优先保留 constraints/prohibitions/当前 plan/active_errors，其次 read_files/changed_files，再 key_evidence/questions，最后 recent_results；同优先级按稳定 key 排序。
- pinned 核心超过可用消息预算时返回 `memory/working.py` 中新增的 `MemoryProjectionBudgetError(required_tokens, available_tokens, required_keys)`；Runtime 可以用已有压缩路径尝试释放总上下文，但单个 pinned 条目仍超限时进入现有上下文预算暂停，禁止将错误降级成空记忆或截断关键值。
- 原 WorkingMemorySnapshot 中已经被淘汰的信息不能由本次投影凭空恢复，相关事实仍依赖原始历史和摘要；本轮不宣称解决所有记忆召回问题。

#### 4.3 固定投影预算与 V2 发布兼容

- balanced_v1 每轮检索使用固定 `memory_message_limit - 128` 上限，不再随当前剩余窗口缩小完整检索视图；先形成合法有界投影，再让调度器决定当前窗口是否压缩。
- structured_v1 给工作条目优先使用可用投影预算，语义和 episodic 使用余量；按已有检索排名选择记录，最终模型条目稳定排序。审计中记录 omitted_ids 和预算原因，不把缺失当事实失效。
- Publisher 原有 added/removed/invalidated_values 语义保持。新结构的 stable key 避免 read_files 插入一项导致后续 index 全部改变。
- `_working_state_entries` 保留为旧有效 JSON 的兼容转换；已持久化无效 JSON blob 也原样保留，首次新投影只追加普通 V2 remove/add。不得“修复”旧消息体或重新计算历史 fingerprint。
- 不新增发布缓冲队列或第二份事实存储。当前视图仍归 MemoryPublicationSnapshot，原事实与 provenance 仍归 MemoryManager。

#### 4.4 压缩决策集中在 Coordinator

新增纯函数 `decide_append_only_compression(...) -> CompressionDecision`，字段固定为 `action: continue|compress|pause`、`reason`、`candidate_input_tokens`、`mandatory_rebase_tokens`、`soft_limit`、`ordinary_limit`、`summary_target_tokens`。Runtime 执行决策，Coordinator.prepare 校验决策和 source 绑定，不再独立使用另一套阈值。

- 硬输入预算、工具开销、安全余量和精确 source 检查不变。balanced_v1 软阈值为普通输入上限的 95%，baseline_v1 为 80%。
- mandatory_rebase_tokens 由 root prefix、完整未发送 assistant/tool 后缀、当前有界 V2 snapshot、工具定义计算，不包含待生成摘要。
- 预检为摘要保留原 summary_limit。如果 mandatory 部分加摘要上限不能低于普通预算，且当前窗口尚合法，继续旧 epoch；已经超硬预算则 pause。明确记录 insufficient_rebase_headroom，不发送明知无法按约定重建的摘要请求。
- 非强制软压缩距离上次尝试至少三个普通 step；每次实际发送压缩请求前持久化 attempt step。硬预算超限或 oversized delta 不受软退避影响，且不能借退避超预算发送。
- 首轮没有已提交 source 时不能压缩；合法请求照常发送，非法请求直接预算暂停。
- 新 epoch 仍仅包含一个当前摘要、root、未发送后缀和 snapshot。不新增任意历史尾部保留，不丢失尚未由普通请求消费的工具结果或用户输入。
- 摘要后必须低于硬预算且严格小于压缩前候选；原 no_gain 拒绝行为保留。另记录回收比例和距下次软阈值的空间，低收益不自动重试。

95% 和三步退避是本项目的初始候选参数，已有压缩 source 预留使推迟触发具备预算依据；离线对照未达到 AOP 出口时不得通过调整 gate 隐藏问题。

#### 4.5 摘要内容目标与 Provider 参数

- 保留七字段 JSON 合同和原硬 summary_limit。balanced_v1 增加不超过 `min(1024, summary_limit)` 的内容目标，重点写精确约束、当前决策、失败教训、已验证结果和下一动作。
- 不逐条抄写 recent_events、已读文件清单和已由新 snapshot 表达的进度，不使用路径代替任务事实。重要值、空白规则、错误字符串必须完整。
- `CacheEpoch.compression_request` 接受显式 `instruction`，默认原常量；动态目标只能追加在精确 source 后。`compute_prefix_budget` 使用同一条最终 instruction 估算预留，避免指令变长后超限。
- 本轮保持相同模型、endpoint、reasoning high 和工具定义；压缩仍 tool_choice=none。非 reasoning 的已有输出上限策略保持；reasoning 不直接把总生成额度砍到 1024，因为额度可能同时包含推理 token。
- 格式错误、tool call、超过硬摘要预算或无收益均沿用有界失败路径；不增加隐式补救模型调用。目标长度是内容提示，不能伪装成精确 tokenizer 或生成上限保证。
- 待压缩 request 的最终 instruction、max_output_tokens、策略版本和 source 均以现有请求/Checkpoint 生命周期固定；扩展 `set_compression_request_id`，在写 pending checkpoint 时一并保存 instruction/target，在清除 pending ID 时原子清除。新进程恢复先读取保存的 instruction，再校验 input_digest，不重新生成不同指令。旧 baseline_v1 pending 缺 instruction 时使用保留不变的原 V1 常量；不得用 balanced_v1 指令替换它。

#### 4.6 诊断和成本语义

在现有 cache 事件中增加可选字段：optimization_version、projection_format、projection_fallback_reason、new_memory_tokens、working_item_count、opaque_working_blob_count、decision_reason、mandatory_rebase_tokens、summary_target_tokens、summary_estimated_tokens、freed_input_tokens、headroom_after_rebase。

汇总从 Provider request/attempt ID 关联完成用量和起止时间，按 agent_step / epoch_compression 统计输入、缓存输入、未缓存输入、输出、时间、调用次数、成功 rollover 和失败压缩。使用 attempt 起止时间计算 Provider 耗时；审批停顿、工具和总任务时间另列，不用 wall-clock 中间等待冒充模型耗时。

完整费用只在价格、所有 attempt 用量与关联完整时计算；有未知 attempt 时同时展示 known_totals 和 unknown_attempt_count，完整指标保持 unknown。全零价格可以运行 token 限额测试，但不能证明费用节省；将现有 PPS 费用 gate 的零基线情况明确标为 unverified。

#### 4.7 分层前置检查与真实评测暂停

| 层级 | 何时执行 | 通过标准与后续 |
| --- | --- | --- |
| L0 离线优化 | AOP 开发期间；仅 Fake/loopback 和已有证据分析 | 完整前缀/恢复通过；结构化投影没有截断 blob；固定动作开销对照达到下述门槛。通过只表示可进入有限真实验证 |
| L1 小规模真实验收 | AOP-01～08 实现、L0 全部通过之后；本规划轮不执行 | 两场景各一对，固定预算；四任务完成、隐藏测试全过、用量完整，候选每场景不超 300 秒；候选压缩调用目标合同≤3、长输出≤4，完整 warm≥70%。未满足则回优化，不扩量 |
| L2 PPS 正式配对 | L1 通过且批次 token/费用上限已明确 | 同一修订两场景各三对，原 PPS 质量、恢复和收益门禁；L1 不能合并成 L2 的重复样本 |
| L3 第二阶段真实任务集 | append_only 前置优化完成，Workspace/Skill/审批/Sandbox/CLI 工作流就绪，代表性费用测清 | ≥5 仓库、≥30 任务、每项≥3次及第二阶段全部量化目标 |

L0 开销对照固定同一组工具动作、工具结果、预算和 scripted Provider 响应，使用当前 baseline_v1 与 balanced_v1，各三次。合同/长输出两场景合计新增记忆估算 token 至少减少 50%，每场景压缩调用数不增加、合计至少减少 25%，总估算输入不增加。增补 100 次单项更新流：单项变更只发布相应条目变化，插入 read_file 不重发其他文件。它只能证明实现结构与开销，不能证明真实模型质量、延迟或收费。

原“至少十次压缩”保留为小预算压力与恢复测试，不能用于要求正常性能场景频繁压缩；二者的 suite_id 和判定分开。

真实 runner 新增显式 `--execute-real`、`--readiness-report`、`--max-batch-input-tokens`、`--max-batch-output-tokens`、`--max-batch-cost-usd`。离线报告绑定代码修订/源码摘要、fixture 摘要、策略和测试结果；源码或 fixture 漂移则拒绝。报告缺失/失败时在读取凭据、创建任务或请求模型前退出。正式启动不能仅依据文件中的布尔 passed，需重新校验源码与证据引用。

`AopReadinessReport` 放在 `evaluation/gates.py`，字段固定为：schema_version="aop.v1"、revision、source_fingerprint、fixture_fingerprint、optimization_version、baseline_version、checks（复用 CacheGateCheck）、evidence_files（相对报告目录的路径与 SHA-256）、ready_for_bounded_validation。`CacheAcceptanceEvaluator.evaluate_optimization(report, *, evidence) -> AopReadinessReport` 为新增方法，CLI 的 aop 分支显式调用，避免改变现有 evaluate 的返回类型。真实 runner 必须拒绝路径越出报告证据根目录、缺失文件或摘要不匹配。

成本批次不再轮询模型完成次数后发送 SIGINT。需要暂停恢复覆盖时，在正常审批退出且没有 live Provider attempt 的边界提交 SessionService.request_pause，再用新进程消费控制状态、审批和恢复，并验证恢复后的普通前缀。取消在途请求的故障测试仍单独保留，不能从它的费用报告删除未知 attempt。异常中断后的批次保存 partial 报告并停止，不自动补样或重发未知请求。

批次执行采用顺序调度，复用 Task 的累计预算；每个 Task 上限为批次剩余额度与配置上限的较小值，包含压缩。任何未知用量先停止启动后续 Task。每次运行保留实际任务状态、初始/恢复退出码、审批原因、验证结果和 revision；不再靠退出码猜测任务成功。当前 driver 固定的逐 Task 预算值必须改成显式参数下传，Task.max_steps/max_seconds/max_context_tokens 与模型绑定在同一 pair 内完全一致，不能通过增大候选预算达标。

所有费用比较价格必须有来源和版本；L1 尚可用明确声明的零价配置做 token/质量预检，但 L2 的费用收益和后续大规模费用估计继续 unverified。用户配置本身不等于供应商实际计费承诺。

### Target Flow

```text
固定 Task 策略 → 完整 WorkingMemorySnapshot → 稳定条目和有界模型投影
→ V2 preview（原消息不变）→ Coordinator 预算与调度决策
  ├─ continue → checkpoint → 普通 Provider 请求
  ├─ compress → 精确 source + 有目标的摘要指令 → PGW journal
  │             → 校验 → 原子重建 epoch → checkpoint → 普通请求
  └─ pause → 现有上下文预算暂停与恢复说明
→ 结构 / 开销 / 未知用量分开汇总 → L0 → 后续 L1/L2/L3
```

## 5. Change Map

| File | Symbol | Change |
| --- | --- | --- |
| `src/patchloop/domain.py` | TaskExecutionConfig | 策略版本 |
| `src/patchloop/memory/working.py` | WorkingMemoryProviderEntry / project_working_entries | 新的纯模型投影 |
| `src/patchloop/memory/retrieval.py` | RetrievalSelection / retriever / provider_projection | 按完整条目装配、独立审计预算 |
| `src/patchloop/memory/manager.py` | retrieve | 透传投影模式 |
| `src/patchloop/prompt_cache/publication.py` | _working_state_entries / preview | 新条目透传及旧消息兼容 |
| `src/patchloop/prompt_cache/coordinator.py` | policy / state / budget / decision / prepare / complete | 集中调度及持久化策略 |
| `src/patchloop/prompt_cache/epoch.py` | compression_request | 显式最终摘要指令 |
| `src/patchloop/runtime.py` | _retrieve_memory / _prepare_append_only_window | 新投影、纯决策和诊断集成 |
| `src/patchloop/evaluation/cache.py` | CacheBenchmarkRunner / collector / reports | 新离线场景与 purpose 开销 |
| `src/patchloop/evaluation/gates.py` | 新 CacheAcceptanceEvaluator.evaluate_optimization | L0 前置检查与零价处理 |
| `src/patchloop/cli.py` | run / session start / benchmark-cache / validate-cache-gates | 策略配置和离线 AOP 入口 |
| `benchmarks/run_pps_server.py` | main / 预算与审批驱动 | 显式启动、前置报告、静止边界恢复 |
| `tests/fixtures/prompt_cache/optimization/` | 新 fixture | 脱敏结构化状态、固定动作、预期事实与摘要 |
| `tests/unit/`、`tests/integration/`、`tests/e2e/` | 下述指定测试 | 不变量、真实调用链、兼容、开销 |

## 6. Implementation Tasks

### AOP-01：建立可复现的记忆增长与压缩诊断

**状态：已完成（2026-09-15）。** 已加入匿名化 working snapshot、三次固定 baseline_v1 报告、真实截断链路回归、按 purpose 的 Provider 汇总，以及压缩请求、成功 rollover、失败压缩和未知用量的独立诊断。

**Goal**：在离线测试中复现截断整段发布，并准确区分压缩请求、成功重建和未知用量。

**Files / Symbols**

```text
src/patchloop/runtime.py::_prepare_append_only_window
src/patchloop/evaluation/cache.py::CacheRunReport, RealProviderCacheCollector
tests/fixtures/prompt_cache/optimization/
tests/unit/test_layered_memory_retrieval.py
tests/unit/test_cache_gates.py
```

**Implementation**

1. 从已有 pilot-04 的结构提炼匿名 working snapshot，含 12 文件、计划、近期结果和超过检索层预算的内容；不得复制 .env、完整私有路径或真实密钥。
2. 用真实 CrossLayerMemoryRetriever → provider_projection → Publisher 复现失效链路，新增回归先证明旧路径产生 opaque blob。
3. 增加第 4.6 节诊断字段与 purpose 汇总；缺字段旧报告返回 None。
4. 固定 baseline_v1 的离线动作及三次基线报告，不调用真实 Provider。

**Interface Changes**：报告/事件增加可选诊断字段；历史 schema 兼容读取，不生成假的缓存 usage。

**Tests**：未成功 rollover 的压缩也计入费用；重复事件按 ID 去重；取消 attempt 未知保留；真实截断链路复现。

**Acceptance Criteria**：基线可重复；压缩调用数与 rollover 数可不同；已有报告仍能读取；无秘密进入 fixture。

### AOP-02：固定优化策略与恢复契约

**状态：已完成（2026-09-15）。** Task 和 append-only Checkpoint 已固定 baseline_v1/balanced_v1 策略；Coordinator 提供冻结策略工厂并持久化软压缩退避位置；`run` 与 `session start` 支持显式选择，非法布局组合在加载 Provider 前拒绝；Runtime 恢复和持久化更新均拒绝同一 Task 静默切换策略。旧 Task 与旧快照缺字段时保持 baseline_v1。

**Goal**：让候选策略可选择、可审计，恢复时不能静默改变配置。

**Files / Symbols**

```text
src/patchloop/domain.py::TaskExecutionConfig
src/patchloop/prompt_cache/coordinator.py::AppendOnlyOptimizationPolicy, AppendOnlyPromptState
src/patchloop/cli.py::run_task, start_session_task
tests/unit/test_prompt_cache_coordinator.py
tests/integration/test_prefix_cli.py
```

CLI 修改现有 `run_task` 和 `start_session_task`，不另建入口；对应恢复入口读取既有 Task 配置。

**Implementation**

1. 增加 baseline_v1/balanced_v1 策略字段、固定参数工厂和 last_compression_attempt_step。
2. 创建任务时解析参数，非法布局组合拒绝；任务和 checkpoint 策略一致性在 Runtime 恢复入口检查。
3. 旧字段缺失使用 baseline_v1；pending 请求使用既有持久化参数。

**Interface Changes**：第 4.1 节接口；不修改全局默认布局。

**Tests**：旧快照、不同策略恢复拒绝、CLI JSON 投影、非 append_only 参数错误、同输入策略可重复。

**Acceptance Criteria**：现有用户任务不自动迁移；新候选可显式启动；策略变化不能改写 pending request。

### AOP-03：贯通结构化工作记忆和稳定增量

**状态：已完成（2026-09-16）。** balanced_v1 的检索链路已按稳定 key 生成完整工作记忆条目，并将结构化 payload 直接交给 V2 Publisher；模型投影选择与审计 render 解耦，按最终 V2 snapshot envelope 执行固定预算，省略项带明确原因。pinned 核心条目超限会在 Provider 请求前进入上下文暂停。baseline_v1、旧 V1/V2 消息、旧 opaque blob 的追加升级和 replay 指纹保持兼容。

**Goal**：新策略的真实检索链路不再发布截断工作记忆 blob。

**Files / Symbols**

```text
src/patchloop/memory/working.py::WorkingMemoryProviderEntry, project_working_entries
src/patchloop/memory/retrieval.py::RetrievalSelection, CrossLayerMemoryRetriever, LayeredMemoryContext
src/patchloop/memory/manager.py::retrieve
src/patchloop/prompt_cache/publication.py::MemoryDeltaPublisher
tests/unit/test_working_memory.py
tests/unit/test_layered_memory_retrieval.py
tests/unit/test_memory_publication.py
tests/e2e/test_working_memory_runtime.py
```

**Implementation**

1. 按第 4.2、4.3 节生成、过滤和稳定排序条目，保留完整关键值。
2. structured_v1 模型选择独立于审计 render；按最终消息 envelope 估算预算，记录省略原因。
3. 新格式直接进入 V2 payload，列表索引不参与新条目身份；一项新增不重发既有文件。
4. balanced_v1 使用固定投影预算；旧布局与旧策略不变。旧 opaque blob 用正常追加 delta 转换。
5. 核心条目预算错误进入显式上下文暂停，不能被通用 MemoryManager fallback 吞掉。

**Interface Changes**：projection_mode、provider_items、MemoryProjectionBudgetError；Publisher 的 V2 消息协议和 replay 不变。

**Tests**：长 JSON、引号/换行/中文、插入/重排文件、revision-only 变化、pinned 超限、过期事实、旧截断 blob 升级、检索不可用与成功空结果区别。

**Acceptance Criteria**：新路径 opaque_working_blob_count=0；100 次单项更新没有无关文件重发；JSON 始终完整；旧消息逐字和指纹不变；前缀测试通过。

### AOP-04：统一预算驱动的压缩调度

**状态：已完成（2026-09-16）。** Coordinator 已提供纯 `CompressionDecision`，统一输出 continue/compress/pause、原因、候选输入、强制重建开销和摘要目标；Runtime 只执行并记录该决策，prepare 会按精确 source、bounded V2 snapshot、完整未发送后缀和工具定义重新校验。balanced_v1 使用 95% 软阈值和三步退避，硬上限及 oversized delta 始终优先；无已提交 source 或重建空间不足时按当前请求是否合法继续或暂停，实际压缩尝试位置随 checkpoint 持久化。

**Goal**：减少不必要的提前压缩，同时保证每次提交满足硬预算。

**Files / Symbols**

```text
src/patchloop/prompt_cache/coordinator.py::compute_prefix_budget, CompressionDecision, decide_append_only_compression
src/patchloop/runtime.py::_prepare_append_only_window
tests/unit/test_prompt_cache_coordinator.py
tests/e2e/test_prompt_prefix_runtime.py
```

**Implementation**

1. 将 Runtime 触发判断收敛到第 4.4 节纯决策；95% 软阈值仅用于 balanced_v1。
2. 计算 mandatory_rebase_tokens，按摘要硬上限预检，记录继续/压缩/暂停原因。
3. 持久化三步软退避，硬上限及单条超大 delta 保持优先。
4. 保留 source 与未发送后缀的精确验证，所有副作用由现有请求与 checkpoint 提交流程承载。

**Interface Changes**：CompressionDecision 与显式 policy 参数；prepare 接受已验证决策，不能绕过硬预算。

**Tests**：80%～95% 区间继续、硬越界强制、首轮超限、长工具后缀、重建空间不足、软退避、恢复后决策一致、连续压缩失败不无限请求。

**Acceptance Criteria**：任何普通请求不超 ordinary_limit；任何压缩请求不超 input_limit；同 source 失败不重复计费重试；减少次数必须由固定动作对照证明。

### AOP-05：缩短摘要目标并保持事实与缓存合同

**状态：已完成（2026-09-16）。** balanced_v1 现使用带版本和内容目标的七字段压缩指令，目标同时受摘要硬上限、策略上限和 Provider 输出上限约束；压缩预留按最终指令计算。压缩请求仍只追加到最后已提交 source，未发送后缀不进入摘要源。最终 instruction、目标和非推理输出上限随 pending request 持久化，旧快照缺字段时精确回退原 V1 指令；成功或失败结算会原子清理这些字段。rollover 继续严格校验完整 JSON、tool pair、预算与收益，诊断分别记录目标、实际摘要估算、是否达标和释放空间，且不改变工具定义及 Provider generation/reasoning 参数。

**Goal**：减少摘要重复内容，同时保留严格 JSON、关键事实和原请求前缀。

**Files / Symbols**

```text
src/patchloop/prompt_cache/epoch.py::compression_request, validate_compression_summary
src/patchloop/prompt_cache/coordinator.py::compute_prefix_budget, complete_append_only_compression
src/patchloop/runtime.py::_prepare_append_only_window
tests/unit/test_cache_epoch.py
tests/unit/test_prompt_prefix_transport.py
```

**Implementation**

1. 构造带内容目标的版本化压缩指令，固定七字段，不逐条复述事件日志。
2. 用最终指令计算压缩预留；只追加在最后已提交请求之后，不把未发送结果冒充已总结内容。
3. 保留 Luna/high 与现有 generation，记录摘要估算大小、收益和失败原因。
4. 原子提交新 epoch；非法摘要、不完整 tool pair、超预算或无收益保留旧状态并按既有规则继续或暂停。

**Interface Changes**：compression_request 新增可选 instruction；prepared 状态固定 instruction/目标，旧默认保持。

**Tests**：精确字段/空白/错误消息 fixture、工具结果晚于 source、完整 JSON 与预算边界、Chat/Responses prefix、Provider 参数未变、摘要失败原子性。

**Acceptance Criteria**：无截断 JSON、无工具定义变化、最多一条当前摘要；目标长度和真实生成效果分开报告，不把 Fake 摘要当质量证据。

### AOP-06：覆盖新策略的跨进程恢复与故障路径

**状态：已完成（2026-09-16）。** baseline_v1 与 balanced_v1 均已覆盖投影保存但请求未登记、压缩请求已登记、响应提交但未 rollover、新 epoch 已保存四个跨进程边界；恢复会比较原请求前缀、工具顺序、策略、退避位置、usage request ID 和副作用次数。新增恢复入口校验会拒绝 pending 压缩缺少完整 source、source 与最后提交请求不符、状态 generation 与 epoch 不符、epoch 标识/前缀计数不符或 publication 属于其他 epoch 的快照。测试同时覆盖压缩期间的新用户消息、invalid/no_gain 原子失败、Provider 取消后的未知用量、租约丢失、拒绝审批、已执行文件副作用确认、旧字段缺失的 V2 checkpoint，以及十代以上只保留一个当前摘要的耐久路径。恢复 pending 请求时同步清除暂存 request ID 与旧输出上限，避免产生不可恢复的中间状态。

**Goal**：确保优化不改变 SRF/PGW 的恢复、安全与计量语义。

**Files / Symbols**

```text
tests/integration/test_prompt_prefix_recovery.py
tests/integration/test_provider_persistence.py
tests/e2e/test_effect_recovery.py
tests/e2e/test_approval_recovery.py
src/patchloop/prompt_cache/coordinator.py::from_snapshot
src/patchloop/runtime.py::resume
```

**Implementation**

1. 在投影完成未发送、压缩请求已登记、响应已提交未 rollover、新 epoch 已保存四个点重建 Runtime。
2. 覆盖用户新消息、待审批工具、Provider 取消和摘要 invalid/no_gain。
3. 比较恢复前后确切请求、usage 去重、side effect 次数、策略和退避状态。
4. 原压力场景继续覆盖十次以上压缩；作为耐久测试独立保留。

**Interface Changes**：N/A；发现必要校验缺失只扩展已有恢复入口。

**Tests**：上述故障点 × 新旧策略；旧 V2 blob/checkpoint 兼容；租约丢失后不写；拒绝审批不可绕过。

**Acceptance Criteria**：确认副作用重复=0；未知用量保持未知；历史消息/工具顺序不变；成功重建只有一个当前摘要。

### AOP-07：建立离线优化前置 gate

**状态：已完成（2026-09-16）。** 已增加独立的 `append-only-overhead` 固定动作 suite 和 `aop.v1` readiness report；baseline_v1/balanced_v1 在合同迁移、长输出两场景各执行三次，并绑定 revision、源码摘要、fixture 摘要及证据文件摘要。当前 L0 实测新增记忆估算 token 从 6726 降至 3336（50.4%），压缩调用从 33 降至 18（45.5%），总估算输入约从 22.35 万降至 20.99 万（6.1%）；100 次单项更新未重发无关 working 条目，前缀和恢复检查通过。该结果只允许进入有预算的有限真实验证，不表示 PPS 收益或发布 gate 已通过。

**Goal**：明确“优化开发完成”和“真实收益通过”的不同出口。

**Files / Symbols**

```text
src/patchloop/evaluation/cache.py::CacheBenchmarkRunner
src/patchloop/evaluation/gates.py::CacheAcceptanceEvaluator.evaluate_optimization
src/patchloop/cli.py::benchmark_cache, validate_cache_gates
tests/unit/test_prefix_acceptance.py
tests/integration/test_prefix_cli.py
```

**Implementation**

1. 增加 `benchmark-cache --suite append-only-overhead`，固定两场景动作与 structured working memory，baseline_v1/balanced_v1 各三次。
2. 增加 `validate-cache-gates --profile aop`，使用第 4.7 节 L0 门槛生成 `aop.v1` 前置报告；成功仅置 ready_for_bounded_validation=true。
3. 报告保存测试产物摘要、源码与 fixture 摘要、revision、策略、基线对照和各检查结果；不启用布局默认。
4. 正式 PPS 零价格基线返回 unverified；known warm 只能进入独立诊断字段，unknown attempt 不可绕过正式 gate。

**Interface Changes**：新增 suite/profile 和 AopReadinessReport；不混用 pps.v1 发布状态。

**Tests**：性能缺项不通过、基线漂移拒绝、三次重复要求、零价/未知用量、压力 fixture 与性能 fixture 分离、固定动作数据不可冒充 provider_reported。

**Acceptance Criteria**：达到 L0 明确门槛；真实 gate 仍 unverified；旧 PPS gate 回归通过。

### AOP-08：准备有预算的后续真实验收入口

**状态：已完成（2026-09-16）。** 真实 runner 默认只复验 `aop.v1` 报告、源码/fixture/证据摘要、场景和预算配置，输出 `model_requests=0`，既不读取凭据也不创建工作目录。只有显式增加 `--execute-real` 才按剩余批次预算顺序启动任务；中断、真实任务未完成、未知用量或预算不足均写入 partial 原因并停止扩量。每轮 manifest 固化布局、优化版本、Provider/模型、场景、完整 Task 预算、精确审批范围、验证要求和实际执行状态。正常成本路径使用无在途 attempt 的持久化 pause/resume，主动取消保留为独立故障入口。

**实施验证：** runner/CLI/gate 定向集合 57 项通过，AOP Runtime/记忆/恢复集合 141 项通过；ruff、mypy 和 diff 检查通过。全量测试为 846 passed、2 skipped、1 个既有 Windows 默认 CA 路径失败，该项用 certifi 只作用于复验进程后单独通过。`append-only-overhead` 三次固定重复和 `aop` L0 的 13 项检查全部通过；默认预检返回 `preflight_only`、`model_requests=0` 且未创建工作目录。本轮没有使用 `--execute-real`。

**Goal**：使优化后才能启动真实试跑，并在异常时停止扩量。

**Files / Symbols**

```text
benchmarks/run_pps_server.py::main
tests/integration/test_pps_server_preflight.py（新增）
README.md
docs/PROMPT_PREFIX_STABILITY_PLAN.md
docs/PHASE_2_DEVELOPMENT_PLAN.md
```

**Implementation**

1. 增加第 4.7 节显式启动、前置报告和批次预算参数；无 execute-real 时仅做不加载凭据的配置/产物预检。
2. 校验源码、fixture、策略和离线证据后才能加载凭据、创建任务。完整固定策略写入每个 trial manifest。
3. 顺序分配剩余预算；中断、未知 usage 或预算不足停止启动新任务，保存 partial 原因。
4. 成本试跑使用静止审批边界的持久化 pause/resume；独立取消故障入口保留原未知用量证据。使用 Fake/loopback 验证该驱动，不在本任务调用真实模型。
5. 文档写明 L0/L1/L2/L3 条件与恢复命令。先归档实现和离线产物，后续实施轮才能进入 L1。

**Interface Changes**：真实 runner 的显式参数与 provenance；默认命令不发请求。

**Tests**：通过替身断言报告缺失/漂移/预算无效时零模型请求；未知 attempt 停批；静止暂停无在途请求；真实 task 状态而非单退出码判分；审批范围仍精确。

**Acceptance Criteria**：本阶段模型请求数=0；所有离线检查通过；执行者无需重新设计即可按 L1 命令启动后续有限验收。

## 7. Implementation Order

```text
AOP-01 → AOP-02 → AOP-03 → AOP-04 → AOP-05 → AOP-06 → AOP-07 → AOP-08
                                                                  ↓
                                                      L0 完成后才可安排 L1
```

AOP-01 的报告统计与 AOP-02 的配置模型可由独立实现工作并行；AOP-03～06 共享 Runtime/Coordinator，建议顺序提交。第二阶段的审批/Sandbox/Workspace 专项设计和离线开发可以与 AOP 并行，不等待真实费用评测；本次不启动多 Agent。

## 8. Verification

以下命令是 AOP 实现和后续源码变更后的验证入口：

```powershell
.venv\Scripts\python.exe -m pytest -q tests/unit/test_working_memory.py tests/unit/test_layered_memory_retrieval.py tests/unit/test_memory_publication.py
.venv\Scripts\python.exe -m pytest -q tests/unit/test_cache_epoch.py tests/unit/test_prompt_cache_coordinator.py tests/unit/test_prompt_prefix_stability.py tests/unit/test_prompt_prefix_transport.py
.venv\Scripts\python.exe -m pytest -q tests/e2e/test_prompt_prefix_runtime.py tests/e2e/test_working_memory_runtime.py tests/integration/test_prompt_prefix_recovery.py
.venv\Scripts\python.exe -m pytest -q tests/unit/test_prefix_acceptance.py tests/unit/test_cache_gates.py tests/integration/test_prefix_cli.py tests/integration/test_pps_server_preflight.py
.venv\Scripts\python.exe -m patchloop benchmark-cache --suite append-only-overhead --output benchmarks/results/aop_overhead_runtime.json
.venv\Scripts\python.exe -m patchloop validate-cache-gates --profile aop --report benchmarks/results/aop_overhead_runtime.json --output benchmarks/results/aop_readiness.json
.venv\Scripts\ruff.exe check src tests benchmarks/run_pps_server.py
.venv\Scripts\mypy.exe src/patchloop
.venv\Scripts\python.exe -m pytest -q
git diff --check
```

全量测试的 Windows CA、进程权限和符号链接主机条件单列记录，必要时按已有方式单项复验，不能删测试或将跳过计为通过。源码摘要从实际影响请求、记忆、预算与 runner 的文件计算；仅文档结果追加不使已测源码失效，源码发生变化则重新跑对应 L0。

## 9. Blockers

**设计未决：None。** 关键模块、接口、状态归属、兼容路径、错误传播、任务边界和离线出口已确定。

**实施/验收条件：** AOP-01～08 已实现，L0 已达到结构与开销门槛，且后续真实入口具备显式启动、报告复验和批次预算保护。本轮没有增加 `--execute-real`，模型请求数为 0；提交后的源码修订变化会使旧 readiness 失效，进入 L1 前必须先在目标修订重新生成并归档 L0 产物。真实命中和摘要质量只能在之后 L1/L2 确认；当前零价格无法证明费用下降，完整 Provider/Docker/SRF 和第二阶段验收也仍未完成。这些外部证据限制不阻塞后续离线开发。
