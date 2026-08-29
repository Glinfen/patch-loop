# Week 2 验收记录

## 结论

PatchLoop 已于 2026-08-29 通过第二周“最小执行闭环”验收。确定性 Agent 能在固定缺陷仓库中先建立计划，再原子修改代码、运行回归测试、生成统一 diff，并持久化结构化最终报告。

## 交付能力

| 能力 | 实现 | 验收证据 |
| --- | --- | --- |
| Plan-Execute | `Plan`、`PlanItem`、`update_plan`；写入和执行默认要求先有计划 | 未规划写入会返回 `permission_denied`，规划后同一调用成功 |
| 创建文件 | `create_file` 使用同目录临时文件和原子替换 | 单元测试验证不覆盖已有文件及 diff 跟踪 |
| 应用补丁 | `apply_patch` 一次应用多个精确编辑，每项包含出现次数断言 | 任一编辑冲突时整批不写入；成功路径一次原子落盘 |
| 受限命令 | `run_command` 只开放固定的 Git 诊断与 Python `compileall` 命令 | 任意 Python `-c` 被拒绝，允许命令返回结构化退出码 |
| 测试执行 | `run_tests` 只接受 pytest/unittest，限制路径、环境、超时和输出 | 样例修复后回归测试返回退出码 0 |
| 统一 diff | `FileChangeTracker` 保留首次修改前内容 | 最终产物包含 `calculator.py` 的 unified diff |
| 最终报告 | `TaskReport` 汇总变更、验证、工具成功率、Token 和摘要 | 成功与失败任务都会携带机器可读报告 |
| 产物持久化 | `ArtifactStore` 原子写入 `report.json` 和 `changes.diff` | 端到端测试校验两个产物存在且内容正确 |

## 端到端场景

固定任务：`benchmarks/tasks/calculator_bug.json`。

执行轨迹：

1. 读取 `calculator.py` 并定位整数地板除法错误。
2. 调用 `update_plan`，声明检查、修改和验证步骤。
3. 使用 `apply_patch` 将地板除法改为真除法。
4. 使用 `run_tests` 执行 `test_calculator.py`。
5. 使用 `get_diff` 检查最终变更。
6. 将计划所有步骤标记为完成并附加证据。
7. 输出总结，生成任务报告和 diff 产物。

自动断言包括：

- 任务状态为 `completed`。
- 三个计划步骤全部完成。
- 修改文件仅包含 `calculator.py`。
- 回归测试退出码为 0。
- diff 包含预期修复。
- `report.json` 与 `changes.diff` 均已持久化。

## 安全边界

- 写入和执行权限仍需 CLI 显式开启。
- 即使权限已开启，Agent 也必须先创建执行计划。
- 补丁采用精确匹配与出现次数断言，不进行模糊套用。
- `run_command` 不是通用 Shell，不接受自由命令字符串。
- 本地进程模式仍不等价于 Docker 强隔离，不应运行不可信测试代码。

## 下一阶段

第三周重点转向失败恢复：更细的错误分类、测试失败后的重新规划、Token/费用预算，以及跨步骤的重复错误检测。

