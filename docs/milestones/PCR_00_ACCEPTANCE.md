# PCR-00 行为基线与架构契约

## 交付内容

- 新增 `tests/unit/test_pcr00_behavior_baseline.py`，固定请求规范化后的 wire bytes、区段指纹、legacy/stable 消息角色与顺序、epoch 快照、记忆快照/增量、Runtime Trace 和缓存报告字段。
- 增加 SQLite checkpoint 往返测试：恢复诊断快照后，下一次相同请求的请求指纹、区段指纹、Provider 指纹和 epoch 指纹保持一致。精确公共字节前缀不作为恢复等价条件，因为 checkpoint 不保存原始请求字节。
- 增加稳定布局的 epoch 压缩运行特征测试，固定 `cache.compression.requested`、`cache.layout`、`cache.epoch.rolled_over` 的事件顺序、关键字段和累计报告结果。
- 保存确定性 Fake Provider 缓存矩阵基线：`benchmarks/results/pcr00_cache_baseline.json`。运行时长、临时目录、事件时间戳和随机任务 ID 不参与比较；等价比较使用 fixture fingerprint、deterministic fingerprint、逐步请求指纹、用量、归因和报告摘要。

## 验证命令

```text
python -m pytest tests/unit/test_pcr00_behavior_baseline.py -q
python -m pytest tests/unit/test_cache.py tests/unit/test_cache_epoch.py tests/unit/test_memory_publication.py tests/unit/test_prompt_layout.py tests/e2e/test_checkpoint_resume.py -q
python -m patchloop benchmark-cache --mode deterministic --repeats 3 --output benchmarks/results/pcr00_cache_baseline.json
```

当前基线固定值：

- `fixture_fingerprint`: `6d666620d936707fb35f2e8cbd4ce8b2c5f8458d93bd04bec703b4198a777983`
- `deterministic_fingerprint`: `bc64bd00c5019898cfd866a10d4271d8032960986586a280fba792105d5f0613`
- 6 个布局变体、7 个场景、3 次重复，共 18 个确定性运行。

后续 PCR 任务若改变请求字节、指纹、Pydantic 快照 JSON、事件字段/顺序或确定性矩阵，应先使这些特征测试或基线比较失败，再由独立任务明确更新协议。

## 当前依赖图

PCR-00 记录重构前的实际依赖，不移动生产模块：

```text
runtime
├── cache
├── cache_epoch ──> cache
├── prompt_layout
├── memory_publication
├── persistence ──> cache, cache_epoch, memory_publication
├── memory/*
├── providers
└── observability/events

evaluation/cache ──> cache, cache_epoch, providers.fake/base, events
observability ──> cache, events
cache ──> providers.base, security
cache_epoch ──> cache, providers.base, security
memory_publication ──> providers.base
```

## 目标依赖方向

后续迁移允许的方向如下：

```text
AgentRuntime ──> PromptCacheCoordinator, MemoryManager, Provider, Persistence, Observability
PromptCacheCoordinator ──> prompt_cache 内部纯状态模块, Provider 基础契约, 领域配置, 安全脱敏接口
MemoryManager ──> memory 内部模型/存储/检索/压缩, 领域模型, 安全接口
evaluation ──> patchloop.prompt_cache 的公开契约
persistence ──> 版本化快照契约
providers ──> Provider 自身基础契约
```

明确禁止的反向依赖：`memory -> prompt_cache`、`providers -> prompt_cache`、`prompt_cache -> runtime/persistence/observability`。这些禁止项的静态门禁属于 PCR-05；PCR-00 只锁定基线和契约。

