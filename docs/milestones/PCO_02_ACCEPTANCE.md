# PCO-02 稳定前缀请求组装验收

## 结论

PCO-02 已完成第一版实现。PatchLoop 增加了 `legacy`/`stable` 请求布局开关；默认保留
legacy 以支持灰度与快速回退，任务可通过 `TaskExecutionConfig.prompt_cache_layout="stable"`
或 CLI 的 `--prompt-cache-layout stable` 启用新布局。

stable 布局将静态 system 指令、可选项目指令快照和任务目标组成固定前缀；工具 Schema
在任务起点复制并冻结；Layered Memory 和上下文裁剪产生的 `TaskMemory V1` 均作为独立
的动态 system 消息追加，不再原位修改第一条 system 消息。

## 实现边界

- checkpoint 保存冻结工具 Schema 和稳定前缀消息数量，恢复后不因重新读取注册表而重排工具面。
- ContextEngine 支持显式稳定前缀长度、独立运行时记忆消息和独立降级记忆消息，并继续执行
  原有硬预算、工具原子组、安全过滤和过期记忆排除。
- 评测 harness 复用同一 ContextEngine 组装路径，不再把层级记忆拼回第一条 system 消息。
- `cache_epoch`、项目指令快照和 PCO-01 的区段指纹接口已预留；模型/思考配置变化仍由
  PCO-01 记录，完整 epoch 轮换协议留待 PCO-04。

## 验证结果

| 检查项 | 结果 |
| --- | ---: |
| Ruff | 通过 |
| Mypy | 51 个源文件通过 |
| PCO-02 专项/相关回归 | 41 passed |

现有 legacy 路径测试保持通过；stable 路径新增测试证明第一条 system 消息、项目指令快照
和任务目标保持独立，运行时记忆不会写入第一条 system 前缀。
