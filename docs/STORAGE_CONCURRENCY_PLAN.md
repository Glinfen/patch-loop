# PatchLoop 存储并发与多 Agent 就绪开发计划

## 1. 状态与目标

状态：提案，尚未开始实施。

2026-09-05 优先级补充：本计划的单 writer、version、lease 与 fencing 不只服务未来多 Agent，也直接阻塞单 Agent 的多终端执行和重启恢复。第二阶段首个闭环由 [持久化 Session 与恢复闭环计划](SESSION_RUNTIME_FOUNDATION_PLAN.md) 统一安排，并复用本计划的相关任务；本文后续关于可延后到多 Agent 前实施的排期建议不再适用于这一闭环。本文的 execution session 在新计划中称为 Execution，与长期用户 Session 区分。lease 只保护数据库提交，外部进程与文件写入接管还须遵守新计划第 5.4 节的清理和独占约束。

PatchLoop 当前继续使用 workspace-local SQLite，不提前迁移 PostgreSQL。本计划的目标是在不改变本地优先部署方式的前提下，补齐未来多 Agent 所需的三项基础能力：

- 以稳定接口隔离 Runtime 与 SQLite 实现，后续可以替换为 PostgreSQL，而不重写 Agent Runtime。
- 以 task version 和 task lease 防止同一任务被多个执行会话重复运行或相互覆盖。
- 以 workspace write lease 保证同一工作区只有一个执行会话能够修改文件、运行一致性敏感命令并提交状态。

本计划解决的是单机和未来分布式执行都需要的并发语义，不以增加并发量为目标。完成后，多 Agent 可以并行检索、分析和提出方案，但同一 workspace 的修改仍由持有写 lease 的协调执行会话串行完成。

## 2. 当前能力与主要差距

当前实现具有以下基础：

- 每个仓库使用 `<repository>/.patchloop/patchloop.db`，不同 workspace 默认隔离。
- `SQLiteStore` 和 `SQLiteMemoryStore` 使用同一个数据库文件。
- 每次操作创建独立连接，开启外键和 WAL，并配置 10 秒连接等待。
- task、step、tool result、checkpoint、artifact 和 memory batch 已有事务回滚。
- 工具调用通过 `(task_id, call_id)` 去重，恢复时能够跳过已经确认的工具结果。

但当前并发边界不完整：

- `AgentRuntime`、CLI 和评测代码直接依赖具体的 `SQLiteStore`。
- task 记录没有单调递增的 version，无条件 upsert 可能让旧状态覆盖新状态。
- `cancel_task` 是先读后写，不是单个条件更新事务。
- checkpoint 是最后写入获胜，无法识别失去所有权的旧 worker。
- 没有 task owner、lease 到期时间或 fencing generation。
- 没有 workspace 级写入所有权；两个任务可以同时修改同一个仓库。
- WAL 和 busy timeout 没有启动验证与并发回归测试。
- 没有统一的 lease 丢失错误、Trace、指标和 CLI 退出语义。

## 3. 设计原则

- SQLite 保持默认实现；只有出现跨机器 worker、集中任务队列或实测写入瓶颈时才增加 PostgreSQL 实现。
- 先固定语义，再抽象接口；接口表达 PatchLoop 需要的原子操作，不暴露通用 SQL 或 ORM 查询对象。
- 存储 version 与业务 `Task` 模型分离，避免把数据库并发元数据混入 Provider 上下文、报告和任务 JSON 契约。
- lease 只是临时执行所有权，task 状态是持久业务状态，两者不能互相替代。
- task lease 与 workspace write lease 使用不可复用 token 和单调递增 generation。旧执行者即使在 lease 过期后恢复，也不能继续提交写入。
- 所有关键校验和对应写入必须处于同一数据库事务，禁止先检查、退出事务后再写。
- 失去 lease 时失败关闭：停止新的工具调用和持久化，不自动抢回 lease，也不把任务误报为成功。
- 一个多 Agent 编排会话共享一个 execution owner。协调者持有 workspace lease，子 Agent 通过协调者提交写操作，不为每个子 Agent 单独建立 workspace writer。
- 事务内只做必要的条件查询和数据库写入。脱敏、压缩、模型验证及大对象序列化尽量在事务开始前完成。
- 时间、UUID 和 owner ID 均可注入，保证 lease 到期和竞争测试确定可复现。

