# ADR-019：Prompt Cache 包边界与 Runtime 协调器

## 状态

已接受

## 背景

Prompt Cache 的布局、诊断、epoch、记忆发布和用量统计最初散落在顶层模块与 `AgentRuntime` 中。继续向 Runtime 增加 Provider、Session 或 Memory 能力会扩大耦合，也容易让普通请求、压缩请求和恢复路径产生不同的统计语义。

## 决策

### 采用 `src` layout 与领域包

缓存相关契约统一位于 `src/patchloop/prompt_cache/`：

- `layout.py` 负责 legacy/stable 消息布局和工具冻结。
- `diagnostics.py` 负责脱敏指纹、公共前缀和未命中归因。
- `epoch.py` 负责冻结前缀、压缩协议和 epoch 轮换。
- `publication.py` 负责 epoch 内记忆快照与增量消息。
- `usage.py` 负责缓存字段的缺失/零值语义和累计。
- `coordinator.py` 组合上述纯状态对象，暴露开始、恢复、请求、响应、压缩和快照接口。

`prompt_cache` 不调用 Provider，不执行工具，不写数据库、Trace 或 Memory Store；这些副作用仍由 Runtime 及其端口负责。

### Runtime 只持有一个协调器

Runtime 创建或恢复一个 `PromptCacheCoordinator`，通过它准备 Provider 请求、观察 `ModelUsage`、推进压缩生命周期并生成旧 checkpoint 字段。旧顶层模块保留显式重导出层，兼容期内不改变公开导入路径和持久化 JSON 字段。

### 依赖方向固定

允许 Runtime、evaluation 和 persistence 使用 `prompt_cache` 的公开契约；禁止 `memory` 或 `providers` 反向依赖 `prompt_cache`，也禁止 `prompt_cache` 依赖 Runtime、persistence 或 observability。依赖门禁以 AST 测试固定这一边界。

## 后果

普通请求与压缩请求共享同一用量和诊断生命周期，checkpoint 恢复只需适配一次。代价是跨边界的 I/O 需要由 Runtime 显式编排，不能在缓存模块中隐藏快捷调用；旧顶层模块作为兼容层存在，待后续主版本迁移后再考虑清理。

