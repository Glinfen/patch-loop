# LCM-04 情景记忆验收

## 结论

PatchLoop 已于 2026-08-30 完成 LCM-04。Runtime 现在从工具执行、失败、重新规划、写入、验证和 checkpoint 边界生成确定性的情景记录。每条记录都有不可变来源，失败与修正操作通过因果 ID 相连；恢复任务能够从 checkpoint 快照或 SQLite 记录重建最后一个已验证情景。

## 情景内容

`EpisodicMemoryManager` 为每次工具调用生成一条 `MemoryKind.EPISODIC` 记录，内容 schema 为 `1.0`，包括：

- `intent` 与活动 `plan_phase`。
- `actions`：工具名、脱敏参数指纹、是否写入、是否验证。
- `observations`：写前脱敏并有界压缩的结果。
- `outcome`：成功、失败、恢复、验证、计划更新或 checkpoint。
- `paths`、`error_kind`、步骤和原始 call ID。
- `recovers_episode_ids`：当前情景恢复的失败情景 ID。

每个步骤完成时再生成 checkpoint 边界情景。路径来源按动作精确生成：文件工具使用调用路径，验证工具关联当前变更路径，checkpoint 关联完整变更集。这样既支持文件查询，又保证工具结果重放时来源身份稳定。

## 失败与恢复

- 失败情景进入 `unresolved_failures`，并以工具参数的脱敏规范指纹识别完全相同动作。
- 同类工具使用修正参数成功后生成 `recovered` 情景并链接原失败；成功测试或诊断生成 `verified` 情景，并成为恢复锚点。
- `update_plan` 本身不清除失败，避免把“已经重新规划”误判为“已经恢复”。
- Runtime 在执行前阻止仍未解决且完全相同的稳定调用级失败：未知工具、非法参数、路径拒绝和执行错误。
- 权限/计划前置条件失败不会被硬阻止；测试、命令和语法失败也允许在代码变化后原样重跑。它们仍保留为活动情景并进入提示上下文。

这一区分修复了一个真实回归：因“必须先重新规划”被拒绝的正确补丁，在完成 replan 后应当允许执行。

## 查询能力

`MemoryQuery` 新增可选的 `error_kinds`、`plan_phases` 和 `episode_outcomes`。这些过滤器可与 LCM-02 已有的路径、来源步骤、创建时间、状态、作用域和 Token 预算组合。非情景记录不会错误匹配结构化情景过滤器。

固定查询测试同时约束：

- `src/parser.py` 来源路径。
- 第 7 步来源范围。
- `execution_error` 错误分类。
- `Repair payment parser` 计划阶段。
- `failed` 结果和创建时间窗口。

## Checkpoint 与恢复

`EpisodicMemorySnapshot` 保存最近 32 个情景、最多 128 个未解决失败、已记录 call/checkpoint、累计情景与恢复数，以及最后已验证情景 ID。

- 新 checkpoint 直接恢复快照，已记录 call ID 不会再次创建情景。
- 旧 checkpoint 没有情景字段时，Runtime 从 SQLite 的 schema `1.0` 情景记录确定性重建状态。
- 工具结果与情景使用稳定 ID；若进程在情景写入后、checkpoint 前中断，重放仍通过 LCM-02 内容寻址保持幂等。
- 情景上下文仅在存在活动失败、最近恢复或验证锚点时进入系统消息，避免正常短任务无条件增加上下文。

恢复端到端测试在补丁和测试成功后主动中断，随后删除传入 checkpoint 的情景快照以模拟旧版本；Runtime 仍从持久化记录找到验证锚点，直接完成而不重复写入。

## 可观测性与验证

Trace 新增：

- `episode.created`：阶段、结果、路径、错误和恢复链接。
- `episode.repeat_blocked`：被阻止的完全相同失败调用。
- `context.built.episodic_memory`：当前可恢复快照。
- `task.resumed.last_verified_episode_id`：恢复选择的验证锚点。

任务报告和 `TaskMetrics` 增加情景数、恢复数、最后验证 ID 和重复失败阻止数。

新增测试覆盖：

- 失败、修正操作和成功验证的因果链接。
- 快照 round-trip、重复 call/checkpoint 去重和从记录重建。
- 意图、动作、观察、结果、路径和错误字段完整性。
- 错误、阶段、路径、步骤、结果和时间组合查询。
- 完全相同无效补丁在执行前阻止，修正补丁和测试正常执行。
- 验证后中断、旧 checkpoint 重建和不重复已完成写入。
- 既有失败—replan—重试流程保持兼容。

架构理由见 [ADR-013](../adr/ADR-013-event-segmented-episodic-memory.md)。

## 下一任务

LCM-05 将实现确定性语义抽取器、类型化事实、来源权威等级、冲突检测和活动事实替代链。
