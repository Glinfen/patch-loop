# LCM-00 长上下文基线与评测协议验收

## 结论

PatchLoop 已于 2026-08-30 完成 LCM-00。项目现在可以用一条 `benchmark-memory` 命令生成 20、50、80、120 步合成长历史，对比 `recent-only` 和当前 `TaskMemory V1`，并分别运行确定性上下文检查或 DeepSeek V4 Flash 模型探针。

本任务只建立协议和基线，没有修改现有记忆策略。`hierarchical-memory` 已进入稳定枚举，但在 Memory 2.0 完成前会明确拒绝运行，不会用当前策略伪造未来变体成绩。

## 固定任务集

清单位于 `benchmarks/memory_tasks.json`，revision 为 `lcm00-2026-08-30`，包含 8 个任务：

| 任务 | 步数 | 关键能力 |
| --- | ---: | --- |
| early-constraint-survival | 80 | 早期禁止约束存活 |
| superseded-api-contract | 50 | 新事实替代旧事实 |
| failed-strategy-avoidance | 80 | 记住已失败方案 |
| checkpoint-memory-resume | 50 | 检查点前验证结论 |
| delayed-cross-file-evidence | 120 | 组合三个远距离事实 |
| repetitive-test-output | 50 | 重复日志中的根因和最终验证 |
| memory-poisoning-defense | 20 | 不可信历史指令边界 |
| multi-generation-compression | 120 | 多阶段关键事实存活 |

每条事实声明 ID、精确值、类型、引入步骤、可选失效步骤、可选替代目标、相关查询和必须召回的探针步骤。`recall_queries` 是后续检索评测的稳定协议字段；LCM-00 的模型探针仍以任务级 `goal` 作为最终查询。噪声由任务 ID 和步骤号确定性生成；评测答案来自清单标注，不读取模型摘要决定结果。

## 评测模式

### recent-only

只保留最近完整的 assistant/tool 原子消息组，不生成记忆。该变体用于量化“仅扩大最近窗口”能保留多少早期事实。

### TaskMemory V1

直接调用生产 `ContextEngine`，使用当前相关性选择、结构化 `TaskMemory` 和硬 Token 预算。它是 Memory 2.0 必须超过或至少保持的基线。

### 确定性检查

在每个标注探针中直接检查最终上下文是否包含有效事实值和已失效事实值。每个历史步骤都构建一次上下文，因此报告能够记录模拟累计输入 Token、压缩次数、最大上下文和预算溢出。

### 模型探针

只在标注探针调用真实模型。DeepSeek 思考模式不接受缺少原始 `reasoning_content` 的合成 assistant 历史，因此模型探针将已选上下文封装为一段明确标记为不可信的用户侧历史数据；确定性路径仍使用真实 assistant/tool 原子组。报告将模拟累计 Token 与真实 Provider Token 分开保存。

## 指标定义

- `critical_fact_recall`：要求召回的精确事实值中实际出现的比例。
- `stale_fact_rate`：已失效事实值中仍出现在上下文或模型回答里的比例。即使模型只在解释中复述旧值，也算陈旧命中。
- `repeated_failure_risk_rate`：要求记住的失败方案没有被召回的比例。LCM-00 中这是重复失败风险代理，不等同于 Agent 已实际重复工具动作。
- `estimated_cumulative_input_tokens`：在每个合成步骤构建上下文后的估算量之和。
- `provider_input_tokens` 与 `provider_output_tokens`：仅统计真实模型探针返回的计费用量。
- `stable_outcomes_across_repeats`：忽略时延、费用和自由文本后，逐任务成功、召回、陈旧命中与上下文指标是否跨重复一致。

## 确定性三轮基线

| 指标 | recent-only | TaskMemory V1 |
| --- | ---: | ---: |
| 成功运行 | 0/24 | 21/24 |
| 成功率 | 0% | 87.5% |
| 关键事实召回 | 0% | 100% |
| 陈旧事实率 | 0% | 100% |
| 重复失败风险 | 100% | 0% |
| 上下文溢出 | 0 | 0 |
| 最大上下文估算 | 2,057 | 3,972 |
| 模拟累计输入 Token | 3,254,532 | 6,323,001 |
| 跨三轮结果稳定 | 是 | 是 |

TaskMemory V1 能召回所有标注关键事实，但无法抑制已经失效的旧 API 值，因此 `superseded-api-contract` 每轮失败。这为 LCM-05 的状态和替代链提供了固定失败基线。

## DeepSeek V4 Flash 三轮基线

| 指标 | recent-only | TaskMemory V1 |
| --- | ---: | ---: |
| 成功运行 | 0/24 | 22/24 |
| 成功率 | 0% | 91.67% |
| 各轮成功率 | 0%、0%、0% | 87.5%、87.5%、100% |
| 关键事实召回 | 0% | 100% |
| 陈旧事实率 | 0% | 66.67% |
| 重复失败风险 | 100% | 0% |
| 上下文溢出 | 0 | 0 |
| Provider 输入 Token | 39,324 | 87,189 |
| Provider 输出 Token | 43,548 | 9,392 |
| 平均探针时延 | 16.78 秒 | 5.72 秒 |
| 费用 | 0.014011 美元 | 0.006301 美元 |

TaskMemory 的输入上下文更大，但输出 Token 下降 78.43%，平均时延下降 65.92%，费用下降 55.03%。`recent-only` 看不到早期事实，模型经常长时间解释证据不足或尝试调用不存在的工具。

TaskMemory V1 的两次失败都来自 `superseded-api-contract`：模型给出了正确的新值，却在解释来源时再次复述旧值。第三轮只输出新值并通过。这表明当前抽取记忆能保留事实，却没有稳定的陈旧事实过滤能力。

## 运行命令

```powershell
patchloop benchmark-memory `
  --manifest benchmarks/memory_tasks.json `
  --variant task_memory_v1 `
  --mode deterministic `
  --repeats 3 `
  --output benchmarks/results/lcm00_task_memory_v1.json
```

真实模型基线将 `--mode` 改为 `model`，命令只在该模式读取项目根目录下已被 Git 忽略的 `.env`。可以用 `--task id1,id2` 只运行指定任务。

## 机器可读证据

- `benchmarks/results/lcm00_recent_only.json`
- `benchmarks/results/lcm00_task_memory_v1.json`
- `benchmarks/results/lcm00_recent_only_deepseek.json`
- `benchmarks/results/lcm00_task_memory_v1_deepseek.json`

## 限制

- 当前任务使用合成历史和精确事实标记，不是完整代码修改任务。
- 模型只在最终探针调用一次，模拟累计输入 Token 不代表实际 API 账单。
- `checkpoint-memory-resume` 在 LCM-00 只验证检查点前事实召回，真正的中断恢复一致性将在 LCM-02 和 LCM-08 实现。
- `repeated_failure_risk_rate` 是风险代理，实际重复动作需要未来 Runtime 集成任务判定。
- 模型探针把选中历史封装为不可信用户数据，用于兼容 DeepSeek 思考协议；该结果不能代替真实 Agent 长任务评测。

## 后续进度

LCM-01 已固定 `MemoryRecord`、来源、生命周期、替代链、召回结果和压缩报告模型，见 [LCM-01 验收记录](LCM_01_ACCEPTANCE.md)。下一项 LCM-02 将实现持久化 Memory Store。
