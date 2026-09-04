# PCO-07 验收、灰度与恢复门禁

## 交付内容

- `CacheAcceptanceEvaluator` 将 PCO-07 门禁拆成独立、可审计的检查项：Provider 真实字段、三次运行、聚合/稳态命中率、单请求大额未命中、稳定前缀指纹、压缩前缀复用和输入 Token 降幅。
- `MemoryQualityEvidence` 要求同时提供公开 1/1、隐藏 5/5、安全/过期事实结果，以及与基线比较的任务正确率、关键事实召回、过期事实、重复失败和恢复一致性；缺失证据为阻断，不以缓存指标代替。
- 大额未命中只能在存在结构化布局归因时通过；冷启动、epoch 轮换和模型切换不计入稳态门禁。
- `CacheRolloutPolicy` 固定 stable 候选、legacy 回退、至少一个发布周期保留旧路径，并禁止持久化迁移。`enabled=false` 时选择 legacy，便于无数据迁移回退。
- `validate-cache-gates` 支持真实 Provider 报告与本地确定性报告分离：真实报告负责服务端缓存门禁，本地报告负责压缩前缀和布局 Token 对比。

## 验证命令

先生成本地矩阵：

```text
patchloop benchmark-cache --output benchmarks/results/pco06_cache_matrix.json
```

真实 DeepSeek 轨迹应单独生成 Provider 报告；正式门禁默认拒绝仅有 deterministic 数据的报告：

```text
patchloop benchmark-cache --mode provider --trace .patchloop/traces/<task-id>.jsonl --output benchmarks/results/pco07_provider.json
patchloop validate-cache-gates --report benchmarks/results/pco07_provider.json --local-report benchmarks/results/pco06_cache_matrix.json --quality benchmarks/results/pco07_quality.json --enable-stable
```

开发者可使用 `--allow-simulated` 做本地干运行，但其结果不能作为真实 Provider 放量结论。

## 恢复与质量门禁

验收输入必须证明 checkpoint 恢复后的记忆/前缀指纹与未中断路径一致，恢复不重复已完成写入，过期事实命中为 0，越界修改和秘密泄漏为 0。任何失败项都写入 `blocking_checks`，CLI 以非零状态码退出；报告仍会落盘供审查。

当前实现保留 `PromptCacheLayout.LEGACY` 旧路径和已有 checkpoint schema，因此切换回 legacy 不需要迁移持久化数据。
