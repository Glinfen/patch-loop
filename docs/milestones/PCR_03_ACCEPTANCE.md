# PCR-03 验收记录：Prompt Cache Coordinator

## 目标

将 Prompt Cache 的布局、诊断、epoch、memory publication 与 usage 生命周期收敛到纯协调器中。协调器只返回 Provider 请求所需的数据和可持久化状态，不执行 Provider、数据库、Trace 或工具 I/O。

## 交付内容

- 新增 `patchloop.prompt_cache.coordinator.PromptCacheCoordinator`。
- 支持 legacy/stable 两种布局的启动、请求准备、响应观察和 checkpoint 快照恢复。
- stable 布局支持压缩请求准备、压缩响应观察、摘要驱动的 epoch rollover。
- 对未观察响应、交错请求、错误 epoch 和 legacy 压缩等非法转换抛出显式 `PromptCacheCoordinatorError`。
- 新增 coordinator 纯单元测试，未引入 Runtime、Provider、SQLite 或 Trace 依赖。

## 验证

```text
pytest tests/unit/test_prompt_cache_coordinator.py
```

验收标准：测试覆盖 legacy/stable 请求、memory publication、快照恢复、usage 记账、压缩生命周期与非法状态转换，并全部通过。

