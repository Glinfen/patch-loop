# PCO-06 缓存感知评测框架验收

## 交付内容

- `DeterministicPrefixCacheSimulator` 使用缓存诊断的规范化请求字节，在进程内精确计算连续请求的最长公共前缀，并合成命中/未命中 Token；不把原始请求写入报告。
- `CacheBenchmarkRunner` 固定比较当前布局、仅稳定前缀、固定工具面、精简 Provider 投影、cache epoch 与压缩、完整优化六种配置。
- 每种配置默认执行 3 次完整 fixture，每次包含冷启动、预热连续步骤、权限变化、项目变化、显式压缩、checkpoint 恢复和模型切换；模型切换标记为预期全失效对照。
- `RealProviderCacheCollector` 直接读取 `cache.layout` 与 `model.completed` 事件中的 Provider 字段。Provider 未报告的字段保持 `null`，不使用本地模拟值替代。
- Provider 能力矩阵只在能力声明允许时序列化显式断点或会话键；DeepSeek 输出为空，不发送其他厂商的缓存参数。
- `patchloop benchmark-cache` 同时写出机器可读 JSON 矩阵和人类可读摘要。

## 验证方式

```text
patchloop benchmark-cache --output benchmarks/results/pco06_cache_matrix.json
```

确定性运行的 `deterministic_fingerprint` 对相同本地 fixture 保持一致。JSON 中保留每次运行和每一步的输入 Token、命中/未命中 Token、命中率、公共前缀、时延、费用、压缩次数、前缀轮换、指纹变化和归因计数；摘要提供均值、最小值和最大值。

真实 Provider 事件可通过以下方式采集，报告中的缓存字段来自 trace，不由本地估算：

```text
patchloop benchmark-cache --mode provider --trace .patchloop/traces/<task-id>.jsonl
```

## 测试证据

- Fake 模拟器的公共前缀、Token 守恒和跨重置指纹稳定性测试通过。
- 六配置 × 三次运行矩阵、逐步场景覆盖和确定性报告指纹测试通过。
- Provider 报告字段直采以及 DeepSeek 能力门控测试通过。
