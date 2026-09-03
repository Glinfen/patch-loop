# LCM-08 Runtime、Context Engine 与恢复集成验收

## 结论

PatchLoop 已于 2026-09-03 完成 LCM-08。Runtime 现在通过一个 `MemoryManager` 增量摄取目标、工具结果和步骤边界，在每次 Provider 调用前检索唯一分层上下文，并用带事件水位的 checkpoint 恢复。派生记忆故障不再导致编码任务失败，而是安全回退到 `TaskMemory V1`。

## 统一摄取与压缩

- 任务目标、每个工具结果和每个步骤结束都有稳定事件 ID。
- 管理器统一更新工作、情景和语义记忆，并在单个 Memory Store 批次中保存来源与记录。
- 活动原始记录达到 64 条时尝试首代压缩；活动摘要达到 8 条时推进下一代，自动代数不超过 3。
- `memory.ingested`、`memory.written` 和 `memory.compacted` 事件共享事件 ID 与游标位置。
- TaskReport 新增摄取数、记录写入数、压缩次数、压缩输入/输出 Token 和降级次数。

100 步 Runtime 场景实际触发自动压缩，工作记忆仍保持在 600 Token 上限内；压缩输出严格少于输入，Trace 压缩事件数与报告和 checkpoint 一致。

## Context Engine 边界

正常 Memory 2.0 路径只注入 `LayeredMemoryContext`，Context Engine 继续负责总预算、最近历史预算和 assistant/tool 原子消息组。旧 `TaskMemory V1` 摘要在该路径关闭，避免重复摘要；它只在记忆故障时重新启用。

若扣除强制系统提示与工具 schema 后的额度连一条分层选择都无法容纳，本轮临时使用 `TaskMemory V1`，下一轮仍会重新尝试 Memory 2.0。既有 900 Token 早期证据长任务因此继续通过，不会因新层元数据开销丢失关键结论。

结构化状态为 `superseded` 或 `invalidated` 的语义值会从 Provider 历史副本中替换为固定占位符。遮蔽不修改 Runtime 原消息、SQLite 工具结果或 Trace，因此审计证据完整，同时过期值无法从原始历史绕过活动记录过滤。

## checkpoint 与无重复副作用

新 `MemoryManagerSnapshot` 包含完整工作/情景快照、已处理事件、下一事件序号、pending 集合、压缩累计值和降级状态。

- 完整步骤保存后 pending 集合为空。
- 恢复后重复提交相同 `tool:<call-id>` 会返回 replay，不生成重复记忆。
- 旧 checkpoint 没有统一快照时，使用旧工作/情景字段与 SQLite 记录重建。
- 中断恢复端到端测试中，补丁工具只执行一次，恢复前的记忆 ID 全部保留，游标中的写入事件也只出现一次。

## 故障降级

写入故障测试使用正常 SQLite 保存任务、工具结果和 checkpoint，只让 Memory Store 写入失败。任务仍完成，工具结果只执行一次，下一 Provider 请求不再包含分层记忆前缀，Trace 只产生一次 `memory.fallback`，策略明确记录为 `task_memory_v1`。

压缩事务故障测试确认原活动记录不被失效、pending 队列清空，并进入同一降级路径。管理器不会在后续步骤持续重试同一故障。

## 固定长上下文评测

结果保存在 `benchmarks/results/lcm08_hierarchical_memory.json`。八个 20～120 步任务执行三轮，共 24 次：

- 成功 24/24，三轮结果稳定。
- 关键事实召回率 100%。
- 过期事实暴露率 0。
- 重复失败风险率 0。
- 上下文越界 0。
- 最大上下文使用 3,412 Token，低于固定 4,000 Token 上限。
- 累计估算输入 5,109,372 Token；LCM-00 同清单 `TaskMemory V1` 基线为 6,323,001 Token。

## 回归验证

专项测试覆盖游标非法状态、快照 replay、真实 SQLite 恢复、自动压缩水位、压缩事务失败、Memory Store 写入降级、过期值遮蔽和 legacy Context 回退。最终验证结果：

- Ruff lint 与格式检查通过。
- mypy 对 49 个源文件检查通过，无类型错误。
- pytest 共收集 158 项：157 项通过，1 项因当前 Windows 主机不支持符号链接而跳过。
- 项目总覆盖率为 92%；新增 `memory/manager.py` 覆盖率为 92%。

架构决策见 [ADR-017](../adr/ADR-017-watermarked-memory-runtime-integration.md)。

## 下一任务

LCM-09 将补齐 memory CLI、逐条召回解释、读写与压缩耗时指标、安全事件和 replay 的记忆决策链。
