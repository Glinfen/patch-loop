# PatchLoop

PatchLoop 是一个运行在本地代码仓库中的命令行 Coding Agent。你可以用自然语言描述开发任务，
让它检索代码、制定计划、修改文件、运行测试，并根据执行结果继续修复，最终得到可审查的 diff、
执行报告和完整轨迹。

PatchLoop 默认以只读权限启动。文件写入和命令执行必须显式授权，命令可在受限 Docker 容器中
运行；任务状态、检查点、记忆和事件保存在目标仓库的 `.patchloop/` 目录中，进程中断后可以继续。

## 功能

- 自然语言驱动的“检索 → 规划 → 修改 → 测试 → 修复 → 汇报”执行闭环。
- Python AST 索引与混合代码检索，返回文件、行号、符号、片段和排序依据。
- 结构化计划、原子文件修改、受限命令执行、测试运行和 Git diff 检查。
- SQLite 持久化、步骤检查点、任务恢复，以及对已确认工具结果的安全回放。
- 工作记忆、语义记忆和情景记忆，支持预算化上下文、压缩谱系和可解释召回。
- Session、Task 与 Workspace 执行租约，防止多个执行者同时写入同一工作区。
- Docker 命令沙箱、权限策略、风险审批、路径边界检查和敏感信息脱敏。
- JSONL 事件轨迹、任务回放、指标统计、上下文诊断和机器可读评测。
- DeepSeek OpenAI-compatible Provider，支持 thinking、重试、Token 用量和成本统计。

## 环境要求

- Python 3.12 或更高版本。
- Git。
- DeepSeek API Key，或兼容端点的 API Key、Base URL 与模型 ID。
- Docker（推荐）：用于隔离测试和命令执行。不使用 Docker 时可以选择本地执行后端，但它不提供
  操作系统级隔离。

## 安装

从源码安装：

```bash
git clone https://github.com/Glinfen/patch-loop.git
cd patch-loop
python -m venv .venv
```

激活虚拟环境：

```bash
# Linux / macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

安装 PatchLoop：

```bash
python -m pip install --upgrade pip
python -m pip install .
patchloop --help
```

如果要使用默认 Docker 沙箱，先构建镜像：

```bash
docker build -f docker/sandbox.Dockerfile -t patchloop-sandbox:py313 .
```

## 配置模型

最少只需设置 API Key：

```bash
export DEEPSEEK_API_KEY="your-api-key"
```

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API key" -MaskInput
```

也可以覆盖服务地址和模型：

| 环境变量 | 备用变量 | 用途 | 默认值 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | `LLM_API_KEY` | API 凭据 | 无 |
| `DEEPSEEK_BASE_URL` | `LLM_BASE_URL` | OpenAI-compatible API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | `LLM_MODEL_ID` | 模型名称 | `deepseek-flash` |

`run`、`resume` 和 Session 的 `start/resume` 只读取当前进程环境变量，不会自动信任目标仓库中的
`.env`。Provider 上下文、SQLite、轨迹和任务产物会对常见 API Key、Bearer Token、密码及敏感
字段进行脱敏。

### Provider profiles

本仓库的 [providers.toml](providers.toml) 提供默认 `local-openai` profile，使用标准
Chat Completions。启动时显式指定这一份配置和根目录 `.env`：

```powershell
# 仅解析配置，不发送模型请求，也不校验 API Key 是否有效。
.\.venv\Scripts\python.exe -m patchloop provider check local-openai --provider-config .\providers.toml --env-file .\.env

# 填好 OPENAI_API_KEY 后，在测试仓库运行。
.\.venv\Scripts\python.exe -m patchloop run "检查仓库" --repo D:\path\to\test-repo --provider-config .\providers.toml --env-file .\.env --prompt-cache-layout append_only
```

Profile 的 `base_url_env`、`model_env`、`reasoning_effort_env` 分别映射 `.env` 中的
`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_REASONING_EFFORT`。优先级为进程环境、显式
`.env`、profile 默认值；显式 `--model` 优先于环境变量。模型必须已有对应能力定义，
不能只改模型名就沿用其他模型的能力。密钥单独从 `credential_env` 指向的变量读取。
这些映射只在新任务创建时解析；恢复使用已保存的 binding。

