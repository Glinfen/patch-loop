# PCO-00 缓存用量契约与可观测基线验收

## 结论

PCO-00 已于 2026-09-04 完成。PatchLoop 现在保留 Provider 是否报告缓存数据的语义区别，并将提示缓存命中、未命中和可选写入 Token 贯通到模型调用 Trace、Runtime checkpoint、TaskReport、`status`、`metrics` 和 `replay`。

协议异常只增加可观测计数，不会覆盖 Agent 的任务结果。DeepSeek 缺少缓存字段或返回不一致字段时，费用仍按全部输入未命中保守估算；原始可选字段继续保留以供诊断。

## 数据契约

- `ModelUsage` 使用可选字段表达 `cache_hit_tokens`、`cache_miss_tokens` 和 `cache_write_tokens`，因此未知值与 Provider 明确报告的零值不同。
- Runtime 对完整、未报告、部分报告和数值不一致的调用分别计数。
- checkpoint 保存累计 Token 和调用计数，中断恢复后继续累加。
- TaskReport 和 TaskMetrics 输出累计缓存命中率。
- TaskReplay 新增 `provider_usages`，逐调用保留步骤、Token、费用、命中率和一致性判断。
- DeepSeek 未提供缓存写入字段，因此 `cache_write_tokens` 保持 `null`，不由 miss Token 推断。

完整报告满足以下关系：

$$
\text{input tokens} = \text{cache hit tokens} + \text{cache miss tokens}
$$

当字段缺失或等式不成立时，`cache_usage_unreported_calls` 或 `cache_usage_inconsistent_calls` 会增加。

## 测试覆盖

自动化测试覆盖：

- Provider 不报告缓存字段；
- 全命中；
- 全未命中；
- 混合命中；
- 完整但数值不一致；
- 只报告 hit 或 miss 的部分数据；
- Trace、报告、metrics、replay 和 CLI 序列化；
- checkpoint 持久化以及中断恢复后的累计一致性。

质量门禁结果：

| 门禁 | 结果 |
| --- | ---: |
| Ruff | 通过 |
| Mypy | 49 个源文件通过 |
| Pytest | 175 passed，1 skipped |
| 跳过原因 | Windows 主机不支持该符号链接测试 |

## DeepSeek 真实基线

使用与首个长上下文记忆验收相同的隔离合成场景、DeepSeek V4 Flash 和 16K 上下文配置复跑。任务 ID 为 `2483e3a8-a34b-40c8-a9fa-9f1b7489aab1`。

| 指标 | 结果 |
| --- | ---: |
| 状态 / 完成原因 | completed / verified_plan |
| 模型调用 | 8 |
| 工具调用 / 失败 | 21 / 0 |
| 输入 / 输出 Token | 83,061 / 2,104 |
| 缓存命中 / 未命中 Token | 3,584 / 79,477 |
| 聚合缓存命中率 | 4.31% |
| 缓存字段完整 / 未报告 / 不一致调用 | 8 / 0 / 0 |
| 缓存写入 Token | Provider 未报告 |
| 费用 | 0.0117259352 美元 |
| 公开测试 | 1/1 |
| 隐藏测试 | 5/5 |
| 合成 evidence | 12/12，均只读一次 |
| 过期记忆命中 | 0 |
| Trace、SQLite 与报告中的合成凭据原文 | 0 |

本次运行在 8 步内收敛，因此不会机械复现此前 20 步样本的约 80k/141k 数值。它证明新报告可以从单个任务精确还原 Provider 缓存结果，也形成了更严格的新基线：冷启动后的每一步仍有超过 92% 的输入 Token 未命中。首个冷启动后大额未命中出现在步骤 1，最低命中率出现在步骤 5，仅 2.04%。

机器可读的累计值和逐步瀑布保存在 `benchmarks/results/pco00_cache_baseline.json`。

## 下一步

PCO-01 将对 system、项目指令、工具 Schema、记忆投影、历史组和完整请求生成脱敏指纹与最长公共前缀指标，用证据定位步骤 1 开始的大额未命中由哪个请求区段首先变化。PCO-01 完成前不修改消息布局，确保后续优化可归因。
