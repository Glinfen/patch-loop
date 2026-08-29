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
- [Week 1 验收记录](docs/milestones/WEEK_01_ACCEPTANCE.md)：安装、CLI、测试、类型检查和覆盖率证据。
- [Week 2 验收记录](docs/milestones/WEEK_02_ACCEPTANCE.md)：Plan-Execute、补丁、测试、diff 和最终报告证据。
- [Week 3 验收记录](docs/milestones/WEEK_03_ACCEPTANCE.md)：错误分类、失败恢复、重规划和预算控制证据。
- [Week 4 验收记录](docs/milestones/WEEK_04_ACCEPTANCE.md)：SQLite 持久化、检查点恢复和完整 CLI 生命周期证据。
- [Week 5 验收记录](docs/milestones/WEEK_05_ACCEPTANCE.md)：Python AST 索引、可解释混合检索和 Recall@1 对比证据。
- [Week 6 验收记录](docs/milestones/WEEK_06_ACCEPTANCE.md)：上下文预算、结构化任务记忆和长任务证据保留验收。
- [Week 7 验收记录](docs/milestones/WEEK_07_ACCEPTANCE.md)：Docker 沙箱、风险审批、敏感信息脱敏和任务回放验收。
- [Week 8 验收记录](docs/milestones/WEEK_08_ACCEPTANCE.md)：固定 30 任务集、并发重试和四变体机器可读评测验收。
- [评测协议](docs/EVALUATION.md)：清单格式、仓库指纹、成功判定、基线定义和运行方式。

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

项目已完成前七周运行时、安全与可观测性切片：

- `Task`、`AgentStep`、`ToolCall`、`ToolResult` 等领域模型。
- 模型无关的 Provider 协议和确定性 Fake Provider。
- DeepSeek V4 Flash Provider，支持 thinking、工具调用、重试和用量统计。
- 带参数校验、路径边界和错误分类的 Tool Gateway。
- `list_files`、`read_file`、`search_text` 三个只读工具。
- 显式 `update_plan`，写入和执行前必须先建立计划。
- 原子 `create_file`、结构化 `apply_patch`、精确 `replace_text` 工具。
- 受限 `run_command`、`run_tests` 和 `get_diff` 工具。
- 默认只读的权限策略，以及仓库路径和符号链接边界检查。
- 有步骤、时间、Token、费用、工具失败和重规划预算的 Agent 执行循环。
- 测试/语法/命令失败分类，失败后强制重新规划和重复错误检测。
- SQLite 任务、步骤、工具调用、检查点和产物存储，以及 JSONL 事件轨迹。
- 完成步骤边界检查点、工作区变更快照和中断后恢复，不重复已确认的写操作。
- 包含变更、验证、工具统计和 Token 用量的最终报告与持久化产物。
- `run`、`status`、`resume`、`cancel`、`diff`、`trace` 等任务生命周期命令。
- 默认非交互运行，并把权限集合随任务持久化，恢复时沿用原权限边界。
- Python AST 仓库索引，可提取模块、类、函数、方法、引用和源码—测试映射。
- `search_code` 混合检索工具，融合关键词、符号、轻量语义、符号距离、文件类型、最近访问和测试关联特征。
- 每条代码定位结果包含文件、行号、符号、片段、来源、特征分数和排序理由。
- 可复现的 `benchmark-search` 对比命令；第五周固定 4 任务集上 Recall@1 从 0.25 提升至 1.00。
- Context Engine 同时预算消息和工具 Schema，按任务相关性、新近度及原子 assistant/tool 调用组裁剪历史。
- 被裁剪步骤生成结构化任务记忆，保留未完成计划、关键证据、失败和历史决策。
- 大型工具输出进入模型前保留头尾并附加截断元数据，完整结果仍用于报告和审计。
- `context.built` 轨迹和 `context` CLI 展示预算占用、选择步骤、丢弃步骤和记忆大小。
- Docker 命令沙箱默认关闭网络，并限制挂载目录、CPU、内存、进程数、Linux capabilities、根文件系统和运行时间。
- Tool Gateway 对读、写、执行和危险动作进行低、中、高、严重四级风险判断；交互模式逐次审批，非交互模式只接受显式权限预授权。
- 凭据脱敏统一覆盖 Provider 上下文、JSONL Trace、SQLite 检查点和任务产物。
- Trace 具有稳定事件 ID、任务 Trace ID 和连续序号；`metrics` 与 `replay` 可定位失败步骤、审批结果、工具耗时、Token 和费用。
- 版本化评测清单以仓库树 SHA-256 锁定环境，30 个任务覆盖 bugfix、feature、test、documentation，easy/medium/hard 各 10 个。
- `benchmark` 支持 1～64 个并发 worker、失败重试、确定性结果排序，以及 single-shot、no-plan、text-only、PatchLoop 四变体对比。
- 第八周固定代码定位切片中，四变体成功率分别为 70%、80%、80% 和 100%；完整任务级结果保存在机器可读 JSON 中。

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
patchloop status <task-id> --repo /path/to/repository
patchloop diff <task-id> --repo /path/to/repository
patchloop trace <task-id> --repo /path/to/repository
patchloop context <task-id> --repo /path/to/repository
patchloop metrics <task-id> --repo /path/to/repository
patchloop replay <task-id> --repo /path/to/repository
patchloop resume <task-id> --repo /path/to/repository
patchloop cancel <task-id> --repo /path/to/repository
patchloop index --repo /path/to/repository
patchloop search "分页边界实现" --repo /path/to/repository --limit 5
patchloop benchmark-search --tasks benchmarks/retrieval_tasks.json --root .
patchloop benchmark --manifest benchmarks/evaluation_manifest.json --root . \
  --variant all --jobs 4 --retries 1 \
  --output benchmarks/results/week08_evaluation.json
```

运行 DeepSeek V4 Flash Agent：

首次使用执行工具前构建标准沙箱镜像：

```bash
docker build -f docker/sandbox.Dockerfile -t patchloop-sandbox:py313 .
```

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API key" -MaskInput
patchloop run "修复分页边界错误并运行回归测试" `
  --repo C:\path\to\repository `
  --allow-write `
  --allow-execute `
  --sandbox docker `
  --max-context-tokens 32000 `
  --max-tool-output-chars 8000 `
  --max-cost-usd 1.0
```

密钥只从进程环境变量读取；Provider 上下文、任务、轨迹、SQLite 和产物会对 API Key、Bearer Token、密码及常见敏感字段统一脱敏。默认权限为只读；只有显式传入 `--allow-write` 和 `--allow-execute` 才允许修改文件与运行测试。默认执行后端是无网络 Docker 沙箱；`--sandbox local` 仅用于受信任环境的兼容调试，不提供操作系统级隔离。

当前版本已完成“检索 → 规划 → 修改 → 沙箱测试 → diff → 汇报 → 回放”的确定性端到端闭环，并接入 DeepSeek V4 Flash、SQLite 检查点、任务恢复、可解释 Repository Intelligence、预算化上下文记忆、安全策略与可观测性。流式输出属于后续阶段。

`benchmarks/fixtures/calculator_bug` 提供了第一个固定缺陷仓库，后续写入闭环以 `benchmarks/tasks/calculator_bug.json` 作为自动验收任务。
