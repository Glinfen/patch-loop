# PatchLoop

PatchLoop 是一个面向真实代码仓库的本地优先 Coding Agent。它能够理解自然语言开发任务，自主检索代码、制定计划、调用受控工具修改文件、运行测试，并根据执行结果迭代修复，最终产出可审查的代码补丁和执行报告。

本项目以“大厂软件工程、AI Infra、Agent 工程岗位的简历项目”为目标，重点展示以下能力：

- Agent 核心闭环：规划、执行、观察、反思与恢复。
- 代码智能：仓库索引、符号检索、上下文压缩和依赖分析。
- 工程能力：沙箱隔离、权限控制、可观测性、测试与持续集成。
- 系统评测：任务成功率、成本、时延、工具调用效率和回归分析。
- 产品完成度：CLI 优先，可选 Web 控制台，支持全过程回放和人工审批。

## 文档

- [项目目标](docs/PROJECT_GOALS.md)：项目定位、目标用户、核心能力、成功指标与边界。
- [实施计划](docs/IMPLEMENTATION_PLAN.md)：技术方案、阶段里程碑、验收标准、风险与交付物。

## 推荐项目周期

按照每周 15～20 小时投入，建议用 10 周完成一个可写入简历并可现场演示的版本：

1. 前 4 周完成单 Agent MVP 和端到端闭环。
2. 第 5～7 周补齐检索、沙箱、可观测性和稳定性。
3. 第 8～9 周完成基准评测、性能优化和对比实验。
4. 第 10 周完成演示、文档、技术文章和简历材料。

## 最终演示场景

在一个陌生的中型开源仓库中输入任务，例如“修复 Issue 中描述的分页边界错误，并补充回归测试”。PatchLoop 应能够：

1. 扫描仓库并定位相关模块。
2. 给出带依据的执行计划。
3. 在隔离环境中修改代码并运行测试。
4. 遇到失败后读取错误信息并自动迭代。
5. 输出补丁、测试结果、关键决策、调用成本及完整轨迹。

项目详细范围和验收口径以 `docs` 目录中的文档为准。

## 当前进度

项目已完成首个运行时基础切片：

- `Task`、`AgentStep`、`ToolCall`、`ToolResult` 等领域模型。
- 模型无关的 Provider 协议和确定性 Fake Provider。
- 带参数校验、路径边界和错误分类的 Tool Gateway。
- `list_files`、`read_file`、`search_text` 三个只读工具。
- 原子 `create_file`、精确 `replace_text`、`run_tests` 和 `get_diff` 工具。
- 默认只读的权限策略，以及仓库路径和符号链接边界检查。
- 有步骤、时间预算和重复动作检测的 Agent 执行循环。
- JSONL 事件轨迹和本地任务存储。
- `task create`、`task show`、`tools`、`trace` CLI 命令。

## 本地开发

需要 Python 3.12 或更高版本：

```bash
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy
pytest
```

创建并读取任务：

```bash
patchloop task create "修复分页边界错误" --repo /path/to/repository
patchloop task show <task-id> --repo /path/to/repository
patchloop tools --repo /path/to/repository
```

当前版本已完成“读取 → 修改 → 测试 → diff → 汇报”的确定性端到端闭环。模型测试仍使用 Fake Provider；真实模型 Provider、SQLite 检查点和 Docker 沙箱属于后续阶段。

`benchmarks/fixtures/calculator_bug` 提供了第一个固定缺陷仓库，后续写入闭环以 `benchmarks/tasks/calculator_bug.json` 作为自动验收任务。
