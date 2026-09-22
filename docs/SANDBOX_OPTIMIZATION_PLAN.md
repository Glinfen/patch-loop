# Sandbox 真实验收优化方案

日期：2026-09-21。状态：Ready for implementation；真实 Linux 验收需满足第 9 节环境前置条件。

本文只规划实现，不表示已修复或已通过新验收。按 `docs/PLANNING_GUIDE.md` 编写。

## 1. Problem

### Current Problem

基线代码：`b167ae69adbbd83a76df6d2d8d17e77cb7e86576`，与本地 HEAD、对抗报告的 `source_commit` 一致。工作区另有未提交改动；执行本方案时不要覆盖它们。

| 输入 | 已有结果 | 正确解读 |
| --- | --- | --- |
| `benchmarks/results/sandbox_real_adversarial_linux.json` | 24 项：17 passed、5 failed、1 unverified、1 not_applicable | 当前 Linux Docker 整体验收失败；部分 passed 还需补正向对照 |
| `benchmarks/results/srf07_backend_acceptance_linux_real.json` | Docker 2 passed、1 failed；Windows 3 unverified | Docker 接管失败；不能据此认定 Windows 失败或通过 |
| `benchmarks/results/sandbox_policy_integration_linux.xml` | 30 tests，0 failures/errors/skips | 证明已有 Policy/Approval/Workspace 集成回归通过，不等价于 Docker 隔离通过 |
| `tests/acceptance/run_real_sandbox_matrix.py` | SHA256 `b0964b681893a205748a89ac35747a1e3745d437c95717fd97d5d209348ee92d` | 与报告中的 harness hash 一致；脚本本身也需要修复 |

失败与根因必须分开记录：

| 问题 | 证据与确定程度 | 优先级 |
| --- | --- | --- |
| 工作区不可写 | `/workspace/write-probe.txt` 抛出 EACCES；当前未设置 `--user`，镜像 Dockerfile 未指定 USER。UID/GID 不匹配、root 丢弃 DAC 能力等是可解释原因，但报告没有实际 UID、目录 mode、userns 信息，不能宣称根因已实测确认 | P0 |
| 内存探针越过预期上限 | `--memory=512m` 下申请并触碰 700 MiB 后正常退出；代码未设置 `--memory-swap`。swap 是强假设，尚无 cgroup 实测值和 OOM 证据 | P0 |
| 输出采集内存无上限 | `_communicate()` 调用 `Popen.communicate()`；执行结束后才切片。返回 4096 字符不能证明采集只占 4096 字符 | P0，代码已确认 |
| 工作区容量无上限 | 普通可写 bind mount 没有配额。现有探针因为 EACCES 写入 0 字节，尚未实际验证容量耗尽 | P1，能力缺失已确认 |
| 崩溃后容器残留 | 父进程退出 0.5 秒后容器仍在；显式恢复后容器已删除，但又报告清理失败。自动回收缺失和错误识别缺陷是两个问题 | P0 |
| SRF-07 接管被错误阻止 | Docker 29 返回 `error: no such object`，`_docker_command_id()` 仅匹配大写 `No such object`，触发 `RecoveryRequired` | P0，代码及堆栈已确认 |

补充发现，不能通过删除用例或降低判定标准处理：

1. `docker run` 在 `_register()` 持久化前已经启动；接管可能读到命令记录时容器尚未创建，也可能错过已开始执行但尚未登记的命令。
2. `_terminate_process()` 对任意 inspect 非零都可能继续等待客户端，没有把 daemon 故障和容器不存在分开；inspect/rm 缺少超时，移除后没有统一确认。
3. 恢复先查名称标签、再按名称删除，中间若名称被复用可能删除另一个容器；应查身份后按不可变 container ID 删除。
4. 后台子进程 marker 的缺失受工作区 EACCES 影响；SRF-07 timeout 用例也有这个问题。
5. TOCTOU 用例的“安全”symlink 指向宿主绝对路径，在容器内通常同样悬空；缺少成功读取 SAFE 的对照。
6. 网络探针连接 `127.0.0.1:1` 没有预先设置宿主监听器；`/proc/net/route` 检查错误地用整行 `startswith('00000000')`，实际第一列是接口名。
7. `docker-user-modified-file` 在 fixture 中实际指向通用文件恢复测试，未调用 Docker；不能作为真实 Docker 修改文件恢复的证据。
8. `_labelled_containers()` 把 Docker 查询失败当空列表，且最终清理所有带 `patchloop.command_id` 的容器，可能误清理并行任务。
9. 工具和 runtime 在 `context.sandbox is None` 时隐式使用 LocalProcessSandbox，与“配置 Docker 就必须隔离”的预期不一致。

### Desired Behavior

- Docker 指定失败时不静默切换本地；显式 local 保留为不具备 OS 隔离的兼容后端。
- 用户工作负载执行前已有可恢复身份记录；旧容器确认停止后才授予新 workspace writer。
- 正常退出、timeout、cancel、pause、lease loss、父进程崩溃都进入同一套身份校验和有限时清理。
- Linux 工作区按实际宿主用户正常读写；不靠扩大目录权限或恢复容器 capabilities 修复 EACCES。
- stdout/stderr 在执行期间有内存上限；容器 RAM、swap、PID、tmpfs 和所声明的工作区容量限制有运行证据。
- 所有验收结论来自真正执行的探针；前提不满足、无法确认与真正失败明确区分。

### Scope

主要目标为 Linux Docker，优先覆盖本报告的 Docker 29 / Python 3.13 / cgroup v2 环境。扩展已有 sandbox、ownership 和 persistence 链路；新增的宿主 supervisor、输出采集器和容量验证模块分别只承担单一职责。

工作区仍为原地 bind mount，修改直接落到同一个工作区，不引入复制后回灌、不改变 WorkspaceService 的 diff、用户改动归属或审批语义。容量受限工作区是管理员预先准备的独立文件系统，见 4.6。

### Non-Goals

- 不增加远程执行平台、容器池、VM/gVisor/Kata 后端、网络代理或依赖下载能力。
- 不将 local 包装成安全沙箱，不在此次重做 Windows Job Object；共享代码改动必须跑 Windows 回归。
- 不声称抵御内核/runtime 零日、拥有宿主 Docker 管理权限的恶意用户、宿主掉电，或 supervisor 与父进程同时被强杀。
- 不承诺整个 PatchLoop 主机零磁盘增长；本方案限制容器工作区、tmpfs 和命令输出，不替代 SQLite/审计日志的宿主存储治理。
- 工作区内的 `.env` 等文件目前属于已挂载内容；已有凭据探针只证明未挂载的宿主凭据和环境变量未泄漏。不得宣称工作区内秘密自动不可读；真实验收只用合成凭据和无真实密钥的工作区。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `src/patchloop/sandbox.py::DockerSandboxConfig` | image/cpus/memory/pids/network，未限制 swap、用户和 workspace 容量 | 增加明确资源配置和宿主执行身份解析 |
| `DockerSandbox.build_command/build_managed_command` | `docker run --rm`，默认 root/镜像用户，可写 bind，两个身份 label | 复用参数构建；新增 create/start 路径、固定 image ID、UID/GID、swap/log 限制 |
| `_docker_command_id/reconcile_managed_command/DockerSandbox._terminate_process` | 两套检查；大小写敏感，按名称 rm；运行路径吞 inspect 错误 | 共用结构化 inspect/remove/verify，按 container ID 删除 |
| `_ManagedSandboxBase._register/_communicate/_finish/terminate_all` | 注册发生在 Popen 后；communicate 全量缓存；结束路径可能竞争 | 输出流式化，Docker 启动门控，结束状态只结算一次 |
| `ManagedCommandIdentity/SandboxResult` | 身份只记录名称和客户端 PID；结果只含 exit/output/backend | 可选 container ID、采集和资源诊断；旧 payload 可读 |
| `src/patchloop/execution/ownership.py::ExecutionOwnershipManager.acquire/_reconcile_commands` | 清理后 claim，再获取 workspace lease；清理失败阻止接管 | 保持顺序，补 absent/race/daemon failure 的故障注入 |
| `src/patchloop/persistence.py::register_managed_command/finish_managed_command` | 带 lease guard 登记；终态不被迟到写回覆盖 | 扩展不可变身份比较，验证 supervisor 登记先于用户命令 |
| `src/patchloop/runtime.py::_with_execution_ownership/_register_managed_command` | acquire → bind → heartbeat → action → cleanup/release；sandbox 缺省回落 local | 缺省按 TaskExecutionConfig 构建，保留 cleanup failure 阻止 release |
| `src/patchloop/tools/execute.py::RunTestsTool/RunCommandTool` | allowlist 后调用 sandbox，缺省 local | 缺失 sandbox 拒绝执行，输出增加可选诊断 |
| `src/patchloop/cli.py::_create_sandbox/_session_runtime_service`，task/session/evaluate 入口 | 只传 backend/image，任务保存对应字段 | 统一传播资源配置，恢复沿用持久化值 |
| `src/patchloop/domain.py::TaskExecutionConfig` | 保存 backend/image | 保存可选 workspace 上限，默认兼容旧 payload |
| `docker/sandbox.Dockerfile` | Python/Git/pytest 工具镜像，无 USER | 继续由调用端选择数值 UID；无需修改镜像权限或安装 Docker socket |
| `tests/unit/test_sandbox.py` | 已覆盖大写不存在、daemon 错误和本地生命周期 | 补小写、错误分类、竞态、UID、swap、采集上限 |
| `tests/integration/test_managed_command_runtime.py`，`test_execution_takeover_recovery.py` | cancel/lease loss/恢复约束 | 补 Docker supervisor 与持久化顺序、迟到 finish |
| `tests/acceptance/run_real_sandbox_matrix.py::Matrix` | 可执行但有弱断言、固定 probe 目录、全局清理 | 独立运行域、正向对照、真实资源证据、故障场景 |
| `src/patchloop/evaluation/backend_acceptance.py` | 所有后端一起汇总，不可用为 unverified | 保留总状态，增加明确目标平台门槛 |
| `tests/fixtures/session_backend_acceptance.json` | Docker 用户文件用例借用通用测试 | 指向新增真实 Docker 用例 |

