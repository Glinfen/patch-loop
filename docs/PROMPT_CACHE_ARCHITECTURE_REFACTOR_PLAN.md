# PatchLoop 提示缓存架构重构计划

## 1. 状态与目标

PCO-00 至 PCO-07 已完成提示缓存的观测、稳定布局、cache epoch、记忆增量发布、评测矩阵和验收门禁。功能闭环已经形成，但相关实现目前平铺在 `src/patchloop` 顶层，并由 `runtime.py` 直接协调多个缓存对象、累计用量和持久化快照。

本计划将等价重构编号为 PCR（Prompt Cache Refactor）。截至 2026-09-05，PCR-00 至 PCR-05 已有交付与 [验收记录](milestones/PCR_05_ACCEPTANCE.md)；下文的问题调查与任务拆分保留为重构前基线和实施记录。真实 DeepSeek stable smoke 尚未验证，限制见验收记录。当前下一任务转为 [持久化 Session 与恢复闭环计划](SESSION_RUNTIME_FOUNDATION_PLAN.md) 的 SRF-00，不重新启动 PCR-00。

PCR 是第一阶段结束后的架构收口，不代表 PatchLoop 已接近成熟 Coding Agent。完成本计划后，项目停止继续纵向扩展缓存和记忆能力，转入 [第二阶段项目目标](PHASE_2_PROJECT_GOALS.md) 与 [第二阶段模块开发计划](PHASE_2_DEVELOPMENT_PLAN.md)，优先补齐 Session、Provider、Approval、Skills、Sandbox、Git 工作区和真实端到端评测。

重构目标：

- 保留标准的 `src/patchloop` 包布局，不移动源码根目录。
- 建立清晰的 `patchloop.prompt_cache` 领域边界。
- 将提示缓存状态机和统计生命周期从 `AgentRuntime` 抽离。
- 保持 PCO 已有请求布局、指纹、Trace、checkpoint、报告和评测结果兼容。
- 让 Provider、记忆系统、持久化和评测通过稳定契约协作，而不是依赖缓存模块内部实现。

这是一轮架构重构，不以新增缓存策略或提高命中率作为完成依据。任何行为变化都必须拆成后续独立任务，并重新运行 PCO 门禁。

## 2. 当前问题

当前缓存相关实现分布如下：

| 位置 | 当前职责 | 判断 |
| --- | --- | --- |
| `cache.py` | 请求指纹、公共前缀分析、未命中归因 | 职责合理，但名称过宽且位于包顶层 |
| `prompt_layout.py` | legacy/stable 请求布局和工具冻结 | 属于提示缓存领域 |
| `cache_epoch.py` | 冻结前缀、周期轮换和两阶段压缩 | 属于提示缓存领域 |
| `memory_publication.py` | 将记忆投影发布为快照与追加增量 | 属于提示缓存和 Provider 消息之间的发布协议 |
| `runtime.py` | 创建、恢复和推进上述对象，累计缓存用量并写入 checkpoint/report | 协调职责过多 |
| `providers/*` | Provider 请求、响应和缓存用量字段解析 | 位置正确，应保持 Provider 所有权 |
| `evaluation/cache.py` | 本地模拟、真实字段采集和缓存门禁 | 位置正确，应只依赖公开契约 |

`src` 下只有 `patchloop` 并不是问题。`src` 是打包源码根目录，`patchloop` 是唯一可导入产品包。真正需要处理的是 `patchloop` 内部领域边界，而不是删除 `src` 层或人为增加第二个顶级包。

当前具体结构风险包括：

1. `runtime.py` 同时持有多个缓存对象和七个缓存用量累计字段，运行状态与缓存状态紧密耦合。
2. 正常模型调用与 epoch 压缩调用分别重复执行请求诊断、用量记录和 Trace 收尾，后续容易出现语义漂移。
3. checkpoint 分别保存 epoch、发布和诊断快照，恢复组合规则散落在 Runtime 中。
4. 内部代码和测试直接导入多个旧顶层模块，移动文件会形成大范围一次性破坏。
5. `cache.py` 容易被误解为本地响应缓存；实际上它只实现 Provider 提示缓存的布局诊断。

## 3. 目标目录结构