## 4. 目标模型与并发语义

### 4.1 Execution session

每次 `run` 或 `resume` 创建一个 execution session：

```text
execution_id: 本次执行的唯一 UUID
worker_id: 当前进程或未来远程 worker 的稳定标识
task_id: 被执行的任务
workspace_id: 规范化仓库路径的内容哈希
```

`execution_id` 是 lease owner 的最小边界。未来一个 execution session 可以包含协调 Agent 和多个只读子 Agent，它们共享所有权，不以 Agent 名称作为数据库锁 owner。

### 4.2 Task version

每个 task 存储记录增加独立的 `version`，从 1 开始，每次业务状态更新后递增。读取返回 `VersionedTask(task, version)`，更新必须携带 `expected_version`：

```text
UPDATE tasks
SET ..., version = version + 1
WHERE id = :task_id AND version = :expected_version
```

影响行数为 0 时抛出 `StaleTaskVersionError`，调用方必须重新读取并显式决定是否重试。禁止在冲突后静默覆盖。

以下变化必须增加 task version：

- `created -> running -> completed/failed/cancelled` 状态迁移；
- plan、report、result 或 error 变化；
- execution 配置或预算等持久字段变化。

lease 续约不增加 task version，避免心跳制造无业务意义的版本冲突。

### 4.3 Task lease

task lease 保证同一 task 同时只有一个活动 execution session。最小字段为：

| 字段 | 含义 |
| --- | --- |
| `lease_owner_id` | execution session ID |
| `lease_token` | 每次获取随机生成、不可复用的 token |
| `lease_generation` | 每次成功获取或强制失效时递增的 fencing number |
| `lease_expires_at` | UTC 到期时间 |
| `lease_acquired_at` | 首次获取时间 |
| `lease_renewed_at` | 最近续约时间 |

获取 lease 使用单个原子事务，只允许以下 task：

- 尚无 lease；
- lease 已过期；
- 调用者持有完全相同的 token，用于幂等恢复获取结果。

返回 `TaskLease` 后，Runtime 的 task、step、tool result、checkpoint 和 memory 写入都必须携带 `LeaseGuard(task_id, token, generation)`。Store 在同一事务内确认 token、generation 和到期时间，再执行写入。

`cancel` 属于控制面操作，可以不持有执行 lease，但必须在一个事务中：

1. 读取并验证当前状态；
2. 将 task 更新为 `cancelled` 并增加 task version；
3. 清空当前 task lease 并增加 lease generation，使旧执行者立即失去提交资格。

### 4.4 Workspace single writer

workspace write lease 防止不同 task 或不同进程同时修改同一仓库。新增 `workspace_leases` 表：

| 字段 | 含义 |
| --- | --- |
| `workspace_id` | 规范化仓库真实路径的稳定哈希 |
| `repository_path` | 用于本地诊断的规范化、脱敏路径 |
| `task_id` | 当前写入任务 |
| `owner_id` | execution session ID |
| `lease_token` | 本次获取 token |
| `generation` | workspace fencing number |
| `expires_at` | UTC 到期时间 |
| `acquired_at` | 获取时间 |
| `renewed_at` | 最近续约时间 |

首版采用以下规则：

- 只读 task 不获取 workspace write lease。
- 具有 WRITE 或 EXECUTE 权限的 `run`、`resume` 在进入 Runtime 前获取，并持有到任务终止或执行退出。
- 同一 execution session 内的多个 Agent 共用一个 workspace lease。
- 不允许另一个 task 在相同 workspace 中“加入”现有 lease。
- 不同 workspace 可以并行执行。
- 获取顺序固定为 task lease、workspace lease；workspace 获取失败时立即释放刚获得的 task lease，避免无用占用。
- release 必须匹配 token 和 generation。旧 owner 的 release 不得删除新 owner 的 lease。

