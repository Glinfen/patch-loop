# PatchLoop 评测协议

## 目标

评测框架以同一份版本化清单运行多个执行变体，并输出任务级机器可读证据。第八周固定套件评估的是“根据开发目标定位正确代码或文档路径”的 Repository Intelligence 切片，不把定位成功率表述为完整代码修复成功率。端到端修改、测试与费用评测将在后续套件复用同一清单和执行器协议扩展。

## 清单

默认清单位于 `benchmarks/evaluation_manifest.json`，包含：

- `schema_version`、套件 ID 和套件 revision。
- 仓库相对路径、逻辑 revision、完整文件树 SHA-256、环境构建命令和测试命令。
- 任务 ID、目标、检索查询、任务类型、难度、Top-K 和成功条件。

运行前会重新计算仓库树指纹。目录越过评测根目录或任何受管文件发生变化时，评测在执行任务前失败。`.git`、`.patchloop`、pytest cache 和 Python bytecode 不进入指纹。

## 固定任务集

`patchloop-week08` 包含 30 个任务：

- bugfix、feature、test 各 8 个，documentation 6 个。
- easy、medium、hard 各 10 个。
- 所有任务指向 `benchmarks/fixtures/eval_suite` 的固定 revision。
- Fixture 自带 10 项 pytest，用于确认仓库环境有效。

成功判定只读取执行器返回的候选路径，计算期望路径 Recall，并与任务的 `minimum_path_recall` 比较。判断逻辑独立于执行器，避免每个基线使用不同口径。

## 执行变体

- `single_shot`：只保留纯文本检索第一项，不规划。
- `no_plan`：使用混合检索 Top-K，不生成计划。
- `text_only`：使用纯文本检索 Top-K，并记录单步检索意图。
- `patchloop`：使用混合检索、任务类型路由和两步证据验证计划；文档任务会补充通用文档检索。

这些变体是确定性离线基线，不调用远程模型，因此适合 CI 回归。它们不等价于真实模型消融；第九周将基于 Provider 执行器增加模型成本、时延和完整任务成功判定。

## 并发、重试与报告

`EvaluationRunner` 使用固定大小线程池。每个任务失败或抛出异常后最多重试 `retries` 次，所有尝试及错误都会保留；最终结果始终恢复为清单顺序，因此 worker 完成时序不影响报告结构。

运行完整套件：

```bash
patchloop benchmark \
  --manifest benchmarks/evaluation_manifest.json \
  --root . \
  --variant all \
  --jobs 4 \
  --retries 1 \
  --output benchmarks/results/week08_evaluation.json
```

报告包含每个变体的总体成功率、尝试次数、按难度和类型的成功率，以及每个任务的候选路径、Recall、计划步数、耗时和错误。
