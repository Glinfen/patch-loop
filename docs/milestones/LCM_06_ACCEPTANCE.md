# LCM-06 跨层检索与预算验收

## 结论

PatchLoop 已于 2026-08-30 完成 LCM-06。Runtime 现在在每次模型调用前，把任务目标、活动计划、当前错误、目标文件和最近动作组成检索查询，从工作、语义与情景记忆中选择有解释的活动候选，并在 ContextEngine 总上限内执行分层 Token 预算。

## 查询与候选

`RetrievalQueryBuilder` 固定组合五类信号并执行脱敏：

- 任务目标。
- 未完成计划项。
- 工作记忆中的活动错误。
- 当前变更路径与工作记忆路径。
- 最近四个工具动作和观察摘要。

工作记忆快照直接进入候选。长期记录必须同时满足当前 task、`active` 状态和 task/repository 作用域；`superseded`、`invalidated` 和其他仓库记录在排序前排除。无持久化 Store 时，Runtime 仍可使用进程内活动语义事实和情景恢复快照。

## 混合排序与多样性

`CrossLayerMemoryRetriever` 的长期分数包括：

- 词项和代码符号重叠。
- 目标路径精确匹配。
- 相对新近度。
- 记录重要度和置信度。
- 用户消息、工具结果、checkpoint 等来源质量。

每条选择输出完整分量和多样性 key。相同文件/来源默认最多两条，文本高度重复会被降权；语义层最多占长期名额的 60%，跨层同路径时优先保留类型化事实，避免连续测试日志或 checkpoint 淹没当前契约。

## 分层预算

标准预算为工作记忆 20%、语义记忆 12%、情景记忆 8%、原始最近历史 60%。

- Runtime 先扣除系统、用户和工具 schema 的强制成本。
- 空间不足时，三个记忆层按 50:30:20 等比例收缩。
- ContextEngine 新增原始历史硬额度，最近消息组与 LCM-00 摘要之和不能越界。
- 分层 JSON、选择理由和截断标记全部计入记忆额度。
- 最终 ContextEngine 继续执行总 Token 硬上限。

## Runtime 与恢复

每次 Provider 调用前生成 `LayeredMemoryContext`，其中包含查询、四层额度、选择、原因、省略 ID、逐层用量和最终渲染 Token。旧的 `PATCHLOOP_WORKING_MEMORY_V1` 与 `PATCHLOOP_EPISODIC_MEMORY_V1` 仍保留在分层载荷内，已有长任务和恢复策略无需迁移。

checkpoint、任务报告和 `TaskMetrics` 新增：

- `memory_retrievals`。
- `memory_retrieval_hits`。
- `memory_retrieval_tokens`。

`memory.retrieved` Trace 保存查询信号、额度、记录 ID、分数、选择原因和省略项；`context.built.layered_memory` 保存同一轮最终选择摘要。

## 固定质量任务

固定任务包含四条相关活动记录、五条同日志噪声、一条已替代旧事实和一条错误仓库记录，并同时提供工作错误、活动计划和三条目标路径。

结果保存在 `benchmarks/results/lcm06_retrieval.json`：

- `memory_recall@5 = 1.00`，目标不低于 0.90。
- `memory_precision@5 = 0.80`，目标不低于 0.70。
- 过期事实召回率 `0.00`。
- 同一多样性 key 在候选中最多出现两次。

真实 Runtime 端到端任务执行“读取 `MODE=old`、补丁写入 `MODE=new`、下一轮检索”。长期分层载荷只包含活动新值，旧值仅保留在工作/原始审计上下文，不会作为长期活动事实返回；选择理由、Trace、报告和 checkpoint 计数一致。

## 回归验证

最终命令 `python -m pytest --cov=patchloop --cov-report=term-missing -q` 得到 142 个测试通过，1 个 Windows 符号链接测试跳过。项目总覆盖率为 92%，`memory/retrieval.py` 覆盖率为 96%。`ruff check .`、`ruff format --check .` 与 `mypy` 全部通过。

既有 900-token 长任务继续保留早期结论，峰值上下文从 862 降为 819；工作记忆 100 步、失败恢复、语义替代与中断恢复测试全部兼容。

架构理由见 [ADR-015](../adr/ADR-015-budgeted-cross-layer-memory-retrieval.md)。

## 下一任务

LCM-07 已完成无损归一化、记录级合并、情景级聚合和多代压缩，并保证约束、失败方案、验证证据和替代关系可追溯。下一步进入 LCM-08 的 Runtime、Context Engine 与恢复流程统一集成。
