# Week 1 验收记录

## 结论

PatchLoop 已于 2026-08-29 通过第一周工程基线、模型协议、只读工具和可观测性验收，可以进入持久化与真实任务稳定性迭代。

## 验收环境

- Windows 11
- Python 3.13.5；项目声明支持 Python 3.12 及以上版本
- Pydantic 2.10.3
- Typer 0.9.0
- pytest 8.3.4
- Ruff 0.15.19
- mypy 1.14.1 strict 模式

## 清单

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| Git 与 Python 工程初始化 | 通过 | `main` 分支已有可追溯提交；`pyproject.toml` 定义构建、依赖和 CLI 入口 |
| 独立环境可安装 | 通过 | 在临时虚拟环境执行 editable install，成功构建并安装 `patchloop-0.1.0` |
| CLI 创建和读取任务 | 通过 | 安装后的 `patchloop task create/show` 往返保持任务 ID、目标、仓库与预算 |
| 核心领域模型 | 通过 | `Task`、`AgentStep`、`ToolCall`、`ToolResult` 均由 Pydantic 校验 |
| Provider 协议 | 通过 | Fake Provider 支持确定性多轮测试；DeepSeek Provider 独立于运行时实现 |
| 只读工具 | 通过 | 文件列表、文件读取、全文搜索均经过参数校验和仓库路径边界检查 |
| 多轮工具调用 | 通过 | 集成测试连续执行 `list_files` 和 `read_file` 并返回最终结论 |
| 结构化轨迹 | 通过 | JSONL 记录任务、步骤、模型响应、工具参数、结果、耗时和用量 |
| 自动化质量门 | 通过 | Ruff、格式检查、mypy strict、pytest 与分支覆盖率在 CI 中统一执行 |
| 固定样例任务 | 通过 | `calculator_bug` 包含缺陷、回归测试、任务目标和判定命令 |

## 自动化结果

最终验收命令：

```powershell
ruff check .
ruff format --check .
mypy
pytest --cov=patchloop --cov-branch --cov-report=term --cov-fail-under=85
```

结果：

- Ruff：通过。
- mypy strict：通过，18 个源文件无类型错误。
- pytest：24 个测试通过，1 个符号链接测试因当前 Windows 主机没有创建权限而跳过。
- 行覆盖率：90%。
- 含分支综合覆盖率：87.02%，高于 CI 的 85% 门槛。

## 超出第一周范围的增量

- 原子文件创建和精确文本替换。
- 受限测试执行和统一 diff。
- 读、写、执行三级权限策略。
- 重复动作检测和工具错误恢复。
- DeepSeek V4 Flash 真实 Provider 与 `patchloop run`。

这些增量已有自动化测试，但不改变下一阶段重点：任务持久化、检查点、恢复语义和真实模型闭环稳定性。

## 已知限制

- 当前任务存储为 JSON 文件，尚未迁移 SQLite。
- 本地测试进程不是强隔离沙箱；不应执行不可信仓库代码。
- DeepSeek 实际网络调用需要用户在进程环境中安全设置 `DEEPSEEK_API_KEY`。
- 当前 Provider 为同步非流式实现。

