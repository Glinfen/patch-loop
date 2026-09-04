# PCO-04 缓存周期与冻结前缀脊柱验收

## 结论

PCO-04 已完成第一版实现。stable 布局现在拥有可 checkpoint 的 cache epoch：epoch 保存
冻结前缀消息、每条前缀消息的稳定 ID/指纹、整体前缀指纹和轮换代数。周期内新的 assistant、
tool 消息只进入动态尾部；上下文预算压力由 ContextEngine 优先裁剪未冻结尾部，不修改已冻结
前缀。

当上下文窗口出现裁剪时，Runtime 执行两阶段轮换：第一阶段使用旧 epoch 的完全相同前缀和
冻结工具面，只在尾部追加固定格式的压缩指令；第二阶段通过安全过滤后的固定 JSON 摘要创建
新 epoch，并以新前缀开始后续普通请求。压缩请求自身的 Provider 用量和缓存布局也进入现有
诊断链路。legacy 布局不启用 epoch 轮换，继续保持原有回退行为。

## 实现边界

- `CacheEpochSnapshot` 持久化 epoch ID、generation、前缀消息 ID、逐消息指纹和整体指纹。
- checkpoint 恢复优先使用已保存的 epoch 前缀；缺少旧字段时从现有 stable 消息安全引导。
- 新 epoch 只在 context threshold 触发自动轮换；`CacheEpochBoundary` 同时为计划阶段切换、
  成功验证、显式压缩和恢复边界提供明确协议枚举。
- 摘要生成失败或返回 tool call 时不替换旧前缀，保留当前任务历史并记录失败事件。
- 摘要字段固定为 `constraints`、`paths`、`decisions`、`failures`、`tests`、`unfinished`、
  `next_step`；摘要仍标记为不可信证据，不会变成 system 指令。

## 验证结果

| 检查项 | 结果 |
| --- | ---: |
| PCO-04 专项测试 | 11 passed |
| 完整测试集 | 188 passed, 1 skipped |
| Ruff | 通过 |
| Mypy | 52 个源文件通过 |
| checkpoint 前缀指纹恢复 | 通过 |
| 压缩请求旧前缀复用 | 通过 |

跳过项为 Windows 主机不支持符号链接的既有工具测试。