本地 profile 的 128,000 上下文和 8,192 输出是暂定声明，工具/usage 支持尚待该代理验证；
缓存统计暂未声明支持。价格按用户确认的“不按 token 计费”设置为零，是本地预算口径，
不是上游模型报价；零价基线不能用于证明 PPS 的费用下降目标。此文件需显式选择，
不会自动加载被操作仓库里的配置或 `.env`。

内置 `deepseek` profile 继续支持上面的环境变量。通用 Chat Completions、OpenAI Responses 和
本地兼容服务使用 `~/.patchloop/providers.toml`，也可以通过 `--provider-config` 指定其他文件。
下面给出四种接入方式；`models.<id>` 下必须显式声明能力、生成参数和价格，免费本地模型也要写
零价，避免预算把“未知价格”误判为免费。

```toml
schema_version = 1
default_profile = "chat"

# 1. DeepSeek：可直接使用内置 profile，无需在此重复配置。

# 2. 标准 Chat Completions
[profiles.chat]
protocol = "chat_completions"
base_url = "https://chat.example.com/v1"
credential_env = "CHAT_API_KEY"
default_model = "chat-model"

[profiles.chat.models.chat-model.capabilities]
tools = true
multiple_tool_calls = true
streaming = true
context_window_tokens = 32768
max_output_tokens = 4096
usage_supported = true
cache_usage_supported = true

[profiles.chat.models.chat-model.generation]
max_output_tokens = 4096

[profiles.chat.models.chat-model.pricing]
version = "chat-2026-09"
input_per_million = 1.0
cached_input_per_million = 0.25
output_per_million = 4.0

# 3. Responses（其余 capabilities/generation/pricing 字段写法相同）
[profiles.responses]
protocol = "responses"
base_url = "https://responses.example.com/v1"
credential_env = "RESPONSES_API_KEY"
default_model = "responses-model"

[profiles.responses.models.responses-model.capabilities]
tools = true
multiple_tool_calls = true
streaming = true
reasoning_transport = "responses_items"
context_window_tokens = 128000
max_output_tokens = 8192
usage_supported = true
cache_usage_supported = true

[profiles.responses.models.responses-model.generation]
max_output_tokens = 8192
reasoning_enabled = true
reasoning_effort = "medium"

[profiles.responses.models.responses-model.pricing]
version = "responses-2026-09"
input_per_million = 2.0
cached_input_per_million = 0.5
output_per_million = 8.0

# 4. 无凭据的 loopback 本地服务（显式零价）
[profiles.local]
protocol = "chat_completions"
base_url = "http://127.0.0.1:8000/v1"
auth = "none"
default_model = "local-model"

[profiles.local.models.local-model.capabilities]
tools = true
multiple_tool_calls = true
streaming = true
context_window_tokens = 32768
max_output_tokens = 4096
usage_supported = true

[profiles.local.models.local-model.generation]
max_output_tokens = 4096

[profiles.local.models.local-model.pricing]
version = "local-zero-v1"
input_per_million = 0
output_per_million = 0
```

先进行本地检查；只有显式增加 `--connect` 才会发出一个最小请求：

```bash
patchloop provider list --provider-config /path/to/providers.toml
patchloop provider show local --provider-config /path/to/providers.toml
patchloop provider check local --provider-config /path/to/providers.toml
patchloop provider check local --provider-config /path/to/providers.toml --connect
```

新 Task 可使用 `--provider`、`--model` 和 `--provider-config` 选择配置。Task 创建后 binding 固定，
`resume` 不允许切换 profile、模型、协议或端点；旧的无 binding Task 只能显式使用
`--legacy-provider` 完成一次性绑定。每个网络 attempt 在发送前按未缓存价格保守预留预算，已送达但
用量未知时预留不会释放。机器输出默认或 `--json` 为单个对象；流式消费者使用
`--events-jsonl`，终端查看可使用 `--human`，三者不能组合。

### Provider 验收

PGW 的离线验收通过真实 loopback HTTP 同时运行 Chat Completions 与 Responses，并对恢复、取消、
审批和 lease 场景各保留 3 次结果：

```bash
python -m patchloop.evaluation.provider \
  --manifest tests/fixtures/providers/acceptance.json \
  --output benchmarks/results/provider_gateway_acceptance.json
```

