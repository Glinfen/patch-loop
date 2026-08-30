# LCM-05 语义记忆验收

## 结论

PatchLoop 已于 2026-08-30 完成 LCM-05。Runtime 现在从用户约束、计划证据、文件读取、累计 diff 和验证结果生成带来源的类型化事实，并按事实槽、证据状态和来源权威解析冲突。默认查询只暴露唯一活动版本；旧事实、被拒绝猜测和完整替代关系仍可审计。

## 类型与证据状态

`SemanticMemoryManager` 生成 `MemoryKind.SEMANTIC`、`semantic_schema=1.0` 的记录。首版类型包括：

- `constraint` 与 `prohibition`：用户明确要求，状态为 `user_asserted`。
- `code_symbol` 与 `code_observation`：来自文件读取或 diff，状态为 `observed`。
- `plan_evidence`：来自已完成计划项的证据，保守标记为 `inferred`。
- `verification`：测试或命令结果；成功为 `verified`，失败为 `observed`。
- `inference`：可选模型抽取器的输出，核心层强制保持 `inferred`。

权威顺序为 `verified(100) > user_asserted(80) > observed(50) > inferred(20)`。模型抽取器即使声明已验证也会降级，置信度不超过 0.6。

## 归一化、冲突和替代

事实槽由作用域、类型、subject 和 predicate 构成，value 是该槽的当前取值。解析规则为：

- 相同槽、相同规范值、较低或相等权威：抑制重复。
- 相同槽、相同值、更高权威：保存证据升级并替代旧记录。
- 相同槽、不同值、新权威不低于当前值：新事实替代旧事实。
- 相同槽、不同值、新权威更低：新事实保存为 `invalidated`，不能成为活动事实。

每次替代都维护 `supersedes_id` 与 `superseded_by_id`，并在提交前验证边界一致、双向和无环。来源、新事实和旧事实的生命周期更新与工作/情景记忆一起在单个 SQLite 事务中保存。

超长观察会规范化为有界前缀和完整值哈希。12,000 字符赋值的回归任务可以继续运行，且有界事实仍拥有稳定身份。

## Runtime、恢复和查询

- 任务启动时立即保存用户约束；每个工具结果之后执行确定性抽取。
- 工具来源包含稳定 call ID、步骤、路径和脱敏证据哈希。
- checkpoint 保存创建、替代、拒绝和去重计数；恢复时从 SQLite 类型化语义记录重建活动槽。
- `MemoryQuery` 新增 `fact_types` 与 `epistemic_statuses`，可与状态、路径、步骤、作用域、时间和 Token 预算组合。
- 结构化语义过滤不会误匹配 LCM-03 的非类型化语义摘要，也不会匹配工作或情景记录。

端到端固定任务先读取 `MODE="old"`，再通过真实补丁写入 `MODE="new"`，最后运行 pytest。历史查询得到“旧观察、新观察、新验证”三段完整替代链；默认活动查询只得到 `verified` 的新值。

## 可观测性

Trace 新增 `semantic.facts_resolved`，记录创建、替代、低权威冲突拒绝、重复抑制和相关记录 ID。`TaskReport`、`RuntimeCheckpoint` 与 `TaskMetrics` 暴露相同累计计数，便于比较抽取器和长上下文任务表现。

## 验证范围

新增测试覆盖：

- 中英文用户约束与禁止事项抽取。
- 同权威新观察替代旧值和多段替代链。
- 已验证事实拒绝低权威推断。
- 模型辅助输出强制降级。
- 失败验证被成功验证替代。
- 超长观察的确定性有界化。
- 类型与证据状态组合查询。
- Runtime 中读取、真实补丁、真实 pytest、事务持久化、报告和 Trace 指标。

最终验证命令为 `python -m pytest --cov=patchloop --cov-report=term-missing -q`：136 个测试通过，1 个因当前 Windows 主机不支持符号链接而跳过；项目总覆盖率为 92%，`memory/semantic.py` 覆盖率为 97%。`ruff check .`、`ruff format --check .` 和 `mypy` 同时通过。

架构理由见 [ADR-014](../adr/ADR-014-authority-aware-semantic-facts.md)。

## 下一任务

LCM-06 已实现工作、语义和情景记忆的统一候选检索、混合排序、多样性约束与分层 Token 预算，见 [LCM-06 验收记录](LCM_06_ACCEPTANCE.md)。下一任务 LCM-07 将实现安全的分层记忆压缩。