持有整个任务周期会降低同一 workspace 的并发度，但可以避免两个任务的补丁、测试、diff 和 checkpoint 相互污染。未来如需更高并行度，应为每个任务创建独立 Git worktree，而不是缩小锁范围让多个任务直接交错修改同一目录。

### 4.5 Lease 生命周期

默认 TTL、续约间隔和退出行为集中配置，不散落在 Runtime 与 CLI 中。初始建议：

- task lease TTL：60 秒；
- workspace lease TTL：60 秒；
- 后台续约间隔：20 秒；
- 进入模型请求、工具调用和 checkpoint 写入前额外执行一次快速所有权检查；
- 外部命令最大超时不得大于 lease TTL，除非续约线程在命令执行期间保持活动。

如果续约失败或发现 generation 改变：

1. 将当前 execution session 标记为 `lease_lost`；
2. 阻止新的 Provider 和 Tool Gateway 调用；
3. 尝试终止仍在运行的本地或容器命令；
4. 不再保存普通 checkpoint、task 成功状态或 memory；
5. 记录脱敏事件并向 CLI 返回专用退出码；
6. 保留 task 当前持久状态，由新 owner 或用户决定恢复。

进程崩溃后不要求主动清理；lease 到期后可以由新 execution session 获取。新 owner 获取时 generation 必须递增，从而隔离迟到的旧写入。

## 5. 存储接口设计

### 5.1 接口边界

新增 `patchloop.persistence.contracts`，定义领域化 Protocol 和数据模型：

```text
RuntimeStateStore
  create_task
  get_task
  update_task
  cancel_task
  list_steps
  record_step
  get_tool_result
  record_tool_call
  get_checkpoint
  save_checkpoint
  list_artifacts
  record_artifact
  delete_task

TaskLeaseStore
  acquire_task_lease
  renew_task_lease
  release_task_lease
  assert_task_lease

WorkspaceLeaseStore
  acquire_workspace_lease
  renew_workspace_lease
  release_workspace_lease
  assert_workspace_lease

MemoryStore
  继续保留当前查询与 save_batch 能力
  写方法增加可选且最终强制的 LeaseGuard
```

配套类型：

- `VersionedTask`
- `TaskLease`
- `WorkspaceLease`
- `LeaseGuard`
- `WorkspaceLeaseGuard`
- `LeaseConflict`
- `StaleTaskVersionError`
- `TaskLeaseHeldError`
- `WorkspaceLeaseHeldError`
- `LeaseLostError`

接口只提供 PatchLoop 所需的原子操作，不加入 `execute_sql`、通用 transaction callback 或后端特有连接对象。

### 5.2 依赖注入

- `AgentRuntime.state_store` 从 `SQLiteStore | None` 改为 `RuntimeStateStore | None`。
- Memory Store 独立注入或通过明确的 `memory_store` 属性暴露，不再依赖具体 `SQLiteMemoryStore` 类型。
- CLI 的 composition root 负责创建 `SQLiteStateStore`、clock、lease policy 和 execution session。
- 测试使用实现相同 Protocol 的确定性 fake；涉及锁和事务语义的测试必须运行真实 SQLite contract suite，不能只依赖 fake。
- 保留 `SQLiteStore` 兼容别名一个发布周期，随后统一命名为 `SQLiteStateStore`。

### 5.3 写入守卫

所有 execution-owned 写入均增加 guard：

```text
update_task(..., expected_version, task_lease)
record_step(..., task_lease)
record_tool_call(..., task_lease)
save_checkpoint(..., task_lease)
record_artifact(..., task_lease)
memory.save_batch(..., task_lease)
```

创建 task、只读查询、控制面 cancel 和数据库迁移不使用普通 execution guard。是否删除 task必须保持显式管理操作，并拒绝删除仍有有效 lease 的 task，除非未来增加单独的管理员强制操作。

## 6. SQLite schema 与连接策略

### 6.1 Runtime schema migration

复用 `patchloop_schema_migrations`，为 `runtime` 组件建立独立版本，不再只依赖 `CREATE TABLE IF NOT EXISTS`。迁移顺序为：

