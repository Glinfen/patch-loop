# PCO-01 安全请求指纹与前缀诊断验收

## 结论

PCO-01 已完成。PatchLoop 现在为每个模型步骤生成 `cache.layout` Trace，按
system 指令、任务/项目快照、工具 Schema、epoch 快照、记忆投影、历史消息组和
完整请求记录脱敏后的不可逆指纹，并同时记录稳定前缀与连续请求最长公共前缀。

诊断层不实现响应缓存，也不改变请求布局。它只使用 Provider 返回的真实命中/未命中
Token 补充归因；高于配置阈值但没有结构性变化时才归因为
`provider_best_effort`，无法解释的变化记录为 `unknown`。

## 安全与恢复

- 指纹输入先经过现有 `SecretRedactor`；Trace 和 checkpoint 只保存 SHA-256、长度、
  区段名和归因，不保存原始提示、仓库正文或 API 凭据。
- 工具 Schema 采用递归规范化 JSON，并按工具名稳定排序；相同能力集合不受注册输入顺序影响。
- 历史只保存消息组指纹，因此可以区分追加式历史和历史重选，而不需要持久化原始消息。
- checkpoint 保存上一请求的安全指纹快照；恢复时用区段级确定性估算继续计算公共前缀。
- `TaskMetrics`、`TaskReplay` 和 CLI 现有 `metrics`/`replay` 路径可消费布局 Trace。

## 测试结果

| 检查项 | 结果 |
| --- | ---: |
| Ruff | 通过 |
| Mypy | 50 个源文件通过 |
| Pytest | 179 passed，1 skipped |
| 跳过原因 | Windows 主机不支持符号链接测试 |

专项测试覆盖冷启动、相同请求跨进程确定性、工具顺序归一化、秘密过滤、记忆投影变化、
历史重选、模型/思考配置变化、epoch 轮换、高额未命中和 Provider 尽力而为归因。

## 后续

PCO-02 可以在保留本诊断协议的前提下调整消息布局；优化前后应继续比较 `cache.layout`
中的区段指纹、最长公共前缀和 Provider 实际 Token，不能用本地估算冒充 Provider 命中。