真实验收必须先在环境中显式提供 `.env.example` 所列的 `PGW_DEEPSEEK_*`、`PGW_OPENAI_*` 或
`PGW_LOCAL_*` 锁定值，再增加 `--real` 并使用独立输出文件。Runner 不会下载模型、启动本地服务，
也不会把缺凭据、缺服务或 pytest skip 计为通过；这些结果会保留为 `unverified`。API Key 只经环境
传给对应子进程，不写入报告。

## Prompt 前缀稳定性（PPS）

`run` 和 `session start` 都支持 `--prompt-cache-layout legacy|stable|append_only`。
当前新任务默认仍为 `legacy`；真实 Provider 配对试跑已执行，质量修复已通过最新小样本验证，正式收益验收仍未通过，默认切换等待 PPS 发布门禁。
`append_only` 在同一 epoch 内保持已发送消息及工具顺序，新输入和记忆增量只追加到末尾；
预算压缩会显式创建新 epoch，保留固定根前缀和最新摘要。

```bash
patchloop run "检查仓库" --repo /path/to/repo --prompt-cache-layout append_only
patchloop session --repo /path/to/repo start SESSION_ID "检查仓库" --prompt-cache-layout append_only

# 回退通过新建任务选择 legacy；已有任务 resume 使用保存的布局与 Provider 配置。
patchloop run "检查仓库" --repo /path/to/repo --prompt-cache-layout legacy
```

离线验收走实际 Runtime、MemoryManager、ToolGateway 和 checkpoint 恢复，不产生付费调用：

```bash
patchloop benchmark-cache --suite prefix-runtime --output benchmarks/results/pps_prefix_runtime.json
patchloop validate-cache-gates --profile pps --report benchmarks/results/pps_prefix_runtime.json \
  --output benchmarks/results/pps_prefix_acceptance.json
```

第二条命令在真实证据缺失时输出 `unverified` 检查项并返回退出码 1。`--allow-simulated`
不会绕过 PPS 的真实用量和费用门禁。已生成的报告见
[Runtime 结构报告](benchmarks/results/pps_prefix_runtime.json) 和
[PPS 验收报告](benchmarks/results/pps_prefix_acceptance.json)。

append_only 优化的分层门禁为：L0 是 Fake/loopback 固定动作开销与恢复检查；L1 是两场景各一对的有限真实验收；L2 是同一修订下两场景各三对的 PPS 正式配对；L3 才是第二阶段至少 5 个仓库、30 个任务、每项至少 3 次的端到端验收。L0 只授权进入 L1，不等于真实收益、费用或默认发布已经通过。

每次源码变更后先重新生成 L0 产物。下面的 runner 命令故意不带 `--execute-real`，只复验报告、源码、fixture、证据文件和预算；它不会读取 `.env`、创建任务或发出模型请求：

```powershell
.venv\Scripts\python.exe -m patchloop benchmark-cache --suite append-only-overhead --output benchmarks/results/aop_overhead_runtime.json
.venv\Scripts\python.exe -m patchloop validate-cache-gates --profile aop --report benchmarks/results/aop_overhead_runtime.json --output benchmarks/results/aop_readiness.json
.venv\Scripts\python.exe benchmarks/run_pps_server.py --work-root .patchloop/pps-l1-preflight --env-file .env --readiness-report benchmarks/results/aop_readiness.json --repeats 1 --max-batch-input-tokens 1600000 --max-batch-output-tokens 320000 --max-batch-cost-usd 4
```

预检输出必须为 `status=preflight_only`、`model_requests=0`。归档实现提交和这两份 L0 产物后，后续获准的 L1 执行轮复用同一命令及全新 `--work-root`，并显式增加 `--execute-real`；可用 `--provider`、`--model` 锁定配置。runner 为每项任务分配批次剩余预算，任何中断、未知 usage、预算不足、任务/隐藏测试失败都会保存 `manifest.json` 的 `partial_reason` 并停止新任务。`--inject-inflight-cancel` 仅用于独立取消故障批次，不得混入成本样本。

暂停任务由 runner 在无在途 Provider attempt 的边界持久化并自动恢复。若进程意外退出，先检查 partial 证据，不要在原目录直接扩量：