1. 创建最小基础表；
2. 执行 Runtime schema migration；
3. 执行 Memory schema migration；
4. 验证表、列、索引和约束完整；
5. 任一步失败则回滚并拒绝启动。

现有 task 表新增 task version 和 lease 字段；新增 `workspace_leases` 表及到期时间索引。迁移必须保留已有 task、checkpoint、tool result 和 memory 数据，旧 task 初始 version 设为 1，lease 字段为空，generation 设为 0。

不要在此次迁移中修改 `Task` JSON schema。关系列承担并发控制，`payload_json` 继续保存业务 Task。

### 6.2 事务策略

- lease 获取、过期接管、cancel 和受 guard 保护的写入使用显式短事务。
- 需要抢占写锁的条件更新使用 `BEGIN IMMEDIATE`，避免读取成功后在升级写事务时产生不明确竞争。
- 序列化、脱敏、压缩和内容哈希在事务外完成。
- transaction 内使用数据库 UTC 时间或统一注入的时间值；同一操作只能读取一次 `now`。
- 所有失败路径显式 rollback，不能依赖连接关闭的隐式行为。
- 不在 transaction 内调用 Provider、文件系统、Docker、Git 或其他外部命令。

### 6.3 连接配置

将 SQLite 连接创建集中到一个 connection factory，并在每个连接上设置和验证：

```text
foreign_keys = ON
journal_mode = WAL
busy_timeout >= 10000 ms
```

启动时检查 `journal_mode` 返回值。如果目标文件系统不支持 WAL，则给出明确错误，不静默退回 DELETE journal。`synchronous` 和 `wal_autocheckpoint` 暂时使用有文档的 SQLite 默认或当前显式值，只有基准数据证明需要时才调整。

SQLite 数据库不得放在 NFS、SMB 或其他跨主机共享文件系统上供多个 worker 写入。检测不到文件系统类型时，在文档和启动提示中明确这一部署限制。

## 7. Runtime、Tool Gateway 与 CLI 集成

### 7.1 Run 和 resume

`run` 与 `resume` 的入口流程统一为：

```text
创建 execution session
  -> 原子获取 task lease
  -> 有写入或执行权限时获取 workspace write lease
  -> 条件更新 task 状态/version
  -> 启动 lease heartbeat
  -> 执行 Agent Runtime
  -> 条件保存终态
  -> 停止 heartbeat
  -> 释放 workspace lease
  -> 释放 task lease
```

如果进程无法获取任一 lease，不创建 Provider 请求，不执行工具，也不修改 task 业务状态。

### 7.2 Tool Gateway

- READ 工具允许在没有 workspace write lease 时运行，但仍要求调用者持有 task lease。
- WRITE 工具执行前必须验证 task lease 和 workspace lease。
- EXECUTE 工具可能读取或修改 workspace，也纳入 workspace lease；首版不尝试按命令内容推断“纯读取”。
- lease 在工具运行中丢失时，结果可以写入本地 Trace 作为诊断，但不得作为已确认工具结果提交到 SQLite。
- 多 Agent 模式下，子 Agent 的修改请求必须进入同一个 Tool Gateway/协调者队列，不能绕过 lease-aware gateway 直接操作文件。

### 7.3 CLI 行为

- lease 冲突输出持有 task、owner 的安全摘要和到期时间，不输出 lease token。
- `status` 显示是否有活动执行者、lease 剩余时间和 generation。
- `cancel` 原子取消并 fence 旧 owner；旧进程下一次 guard 检查后退出。
- `resume` 只允许没有有效 owner 或 lease 已过期的 running task。
- 首版不提供 `--force-unlock`。开发调试需要时提供只读诊断命令；强制接管必须等审计和 fencing 测试完成后另行设计。

## 8. 开发任务

### SAR-00：并发契约与失败模型

开发内容：

- 新增 ADR，固定 task version、task lease、workspace single writer、generation fencing 和 crash recovery 语义。
- 定义哪些操作属于 execution-owned、control-plane 和 read-only。
- 固定 lease 获取顺序、TTL、续约、释放和丢失后的行为。
- 建立当前实现的双 worker 失败复现测试，证明无版本控制时可能覆盖 task/checkpoint。

