# PatchLoop 端到端代码任务评测

## 目标

该套件评估 Agent 是否真的完成代码修改并通过测试，不把代码定位、Agent 自述完成或一次工具调用视为成功。它与 `benchmark`、`experiment` 的离线定位切片分开报告。

默认清单是 `benchmarks/coding_tasks.json`，当前包含 6 个独立 Python 小仓库：

| 任务 | 类型 | 难度 | 核心能力 |
| --- | --- | --- | --- |
| calculator-division | 缺陷修复 | easy | 算术语义修复 |
| pagination-one-based | 缺陷修复 | easy | 分页边界 |
| cache-ttl-boundary | 缺陷修复 | easy | 时间边界 |
| slug-lowercase | 缺陷修复 | medium | 输入验证 |
| retry-delay-cap | 功能开发 | medium | API 扩展与参数行为 |
| csv-escaping | 功能开发 | hard | 标准格式转义 |

每个仓库在发布清单时都处于测试失败状态。任务覆盖目标源码、不可修改文件、固定测试命令、步骤预算和费用预算，并由 SHA-256 锁定仓库内容。

hard suite 使用 `hidden_tests` 为任务声明独立的源文件、工作区目标路径和 SHA-256。评测器在 Agent 停止后才校验并复制这些文件，Agent 的文件工具和测试过程无法提前读取；隐藏测试注入不会计入 Agent 变更范围。

## 独立成功判定

每次运行按以下协议执行：

1. 校验 fixture 指纹，发现任务集漂移立即停止。
2. 将 fixture 复制到唯一工作区，Agent 不接触原始任务仓库。
3. 运行完整 PatchLoop Runtime，允许受控读取、写入和命令执行。
4. Agent 结束后创建新的沙箱实例，由评测器重新运行清单中的固定测试命令。
5. 只有外部测试通过、所有预期源码已修改、没有修改禁止文件且没有其他额外文件变更时，任务才成功。

`agent_status` 与 `success` 分开记录。Agent 即使自述完成，只要独立验证不通过，最终仍判失败。报告还保存步骤、工具调用、Token、费用、时延、测试输出、工作区和轨迹路径，便于逐项回放。

## 运行

推荐使用无网络 Docker 沙箱：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API key" -MaskInput
patchloop benchmark-code `
  --manifest benchmarks/coding_tasks.json `
  --root . `
  --sandbox docker `
  --repeats 1 `
  --output benchmarks/results/code_benchmark_latest.json
```

也可以在 `--root` 指向的项目根目录创建已被 Git 忽略的 `.env`：

```dotenv
LLM_API_KEY=your-key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL_ID=deepseek-v4-flash
```

显式进程环境变量的优先级高于 `.env`。同时兼容 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_MODEL`；密钥不会写入评测报告或轨迹。只有 `benchmark-code` 会从显式评测根目录读取该文件，针对任意目标仓库的 `run` 和 `resume` 不会自动信任仓库内配置。

没有 Docker 的受信任开发机可以显式使用 `--sandbox local`。该模式会在宿主机运行 fixture 的固定测试命令，不提供操作系统级隔离。

调试失败任务时可以传入 `--task <task-id1,task-id2>` 只运行逗号分隔的选定任务，避免对已通过任务重复产生模型费用。

## 结果解释

- `success_rate` 是独立验证后的端到端修复率。
- `test_pass_rate` 只表示测试结果，不包含变更范围约束。
- `agent_completion_rate` 只表示 Agent 自身的完成状态，不能单独作为正确率。
- 单轮 6 个小任务适合冒烟测试，不足以代表未知大型仓库的通用能力。
- `FakeProvider` 端到端测试只验证评测器和 Runtime 协议，不计入模型成绩。

公开测试便于回放和诊断，但可能被模型针对。后续扩大套件时应加入只对外部判定器可见的隐藏测试、跨语言任务和真实仓库留出集。

当前 hard suite 已加入运行时不可见测试，但测试源码仍随项目发布，不属于保密留出集。