```text
src/
└── patchloop/
    ├── prompt_cache/
    │   ├── __init__.py
    │   ├── diagnostics.py
    │   ├── layout.py
    │   ├── epoch.py
    │   ├── publication.py
    │   ├── usage.py
    │   └── coordinator.py
    ├── memory/
    ├── providers/
    ├── evaluation/
    │   └── cache.py
    ├── observability.py
    ├── persistence.py
    └── runtime.py
```

模块职责：

| 模块 | 单一职责 |
| --- | --- |
| `diagnostics.py` | 脱敏请求指纹、最长公共前缀和未命中归因 |
| `layout.py` | 稳定请求前缀、项目指令快照和工具冻结 |
| `epoch.py` | cache epoch 状态、轮换边界和压缩请求协议 |
| `publication.py` | 将已有记忆 Provider 投影转换为 epoch 内快照与增量消息 |
| `usage.py` | 缓存命中、未命中、写入和字段一致性累计 |
| `coordinator.py` | 组合以上纯状态对象，对 Runtime 暴露一个生命周期入口 |

`publication.py` 不放入 `memory/`。记忆模块负责生成安全、确定性的 Provider 投影；发布模块负责决定该投影如何按 cache epoch 变成模型消息。这样记忆核心不依赖 Provider 消息协议，依赖方向保持单向。

不在仓库中新增 `.cache`、`storage/cache` 或其他本地 KV 目录。PCO 管理的是远端 Provider 的提示前缀复用，本轮不实现本地响应缓存。

## 4. 依赖边界

目标依赖方向为：

```text
AgentRuntime
├── PromptCacheCoordinator
├── MemoryManager
├── Provider
├── Persistence
└── Observability

PromptCacheCoordinator
├── prompt_cache 内部纯状态模块
├── Provider 基础消息与用量契约
├── 领域配置
└── 安全脱敏接口

MemoryManager
├── memory 内部模型、检索、压缩和存储
├──领域模型
└──安全接口
```

必须遵守以下约束：

- `memory` 不导入 `prompt_cache`，只输出精简的 Provider 投影。
- `providers` 不导入 `prompt_cache`；Provider 只报告能力和实际用量。
- `prompt_cache` 不发起网络调用、不执行工具、不直接写数据库或 Trace。
- `PromptCacheCoordinator` 生成请求所需状态并接收模型结果；实际 Provider I/O 仍由 Runtime 驱动。
- `evaluation` 使用 `patchloop.prompt_cache` 的公开导出，不导入内部私有函数。
- `persistence` 只持久化版本化快照契约，不持有活动协调器。
- `runtime.py` 不再直接操作 `CacheDiagnostics`、`CacheEpoch`、`MemoryDeltaPublisher` 或缓存计数器。

## 5. 兼容策略

本轮采用先兼容、后清理策略：

- `patchloop` 包根部已经公开导出的类名保持不变。
- `patchloop.cache`、`patchloop.cache_epoch`、`patchloop.prompt_layout` 和 `patchloop.memory_publication` 暂时保留为薄重导出模块。
- 项目内部代码统一改用 `patchloop.prompt_cache` 或其明确子模块，不继续增加旧路径使用方。
- 兼容模块不得复制实现、保存状态或引入双向依赖。
- 首轮不主动发出弃用警告，避免污染 CLI、测试和下游日志；删除旧路径必须等待独立版本决策。
- checkpoint JSON 字段、schema version、枚举值、Trace 事件名、报告字段和 benchmark JSON 字段保持兼容。
- 请求规范化和指纹算法不得因模块移动发生变化。

项目不使用 pickle 保存这些对象，因此类的 Python `__module__` 变化不属于持久化协议；Pydantic JSON 和 SQLite 中的显式 schema 才是兼容边界。

## 6. 开发任务

### PCR-00：锁定行为基线与架构契约

开发内容：

- 为现有请求指纹、布局 Trace、epoch 快照、记忆发布快照、缓存用量累计和 Runtime checkpoint 增加特征化测试。
- 固定 legacy/stable 两种布局的消息角色、顺序和规范化内容。
- 固定一次普通模型步骤和一次 epoch 压缩步骤的 Trace 事件与报告字段。
- 固定 checkpoint 保存后恢复的缓存状态，并验证恢复后下一次请求指纹一致。
- 记录当前模块依赖图和允许的目标依赖方向。
- 保存一份确定性 Fake Provider 缓存评测结果作为重构前基线；运行时长、临时路径和随机任务 ID 不进入等价比较。