```powershell
Get-Content .patchloop/pps-l1-*/manifest.json
$trialRepo = "D:\path\to\pps-l1-batch\contract-migration-1-append_only\workspace"
$sessionId = "session-id-from-manifest"
.venv\Scripts\python.exe -m patchloop session --repo $trialRepo --json resume $sessionId
```

恢复仅用于收敛已存在的持久化任务。未知用量或取消故障不能通过补跑覆盖；应保留原目录，重新生成 readiness，并在明确批准后用新目录启动新批次。

L1 通过后，配置好 Provider，在新的独立工作目录将 `--repeats` 调为 `3`，对同样的两类任务各执行三次 legacy/append_only 配对实验，
按配对交错运行，并锁定模型、端点、工具、输入预算及价格版本。每轮任务必须设置费用预算；
缓存是服务端 best-effort，交错运行也不能保证严格冷缓存隔离。把真实任务的 JSONL 路径写入 manifest：

```json
{
  "runs": [
    {
      "trace": "traces/contract-legacy-1.jsonl",
      "variant": "current_layout",
      "repeat": 1,
      "batch_id": "pps-batch-1",
      "task_case": "contract-migration",
      "pair_id": "contract-1"
    },
    {
      "trace": "traces/contract-append-1.jsonl",
      "variant": "append_only",
      "repeat": 1,
      "batch_id": "pps-batch-1",
      "task_case": "contract-migration",
      "pair_id": "contract-1"
    }
  ]
}
```

上例仅展示一对；实际需要补齐两个 task_case 各三对。路径相对于 manifest，单个 trace 包含多个
任务时还需提供 `task_id`。Collector 按 `request_id` 关联普通请求、压缩和 usage，按 attempt ID
去重；开始但没有明确结束的 attempt 保持未知费用。导入及评估命令如下：

```bash
patchloop benchmark-cache --mode provider --suite prefix-runtime --trace-manifest pps-traces.json \
  --output benchmarks/results/pps_provider_pairs.json
patchloop validate-cache-gates --profile pps --report benchmarks/results/pps_provider_pairs.json \
  --local-report benchmarks/results/pps_prefix_runtime.json --quality pps-quality.json \
  --enable-append-only --output benchmarks/results/pps_prefix_acceptance.json
```

`pps-quality.json` 使用 `MemoryQualityEvidence` 字段，记录实际 public/hidden 测试计数、
`verified_task_cases`、成功率与关键事实/约束召回的 candidate/baseline 对照、越界修改、秘密泄漏、
失效事实使用、`approval_bypasses` 和 `fault_matrix_passed`。未知项保留 `null`，不能用缓存结果代替。
报告中的 rollout 选择只是门禁结果，不会修改已保存任务或自动改写程序默认值。

`normalized_messages_v1` 指标证明本地消息结构前缀；`common_prefix_estimated_tokens` 和旧 JSON LCP
都是估算。真实命中率仅使用 Provider 报告的 hit/miss，按 token 加权；成本含压缩和 attempt，
未知费用会阻止费用验收。旧 checkpoint 缺消息摘要向量时指标不可用；旧任务缺布局字段时仍按
legacy 恢复。新 append_only 数据不保证能被旧版本程序继续执行。

## 快速开始

先准备一个独立 Git 仓库。不要把第一次试运行指向 PatchLoop 自身或包含重要未提交改动的目录：

```bash
mkdir -p /tmp/patchloop-demo
cd /tmp/patchloop-demo
git init
printf '# Demo\n' > README.md
git add README.md
git commit -m "initial state"
```

以下命令中的 `/path/to/repository` 均替换为这个独立仓库的绝对路径。

### 1. 创建并启动 Session

创建 Session；输出中的 `session_id` 用于后续命令：

```bash
patchloop session --repo /path/to/repository create
```

启动任务。只有 `start` 和 `resume` 会获取 Execution 并调用模型：

```bash
patchloop session --repo /path/to/repository start <session-id> \
  "修复分页边界错误并补充回归测试" \
  --allow-write \
  --allow-execute \
  --sandbox docker
```

默认权限仍然只有读取：