完成标准：ADR 被接受；并发状态表覆盖 run、resume、cancel、过期接管、正常完成、进程崩溃和 lease 丢失；至少一个旧实现失败用例能够稳定复现。

### SAR-01：存储接口与 contract suite

开发内容：

- 新增 Store Protocol、版本化读取模型、lease 模型和专用错误。
- Runtime、CLI 和 Memory Manager 改为依赖 Protocol。
- 将当前 SQLite 实现接入接口，暂时保持既有行为。
- 建立后端 contract suite，覆盖 CRUD、事务回滚、幂等工具结果、级联删除和 Memory 批次。

完成标准：核心 Runtime 不再导入具体 `SQLiteStore`；现有功能和测试保持通过；SQLite 与测试 fake 均通过适用的 contract suite。

### SAR-02：Runtime schema migration 与连接基线

开发内容：

- 为 Runtime schema 增加版本化迁移。
- 新增 task version、task lease 字段和 `workspace_leases` 表。
- 集中 SQLite connection factory，显式设置 busy timeout 并验证 WAL。
- 增加旧数据库升级、重复升级、迁移回滚和不完整 schema 拒绝启动测试。

完成标准：已有数据库无数据丢失升级；迁移幂等；WAL、foreign keys 和 busy timeout 可由自动化测试读取验证；不支持 WAL 时失败信息明确。

### SAR-03：Task version 与 task lease

开发内容：

- 实现条件 task 更新和 `StaleTaskVersionError`。
- 实现 task lease 获取、幂等获取、续约、释放、过期接管及 generation fencing。
- 将 task、step、tool result、checkpoint、artifact 和 memory 写入接入 `LeaseGuard`。
- 将 cancel 改为原子状态更新并使旧 lease 失效。

完成标准：多个连接竞争同一 task 时只有一个成功；旧 version 和旧 lease 的所有写入均失败；取消后旧 owner 不能再保存 checkpoint 或完成状态。

### SAR-04：Workspace single writer

开发内容：

- 实现稳定 workspace ID 和 workspace lease Store。
- 在 CLI run/resume 生命周期中获取、续约和释放 workspace lease。
- Tool Gateway 对 WRITE/EXECUTE 执行双重 guard。
- 为未来多 Agent 定义共享 execution owner 和协调写入队列接口，但本阶段不实现多 Agent 调度器。

完成标准：相同 workspace 的两个不同 task 只有一个能够进入写执行；不同 workspace 可以并行；旧 owner 不能释放或覆盖新 owner；任何 WRITE/EXECUTE 工具都无法绕过 guard。

### SAR-05：Heartbeat、故障恢复与可观测性

开发内容：

- 实现可停止的 lease heartbeat，并覆盖模型等待和长工具运行期间。
- lease 丢失后触发 fail-closed Runtime 路径和沙箱终止。
- 新增 `lease.acquired`、`lease.contended`、`lease.renew_failed`、`lease.lost`、`lease.released` 和 `lease.takeover` 事件。
- 指标记录竞争次数、等待时间、续约失败、过期接管和 stale write 拒绝；不记录 token。
- `status`、`replay` 和错误输出增加 lease 解释。

完成标准：模拟进程停止续约后，新 owner 能接管；旧 owner 恢复后不能产生持久写入；Trace 可以解释所有权如何转移；凭据与 lease token 泄漏为 0。

### SAR-06：并发、性能与回归验收

开发内容：

- 使用多个真实 SQLite 连接和进程级测试验证竞争，不只使用线程内 fake。
- 重复执行 task claim、workspace claim、cancel、checkpoint、memory batch 和过期接管竞争。
- 记录 transaction duration、busy wait、锁错误和 lease 操作 p50/p95/p99。
- 运行全部 lint、类型检查、单元、集成、端到端和恢复测试。
- 补充部署限制、故障排查和未来 PostgreSQL Adapter 说明。

完成标准见第 10 节。

## 9. 实施顺序与预计工作量