完成标准：后续任务若改变请求字节、指纹、快照 JSON、事件语义或评测结果，测试必须在提交前失败；本任务不移动生产模块。

### PCR-01：建立 `prompt_cache` 包并迁移纯模块

开发内容：

- 新建 `src/patchloop/prompt_cache`。
- 将诊断、布局、epoch 和记忆发布实现分别迁移到 `diagnostics.py`、`layout.py`、`epoch.py` 和 `publication.py`。
- 在 `prompt_cache/__init__.py` 明确导出稳定公共类型，避免通过通配符泄漏内部帮助函数。
- 把项目内部导入切换到新路径。
- 将四个旧模块改成无状态的兼容重导出层。
- 将对应测试调整为与新包结构镜像，同时增加新旧导入路径身份一致测试。

完成标准：新旧路径引用的是同一类和函数对象；没有实现副本；请求指纹、消息布局、Pydantic JSON 和完整测试结果不变。

### PCR-02：抽取缓存用量累计器

开发内容：

- 新增不可混淆“未报告”和“报告为零”的 `CacheUsageAccumulator` 及其快照。
- 从 Runtime 移出命中、未命中、写入、已报告、未报告和不一致调用的累计字段。
- 统一普通调用与压缩调用的用量校验和命中率计算。
- 让 TaskReport、metrics、replay 和 checkpoint 继续使用原有字段名。
- 覆盖字段缺失、零值、混合命中、输入 Token 不一致、仅 write 未知和恢复后继续累计。

完成标准：`runtime.py` 不再持有独立的 `_cache_*` 数值累计字段；PCO-00 的所有语义和报告结果保持不变。

### PCR-03：实现纯状态 `PromptCacheCoordinator`

开发内容：

- 新增协调器，统一管理布局、诊断、epoch、记忆发布和用量累计对象。
- 定义明确的开始、恢复、请求准备、响应观察、压缩准备、epoch 轮换和快照接口。
- 请求准备返回消息、冻结工具和诊断元数据，不直接调用 Provider。
- 响应观察接收 `ModelUsage` 并返回应写入 Trace/report 的稳定数据，不直接调用 Observability。
- 压缩准备只生成结构化压缩请求；Runtime 完成 Provider I/O 后再把摘要结果交还协调器。
- 为非法状态转换提供显式错误，例如未启用 stable 布局却请求 epoch 轮换、摘要响应对应错误 epoch。

完成标准：协调器可以在不创建 `AgentRuntime`、数据库或真实 Provider 的情况下完成完整生命周期单元测试；不得形成 `runtime`、`memory` 或 `providers` 到 `prompt_cache` 的反向循环依赖。

### PCR-04：精简 Runtime 集成与恢复路径

开发内容：

- Runtime 只持有一个 `PromptCacheCoordinator` 活动实例。
- 用协调器替换 Runtime 内重复的诊断 observe/finalize、缓存用量记录、epoch 恢复和发布恢复逻辑。
- 将现有 `_record_model_usage()`、`_cache_hit_rate()` 和 `_compress_epoch()` 的缓存状态职责移出 Runtime；Provider 调用和任务状态转换仍留在 Runtime。
- checkpoint 创建和恢复通过一个版本化协调器快照适配层完成，同时继续读取已有分散字段。
- 验证任务首次运行、正常完成、预算失败、模型失败、压缩失败、取消和中断恢复路径都不会丢失缓存统计。
- 保持 legacy 布局无需 epoch 状态即可运行。

完成标准：`runtime.py` 只通过协调器公开接口参与缓存生命周期，不再直接导入布局、诊断、epoch 和发布实现类；旧 checkpoint 可恢复，新 checkpoint 的外部字段保持兼容。

### PCR-05：依赖治理、完整回归与文档收口

开发内容：

- 增加轻量依赖边界测试，拒绝 `memory -> prompt_cache`、`providers -> prompt_cache` 和 `prompt_cache -> runtime/persistence/observability` 依赖。
- 运行 Ruff、格式检查、严格 mypy、完整 pytest 和缓存专项测试。
- 重新运行确定性缓存矩阵，与 PCR-00 基线比较请求布局、结果指纹和门禁状态。
- 至少运行一次可控的 DeepSeek stable 场景，确认 Provider 缓存字段仍被采集；远端命中率只做非退化观察，不把服务端波动当成等价重构失败。
- 新增架构 ADR，记录 `src` layout、提示缓存包边界、纯协调器和兼容重导出的决策。
- 更新 README、PCO 总计划、第二阶段衔接说明和模块文档，并生成 PCR 验收记录。

