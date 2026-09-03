# ADR-017：带事件水位的统一记忆运行时

## 状态

已接受。

## 背景

LCM-03 至 LCM-07 已分别实现工作记忆、情景记忆、语义记忆、跨层检索和安全压缩，但 Runtime 直接调用每个模块并分别处理持久化。checkpoint 只有工作与情景快照，没有统一的已处理事件位置；Memory Store 的读写异常也会沿 Runtime 主循环传播，使本可继续的编码任务失败。

分层记忆与旧 `TaskMemory V1` 同时汇总历史还会形成旁路：长期检索已经排除的 `superseded` 值，仍可能被旧摘要重新带入 Provider 上下文。

## 决策

### 统一所有权与同步摄取

新增 `MemoryManager`，由它持有三个记忆状态机、跨层检索器、压缩器和可选 Memory Store。Runtime 只在三个边界调用管理器：任务目标初始化、每个工具结果完成、每个步骤 checkpoint 完成。

一次摄取先更新内存状态，再用一个 Memory Store 批次保存来源和派生记录。返回的 `MemoryManagerUpdate` 包含晋升、情景、语义解析、写入 ID、压缩报告和新产生的降级原因，Runtime 据此记录事件，不再重复编排各模块。

### 事件游标与恢复

`MemoryEventCursor` 保存：

- 下一个事件序号。
- 最后完成的事件 ID。
- 已处理事件 ID 集合。
- 尚未完成的事件 ID 集合。

工具事件使用稳定的 `tool:<call-id>`，步骤边界使用 `checkpoint:<task-id>:<step>`。事件只有完成内存更新后才从 pending 移到 processed。重复事件直接返回 replay 结果，不产生来源、记录或压缩。完整 `MemoryManagerSnapshot` 与旧工作/情景字段同时写入 checkpoint；读取旧 checkpoint 时从旧快照和持久化记录重建游标，保持向后兼容。

### 自动压缩水位

默认在活动第 0 代记录达到 64 条时尝试首代压缩；活动压缩记录达到 8 条时允许一次显式代际汇总；自动代数上限为 3。只有 LCM-07 返回有效且节省 Token 的报告才提交。压缩状态、新摘要和报告继续使用同一个 SQLite 事务。

语义压缩摘要保留共享的类型、槽和值，代际压缩不会跨语义槽合并，确保后续新权威值仍能建立替代链。

### Context Engine 边界

正常路径在 Provider 调用前取得唯一 `LayeredMemoryContext`，再由 Context Engine 执行最终硬预算与 assistant/tool 原子组裁剪。此时关闭 `TaskMemory V1` 摘要，避免两套记忆同时解释历史；已替代或失效的结构化语义值会从发送给 Provider 的历史副本中遮蔽，但原始 Trace、checkpoint 和工具结果保持不变。

若极小上下文在扣除系统提示和工具 schema 后无法容纳任何分层选择，本轮临时交还 `TaskMemory V1`；这不是持久化故障，不关闭后续 Memory 2.0 检索，也不增加故障降级计数。

### 故障降级

Memory Store 恢复、写入、检索或压缩失败只会把管理器切换到一次性的 `fallback_active` 状态。Runtime 记录 `memory.fallback`，后续直接把原始消息交给 Context Engine，由 `TaskMemory V1` 保持旧行为。工具调用、工作区修改、任务状态和普通 checkpoint 持久化不依赖这条记忆链，因此不会因派生记忆故障回滚或重复副作用。

## 后果

三轮、八任务确定性长上下文评测达到 24/24：关键事实召回率 100%、过期事实暴露率 0、重复失败风险 0、上下文越界 0。相比同一清单的 LCM-00 `TaskMemory V1` 确定性基线，成功率从 87.5% 提升到 100%，累计估算输入 Token 从 6,323,001 降到 5,109,372。

代价是默认压缩水位目前是固定值，尚未按仓库规模或访问热度自适应；进入 fallback 后本次 Runtime 不自动重试 Memory Store，以避免在每一步重复触发相同故障。LCM-09 将补齐 CLI 查询、耗时指标和更完整的召回解释。
