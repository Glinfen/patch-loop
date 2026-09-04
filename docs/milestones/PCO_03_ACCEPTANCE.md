# PCO-03 Provider 投影与审计投影分离验收

## 结论

PCO-03 已完成。`LayeredMemoryContext` 现在同时保留完整审计投影和精简 Provider
投影：完整投影继续包含查询信号、分配、评分、召回理由、来源和省略项，供 Trace/Replay
解释召回；Provider 投影只发送当前工作状态、有效事实、失败经验和明确约束。

stable 布局将精简投影作为独立动态 system 消息发送，legacy 布局继续发送完整投影以支持
灰度和快速回退。Provider 投影不包含记录 ID、来源 ID、浮点评分、召回理由、预算分配、
时间戳、修订号或省略 ID，并按语义类型、稳定作用域和内容哈希排序。

## 安全与稳定性

- 记录检索文本和 Provider 语义字段都先经过凭据脱敏及 Prompt Injection 过滤，再生成两类投影。
- Provider 投影只从安全的语义字段生成，不复用包含 provenance 和排序诊断的检索文本。
- 固定记忆集合在改变评分、召回理由或选择顺序后仍生成相同的 Provider 快照。
- 完整投影仍保留全部召回解释，`cache.layout` 和 `context.built` 事件同时记录实际 Provider
  投影，便于对比缓存布局与 Token 占用。

## 验证结果

| 检查项 | 结果 |
| --- | ---: |
| Ruff | 通过 |
| Mypy | 51 个源文件通过 |
| PCO-03 专项测试 | 6 passed |
| 完整测试集 | 185 passed, 1 skipped |
| Provider 投影 Token 门槛 | 固定 fixture 至少降低 30% |

跳过项为 Windows 主机不支持符号链接的既有工具测试，不影响 PCO-03 逻辑。