- `--allow-write` 允许 PatchLoop 创建和修改仓库文件。
- `--allow-execute` 允许 PatchLoop 运行受限命令和测试。
- `--sandbox docker` 使用无网络、受资源限制的 Docker 沙箱。
- `--sandbox local` 直接在主机运行命令，仅适合受信任的仓库和环境。

`create` 和 `enter` 不会自行执行未知动作。任务数据库、检查点、事件轨迹与报告保存在目标仓库的
`.patchloop/` 中。

### 2. 从第二终端补充约束或控制执行

`start` 正在运行时，可以从另一个终端读取状态并追加约束：

```bash
patchloop session --repo /path/to/repository show <session-id>
patchloop session --repo /path/to/repository send <session-id> \
  "不要修改公共 API；先补充边界回归测试" \
  --client-submission-id constraint-001
```

`client-submission-id` 可用于安全重试提交；相同 ID 和内容不会生成重复 Turn。暂停和最终取消也可
从第二终端提交：

```bash
patchloop session --repo /path/to/repository pause <session-id>
patchloop session --repo /path/to/repository cancel <session-id>
```

执行中的 Ctrl+C 会提交 pause 并显示清理状态；退出 `session enter` 只离开交互界面，不会取消任务。

### 3. 明确审批并继续

写入或执行动作需要持久化的一次性 Approval。缺少授权时，`start` 或 `resume` 返回
`waiting_for_approval`、Approval ID 和退出码 10，后端动作不会执行：

```bash
patchloop approval --repo /path/to/repository list <task-id>
patchloop approval --repo /path/to/repository decide <approval-id> --approve --source operator
patchloop session --repo /path/to/repository resume <session-id>
```

拒绝动作时使用 `--deny`。Approval 精确绑定 Effect、参数、工作区、策略版本和配置版本，只能消费
一次；审批命令本身不会隐式启动新的执行者。

CI 也必须执行相同的显式 `list → decide → resume` 流程。旧 `run --non-interactive` 和默认的
`non_interactive=true` 不再自动批准写入或执行；该选项仅保留兼容性，不能作为授权。

### 4. 重启和 unknown Effect 恢复

终端或 PatchLoop 进程退出后，重新运行以下命令即可读取 workspace-local SQLite 中的状态：

```bash
patchloop session --repo /path/to/repository show <session-id>
patchloop session --repo /path/to/repository resume <session-id>
```

普通暂停或审批等待可直接恢复。如果状态为 `recovery_required`，先查看证据：

```bash
patchloop session --repo /path/to/repository recover <session-id>
```

此状态表示原动作结果未知。普通 `resume` 或 `approval decide` 不会重试它。应根据外部证据选择确认
成功、确认失败、放弃或创建全新的重试 Effect：

```bash
patchloop session --repo /path/to/repository recover <session-id> \
  --effect-id <effect-id> \
  --confirm-success \
  --result "verified result" \
  --evidence '{"verified_by":"operator"}'

patchloop session --repo /path/to/repository recover <session-id> \
  --effect-id <effect-id> \
  --retry \
  --acknowledge-duplicate-risk \
  --evidence '{"reviewed_by":"operator"}'
```

原动作可能已经产生外部副作用，因此重试可能重复执行。JSON 模式必须显式传入
`--acknowledge-duplicate-risk`；`--human` 模式可以进行交互确认。新重试仍会生成独立 Approval，
需要批准后再 `resume`。

### 5. 查看 diff、报告和轨迹

`session show` 返回活动目标、计划变化、模型/工具边界、测试结果、Execution 所有权、待审批项和
可复制的下一步命令。也可以使用兼容的 Task 查询入口：

## 任务恢复与检查

`session start` 或兼容入口 `run` 输出的 Task ID 可用于后续操作：

```bash
patchloop status <task-id> --repo /path/to/repository
patchloop diff <task-id> --repo /path/to/repository
patchloop trace <task-id> --repo /path/to/repository
patchloop metrics <task-id> --repo /path/to/repository
patchloop context <task-id> --repo /path/to/repository
patchloop replay <task-id> --repo /path/to/repository
patchloop resume <task-id> --repo /path/to/repository
patchloop cancel <task-id> --repo /path/to/repository
```

其中：