| 阶段 | 任务 | 预计工作量 | 主要产物 |
| --- | --- | ---: | --- |
| A：契约 | SAR-00、SAR-01 | 2～3 天 | ADR、Store Protocol、contract suite |
| B：持久化 | SAR-02、SAR-03 | 3～4 天 | schema migration、version、task lease、fencing |
| C：工作区 | SAR-04、SAR-05 | 2～3 天 | workspace single writer、heartbeat、可观测性 |
| D：验收 | SAR-06 | 1～2 天 | 多进程竞争矩阵、性能报告、部署文档 |

总计约 8～12 个开发日。SAR-00 至 SAR-03 是最小安全闭环；在它们完成前不要开始多 Agent 共享 task。SAR-04 完成前，多任务必须使用独立 worktree 或保持串行。

本计划与提示缓存优化没有代码级前置关系。若当前仍以 PCO 路线为最高优先级，可以先完成 SAR-00 和 SAR-01 固定边界，待开始多 Agent 开发前再连续完成 SAR-02 至 SAR-06；不能只合并接口而长期不实现写入 guard。

## 10. 总体验收门禁

全部门禁通过后，才能声明“单机多 Agent 存储基础就绪”：

- Runtime、CLI 和 Memory Manager 不依赖具体 SQLite 类型。
- 旧 SQLite 数据库可以原地、幂等、无数据丢失升级。
- 8 个竞争者同时获取同一 task，连续 100 轮每轮恰好一个成功。
- 8 个竞争者同时获取同一 workspace，连续 100 轮每轮恰好一个 writer。
- lease 过期接管后，旧 token、旧 generation 和旧 task version 的成功写入数均为 0。
- cancel 与 worker 完成同时发生时，最终状态满足固定优先级，不能出现 cancelled task 被旧 worker 改回 completed。
- checkpoint、tool result 和 Memory 批次在 lease 丢失后均拒绝提交，且不留下半事务。
- 相同 workspace 的 WRITE/EXECUTE 工具最大并行数为 1；不同 workspace 的并行不被全局锁串行化。
- 进程在持有 lease 时被强制终止，新 execution session 能在 TTL 后恢复，不重复已确认工具副作用。
- 应用连接实测 `journal_mode=wal`、`foreign_keys=1`、`busy_timeout>=10000`。
- 并发验收中未处理的 `database is locked` 数量为 0；发生竞争时返回领域错误或在 timeout 内成功。
- 不新增秘密、原始 lease token、未脱敏路径或 Provider 内容泄漏。
- 现有任务恢复、Memory、Prompt Cache、安全和评测测试无回归。

性能数据必须记录但首版不以激进延迟目标阻塞正确性。建议观察门槛为：无竞争的 lease 获取与 guarded write 本地 p95 不高于 25 ms；若 CI 环境波动导致无法使用绝对值，则以改造前相同 SQLite 写入基线的相对增量报告。

## 11. 非目标

- 本阶段不迁移 PostgreSQL，不引入 SQLAlchemy，也不实现双写。
- 不实现远程任务队列、跨机器调度、Web 控制面或多 Agent 编排器。
- 不允许多个 writer 直接交错修改同一 workspace。
- 不使用共享网络文件系统承载 SQLite 多机写入。
- 不为提升吞吐量放宽 lease 校验或在冲突时静默 last-write-wins。
- 不把 lease token 当作用户凭据长期展示或写入普通日志。
- 不实现人工 `force unlock`，除非另有审计、确认和 fencing 设计。

## 12. PostgreSQL 迁移触发条件

满足以下任一硬条件时，新增 PostgreSQL Store 实现并重新运行同一 contract suite：

- worker 需要跨两台或更多机器共同领取任务；
- 需要集中式任务队列、管理控制台或跨 workspace 查询；
- SQLite 文件必须放在共享网络存储；
- 实测持续出现 busy timeout 或单 writer 吞吐成为主要瓶颈；
- 需要数据库级通知、行级并发、在线高可用或集中备份恢复。

迁移时保留本计划定义的 version、lease、generation 和 workspace ownership 语义。PostgreSQL 只替换持久化与原子操作实现，不重新定义 Runtime 并发规则。