完成标准：静态质量检查和完整测试全部通过；确定性评测与重构前等价；真实 Provider 正确性、安全和字段采集不退化；文档中不再把旧顶层模块描述为首选导入路径。

## 7. 实施顺序与提交边界

| 阶段 | 任务 | 依赖 | 建议工作量 | 独立提交产物 |
| --- | --- | --- | ---: | --- |
| A：锁定 | PCR-00 | PCO-07 | 0.5～1 天 | 特征化测试、基线和边界说明 |
| B：归包 | PCR-01 | PCR-00 | 1～1.5 天 | 新包、模块迁移和兼容层 |
| C：状态 | PCR-02 | PCR-01 | 0.5～1 天 | 用量累计器与恢复测试 |
| D：协调 | PCR-03 | PCR-02 | 1～1.5 天 | 纯协调器和状态机测试 |
| E：接入 | PCR-04 | PCR-03 | 1～1.5 天 | 精简 Runtime 与 checkpoint 适配 |
| F：收口 | PCR-05 | PCR-04 | 0.5～1 天 | 依赖门禁、完整评测、ADR 和验收记录 |

预计总工作量为 4.5～7.5 个开发日。每个任务单独提交，并在提交点保持主分支可运行；不得先删除旧模块再用后续提交修复导入。

## 8. 总体验收标准

全部 PCR 任务完成时必须同时满足：

- 仍采用 `src/patchloop`，不新增没有独立发布需求的顶级 Python 包。
- `prompt_cache` 包拥有布局、诊断、epoch、发布、用量和协调器的清晰边界。
- Runtime 不直接管理缓存内部状态对象或独立缓存统计字段。
- 新旧公开导入路径在兼容期内均可用，包根部公共导出不变。
- legacy/stable 请求消息、工具顺序、规范化指纹和压缩协议不变。
- 既有 checkpoint、Trace、metrics、replay、TaskReport 和 benchmark JSON 可继续读取。
- PCO-00 至 PCO-07 的正确性、安全、恢复和确定性缓存门禁不退化。
- 没有新增跨层循环依赖，也没有以全局单例隐藏缓存状态。
- 完整测试与静态检查通过，并形成独立验收记录。

不以减少 `runtime.py` 的绝对行数作为唯一门禁。真正的门禁是职责迁移完成、依赖方向清晰、行为等价且测试能够阻止回退。

## 9. 非目标与风险控制

- 不在重构中改变 DeepSeek、OpenAI 或 Anthropic 的请求参数。
- 不调整缓存命中率阈值、上下文预算、记忆召回、压缩阈值或 epoch 轮换策略。
- 不把 `ModelUsage` 从 Provider 基础契约移入缓存包。
- 不把 Memory Store、SQLite 或向量检索移入缓存包。
- 不删除 legacy 布局或旧导入路径。
- 不为追求目录对称而拆分只有单一调用点的微型模块。

主要风险是移动 Pydantic 类型后破坏 checkpoint 反序列化、内部导入形成循环、兼容层出现双实现，以及 Runtime 接入时让普通调用和压缩调用统计不一致。对应控制分别是 PCR-00 特征化测试、依赖门禁、同一对象身份测试和统一协调器生命周期测试。

## 10. 与第二阶段的关系

本计划只解决提示缓存代码的边界和 Runtime 耦合，不承担产品化功能。PCR 完成后应满足以下交接条件：

- 第二阶段新增 Session 和 Provider 生命周期时，不需要继续向 `runtime.py` 填入缓存内部状态。
- Approval、Skills 和 Sandbox 可以通过稳定的请求、事件和能力契约接入，不直接依赖缓存实现。
- 旧任务、checkpoint、Trace 和 benchmark 继续可读，第二阶段无需先迁移第一阶段数据。
- 缓存命中率和记忆算法进入维护状态；除非真实任务暴露正确性回退，否则不再优先优化。

PCR 的完成只表示内部结构适合继续扩展。第二阶段是否完成，应以真实用户工作流、跨 Provider 行为、安全边界和端到端任务结果判断，不能以模块数量、测试数量或缓存命中率替代。