- `status` 查看任务状态、预算和最终报告。
- `diff` 查看 PatchLoop 记录的代码变更。
- `trace` 查看逐步事件轨迹。
- `metrics` 查看耗时、工具调用、Token、费用和失败统计。
- `context` 查看模型上下文预算、裁剪结果和记忆占用。
- `replay` 按顺序回放执行过程。
- `resume` 从持久化检查点继续未完成任务。
- `cancel` 请求取消任务。

## 旧数据库升级与回退

升级 PatchLoop 前先停止该仓库的所有 PatchLoop 执行者，并复制整个 `.patchloop/` 目录作为外部
备份。新版本第一次打开 `.patchloop/patchloop.db` 时会在事务中升级旧库；有既有数据时还会在
`.patchloop/backups/` 创建升级前 SQLite 快照和同名 JSON manifest：

```bash
patchloop session --repo /path/to/repository list
```

升级不会重复创建映射。旧的已完成 Task 会映射为只读 Session；无法证明安全恢复的旧活动 Task
会进入 `recovery_required`，必须通过 `session recover` 核验，不能直接重跑。

需要回退时：

1. 停止所有 PatchLoop 执行者，确认没有进程仍在写该仓库。
2. 找到 `.patchloop/backups/*.sqlite` 及其 `.sqlite.json` manifest。
3. 使用经过校验的回退函数恢复升级前快照；它会先保留当前升级后数据库。
4. 再安装 manifest 中记录的兼容 PatchLoop 版本。不要用旧版本直接打开升级后的数据库。

```bash
python -c "from pathlib import Path; from patchloop.sqlite_support import restore_migration_backup; print(restore_migration_backup(Path('/path/to/repository/.patchloop/patchloop.db'), Path('/path/to/repository/.patchloop/backups/<backup>.sqlite'), writers_stopped=True))"
```

如果外部备份与自动快照都不存在，不要尝试手工删除表或修改 schema version；保留数据库并先恢复
备份或升级到兼容版本。

## 代码检索

建立或刷新仓库索引：

```bash
patchloop index --repo /path/to/repository
```

检索代码：

```bash
patchloop search "分页边界实现" --repo /path/to/repository --limit 5
```

查看 Agent 可用工具：

```bash
patchloop tools --repo /path/to/repository
```

## 记忆检查

可以按类型、状态、步骤和查询条件检查任务记忆：

```bash
patchloop memory <task-id> \
  --repo /path/to/repository \
  --kind semantic \
  --status active \
  --step 4 \
  --query "分页接口约束"
```

每条结果会展示来源、召回原因、评分分量和替代关系，便于判断 Agent 为什么使用某段历史信息。

## 安全说明

- 默认只读；写入与执行必须由命令行参数显式开启。
- 文件工具限制在目标仓库内，并检查路径穿越和符号链接边界。
- Docker 沙箱默认关闭网络，并限制挂载、CPU、内存、进程数、Linux capabilities 和运行时间。
- Workspace writer 使用数据库租约和 fencing generation，旧执行者不能提交新结果或释放新 owner
  的租约。
- 租约过期后仍会核验受管进程树或容器；无法确认已停止时，任务进入
  `recovery_required`，不会直接开放新的写执行。
- 本地沙箱不提供系统级隔离。对不可信仓库或命令，应使用 Docker 后端。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `patchloop session create/start` | 创建 Session 并启动活动 Task |
| `patchloop session show/send` | 查看 Session 或追加持久化约束 |
| `patchloop session pause/cancel/resume/close` | 控制 Session 生命周期 |
| `patchloop session recover` | 检查并显式处置 unknown Effect |
| `patchloop approval list/decide` | 查看和决定一次性精确审批 |
| `patchloop run/resume` | 兼容的旧任务启动和恢复入口 |
| `patchloop status` | 查看任务状态和报告 |
| `patchloop diff` | 查看代码变更 |
| `patchloop trace` | 查看事件轨迹 |
| `patchloop replay` | 回放任务执行过程 |
| `patchloop metrics` | 查看运行指标与成本 |
| `patchloop context` | 检查上下文构建结果 |
| `patchloop memory` | 检索和解释任务记忆 |
| `patchloop index` | 建立仓库代码索引 |
| `patchloop search` | 执行混合代码检索 |
| `patchloop tools` | 列出可用工具及输入 Schema |

运行 `patchloop <command> --help` 可以查看某个命令的全部参数。
