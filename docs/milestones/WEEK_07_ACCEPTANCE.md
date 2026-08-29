# Week 7 验收记录

## 结论

PatchLoop 已于 2026-08-29 通过第七周“安全与可观测性”验收。执行工具默认进入失败关闭的无网络 Docker 沙箱；Tool Gateway 在运行前完成权限、计划、仓库路径、危险命令、风险等级与审批检查；运行数据经过统一脱敏并可从结构化 Trace 生成指标和逐帧回放。

## Docker 沙箱

`DockerSandbox` 生成固定参数数组并以 `shell=False` 启动容器：

- 仅将当前仓库绑定到 `/workspace`。
- 默认 `--network none`，无隐式联网能力。
- 限制 CPU、内存和 PID 数量，并由外层进程强制超时。
- 根文件系统只读，只提供 64 MiB 的受限 `/tmp`。
- 删除全部 Linux capabilities，并启用 `no-new-privileges`。
- Docker 缺失或容器启动失败时拒绝执行，不回退到本地后端。

标准镜像由 `docker/sandbox.Dockerfile` 构建，包含 Python、Git、pytest 和 pytest-cov。`local` 后端只为受信任环境与单元测试保留。

## 风险与审批

每个已授权工具动作还需经过风险判断：

- 读操作为低风险。
- 写操作为中风险。
- 命令与测试执行为高风险。
- 仓库外路径、Shell 连接符以及 curl、wget、ssh、PowerShell 等网络或 Shell 可执行文件为严重风险，直接拒绝。

交互任务对中风险及以上动作逐次确认；非交互任务只在操作员显式给出对应权限参数后预授权。每次判断写入 `security.decision`，包括风险、审批要求、结果和原因。

## 敏感信息脱敏

`SecretRedactor` 同时识别值模式和字段名，覆盖 `sk-` Key、Bearer Token、API Key、Access/Auth Token、Password、Secret 等。脱敏发生在 Provider 上下文入口以及 JSONL Trace、SQLite 任务/步骤/工具/检查点、最终报告和 diff 产物写入前。

## Trace、指标与回放

事件具有 UUID、任务 Trace ID、UTC 时间和连续序号。新增命令：

```text
patchloop metrics <task-id> --repo <repository>
patchloop replay <task-id> --repo <repository>
```

指标包含任务状态、步骤数、模型和工具调用、失败工具、审批请求与拒绝、Token、费用、工具耗时、总耗时和错误分布。回放按事件序号输出帧，并把步骤开始至完成之间的安全判断与工具结果关联到具体步骤。

## 攻击型端到端验收

`tests/e2e/test_security_observability.py` 构造带凭据查询参数的 curl 工具调用。固定结果记录在 `benchmarks/results/week07_security.json`：

- 网络命令在执行前被严重风险规则拦截。
- 工具结果为 `permission_denied`，Docker 后端未被调用。
- 任务按预算失败后，回放把失败工具定位到步骤 0。
- Trace 序号从 1 连续递增，指标报告一个失败工具。
- 原始凭据没有出现在 Trace 中。
- Docker 默认网络模式为 `none`。

## 质量门禁

第七周验收执行 Ruff、mypy、完整 pytest 和分支覆盖率门禁：Ruff 与 mypy 全部通过，pytest 为 65 项通过、1 项因当前 Windows 主机不支持符号链接而跳过，分支覆盖率为 87.97%，高于 85% 门禁。

## 已知边界

Docker 沙箱共享宿主机内核，不能视为恶意多租户的强隔离边界；仓库挂载必须可写，以便测试和构建产生仓库内文件。当前审批为 CLI 同进程回调，没有远程审批队列。脱敏采用确定性模式，可能遗漏自定义凭据格式，也可能对类似凭据的普通字符串产生误报。

## 下一阶段

第八周将基于当前检查点、沙箱、指标和回放协议建立不少于 30 个固定评测任务，增加批量执行、并发控制、失败重跑和机器可读汇总。
