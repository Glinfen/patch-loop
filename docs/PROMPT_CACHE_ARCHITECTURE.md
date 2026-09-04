# Prompt Cache 模块说明

Prompt Cache 负责 Provider 提示前缀的可观测布局与生命周期状态，不实现本地响应缓存。

## 边界

`patchloop.prompt_cache` 是纯状态边界。它接收已经准备好的模型消息、工具和 Provider 用量，输出请求消息、冻结工具、布局诊断、压缩请求和版本化快照。Provider 网络调用、上下文裁剪、工具执行、SQLite 持久化和 Trace 事件都由调用方负责。

## 推荐入口

新代码从 `patchloop.prompt_cache` 导入公开契约，尤其是 `PromptCacheCoordinator`。此前的顶层 `patchloop.cache`、`patchloop.prompt_layout`、`patchloop.cache_epoch` 和 `patchloop.memory_publication` 仍是兼容重导出层，不再作为新代码的首选入口。

## Runtime 生命周期

1. Runtime 用任务初始消息和工具规格启动协调器。
2. 每一步先由上下文引擎构建 Provider 窗口，再由协调器准备最终请求并记录诊断。
3. Provider 返回后，Runtime 把 `ModelUsage` 交给协调器，取得 Trace/report 所需稳定布局数据。
4. stable 布局触发压缩时，协调器生成压缩请求；Runtime 完成 Provider I/O 后交回摘要，协调器验证并推进新 epoch。
5. checkpoint 通过协调器生成的兼容字段保存，恢复时重新构造同一个生命周期状态。

