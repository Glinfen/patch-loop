# LCM-09 可观测性、安全与 CLI 验收

## 结论

PatchLoop 已于 2026-09-03 完成 LCM-09。Memory 2.0 的持久化查询、Runtime 选择、任务报告、Trace 指标和 replay 现在共享可追踪的记录 ID、来源、状态与评分解释；不可信仓库指令在作为记忆注入 Provider 前会被替换，安全处理本身可审计但不泄露原文。

## memory CLI

新增命令：

```text
patchloop memory <task-id> --repo <repository> \
  --kind semantic,episodic \
  --status active \
  --step 4 \
  --query "payment contract" \
  --limit 20
```

- `--kind` 支持 working、semantic 和 episodic，省略时查询全部类型。
- `--status` 支持 active、superseded 和 invalidated，默认为 active。
- `--step` 为来源精确步骤，默认 `-1` 表示不限制。
- 查询结果逐条返回总分、相关性、新近度、重要性、置信度、来源质量、匹配词、来源定位和 `why_recalled`。
- 非法类型、状态或步骤以退出码 2 失败；不存在的任务以退出码 1 失败。

## 事件、指标与报告

Runtime 已覆盖 `memory.written`、`memory.retrieved`、`memory.superseded`、`memory.compacted`、`memory.fallback`、`memory.replayed` 和 `memory.security_filtered`。

TaskReport 和 metrics 增加：

- 各类型和各状态的记忆库存。
- 写入、替代、召回、压缩、降级、replay 和安全过滤数量。
- 陈旧命中、召回 Token、最大记忆上下文 Token 与占用率。
- 读取、写入和压缩耗时，以及累计压缩比。

MemoryManagerSnapshot 保存累计库存和耗时，因此中断恢复后报告不会重置。Trace 保留单次事件耗时，metrics 仍只从 Trace 派生，不引入第二套事件事实来源。

## replay 决策链

`TaskReplay.memory_decisions` 按事件序号与模型步骤列出每次召回的查询、选择记录、来源、状态、评分分量、可读理由和省略 ID。恢复时的重复记忆事件明确记录为 `event_already_processed`，并标记 `writes_suppressed=true`。

端到端测试确认最终模型步骤能够关联到活动语义记录及其来源；原有失败工具步骤回放保持兼容。

## 安全边界

安全验收在仓库文件中同时植入提示注入文本和 API Key：

- API Key 原文未进入 SQLite 数据库字节、JSONL Trace 或 Provider 请求。
- 覆盖上级指令和索取凭据的片段在记忆副本中替换为 `[UNTRUSTED_INSTRUCTION_BLOCKED]`。
- `memory.security_filtered` 只记录 `credential_redacted`、`prompt_injection_blocked` 等类别以及选择/记录 ID，不记录命中原文。
- 工作记忆错误与近期动作生成的查询信号应用相同过滤，不能从元数据旁路。
- Provider 窗口中的最近工具历史也应用同一过滤，不能绕过分层记忆边界。

## 回归验证

专项测试覆盖 CLI 过滤与解释、结构化评分、来源和状态、指标聚合、replay 决策映射、提示注入清洗、凭据端到端流转、checkpoint 指标兼容和 LCM-08 分层召回。最终验证结果：

- Ruff lint 与格式检查通过。
- mypy 对 49 个源文件检查通过，无类型错误。
- pytest 共收集 164 项：163 项通过，1 项因当前 Windows 主机不支持符号链接而跳过。
- 项目总覆盖率为 92%；`memory/manager.py`、`memory/retrieval.py`、`observability.py`、`security.py` 覆盖率分别为 93%、93%、99%、98%。

架构决策见 [ADR-018](../adr/ADR-018-explainable-memory-observability-and-safety.md)。

## 下一任务

LCM-10 将对 recent-only、TaskMemory V1、关闭语义、关闭情景、关闭压缩和完整 Memory 2.0 运行固定消融，只针对排名最高的真实失败类型优化。
