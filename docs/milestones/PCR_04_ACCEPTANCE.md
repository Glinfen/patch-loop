# PCR-04 验收记录：Runtime 协调器接入

## 目标

让 Runtime 只持有一个活动的 `PromptCacheCoordinator`，由协调器统一负责 Prompt Cache 的初始化、恢复、请求诊断、用量累计、压缩换代和 checkpoint 适配；Provider 调用、上下文构建、工具执行和任务状态转换仍由 Runtime 负责。

## 交付内容

- Runtime 初始化和恢复统一创建 `PromptCacheCoordinator`。
- legacy/stable 请求准备均通过协调器完成，Provider 接收协调器返回的冻结工具和消息。
- 模型响应的缓存布局观察和用量累计通过协调器完成，既有 Trace、TaskReport 字段保持不变。
- stable 压缩流程改为协调器的压缩准备、响应观察和 epoch 完成接口；Provider 失败或摘要无效时保持原有失败语义。
- 旧版分散 checkpoint 字段通过协调器适配读写，兼容既有恢复路径。
- Runtime 不再直接持有 `CacheDiagnostics`、`CacheEpoch`、`MemoryDeltaPublisher` 或独立缓存用量累计器。

## 验证

```text
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

结果：`221 passed, 1 skipped`；跳过项为 Windows 主机不支持符号链接的工具测试。