### Current Flow

```text
CLI TaskExecutionConfig → _create_sandbox → ToolContext
→ runtime acquire execution/workspace leases → sandbox.bind_execution
→ ToolGateway Policy/Approval → RunTestsTool/RunCommandTool
→ docker run（此时可能已执行用户命令）
→ _register → SQLite managed_commands
→ communicate 全量收集 → 结束后截断 → finish
→ runtime terminate_all → release leases

过期 lease → recovery_commands_for_task/workspace
→ reconcile_managed_command → finish_managed_command
→ 新 execution/workspace generation
```

本次设计阶段实际运行：`.venv/Scripts/python.exe -m pytest -q tests/unit/test_sandbox.py tests/unit/test_backend_acceptance.py`，**10 passed**。这些是现有基线测试；未新跑 Docker/远程破坏性探针。

## 3. External Research

读取日期 2026-09-21。以下为实际读取的官方资料/源码，源码固定到调研时提交；不把文档描述等同于 PatchLoop 的验收证据。

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| Claude Code | [官方 sandbox 文档](https://code.claude.com/docs/en/sandboxing.md)：文件系统/网络隔离独立于命令审批；可配置 sandbox 不可用时 fail closed | 将审批和 OS 隔离分别验收，明确不可用错误 | 默认可降级/unsandboxed retry，以及为本任务新增 bubblewrap/代理实现 |
| Qwen Code | 提交 `af4e3b298e24ea8294f77a215b76d93b6c8e383a` 的 [sandbox 用户文档](https://github.com/QwenLM/qwen-code/blob/af4e3b298e24ea8294f77a215b76d93b6c8e383a/docs/users/features/sandbox.md) 描述 Linux UID/GID 映射；[sandboxMounts.ts](https://github.com/QwenLM/qwen-code/blob/af4e3b298e24ea8294f77a215b76d93b6c8e383a/packages/cli/src/utils/sandboxMounts.ts) 对额外挂载默认 ro | 显式执行身份、最小挂载面 | 挂载 `~/.qwen` 一类凭据目录、任意额外 Docker flags、禁用安全标签来修权限 |
| OpenCode | 提交 `70a24697ea0028e19f22712fd63059538cb4bee7` 的 [truncate.ts](https://github.com/anomalyco/opencode/blob/70a24697ea0028e19f22712fd63059538cb4bee7/packages/opencode/src/tool/truncate.ts)：字节/行阈值、truncated 元数据、完整输出文件及保留期 | 结果明确标注截断，预览大小可测 | 该函数接收完整字符串，不能作为流式内存有界的证明；本次不把无限输出转存宿主文件 |

补充依据：[Docker 官方资源限制](https://docs.docker.com/engine/containers/resource_constraints/#--memory-swap-details)。`--memory-swap` 未指定时可能允许额外 swap；与 `--memory` 设置相同正值才禁用 swap，设置 0 不等于禁用。

### Lessons for PatchLoop

1. 修复工作区权限应从 UID/GID 与挂载契约入手，不能恢复 `CAP_DAC_OVERRIDE` 或 chmod 777。
2. 输出结果截断、采集内存限制、Docker daemon 日志限制是三个独立控制点。
3. 保留 Policy/Approval 已通过的语义，但补测“后端缺失也不能走 local”。
4. 父进程崩溃回收不能只靠父进程 `finally`；本方案采用宿主 supervisor，不从容器内访问 Docker socket。

## 4. Design

### Selected Approach

沿用 `CommandSandbox.execute()`、`ManagedCommandSandbox.bind_execution()` 和 SQLite managed_commands，不替换 ownership 系统。分三批交付：P0 修复执行与生命周期；P1 增加可选但严格验证的工作区容量能力；最终在开启全部限制的 Linux 环境执行完整验收。

### Key Decisions

### 4.1 统一 Docker 身份检查和清理

在 `sandbox.py` 增加以下内部接口：

```python
class DockerContainerSnapshot(BaseModel):
    container_id: str
    name: str
    command_id: str | None
    execution_id: str | None
    state: dict[str, object]


def _inspect_docker_container(
    reference: str,
    *,
    docker_host: str | None = None,
) -> DockerContainerSnapshot | None: ...
def _remove_verified_container(identity: ManagedCommandIdentity) -> None: ...
```

`docker inspect --type container <reference>` 解析 JSON，执行超时 5 秒；rm 超时 10 秒，整次清理总预算 20 秒。诊断 stdout/stderr 各最多保留 8 KiB；不使用无上限 capture_output 接收不可信超长错误。

| 情况 | 必须行为 |
| --- | --- |
| inspect 成功 | 验证 command_id 和 execution_id；新记录还验证完整 container_id |
| 明确不存在 | `stderr.casefold()` 识别 `no such object/container`，还必须能绑定本次查询 reference；返回 None |
| daemon 不可达、permission denied、超时、未知文本、畸形 JSON | `SandboxCleanupError`；不认定已停止，不授予新 writer |
| 标签/ID 不匹配 | 拒绝删除；报告 identity mismatch |
| 删除与其他清理者竞争 | 允许 rm 报已不存在，但仍重新 inspect 不可变 ID；确认 None 才成功 |
| 名称被另一容器复用 | 只删除已校验的完整 ID，不删除新的同名容器；报告 mismatch 供恢复处理 |

`ManagedCommandIdentity` 新增 `container_id: str | None = None`、`docker_host: str | None = None`、`purpose: Literal['workload', 'preflight'] = 'workload'`。docker_host 只允许本机绝对 unix socket 或 None；恢复使用记录中的 endpoint，不能误查默认 daemon。三项存入现有 payload_json，不增列；`finish_managed_command()` 将其纳入不可变身份检查。旧记录无 ID 时先以名称查两项标签，再按查到的 ID 删除。旧记录缺少必需标签时 fail closed，不能仅凭名称删除。

`_finish`、timeout、`terminate_all` 使用每命令锁序列化。内部阶段固定为 active → cleaning → outcome_pending → finished；只有清理确认和 callback 持久化成功才移出 active。已结束记录不得被迟到的 EXITED/CLEANUP_FAILED 覆盖。清理失败允许重试，但不释放 lease；runtime 的 finally 保留原始清理异常，避免 unbind 异常覆盖原因。

### 4.2 Docker 启动门控与宿主 supervisor

新增 `src/patchloop/sandbox_supervisor.py`，仅是 Docker 生命周期助手，不是第二套后端。共用 4.1 的身份检查和参数构建。

固定启动协议：

```text
父进程生成 command_id/name，创建工作区外的私有控制目录
→ Popen 当前 Python 的 sandbox_supervisor 模块（独立 session）
→ supervisor docker create：容器此时 stopped，用户命令未运行
→ supervisor inspect，原子写 ready.json（container_id + labels）
→ 父进程校验 ready，登记 ManagedCommandIdentity（PID=supervisor PID）
→ command_started 持久化成功，检查 interruption_probe
→ 父进程通过 supervisor.stdin 发送 START
→ supervisor 对同一 container_id 执行 docker start --attach
→ 双流转发 / timeout / STOP / 控制管道 EOF
→ inspect 最终状态 → verified rm → 确认不存在 → outcome.json
→ 父进程读取结果并 command_finished
```

- Docker execute 从 `run --rm` 改成 `create → start --attach → inspect → rm`，不设自动重启，不使用 `--rm`，以便移除前保留 OOM/exit 证据。`build_command()` 可保留为兼容的纯构建入口，实际 execute 走新 `build_create_command()`，两者共用安全参数函数。
- 控制目录使用宿主 `tempfile.mkdtemp`，POSIX mode 0700、文件 0600，不放在 bind 工作区，不挂进容器；原子写小 JSON，单文件最多 16 KiB。不在里面保存命令完整输出、凭据或 `.env`。
- supervisor 初始化从 stdin 读取一个最多 64 KiB 的 JSON 行，包含 command/config/repository/identity；后续只接受 START、STOP。用户命令 stdin 为 DEVNULL；用户 stdout/stderr 不能解释成控制消息。
- supervisor 在容器 stopped 后 READY；未收到 START 就绝不 start，也不重新 create。父进程在 READY 前崩溃时，EOF 后等待本次 create 得到确定结果并清理；create 结果不确定时根据固定名称和标签重试查询，不能启动用户命令。
- 父进程崩溃关闭控制管道；supervisor 即使转发 stdout 遇到 BrokenPipeError，仍必须继续清理。supervisor 与父进程独立 session；只测试父进程死亡，不假定整宿主进程组死亡后仍能运行。
- START 后父进程消失，目标为 20 秒内确认容器不存在；0.5 秒只记录观测值，不作为不现实的即时删除承诺。验收禁止先手工 reconcile 再计作自动清理通过。
- 20 秒内无法联系 daemon，supervisor 写 cleanup_failed，并以固定名称/ID继续每 5 秒重试，最多 5 分钟后退出；记录保留，后续 ownership 恢复仍 fail closed。该边界不宣称 daemon 永久故障时也能及时删除容器。
- supervisor 无数据库写入权限需求：父进程负责 command_started/finished；父进程死后 managed_commands 保持 running，下一次接管校验不存在后幂等结算。
- 正常父进程读取 outcome 后删除控制目录；崩溃路径由 supervisor 清理其已知目录。未知目录不扫描删除。
- 接管在登记后 START 前发生：恢复删除已创建的不可变 ID，迟到 START 只能失败，不能重新建容器；登记前发生：旧 lease guard 拒绝登记，父进程关闭管道，由 supervisor 清理 stopped 容器。
- 新身份中的 process_id/start_marker 用于确认 supervisor，而不是杀死任意 Docker 客户端。恢复先确认容器不存在；对同 PID 同 creation marker 的 supervisor 可等待至清理期限，必要时发停止信号，绝不能按 PID 单独杀进程。旧 Docker 记录仍按容器身份恢复。

控制文件协议固定如下（均须校验 command_id/execution_id，路径仅由父进程构造）：

```text
ready.json:
  protocol_version=1, command_id, execution_id, container_id,
  container_name, docker_host, stage="ready"
outcome.json:
  protocol_version=1, command_id, execution_id, container_id,
  status="exited"|"terminated"|"cleanup_failed",
  exit_code=int|null, reason=str|null, oom_killed=bool,
  cleanup_confirmed=bool, diagnostic=str（最多 8192 字符）
```

缺少或畸形 outcome 不能据 supervisor 退出码认定工作负载成功。采集器只收集，不调用 command_finished；Docker execute 读取并验证 outcome 后才结算，Local execute 仍在本地进程域停止后结算。Docker `_terminate_process` 先发 STOP 并等 outcome；supervisor 失效才用相同 identity 执行直接 verified cleanup。需要清理成功时 `cleanup_confirmed` 必须为 true。supervisor 自己的诊断写 outcome，不混入用户 stdout/stderr；用户错误码和 helper 错误码分开。

正常退出以 `State.ExitCode` 为准。用户命令返回 125/126/127 不应仅因数字被误判为 Docker 启动失败；结合 StartedAt/State.Error 和 start CLI 错误区分。OOM 使用 `State.OOMKilled`，不是任意非零退出码。

### 4.3 工作区身份与可写性

- Linux rootful Docker 默认传 `--user <os.getuid()>:<os.getgid()>`。不传宿主 supplementary groups；确需组权限时由管理员为指定工作区配置 ACL，执行端保持最小权限。
- 新配置 `user: str | None = None`，只允许数值 `UID:GID`；None 表示 Linux 自动解析。非 Linux 保持镜像用户并做能力检查，不能调用不存在的 os.getuid。
- rootless/userns-remap 不硬套宿主 UID：当前完整保障配置仅接受经预检证明读写及所有权符合预期的映射；失败报 `workspace_identity_unverified`，不得添加 `--userns=host` 绕过。
- 在执行用户命令前，使用相同镜像 ID、UID、mount 和安全参数做受管 preflight：读宿主写入的随机哨兵、容器创建随机文件、宿主验证内容/UID/GID；文件独占创建并只删除本次生成的名称。
- preflight 必须在 workspace lease 内，由相同生命周期清理；以 `(execution_id, root dev/ino, image_id, uid/gid, config hash)` 缓存一次。每次命令仍检查根 dev/ino 未变；其他执行不能复用缓存。
- 实现拆为 `execute → _ensure_preflight → _execute_managed`；preflight 直接调用包内 `_execute_managed(..., purpose='preflight')`，不能递归调用 execute。预检也登记 managed_commands，并在 label/identity 标明 purpose；中断时和 workload 一同恢复。独立调用无 lease 时每次执行都预检，不跨调用缓存。原来断言只有一条记录的测试改为按 purpose 筛选 workload，同时断言所有 preflight 已结束。
- preflight 失败输出宿主 UID/GID、目录 mode、容器 UID/GID、image user、userns 模式和 errno；不输出环境变量全集。禁止递归 chown/chmod，禁止增加 capability。
- 使用 `--mount type=bind,...,bind-recursive=disabled`，避免子挂载扩大实际可写面；不支持该参数的 Docker 版本报不支持，不静默放宽。
- 镜像在本次 execution 首次解析为完整 image ID，create 和 preflight 使用相同 ID；不自动 pull。镜像标签变化只能在新执行时解析，验收记录 Dockerfile hash 和 image ID。

### 4.4 有界 stdout/stderr 采集

新增内部 `src/patchloop/sandbox_output.py`，Local 和 Docker 父进程共用；supervisor 只用固定块转发，不保留全量输出。

```python
class CapturedOutput(BaseModel):
    stdout_tail: str
    stderr_tail: str
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool


def collect_process_output(
    process,
    *,
    max_output_chars: int,
    deadline: float,
    interruption_probe,
    terminate,
) -> CapturedOutput: ...
```

两条 reader thread 分别读二进制 pipe，每块最多 16 KiB，各用增量 UTF-8 decoder（errors=replace）和最多 N 个字符的 tail buffer；不得 `readline()`、`communicate()`、全量 list/queue、无上限临时文件。即使已超上限仍持续排空管道，避免 stderr 堵塞。无无限队列，reader 直接写加锁环形 buffer。

兼容现有输出顺序：`(stdout_tail + stderr_tail)[-N:]`，保持 stdout+stderr 后取尾部的语义。每流保留 N 足以构造相同结果，内存为 O(N + 固定读取块)。返回值上限仍按字符计数；监控总量按原始字节计数。

主循环每 100ms 检查 timeout/interrupt；先停止进程域，之后最多 2 秒排空 EOF/join readers。管道未闭合视为 cleanup_failed，不能留下持有句柄的无限 reader。所有 Windows/Linux reader 都用相同关闭协议。

`SandboxResult` 增加默认值字段 `output_truncated: bool = False`、`stdout_bytes: int = 0`、`stderr_bytes: int = 0`、`oom_killed: bool = False`；工具 JSON 透传前三项。现有字段保留，不把截断作为执行失败。

Docker 使用 `--log-driver=none`，避免 stdout 虽在客户端截断仍填满 daemon json-file 日志。无需保存“完整原始输出”。超限后的字节只计数并丢弃，控制文件大小固定。

### 4.5 RAM、swap、tmpfs 及资源预检

- `--memory=<memory_mb>m --memory-swap=<memory_mb>m`；不允许 `--oom-kill-disable`。保持 cpus/pids 限制。
- 新配置 `tmpfs_mb: int = 64`，范围 16..1024；`/tmp:rw,noexec,nosuid,nodev,size=<n>m`。
- cgroup v2 目标：验证 `memory.max == memory_mb * 1024 * 1024`、`memory.swap.max == 0`、`pids.max`、`cpu.max`；只看 docker inspect 不足以宣称有效。
- 预检沿用受管命令，不把 cgroup 文件挂成可写；无法读取或限制未生效时报 `resource_limits_unverified`，阻止该环境取得完整保障资格。P0 的执行配置要求 RAM/swap 检查通过；不对不支持的 cgroup v1 冒充验证成功。
- 内存验收用 64/128 MiB 的小容器、分块申请并触碰每页，有限时申请到上限的两倍；同时做低于上限的成功对照。要求 OOMKilled 或 memory.events 的 OOM 增量；SyntaxError/启动错误/普通 timeout 不能算通过。
- tmpfs 验收核对 errno ENOSPC 和已经成功写入的字节，不能把 EACCES 算空间限制。

### 4.6 工作区硬容量：专用固定容量文件系统

选定第一版实现：**验证管理员已准备好的专用 ext4 文件系统，整个 repository 就是它的挂载根**。使用固定大小、预分配 backing file 的 loop ext4 是参考部署；不在 sandbox 内创建挂载或提权。不采用周期性 du、RLIMIT_FSIZE、Docker writable-layer `--storage-opt size` 冒充 bind mount 总容量限制。

```python
# DockerSandboxConfig 新字段
workspace_limit_mb: int | None = None  # 配置后必须验证；64..16384
workspace_inode_limit: int = 65536  # 仅 limit_mb 非空时生效


# 新文件 sandbox_storage.py
def verify_workspace_capacity(
    repository: Path,
    *,
    limit_bytes: int,
    inode_limit: int,
) -> WorkspaceCapacityEvidence: ...
```

`WorkspaceCapacityEvidence` 包含 canonical_root、mount_id、device、fstype、total_bytes、total_inodes、limit_bytes、inode_limit、verified_at，不包含凭据。读取 `/proc/self/mountinfo`（处理转义）、`os.statvfs()`，校验：

1. canonical repository 精确等于 mountpoint；fstype=ext4，rw，`nosuid,nodev`，非共享上级文件系统的普通子目录。
2. `f_blocks * f_frsize <= limit_bytes`、`0 < f_files <= inode_limit`，根 dev/ino 与 preflight 一致。限制是整个文件系统容量，包含 baseline 和元数据，不是允许新增的净字节数。
3. repository 下面没有额外挂载；容器 bind 禁用 recursive。每次命令前重验 mount_id、容量和 dev/ino；可信管理员在命令运行中 remount/resize 不属于攻击模型。
4. 条件不满足抛 `SandboxError('workspace_capacity_unverified: ...')`，用户命令不得执行；不自动复制、移动用户仓库，也不自动关闭配置。

兼容与发布语义：旧任务及未配置值继续运行普通 bind mount，但结果/报告必须明确 `workspace_storage_bounded=false`，**不能通过完整 Linux 安全验收**。新 CLI `--sandbox-workspace-limit-mb N` 显式开启；开启后任何不满足前提的目录都 fail closed。不得为了全绿默默把容量用例标 N/A。将所有用户默认切到容量受限存储不在本次自动迁移范围内。

新增 `scripts/provision_sandbox_workspace.sh` 作为管理员运行工具：仅创建指定的全新空工作区与全新 backing file，拒绝已有目标/符号链接，校验路径位于明确传入的 sandbox 存储根，先检查可用空间，再预分配固定大小文件、创建 ext4（固定 inode 数、reserved blocks=0）、loop mount `nosuid,nodev`、将挂载根赋给指定 UID/GID。默认 dry-run，只有 `--apply` 执行；支持 `--destroy` 时必须核对保存的 mount/device/backing-file receipt，拒绝非空用户仓库和被占用挂载。runtime 不调用它。

生产 repo 是否迁移由操作者决定；验收在全新的受限工作区内生成 fixture。超过 10G 的数据/结果必须位于 `/data/PatchLoop`；本方案参考存储根为 `/data/PatchLoop/sandbox`。

### 4.7 后端与错误传播契约

- runtime 的 sandbox 缺省时按持久化 `task.execution.sandbox_backend` 构建；显式注入的测试 backend 继续支持。复用统一 `create_command_sandbox(execution)` 工厂，CLI `_create_sandbox` 变薄适配器。
- 独立调用执行工具时若 `ToolContext.sandbox is None`，直接 `SandboxError('sandbox_not_configured')`，不创建 local。旧依赖隐式 local 的测试改为显式注入。
- `SandboxTimeoutError`、`SandboxInterruptedError` 维持现有映射；能力/启动失败用 SandboxError；无法证明资源已经回收用 SandboxCleanupError，交给 runtime/ownership 保留 RECOVERY_REQUIRED。
- 新配置保存于 TaskExecutionConfig；resume 使用保存值，不能从新的默认值/环境变量悄悄关闭限制。运行中不开放配置修改 API；要改变 backend 或容量必须新建任务，既有审批版本机制保持原样。
- Policy 拒绝时 backend 调用次数仍为 0；批准不豁免 Docker/资源预检。local 只有用户明确选择后才可用，不进行网络下载或自动构建镜像。

### 4.8 验收报告与证据契约

保留旧 case ID，新增 case 不与旧项混淆。真实矩阵升为 schema_version=2，新增 run_id、source_dirty、source_diff_sha256、image/Dockerfile hash、effective_config、preconditions、assurance 和 residual_before_cleanup/residual_after_cleanup。

状态继续用 passed/failed/unverified/not_applicable：前置读写失败时依赖项为 unverified，主前置项为 failed；不把无法执行说成已经成功防御。运行被取消仍原子写报告，再返回非零。

对每个 case 明确 assertion_type=runtime/static、被验证保证、正向对照、容器身份和耗时。原“输出内存”静态 note 改成真实探针；PID/内存/存储压力设置硬上限，禁止无界 fork 或写满宿主盘。

运行域：随机 `.sandbox-real-probes-<run_id>`，带创建 receipt；只允许清理本轮记录的名字和完整 ID，必须再验标签。不得枚举全部 PatchLoop 容器后统一删除。Docker 列举失败是 unverified/failed，不能得到“零残留”。

网络 positive control：宿主先启动本轮 TCP/HTTP listener，宿主客户端成功访问后再测容器；检测宿主实际地址/bridge gateway，不只解析 host.docker.internal；route 用 Destination 列，IPv6 一并测。DNS/外部 HTTP 的宿主控制不通时对应项 unverified，网络隔离配置检查仍可独立运行。

TOCTOU：安全目标改为容器可读的相对 `safe.txt`，至少观察到一次 SAFE 后才判断 secret_reads=0；补写入竞态时要求安全目标曾成功写入、外部哨兵始终完整。父/子进程 marker 探针先证明相同 UID/挂载下能写 marker。

daemon 不可达验证不停止共享 lab-pc 的 Docker：新增验收专用 Unix socket 故障代理，转发到实际本机 daemon，运行中关闭本轮代理连接并暂停接受连接，再恢复。被测 Docker CLI、supervisor、reconcile 全部连接代理 endpoint；这是实际传输断连，不是 mock subprocess。只影响本轮连接，不触碰 daemon 服务和其他任务。固定接口是可信构造参数 `docker_host: str | None = None`，仅支持本机绝对 unix socket，传播给全部控制命令并存入 managed identity；不继承任意 DOCKER_HOST，不向模型工具暴露参数。禁止连接远程宿主后按本机路径 bind。

代理定义在新 `tests/acceptance/docker_fault_proxy.py`，接口 `DockerFaultProxy(upstream_socket, control_dir)`、`start() -> str`、`cut_connections()`、`restore()`、`close()`；每连接双向各最多 64 KiB 缓冲，不记录请求内容。cut 同时关闭已有连接和 listener；restore 在同一私有 socket 路径重新监听；容器看不到 upstream/代理 socket。其控制目录在工作区外，启动和关闭均核对本轮 receipt，只 unlink 自己创建的 socket。

固定 `--daemon-fault-mode proxy|skip`，缺省 proxy；skip 明确使必需的 daemon-unavailable 项 unverified。覆盖启动前失联、运行中失联、清理期间失联及连接恢复后的幂等恢复。报告 `fault_type=client_transport_unavailable`；该结果证明“daemon 对执行端不可达时”的行为，不声称已测试 dockerd 进程崩溃、重启或存储损坏。后者可在专用主机单独扩展，不作为本轮设计的虚假保证。

SRF runner 保留全矩阵总状态，新增 `required_backends` 与 `target_status`：Linux 运行要求 Docker，Windows 运行要求 Windows。总报告中 Windows unverified 原样保留；Linux gate 只能检查明确配置的 Docker target_status。pytest 全 skipped/未收集测试不算 passed，需要 JUnit 证明该 node 真正执行。

### Target Flow

```text
持久化执行配置 → ownership + Policy/Approval
→ 配置/目录身份/容量预检
→ supervisor create stopped 容器 → READY
→ managed identity + lease guard 登记成功 → START
→ 有界流式采集 + 独立生命周期看护
→ 记录真实 exit/OOM → verified remove by ID → 确认不存在
→ 幂等 finish → 返回有限输出 / 对应错误
→ release；无法确认清理时 RECOVERY_REQUIRED
```

## 5. Change Map

| File | Symbol | Change |
| --- | --- | --- |
| `src/patchloop/sandbox.py` | config/identity/result/base/DockerSandbox/reconcile | 统一清理、门控调用、资源参数和状态结算 |
| `src/patchloop/sandbox_supervisor.py`（新增） | `main/Supervisor` | stopped-create、控制管道、转发、父进程消失清理 |
| `src/patchloop/sandbox_output.py`（新增） | `CapturedOutput/collect_process_output` | 固定空间双流采集 |
| `src/patchloop/sandbox_storage.py`（新增） | `WorkspaceCapacityEvidence/verify_workspace_capacity` | 独立文件系统容量校验 |
| `src/patchloop/persistence.py` | register/finish managed command | container_id 不可变性、旧 payload 兼容 |
| `src/patchloop/runtime.py` | 所有权执行段/cleanup finally | 配置后端解析、保留失败原因 |
| `src/patchloop/domain.py`，`cli.py`，`tools/execute.py` | execution config、factory、工具输出 | 配置贯穿任务创建/恢复/执行，取消隐式 local |
| `scripts/provision_sandbox_workspace.sh`（新增） | prepare/dry-run/destroy | 管理员预配独立受限工作区 |
| `tests/unit/test_sandbox.py`，`test_sandbox_output.py`（新增），`test_sandbox_storage.py`（新增） | 定向单测 | 错误/采集/容量和参数 |
| `tests/integration/test_managed_command_runtime.py`，`test_execution_takeover_recovery.py`，`test_execution_ownership.py` | 故障与接管测试 | 完成持久化前不启动；清理未证实不接管 |
| `tests/acceptance/srf07_docker_backend.py` | 三项 Docker case | 正向对照、真实后端文件恢复 |
| `tests/acceptance/run_real_sandbox_matrix.py` | Matrix/CLI | schema v2、限域清理、资源实测 |
| `tests/acceptance/docker_fault_proxy.py`（新增） | `DockerFaultProxy` | 仅本轮连接的真实 Unix socket 故障注入 |
| `src/patchloop/evaluation/backend_acceptance.py`，`tests/unit/test_backend_acceptance.py`，fixture | runner/report/CLI | required target 和真实执行判定 |
| `tests/unit/test_real_sandbox_matrix.py`（新增） | 报告与清理单测 | 不存在/查询失败/目标过滤/取消报告 |

## 6. Implementation Tasks

### TASK-01：修正矩阵的证据和清理边界（P0）

**Goal**

先让失败可信、测试不会误伤其他运行，给后续修复建立基线。

**Files / Symbols**

```text
tests/acceptance/run_real_sandbox_matrix.py
  - Matrix.prepare/add/run/file_escape/symlink_toctou/network_isolation
  - _container_exists/_labelled_containers/main
tests/unit/test_real_sandbox_matrix.py（新增）
```

**Implementation**

1. 按 4.8 增加 run_id、私有 fixture、容器登记集合与限定清理；取消固定目录 rmtree 和全局标签清理。
2. 所有 Docker 查询非零先分类，不得将 daemon 失败当 absent/空列表；报告 finalization 放 finally，KeyboardInterrupt 单独处理。
3. 增加读写、safe symlink、网络 listener 正向对照；改正 route 列解析；依赖失败记 unverified。
4. 加 `--workspace` 参数：缺省仍在本轮临时目录建工作区，传入时必须是专门为本轮准备的空目录（允许 ext4 自带的 `lost+found`），只删除本轮 fixture，不能删除挂载根。

**Interface Changes**

矩阵报告 schema v2；CLI `--workspace PATH`；`Matrix.add(..., requires=...)` 依据已记录前置状态跳过为 unverified。

**Tests**

- 创建另一个同 label 容器模拟对象，确认不会进入 rm 列表；命令失败不能产生零残留结论。
- SAFE 从未读到、marker 不能写、宿主 listener 不通分别阻止对应 passed。
- 任意阶段异常/取消仍生成可解析报告，不覆盖旧基线文件。

**Acceptance Criteria**

当前实现的失败仍保留；不能因为 harness 修复就宣称产品已通过。只清理本轮已核验资源。

### TASK-02：统一幂等 Docker 清理与身份持久化（P0）

**Goal**

修复大小写兼容和不安全错误归类，确保旧 writer 停止事实可信。

**Files / Symbols**

```text
src/patchloop/sandbox.py
  - ManagedCommandIdentity/_inspect_docker_container/_remove_verified_container
  - reconcile_managed_command/DockerSandbox._terminate_process/_finish
src/patchloop/persistence.py::finish_managed_command
tests/unit/test_sandbox.py
tests/integration/test_execution_takeover_recovery.py
```

**Implementation**

1. 实现 4.1 接口，所有清理路径共用；统一 timeout、错误类型和按 ID rm。
2. identity 增加 container_id/docker_host/purpose 及兼容默认值；旧 JSON 和新 JSON 都可恢复，不迁移历史结果；恢复使用原 endpoint。
3. 加每命令结算锁；维护 outcome_pending，失败不伪装成功，不让重复 callback 回退终态。
4. 保持 ownership.acquire 的清理→claim→workspace lease 顺序。

**Interface Changes**

4.1 的两个内部 helper、Snapshot 和可选 container_id；公开 reconcile 签名保持不变。

**Tests**

- 大小写不存在、错误 reference、permission denied、daemon down、timeout、非法 JSON。
- 标签错误、name reuse、rm 后仍在、并发 rm 已不存在、迟到 finish 和 callback 持久化失败。
- daemon 错误必须保持 RecoveryRequired；不得借修复抹去真实失败。

**Acceptance Criteria**

报告中的小写不存在不再阻止合法接管；身份不明或无法确认停止时仍拒绝接管。单纯完成本任务不宣布 parent-crash 已修复。

### TASK-03：实现有界输出采集（P0）

**Goal**

命令输出规模增加不再线性增加执行进程内存。

**Files / Symbols**

```text
src/patchloop/sandbox_output.py（新增）
src/patchloop/sandbox.py::_communicate/LocalProcessSandbox.execute/DockerSandbox.execute
src/patchloop/tools/execute.py::RunTestsTool.run/RunCommandTool.run
tests/unit/test_sandbox_output.py（新增）
tests/unit/test_sandbox.py
```

**Implementation**

1. 按 4.4 实现两个固定块 reader、增量 decoder、tail buffer、字节计数和主线程中断检查。
2. 双后端替换 communicate，保持输出拼接/截断兼容；增量 result 字段透传工具 JSON。
3. 测试 EOF、异常、timeout、cancel 后 reader 和句柄都被关闭，不能靠 daemon thread 掩盖泄漏。

**Interface Changes**

`_communicate` 增加 max_output_chars，返回 CapturedOutput；SandboxResult 新字段均有兼容默认值。

**Tests**

- 8 MiB 双流、长无换行、分块 UTF-8、多字节非法序列、stderr 独占、两流交替、输出期间取消。
- 内部 retained_chars 每流始终 ≤ N；最终输出与旧公式一致；没有随输入增加的队列。
- Windows Local timeout/Job Object 原测试回归。

**Acceptance Criteria**

真实验收输出 256 MiB、N=4096，父进程采集区间 RSS 增量 ≤32 MiB；测 supervisor 后其增量也 ≤32 MiB。按预热后的进程分别测量，不把镜像构建或整宿主内存当采集内存。

### TASK-04：门控启动与父进程崩溃回收（P0）

**Goal**

用户命令只在登记成功后启动，父进程死亡仍有独立清理者。

**Files / Symbols**

```text
src/patchloop/sandbox_supervisor.py（新增）::Supervisor/main
src/patchloop/sandbox.py::DockerSandbox.execute/build_create_command
src/patchloop/sandbox.py::_ManagedSandboxBase._register
src/patchloop/runtime.py::所有权执行段及 cleanup finally
tests/integration/test_managed_command_runtime.py
tests/acceptance/srf07_docker_backend.py
```

**Implementation**

1. 严格实现 4.2 协议和状态顺序；复用 TASK-02 cleanup 与 TASK-03 输出组件，不复制策略逻辑。
2. `ManagedCommandIdentity.process_id` 对新 Docker 命令表示 supervisor；process_start_marker 为 OS creation marker；container_id 在登记时已确定。
3. 父进程开始等 READY 就启动输出 readers，startup 总超时 15 秒；START 后命令 timeout 用调用参数；cleanup 使用独立 20 秒预算，不能无限延长主操作。
4. 限定 stdout/stderr 转发线程固定块，不在转发线程执行清理；BrokenPipe/STOP/EOF 均唤醒主生命周期循环。
5. supervisor 内用 inspect 记录 exit/OOM 后统一 remove；父进程读取 outcome，决定 EXITED/TERMINATED/CLEANUP_FAILED。

**Interface Changes**

supervisor 为包内私有模块；ready/outcome 具有 protocol_version=1，未知版本 fail closed；不新增公开 Tool 或本地高权限服务。

**Tests**

- 注入 crash：create 前、create 中、READY 后登记前、登记后 START 前、运行中、rm 后 finish 前。
- 登记 callback 抛 LeaseLost：容器不得执行用户 marker；接管与迟到 START 竞争时不能重新 create。
- 父进程 SIGKILL 后仅观察 supervisor 自动清理；另测 supervisor 失败后的显式恢复。
- 正常用户 exit 125、OOM、容器启动失败分别分类；重复 terminate 不重复 finish。

**Acceptance Criteria**

健康 daemon 下父进程退出后 20 秒内容器和 supervisor 均退出；多次接管不产生并行 writer。已创建未登记的 stopped 容器也要自动删除。

### TASK-05：修复工作区身份与资源参数（P0）

**Goal**

建立可正常执行测试且资源限制真实生效的 Linux Docker 配置。

**Files / Symbols**

```text
src/patchloop/sandbox.py::DockerSandboxConfig/安全参数构建/preflight
tests/unit/test_sandbox.py
tests/acceptance/run_real_sandbox_matrix.py::configuration/workspace_read_write/memory_limit
```

**Implementation**

1. 按 4.3/4.5 添加 UID/GID、memory-swap、tmpfs nodev、log-driver none、非递归 bind。
2. image tag 解析成 ID；preflight 验证读写和 cgroup v2，生成明确错误信息；复用 TASK-04 管理生命周期。
3. preflight 文件独占创建，不把宿主整个环境传入容器；记录映射、mode、cgroup 限制，不猜 rootless 配置。
4. 内存探针改成有低占用对照、有限的页触碰和 OOM 证据；不以“任意错误”通过。

**Interface Changes**

DockerSandboxConfig 新增 user/tmpfs_mb；内部预检缓存受 execution/root/image/config 约束；其他资源默认保持 512 MiB/1 CPU/128 PIDs。

**Tests**

- 非 1000 UID、目录 0700、只读目录、rootless 映射未验证、标签改变和 root inode 改变。
- 构建参数必须 MemorySwap=Memory、LogConfig.Type=none；不能出现新增 capability/socket mount。
- 低内存正常命令通过，压力命令有 OOMKilled；tmpfs EACCES 不算 ENOSPC。

**Acceptance Criteria**

真实 host 读写往返且文件 UID/GID 符合预期；host inspect 和容器 cgroup 数值一致；现有隔离用例在可写 workspace 上仍通过。

### TASK-06：消除隐式 local 降级并贯穿配置（P0）

**Goal**

所有入口使用同一执行配置，批准命令不导致后端隔离被跳过。

**Files / Symbols**

```text
src/patchloop/sandbox.py::create_command_sandbox
src/patchloop/domain.py::TaskExecutionConfig
src/patchloop/cli.py::_create_sandbox/_session_runtime_service/task run/session send/evaluate
src/patchloop/runtime.py::所有权执行段
src/patchloop/tools/execute.py
tests/unit/test_tools.py
tests/integration/test_cli.py/test_session_cli.py
tests/e2e/test_policy_runtime.py
```

**Implementation**

1. 将后端工厂集中在 sandbox 层，避免 runtime 导入 CLI；用 TYPE_CHECKING 处理 domain 类型引用，避免循环 import。
2. runtime 缺省按 TaskExecutionConfig 构建；工具 context 缺失直接拒绝。所有兼容 local 测试显式设置 LocalProcessSandbox。
3. TaskExecutionConfig 新增 `sandbox_workspace_limit_mb: int | None = None`、`sandbox_workspace_inode_limit: int = 65536`；CLI task run/session send/evaluate 同步新增选项，resume 不重置。
4. CLI 对 local + 非空 workspace_limit 组合报配置错误，不能让用户误以为 local 也执行此限制。配置到 TASK-07 前可构建，但非空时必须明确 not implemented/拒绝执行，不能忽略。

**Interface Changes**

工厂接受保存的 execution 配置；旧 `_create_sandbox(backend, image)` 调用以兼容默认值转接。工具输出增加截断字段，保留原键。

**Tests**

- Docker 不存在、daemon 不通、未传 sandbox 都不能出现本地 marker。
- 明确 local 仍执行且 backend=local；保存/恢复容量配置一致；Policy deny 时 sandbox 调用为 0。
- 原 30 项 Policy/Approval/Workspace 回归全部通过。

**Acceptance Criteria**

CLI、Session 和直接 AgentRuntime 路径的后端选择一致；不存在隐式无隔离执行。

### TASK-07：接入工作区硬容量验证（P1）

**Goal**

开启 workspace_limit 后在可写工作区实现真实总容量及 inode 上限。

**Files / Symbols**

```text
src/patchloop/sandbox_storage.py（新增）
src/patchloop/sandbox.py::DockerSandbox.execute/preflight
scripts/provision_sandbox_workspace.sh（新增）
tests/unit/test_sandbox_storage.py（新增）
tests/acceptance/run_real_sandbox_matrix.py::workspace_storage_limit/main
```

**Implementation**

1. 完整实现 4.6 的 ext4 挂载和容量校验，不新增 XFS/project-quota 适配器。
2. 写管理员 provision 脚本及 `--help`，默认 dry-run，拒绝覆盖已有内容；把预分配文件和真实挂载都记录进 receipt。
3. 矩阵加 `--workspace-limit-mb` 和 `--workspace-inode-limit`，必须与预配实际容量兼容；无配置仍记录缺失能力并阻止完整 gate。
4. 在 64 MiB 空验收工作区写多个小文件、单大文件、稀疏文件和填 inode；总写入循环设置 96 MiB/固定文件数上限；写成功若已越边界立即失败并停止，不持续填盘。
5. fixture 基线与 metadata 占用计入容量；ENOSPC 后删除本轮 filler，再验证原有用户 fixture 未损坏、后续小写入可恢复。

**Interface Changes**

4.6 的 capacity evidence、validator、管理员脚本、两项矩阵 CLI 参数。

**Tests**

- mountinfo 转义路径、普通目录冒充挂载根、共享大文件系统、子挂载、inode 超限、ro、dev/ino 改变必须拒绝。
- 真实容量消耗要求先写入成功且最终 errno=ENOSPC；EACCES/IOError 不通过。
- sparse `truncate` 的逻辑长度不作为物理分配越限证据，填充实际 block 后验证上限；同时覆盖 inode 耗尽。

**Acceptance Criteria**

配置开启且验证失败时用户命令调用为 0；受限环境确实耗尽自身容量而不是宿主共享盘。普通 bind mode 的能力缺口对报告可见。

### TASK-08：补齐真实故障和资源验收（P0/P1 汇合）

**Goal**

用真实运行证据覆盖父进程崩溃、daemon 故障和采集内存上限。

**Files / Symbols**

```text
tests/acceptance/run_real_sandbox_matrix.py::parent_crash/output_limit/run
tests/acceptance/docker_fault_proxy.py（新增）::DockerFaultProxy
src/patchloop/sandbox.py::DockerSandboxConfig/Docker 控制命令
src/patchloop/sandbox_supervisor.py
tests/unit/test_real_sandbox_matrix.py
```

**Implementation**

1. 将 stdout-stderr-capture-memory-bound 静态 note 换成 256 MiB 流量探针；独立小采样进程读取父进程/supervisor 的 `/proc/<pid>/status` RSS，每 20ms 采样，记录 baseline/peak/delta。
2. parent-crash-cleanup 分别记录 0.5 秒观察和 20 秒截止；先验证自动清理，再做单独的 parent-crash-recovery 用例；拒绝把强制 cleanup 后的零残留算自动通过。
3. 配置仅本机 unix docker_host；按 4.8 实现私有 socket 代理，覆盖启动前不通、运行中断连、rm 中断连、恢复后 reconcile；共享 daemon 不执行 stop。先让清理失败结论落盘，再恢复代理连接验证重试，不能过早恢复掩盖失败路径。
4. 配置检查和 final residue 以同一 socket/image/run_id 执行；daemon 查询不通直接使 target gate 非绿。
5. CPU/PID/tmpfs/输出/磁盘限制的声明各关联明确 evidence，不在 threat_model 写超过已验证配置的保证。

**Interface Changes**

Matrix CLI `--docker-host unix:///...` 仅用于可信验收环境，默认使用本机默认 socket；`--daemon-fault-mode proxy|skip` 默认 proxy；资源输出、fault_type 和清理 deadline 证据加入 schema v2。

**Tests**

- RSS 采样进程失败记 unverified；输出管道持续排空；日志驱动无落盘。
- 断连时不得把任何 managed command 写为已确认停止，不得授予新 writer；重连可幂等恢复；全程确认另一正常连接和其他任务容器未受影响。
- 必须在正向可写、已确认用户负载真正 started 后执行 SIGKILL 场景。

**Acceptance Criteria**

23 个原 Linux 适用项全部通过，新增用例全部通过；Windows junction 仍为 not_applicable。Linux 必需项 failed/unverified 都为 0。

### TASK-09：修正 SRF-07 的真实后端与目标门槛（P0）

**Goal**

三项 Docker 验收确实运行 Docker，目标平台状态不被跨平台缺席混淆。

**Files / Symbols**

```text
tests/acceptance/srf07_docker_backend.py
tests/fixtures/session_backend_acceptance.json
src/patchloop/evaluation/backend_acceptance.py
tests/unit/test_backend_acceptance.py
```

**Implementation**

1. timeout case 先做相同 backend 的可写 marker 对照；takeover 等待容器 State.Running 再推动时钟，并在 finally join/cleanup。
2. 新增 `test_docker_recovery_preserves_user_modified_file`：真实 Docker 写入测试 fixture，执行记录持久化；注入“副作用已发生但 effect 结果未结算”的中断，宿主写入 User edit；resume 必须停止旧容器并保留 User edit，不自动重跑原命令。FakeProvider 可用于确定性调度，DockerSandbox 不可替身。
3. fixture 的 docker-user-modified-file 改为新 nodeid；通用文件恢复测试保留，不能删原覆盖。容器 UID/mount 断言证明确实使用目标 backend。
4. runner 加 `--required-backend docker|windows`（可重复），未提供时仍要求两者；report schema v2 加 required_backends/target_status，原 status 仍按全量 results 汇总。
5. 子 pytest 保存 JUnit，必须 tests>0 且 skipped=0、failures/errors=0；仅 returncode=0 不足。target_status 决定 CLI exit：passed=0、failed=1、unverified=2。
6. matrix.docker_image 必须传播到被测 backend，不能只用于 probe；通过 runner 设置受信任 fixture 环境变量传入，测试构造读取并校验值，报告记录实际 image ID。

**Interface Changes**

报告/CLI 如上；schema v1 历史报告仍可读，缺少 target 字段时按原总状态处理；不覆盖历史 artifact。

**Tests**

- Linux Docker 全通过而 Windows 缺席：status=unverified、target_status=passed；未指定 required-backend 时退出 2。
- backend available 但 pytest skipped/未收集/错误 image 均不得 passed。
- Docker 用户修改文件必须验证真正 Docker 的执行记录和容器身份。

**Acceptance Criteria**

Docker 三用例通过且证据来自真实 Docker；Windows 不可用仍明确 unverified；全平台完成另需 Windows 实测。

### TASK-10：完整回归与结果发布（P0/P1 完成门槛）

**Goal**

在同一提交、镜像和配置上形成可复核的优化后报告。

**Files / Symbols**

```text
benchmarks/results/sandbox_real_adversarial_linux_optimized.json（新增）
benchmarks/results/srf07_backend_acceptance_linux_optimized.json（新增）
benchmarks/results/sandbox_policy_integration_linux_optimized.xml（新增）
docs/SANDBOX_OPTIMIZATION_PLAN.md::完成记录（执行时添加）
```

**Implementation**

1. 运行第 8 节检查；每项出现失败先定位，不通过放宽断言、删除用例、提高资源上限掩盖问题。
2. 生命周期/TOCTOU/接管等竞争场景在一次验收内各运行 3 次；一轮失败即整体失败，不采用多数投票。
3. 保存代码 hash、工作树 dirty/diff hash、harness hash、image ID、资源配置和原始 case evidence；验收报告不得只接受任意传入的 source_commit 而不与实际 HEAD 核对。
4. Windows 只运行相关 shared-code 和 Job Object 回归；Linux 完整 gate 不冒充全平台证书。
5. 生成新文件，保留用户提供的原始报告；提交信息遵循 `[英文前缀]:[中文说明]`，例如 `fix:修复Docker沙箱生命周期与资源限制`。本方案本身不要求自动提交。

**Interface Changes**

N/A。

**Tests**

第 8 节全部检查；对报告汇总做严格 schema 和计数一致性验证。

**Acceptance Criteria**

P0 和 P1 门槛分开记录；只有完整容量配置、daemon 故障实测及所有必需真实用例通过，才写“Linux Docker 完整验收通过”。

## 7. Implementation Order

```text
TASK-01（验收可信）
→ TASK-02（清理） + TASK-03（采集，可并行）
→ TASK-04（启动与崩溃管理）
→ TASK-05（身份/资源） + TASK-06（配置贯穿，可并行但协调 sandbox.py）
→ TASK-07（容量） + TASK-09（SRF，可并行）
→ TASK-08（完整真实矩阵）
→ TASK-10（回归与报告）
```

“可并行”仅说明代码依赖，不要求自动创建多 agent。共享 `sandbox.py` 的工作采用分支顺序合并或明确函数归属。

P0 中间版本可用于验证主要缺陷已修复，但报告仍保留 workspace-storage-limit/未完成连接故障的非通过结果；不能当完整安全版本发布。P1 不以性能重构、容器复用为前置。

回退：通过代码版本回退，不通过自动降级 local、取消 resource flags 或吞掉 cleanup error 回退。新 payload 字段回退到不识别该字段的旧代码前，需要兼容读取措施；直接运行老版 `extra='forbid'` 模型会失败，不能宣称无条件双向兼容。旧数据→新版的读取必须保证。

## 8. Verification

以下是**实现完成后的执行命令**；本次设计阶段只运行了第 2 节的 10 个现有单元测试。

### 本地单元/集成回归

```powershell
# Windows，仓库根目录；不触发真实 Docker 压力矩阵
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_sandbox.py tests/unit/test_sandbox_output.py tests/unit/test_sandbox_storage.py tests/unit/test_real_sandbox_matrix.py tests/unit/test_backend_acceptance.py
.\.venv\Scripts\python.exe -m pytest -q tests/integration/test_managed_command_runtime.py tests/integration/test_execution_takeover_recovery.py tests/integration/test_execution_ownership.py tests/unit/test_tools.py tests/integration/test_cli.py tests/integration/test_session_cli.py
.\.venv\Scripts\python.exe -m pytest -q tests/e2e/test_security_observability.py tests/e2e/test_policy_runtime.py tests/integration/test_workspace_service.py
.\.venv\Scripts\python.exe -m ruff check src/patchloop tests/acceptance tests/unit/test_sandbox.py tests/unit/test_sandbox_output.py tests/unit/test_sandbox_storage.py tests/unit/test_real_sandbox_matrix.py tests/unit/test_backend_acceptance.py
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\python.exe -m pytest -q
```

### Linux 真实验收

连接使用 `ssh glinfen@lab-pc`；按仓库约定先正常权限一次，失败后仅在权限允许时沙箱外最多重试两次，再失败停止服务器相关工作并报告原始错误。

下面命令在已准备依赖的隔离 checkout 中执行；容量工作区先由管理员运行新 provision 工具，明确授权后才创建/挂载新文件系统。禁止把用户现有仓库直接格式化、移动或覆盖。运行时不需要 API key，不上传 `.env`。

```bash
# 安装本项目并构建被测镜像；只在准备好的验收环境执行
python -m pip install -e '.[dev]'
docker build -f docker/sandbox.Dockerfile -t patchloop-sandbox:py313 .

# 管理员准备独立 64 MiB / 4096 inode 空工作区；默认先 dry-run
# 脚本需按 TASK-07 实现这些参数，receipt 写在工作区外
bash scripts/provision_sandbox_workspace.sh --root /data/PatchLoop/sandbox --name acceptance-20260921 --size-mb 64 --inodes 4096 --uid "$(id -u)" --gid "$(id -g)"
# 管理员审核 dry-run 后，同参数加 --apply；挂载根为 /data/PatchLoop/sandbox/acceptance-20260921/workspace

python -m pytest -q tests/e2e/test_security_observability.py tests/e2e/test_policy_runtime.py tests/integration/test_workspace_service.py --junitxml=benchmarks/results/sandbox_policy_integration_linux_optimized.xml

python -m patchloop.evaluation.backend_acceptance --matrix tests/fixtures/session_backend_acceptance.json --root . --required-backend docker --output benchmarks/results/srf07_backend_acceptance_linux_optimized.json

python tests/acceptance/run_real_sandbox_matrix.py --root . --workspace /data/PatchLoop/sandbox/acceptance-20260921/workspace --workspace-limit-mb 64 --workspace-inode-limit 4096 --source-commit "$(git rev-parse HEAD)" --output benchmarks/results/sandbox_real_adversarial_linux_optimized.json
```

最后一条矩阵命令默认包含 socket 代理故障用例，并将启动前、运行中、清理中断连和恢复证据写进同一个报告。`--daemon-fault-mode skip` 只能用于诊断，报告必需项仍为 unverified。若使用专用本机 daemon，可传 `--docker-host unix:///实际socket`，镜像也必须存在于该 daemon；恢复严格使用 identity 中的 endpoint，不能退回默认 daemon。

### 发布门槛

| Gate | 必须达到 |
| --- | --- |
| P0 功能 | 可写 workspace；内存/swap 有效；双流采集有界；小写 absent/identity race/启动登记竞态正确；无隐式 local |
| 生命周期 | 正常/timeout/cancel/pause/lease loss/父进程 crash 无残留；daemon 故障时不误判成功；恢复后可接管 |
| P1 容量 | 配置的总容量/inode 经真实写入耗尽验证；无配额环境拒绝完整资格 |
| Linux 对抗矩阵 | 所有 Linux 必需项 passed；failed=0、unverified=0；Windows junction 的 not_applicable 不参与 Linux gate |
| SRF-07 | Docker 3/3 真实用例 passed，target_status=passed；Windows 缺席在总报告中保留 |
| Policy/Approval | 原 30 项全部通过；新增降级/配置用例也通过，不依赖精确总数冻结测试扩展 |
| 证据一致性 | 实际 HEAD/harness/image/config 一致；源码 dirty 明确；清理前后证据分开；无手工修改 passed |

## 9. Blockers

**实现设计无未决架构 BLOCKER**：模块、接口、生命周期和每项断言均已确定。以下是不能用模拟或乐观假设代替的验收/部署前提：

1. **ENV-BLOCKER：真实主机身份与 cgroup 支持。** 现有报告没有 UID/GID、userns、memory.swap.max。TASK-05 预检采集；不满足时阻止对应执行/验收，不能恢复 capabilities 或关闭限制。无需等待这些信息就可写代码及单元测试。
2. **ENV-BLOCKER：容量受限工作区预配。** 现有 lab-pc 普通 bind 目录未证明有独立上限。管理员需准备新的固定容量 ext4 工作区；运行时不提权、不移动用户仓库。缺少这一条件只阻止 TASK-07 真实验收及完整 gate，P0 可继续。
3. **ENV-BLOCKER：本机 Unix socket 访问。** TASK-08 需要当前验收用户能连接本机 daemon，并能在自己的私有目录创建 Unix socket。代理故障不需要管理员停止共享服务；环境禁止代理时对应项保留 unverified。真实 dockerd 进程 crash/restart 不在这项 evidence 的承诺内，不能改写成已测试。
4. **平台边界：Windows。** Linux 报告不证明 Windows junction/Job Object 等已通过。本方案共享采集逻辑有 Windows 回归门槛；独立 Windows 安全矩阵仍需该平台真实报告。

上述条件不要求修改 API key 配置、不需要付费模型实验。本轮只新增方案文档；未执行服务器连接、挂载、故障注入、实现修改或发布。

## 10. 完成记录（2026-09-22）

本节记录方案实施后的实际结果；第 9 节末尾关于“未执行”的描述仅代表方案编写时状态，现已由本节取代。

### 验收环境与证据身份

- Linux 主机：`Linux 6.8.0-139-generic`，Docker client/server `29.1.3`，宿主测试解释器 Python `3.12.3`。
- 隔离验收快照：`c9a37a160678ab8ae91782b7b589d73745b66512`；`source_dirty=false`；源码差异摘要 `33c2da2f369cc6a117055a71d327f41de48fe448131a6a62c936189bf2d3e4c3`。
- 矩阵 harness 摘要：`a4ef85ded79e5b1cdf4a29b2a7d49ea361fcf90742d93e5f7d0d30015dc2cb48`。
- Dockerfile 摘要：`628242a5561805afc406457fe5531a8971a7fd2fd1f2527d6f3664e7a3390f34`。
- 镜像：`patchloop-sandbox:py313`，实际 ID `sha256:564f1ce670ec07cc5e635a942f733f731ac9bc3a61d64e702794ad4a3303caf3`。
- 容量工作区：`/workspace`，独立 ext4，`rw,nosuid,nodev`、private propagation；总容量 `61,820,928` bytes、`4096` inode。
- 上传内容仅包含源码、测试、Dockerfile、脚本和项目元数据；未上传 `.env`、密钥或既有结果。

### 发布产物

| 产物 | SHA256 | 结果 |
| --- | --- | --- |
| `benchmarks/results/sandbox_real_adversarial_linux_optimized.json` | `8109e38901b26bea60fe5e963f175d0e0b64bf53e605d3c57c86168047c4ac70` | `24 passed`、`1 not_applicable`、`0 failed`、`0 unverified` |
| `benchmarks/results/srf07_backend_acceptance_linux_optimized.json` | `7fffde6b9cc48b82494b21b6a4c5e0be155f2347099117eb583a0003e923c6eb` | Docker `3/3 passed`，`target_status=passed`；Windows 缺席仍保留为全局 `unverified` |
| `benchmarks/results/sandbox_policy_integration_linux_optimized.xml` | `75cd380fa3d6427a63bed34d37290dadc768cff2edde88c58234b36483b2e321` | `31 tests`，`0 failures/errors/skips` |

原始 `sandbox_real_adversarial_linux.json`、`srf07_backend_acceptance_linux_real.json` 和 `sandbox_policy_integration_linux.xml` 均保留，未覆盖。

### Gate 结论

- **P0 功能：通过。** workspace 正向可写、UID/GID 与 mount 身份、cgroup memory/swap/PID/CPU、tmpfs、网络、环境与双流有界采集均有真实证据；256 MiB 流量仅保留 4096 字符，独立采样的 RSS 增量低于 96 MiB 门槛。
- **生命周期：通过。** normal/timeout/cancel/pause、启动与身份竞态、父进程 crash、daemon 启动/运行/rm 断连及恢复均通过；16 个生命周期、TOCTOU 与接管类 case 各执行 3 次，任一次失败都会使总报告失败。
- **P1 容量：通过。** 实际写入 `56,623,104` bytes 后得到 `ENOSPC`；创建 `4079` 个文件后 inode 耗尽；96 MiB 稀疏文件物理分配为 0，原 fixture 完整，清理后小写入恢复成功。
- **SRF-07：通过 Docker 目标门槛。** timeout、旧 worker 接管、用户修改文件恢复均由真实 Docker 执行，JUnit 每项 `tests=1`、`skipped=failures=errors=0`。
- **清理：通过。** 最终矩阵的 `residual_before_cleanup` 与 `residual_after_cleanup` 均为空，验收工作区恢复为空目录（除 ext4 的 `lost+found`）。
- **Linux Docker 完整验收通过。** 本结论不冒充独立 Windows 安全矩阵；Windows shared-code 回归及 Job Object 父进程崩溃用例在本地执行，后者连续 3 次通过。
- **最终本地回归：通过。** Windows 工作树执行全量 `pytest -q` 得到 `1059 passed, 4 skipped`；4 个 skip 均为显式环境/平台条件。Ruff 通过，mypy 对 `105` 个源文件检查无问题，`git diff --check` 无 whitespace error。
