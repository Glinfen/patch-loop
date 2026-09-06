# PatchLoop 持久化 Session 与恢复闭环开发计划

## 1. 目标与范围

状态：SRF-01 已完成，当前起点为 **SRF-02**。SRF-00 固定了执行契约、旧数据输入和
旧行为故障基线；SRF-01 已完成领域模型、服务端口和 Fake Store，后续新增接口和命令仍
按编号任务实施。

本轮交付：创建 Session → 提交任务 → 运行中追加要求 → 暂停或等待审批 → 进程重启后恢复 → 完成修改、测试与报告。已确认的副作用不得因恢复再次执行，无法确认结果的动作必须停止自动重试。

PCR 已有 [验收记录](milestones/PCR_05_ACCEPTANCE.md)。本轮复用现有 Provider、Memory、Prompt Cache、工具和执行后端，只补齐 Session 闭环所需能力。新 Provider 协议、Skills、完整 TUI、自动 worktree 和 Git 审查功能由后续模块承担。试用使用显式选择的独立工作区。

与 [存储并发计划](STORAGE_CONCURRENCY_PLAN.md) 共用实现：SRF-00/01 固定契约，SRF-02 承接 SAR-01/02，SRF-03 承接 SAR-03/04/05，SRF-07 承接相关并发验收。这里的 Execution 指一次运行尝试，Session 指长期用户会话。

## 2. 编号开发任务

任务顺序为 SRF-00 → SRF-01 → SRF-02 → SRF-03 → SRF-04 → SRF-05 → SRF-06 → SRF-07。每项先完成对应测试和集成，再进入下一项。以下路径相对仓库根目录，新增模块名可在 SRF-00 调整并回写本文。

### SRF-00：固定执行契约与失败基线

**目标：** 在修改 Runtime 前，明确持久化边界和恢复规则，建立后续改造可以反复运行的基线。

**主要位置：** `runtime.py`、`persistence.py`、`tools/gateway.py`、`cli.py`（均位于 `src/patchloop/`）；`tests/e2e/test_checkpoint_resume.py`；新增 `tests/fixtures/session_legacy/` 和 `tests/support/session_faults.py`。

**开发步骤：**

1. 沿 `run → _execute → _execute_or_replay → gateway.execute → record_tool_call → save_checkpoint` 梳理实际读写顺序，列出 Task、工具结果、Memory、checkpoint、Trace 分别在哪次事务提交。特别标出“外部动作完成、工具结果尚未落库”的窗口。
2. 固定对象归属：Session 串行包含多个 Task；Turn 是用户/Agent 交互记录；Step 是一次模型决策及工具批次；Execution 是一次 run/resume；Effect 是一次工具动作。一个 Session 至多一个活动 Task，一个 Task 至多一个执行 owner。
3. 固定状态维度：Session 为 open/closed；Task 业务结果使用 active/completed/failed/cancelled，运行条件单独使用 idle/running/pausing/paused/waiting_for_approval/recovery_required/ended；Effect 使用 prepared、waiting_for_approval、executing、succeeded、failed、denied、unknown、cancelled。完成/失败/取消的 Task 不重新变为 active，业务终态可与 recovery_required 同时存在。
4. 固定控制规则：pause/cancel 先作为持久化 ControlRequest，经 requested/acknowledged/settled 或 cleanup_failed 闭环；complete/cancel 业务结果按事务先后决定。普通 resume 不能扩大授权，也不能重跑 unknown 动作；离开 recovery_required 必须提交核验结果、创建显式重试或放弃的 RecoveryDisposition。把允许和禁止的转换写成参数化测试数据，避免实现时各模块自行解释。
5. 从当前实现生成并固定最小旧数据 fixture：一个完成任务、一个带未完成计划的 running 任务、已确认写入结果、checkpoint、Memory/Cache 快照、Trace，以及包含旧 runtime/memory 表和代表性行的 SQLite 数据库。记录来源 commit、SHA-256、关键 ID、表行数和摘要，后续迁移测试禁止用新 schema 重建“旧数据”。
6. 建立可由父进程控制、且经过真实 AgentRuntime/Gateway/Store 调用链的故障屏障：旧路径工具派发后、动作开始前、外部动作完成后、结果提交后、checkpoint 提交前。旧路径没有持久化意图，不把测试审计点命名为意图提交；SRF-04 再加入 prepared/executing 的真实提交屏障。使用会向独立审计文件追加记录的非幂等测试工具复现崩溃窗口，不只依靠 KeyboardInterrupt 或数据库结果行数判断动作是否发生。
7. 运行现有质量门禁，记录实际基线与跳过原因；故障复现先作为旧行为特征化测试，SRF-04 再改为安全恢复回归断言，不把旧行为复现成功算作恢复能力通过。

**交付物：** 回写本文的契约决策、旧数据 fixture、故障屏障、旧行为基线及质量检查结果。

**验收：** 五个旧路径故障位置各至少稳定命中一次，外部动作完成而结果缺失的位置重复 3 次；能够证明外部动作发生但数据库结果缺失；JSON 与真实旧 SQLite fixture 均可由当前版本读取且校验摘要一致；后续各任务的状态、事务和恢复判断没有未定的基础分歧。

### SRF-01：实现领域模型与服务端口

**依赖：** SRF-00。

**主要位置：** 修改 `src/patchloop/domain.py`、`persistence.py`；新增 `session/models.py`、`execution/models.py`、`persistence_contracts.py`（位于 `src/patchloop/`）；新增 `tests/unit/test_session_models.py`、`test_execution_models.py`。

**开发步骤：**

1. 实现下表模型。业务 schema version、并发 version、事件 sequence 分开使用；Provider call ID 不作为唯一执行身份。

   | 模型 | 必需字段与约束 |
   | --- | --- |
   | Session | ID、workspace 引用、open/closed、活动 Task 引用、配置版本、创建/更新时间 |
   | Task 扩展 | Session 引用、业务 outcome、独立 runtime condition、并发 version；旧 status 仅作兼容投影 |
   | Turn | ID、Session/可空 Task 引用、role、内容/资源引用、sequence、客户端提交 ID |
   | Execution | ID、Session/Task 引用、owner、token、generation、租约到期时间及运行状态 |
   | Effect | ID、Task/Step 引用、批次位置、Provider call ID、`retry_of_effect_id`、工具/参数摘要、状态、审批与结果引用 |
   | Approval | ID、Effect 引用、动作与资源摘要、policy/config version、状态、决定来源/时间 |
   | ControlRequest | ID、Task/Execution 引用、pause/cancel、状态、请求/确认/清理信息 |
   | RecoveryDisposition | ID、unknown Effect 引用、confirm_result/create_retry/abandon、证据与决定来源 |
   | Checkpoint 扩展 | schema version、Session/Turn 引用、已消费输入序号、事件水位、待处理 Effect；保留原计划/Memory/Cache 字段 |

2. 为 Task 增加兼容的 Session 关联、业务 outcome 和独立 runtime condition，实现显式转换校验；旧 `Task.status` 保留为迁移/旧 API 投影。关闭 Session 时仍有活动 Task 必须报错。新请求重做工作创建关联的新 Task，不修改历史终态。
3. 定义独立的动作身份校验：同一 Effect ID/批次位置重复写入相同内容为幂等；内容不同返回冲突；不同 Step 可以合法调用参数相同的工具。
4. 定义服务端口：Session 的 create/list/get/append_message/start_task/request_pause/request_cancel/close；Runtime 的 `advance(execution)`；Effect 的 prepare/execute/reconcile。`advance` 明确返回 progressed、waiting、paused、recovery_required 或终态，CLI 无须解析异常文本猜测状态。
5. 定义 Store 原子操作及返回值：append turn、claim execution、prepare effects、decide approval、claim effect、commit effect、request/settle control、resolve recovery、commit checkpoint；写方法携带 expected version 和适用的 lease guard。接口不暴露 SQL 或连接对象。
6. 定义 `StaleVersion`、`LeaseConflict`、`LeaseLost`、`ApprovalConflict`、`EffectIdentityConflict`、`RecoveryRequired` 等结构化错误；实现最小 Fake Store，供服务层测试使用。保留现有 `SQLiteStore` 和 `RuntimeCheckpoint` 导入路径。

**交付物：** 可序列化领域模型、状态转换实现、服务/存储 Protocol、Fake Store 和模型测试；本任务不接管生产执行入口。

**验收：** 旧 Task/checkpoint 反序列化通过；非法状态转换被拒绝；重复输入/Effect 身份冲突有明确结果；新模型不依赖 SQLite、CLI 或 Provider 具体实现；类型检查通过。

### SRF-02：实现 Session 存储、事件与旧数据迁移

**依赖：** SRF-01。

**主要位置：** `src/patchloop/persistence.py`、`persistence_contracts.py`、`events.py`、`memory/store.py`；新增 `tests/unit/test_session_store.py`、`tests/integration/test_session_migration.py`、`test_session_events.py`。

**开发步骤：**

1. 将 SQLite 连接初始化集中到同一入口，保留 WAL、foreign keys、10 秒 busy timeout，并读取实际配置验证。增加 Runtime schema migration，与现有 Memory migration 按固定顺序执行；未来版本或不完整 schema 拒绝启动。
2. 增加 sessions、turns、executions、effects、approvals、control_requests、recovery_dispositions、session_events 及 workspace_leases 等必要表，给已有 Task 增加并发 version、业务 outcome、runtime condition 和关联信息。建立外键以及 `(session_id, sequence)`、客户端提交 ID、Effect 批次位置、审批决定和恢复处置的唯一约束。
3. 实现 Session 创建/列表/查询/关闭、Turn 追加/分页读取。Turn 内容与序号在同一事务分配；重复提交返回原记录；同一客户端 ID 提交不同内容报冲突。Task 创建与绑定 Session 活动槽原子化。
4. 将无条件 Task upsert 拆成创建与条件更新；checkpoint、工具结果、审批和事件提供领域化提交方法。`commit_effect` 在一个事务内写入结果、可回放观察、恢复游标与事件，不能由调用者拼接几个独立 Store 方法模拟原子提交。
5. 将关键状态事件写入数据库 journal，JSONL 改为可恢复导出。为事件保留稳定 ID、Session sequence 和旧 task/trace ID；导出进程中断后检查已写 ID、修复不完整尾行并补导，保证最终文件不重复、不漏事件。
6. 从不可变的 `runtime-v0.sqlite` 实现旧数据迁移：旧 Task 一对一映射 legacy Session，保留 Task ID、工具结果、artifact 和 Memory 关联；旧终态映射业务 outcome，旧 created/running 映射 active outcome 和相应 runtime condition；已确认结果映射终态 Effect。缺少执行意图的历史 running 任务标记需恢复核对，不能直接推断未记录的动作没有发生。
7. 迁移前停止 writer，使用 SQLite 一致性备份；每次迁移失败回滚。回退流程验证为停止执行、保留升级后数据、恢复备份及对应程序版本，禁止旧程序直接写新 schema。
8. 对 checkpoint 做版本适配，不把原始 Python 对象或 Provider 专属消息当作 Session 唯一事实源。持久内容继续脱敏；需重放的秘密使用凭据引用，脱敏占位符不得变成真实工具参数。

**交付物：** SQLite Store 实现、迁移/兼容适配器、事务事件与 JSONL 导出；SRF-03 接入前，不启用新的并发执行入口。

**测试与验收：**

- 旧 fixture 升级后 ID、工具结果、计划、Memory/Cache 关键字段一致；重复升级不增加 Session/Effect。
- 在迁移和 `commit_effect` 中途注入异常，事务完全回滚，不留下半结果或孤立事件。
- 两个真实连接同时提交同一消息，只生成一条记录；不同消息序号唯一且有序。
- stale version 更新失败；未来 schema 拒绝；Trace 中断补导后事件 ID/顺序与 journal 一致。
- SQLite 与 Fake Store 运行同一组适用的接口契约测试，事务语义必须另用真实 SQLite 验证。

### SRF-03：实现执行租约、Workspace 独占与进程清理

**依赖：** SRF-02。

**主要位置：** 新增 `src/patchloop/execution/ownership.py`；修改 `persistence.py`、`memory/store.py`、`sandbox.py`、`tools/gateway.py`、`runtime.py`、`cli.py`；新增 `tests/integration/test_execution_ownership.py`、`tests/e2e/test_workspace_ownership.py`。

**开发步骤：**

1. 实现 Session 执行槽、Task lease 和 Workspace writer。固定获取顺序为 Session → Task → Workspace，失败按逆序释放。规范化真实路径生成 workspace ID，同一路径别名不能获得第二个 writer。
2. 实现 acquire/renew/release/assert：token 不复用，接管时 generation 递增；续约和释放必须同时匹配 owner/token/generation。时间、UUID、TTL 可注入，初始沿用 SAR 建议的 60 秒 TTL、20 秒 heartbeat。
3. 将 Task、Step、Effect、结果、checkpoint、artifact 元数据和 Memory 批次等执行写入接入 guard。guard 检查与写入同事务完成；cancel 先提交 ControlRequest，清理完成或明确进入 unknown/recovery_required 后再提交业务终态并使旧 owner 失效。
4. 为 run/resume 增加共同的 Execution 生命周期封装，模型等待和工具运行期间持续续约。获取失败不得发起 Provider 或工具调用；续约失败阻止新动作，旧 owner 不再写正常 checkpoint 或完成状态。
5. 为 Workspace 增加跨进程独占机制，覆盖整个写执行期。WRITE/EXECUTE 工具必须同时持有 Task lease 与 Workspace writer；只读执行仍需 Task/Session 所有权。锁文件本身是否存在不能作为 owner 存活证据。
6. 扩展现有 Sandbox 命令生命周期，使 Runtime 可以终止并确认受管进程树/容器退出。持久化可核对的执行身份，避免只凭可复用 PID 杀进程；支持 timeout、pause、cancel、lease lost 和父进程崩溃后的清理。
7. 实现恢复接管检查：即使数据库 lease 已过期，旧进程或子进程尚未确认停止时也不能开放新写入。清理失败进入 recovery_required；正常暂停/等待审批先清理再释放资源。具体共同约束见第 5.4 节。
8. 增加 lease acquired/contended/renew_failed/lost/released/takeover 事件，输出安全 owner 摘要、generation 与恢复建议，不输出 token。

**交付物：** 所有权管理器、带 guard 的存储/工具入口、可清理的执行后端、租约事件与多进程测试。现有 run/resume 同步接入，不能留下无 guard 的兼容写入口。

**测试与验收：**

- 两进程同时运行同一 Session、同一 Task 或写同一 Workspace，均只有一个成功；不同 Workspace 不被全局锁串行化。
- 旧 version/token/generation 写 Task、checkpoint、结果、Memory 均失败；旧 owner 不能释放新 owner 的锁。
- 长模型等待与长命令期间 heartbeat 有效；停止 heartbeat 后可以进入接管检查。
- 暂停旧 worker 再恢复、强制结束父进程但保留子进程时，验证新 owner 不会与旧 writer 重叠执行。
- complete/cancel 两种提交次序均符合约定；清理失败不得报告已安全暂停。大规模竞争轮次由 SRF-07 收口。

### SRF-04：实现 Effect 提交、持久化审批与副作用恢复

**依赖：** SRF-03。

**主要位置：** 新增 `src/patchloop/execution/effects.py`、`approvals.py`；修改 `runtime.py::_execute_or_replay`、`tools/gateway.py`、`tools/write.py`、`security.py`、`changes.py`；新增 `tests/e2e/test_effect_recovery.py`、`test_approval_recovery.py`。

**开发步骤：**

1. 在执行任何工具前，持久化完整模型响应、工具批次顺序、用量与稳定 Effect ID。标识由 Task/Step/批次位置关联，恢复时读取原批次，不重新生成 ID 或重问模型以“找回”待执行调用。
2. 实现 `prepare`：校验工具、参数、路径和计划要求，保存规范化输入摘要、动作类别、文件前置条件及 Policy 结果。文件修改前将原始内容/不存在标记纳入受保护的恢复数据，避免动作完成后崩溃导致 diff 丢失修改前基线。
3. 将 `ToolPolicy.assess` 改为 allow/deny/require_approval 三态。硬性路径/工具权限拒绝不能通过批准绕过；require_approval 原子创建 Approval 并使 Task 等待，返回控制权，不把等待记作工具失败。
4. 实现 approve-once、deny 和精确预授权匹配。决定绑定 Effect、参数摘要、workspace 与 policy/config version；重复相同决定幂等，矛盾决定报冲突。执行条件变更后旧批准失效；非交互无匹配授权仍等待。
5. 实现 `execute`：调用前重新检查最新输入、资源条件和所有权；同一事务将 Effect 认领为 executing 并消费一次性授权。事务提交后才调用后端；执行结束用 `commit_effect` 原子保存结果、观察、游标与事件。一次性授权不能转给失败后的新 Effect。
6. 实现 `reconcile`，按下表处理崩溃记录。文件工具使用前置/目标内容摘要和执行凭据核对；无法证明完成时保持 unknown。测试/通用命令可能有副作用，不能按“只读”自动重跑。

   | 持久状态 | 恢复处理 |
   | --- | --- |
   | prepared | 重新检查输入、授权、文件基线后执行 |
   | waiting_for_approval | 展示原请求；未决定不执行 |
   | executing 且缺少结果 | 核对执行身份、凭据和外部状态；不能确认则 unknown |
   | succeeded/failed/denied | 回放结果并推进观察，不再次调用工具 |
   | unknown | runtime condition 进入 recovery_required，普通 resume 不重试；Task 可已有 cancelled 业务结果 |

7. 拆开“已确认失败”与“执行可能已产生部分副作用但结果不明”：后者进入 unknown。拒绝则生成结构化工具观察，让 Agent 选替代方案；准备中的动作因新用户约束被取消时也要结束原调用关联，不能留下不成对的 Provider 工具消息。
8. 提供恢复处置 API：以 RecoveryDisposition 核验并补认已有结果、放弃任务、显式请求新的受审批重试动作。保留旧 Effect 和处置证据；重试创建带 `retry_of_effect_id` 的新 Effect 并进入 waiting_for_approval，不把 unknown 直接改回 prepared，也不通过重复 approve 隐式重试。

**交付物：** EffectExecutor、Approval Store/服务、三态 Policy、文件恢复凭据和旧 Gateway 适配；通用命令不承诺跨系统 exactly-once。

**测试与验收：**

- 使用 SRF-00 屏障分别终止在 prepared、executing、外部动作完成、结果提交后；独立审计证明已确认动作没有重复执行。
- 多工具批次在第二个动作前中断：第一个只回放，剩余调用身份和顺序不变；旧 checkpoint 能从已提交结果补齐。
- 审批等待后重启仍是同一请求；并发批准只能领取一次；批准后换参数/路径/配置无法执行原授权。
- 文件部分写入、用户中途改文件、命令结果未知均不会盲目重跑；写入后崩溃仍能恢复修改前基线。
- 未授权执行、unknown 自动重试均为 0；日志与数据库凭据泄漏为 0。

### SRF-05：接入多轮 Session 与可推进 Runtime

**依赖：** SRF-04。

**主要位置：** 新增 `src/patchloop/session/service.py`、`execution/driver.py`；修改 `runtime.py`、`persistence.py::RuntimeCheckpoint`，复用现有 MemoryManager/PromptCacheCoordinator；新增 `tests/integration/test_session_runtime.py`、`tests/e2e/test_session_recovery.py`。

**开发步骤：**

1. 从 `AgentRuntime._execute` 提取可推进边界：处理控制输入、准备模型请求、接收并持久化响应、推进一个 Effect、提交 checkpoint、形成终态。`advance` 到达等待/暂停/恢复障碍时立即返回；`run/resume` 保留为驱动循环兼容入口。
2. 实现 SessionService：create/list/get、append_message、start_task、request_pause/request_cancel、resume、close。用户消息先持久化再确认；活动 Task 接收补充消息，无活动 Task 时只保存对话，由 start 明确创建新目标。
3. 在模型调用前、返回后、每个副作用认领前和恢复后检查输入与控制。将读取到的输入 revision 纳入 Effect 认领的条件提交：在认领前已提交的新约束必须先处理；动作认领后的输入在下一边界处理，不能承诺撤回已开始的动作。
4. 为模型请求记录已消费输入水位。返回时发现新约束，先保存响应和用量，再将未执行的陈旧动作作废并重新规划。补齐原工具调用的结构化取消观察，保证后续 Provider 消息配对合法。
5. 实现 pause/cancel/resume：ControlRequest 先落库；暂停完成清理后才 settled 并进入 paused；取消在清理完成或未决 Effect 已明确进入 unknown/recovery_required 后提交业务终态。恢复先取得新 Execution，再核对所有权、待审批/unknown Effect、RecoveryDisposition 和文件前置条件，最后决定是否继续。
6. 扩展 checkpoint 保存输入水位、事件水位和待处理批次；恢复时重放 checkpoint 之后的已提交观察。Memory 按事件 ID 幂等消费，Prompt Cache 走已有快照适配，禁止向缓存前缀随意插入新用户消息。
7. 保留 Task 的计划、工具历史、变更基线和预算累计。已落库模型响应的用量不能因重放重复累计；收到响应但未落库即崩溃的远端用量标记为未知，不伪造精确费用。明确暂停等待时间不消耗活动执行时间预算。
8. 同一 Session 启动第二个 Task 时保留对话历史和显式约束，按已有 Context 预算选取内容；新建计划、预算、Effect 与一次性授权边界，不能复制前一任务终态。持久精确预授权仅在范围仍匹配时生效。

**交付物：** SessionService、RuntimeDriver、版本化 checkpoint、旧 Runtime 入口适配、多轮和恢复集成测试。

**测试与验收：**

- Provider 阻塞期间追加消息，恢复响应后旧写动作不越过约束；重复提交消息不重复生成逻辑 Turn。
- 两轮输入、多工具执行、暂停、重启、继续完成后，计划/观察/预算/Memory/Cache 与不中断对照一致。
- 拒绝审批后 Agent 可执行安全替代方案；普通 resume 遇到 unknown 返回 recovery_required。
- Session 第二个 Task 可正常运行且不继承已消费的一次性授权；关闭活动 Session 被拒绝，关闭后只读。
- 同步 Provider 不能即时取消时，先报告控制请求已接收，超时或返回后禁止陈旧副作用；不得提前报告清理完成。

### SRF-06：实现 Session CLI、审批与恢复操作

**依赖：** SRF-05。

**主要位置：** `src/patchloop/cli.py`；扩展 `tests/integration/test_cli.py`，新增 `tests/integration/test_session_cli.py` 和双终端进程测试；更新现有 README 的快速开始部分。

**开发步骤：**

1. 新增 `session`、`approval` 命令组，所有命令支持 `--repo`，从当前目录或指定仓库解析 workspace-local 数据库。命令只调用服务层，不能直接修改 SQLite 执行状态。
2. 实现以下命令行为；create/enter 不自动执行未知动作，start/resume 才获取 Execution。

   | 命令 | 行为 |
   | --- | --- |
   | session create/list/show | 创建、列出、显示对话/活动目标/计划/审批与恢复状态 |
   | session enter | 进入轻量交互循环，复用下面的服务命令；退出界面不自动取消任务 |
   | session start/send | 创建目标并运行；向活动任务追加消息，返回持久化 Turn ID/序号 |
   | session pause/cancel/resume/close | 分别请求暂停、最终取消、核对后恢复、关闭已无活动任务的会话 |
   | approval list/decide | 展示原动作与范围，批准一次或拒绝；决定本身不隐式启动新的执行者 |
   | session recover | 查看 unknown 动作证据，并调用 SRF-04 的核验/放弃/显式重试处置 API |

3. 保留简单阻塞式交互；执行期间用户可在第二终端 send/pause/approve。`enter` 的正常退出与 Ctrl+C 行为明确区分；运行中 Ctrl+C 提交 pause 请求并等待/显示清理状态，硬终止仍走崩溃恢复。
4. 输出模型/工具边界、计划变化、审批等待与测试结果，首版不要求逐 Token 流式渲染。复用现有 diff/status/replay 能力，显示当前工作区与 Execution 所有权，提供可复制的下一步命令。
5. 固定 JSON 字段：schema version、Session/Task/Execution ID、状态、最新 sequence、待审批/恢复项目、错误类别、下一步命令。固定退出码，区分完成、等待审批、暂停、冲突、需恢复处置和执行失败；JSON 模式不混入提示文本。
6. 将旧 run/resume/cancel/status 转到共同服务或查询投影；去除 `_approval_handler(non_interactive)` 的自动 True 语义。CI 通过显式精确授权运行，缺少授权返回请求 ID 和等待状态。
7. 让 `session recover` 在显式重试前展示原动作结果未知和可能重复的事实；交互确认或专用显式参数才创建新 Effect。普通 resume/approve 不能等价于该授权。
8. 更新现有快速开始：准备独立仓库、创建/启动、第二终端补充约束、审批、重启恢复、查看 diff/报告；说明旧非交互行为变化和旧库升级/回退步骤。

**交付物：** 可用 CLI、稳定 JSON/退出码、旧入口迁移、双终端自动化脚本及现有快速开始更新。

**测试与验收：**

- 使用 Fake Provider 和临时 SQLite，通过 CLI 完成创建、执行、补充输入、暂停、重启、审批、完成全流程，不手工改库。
- 两个真实 CLI 进程验证运行时可以追加消息/控制，竞争 resume 明确报冲突。
- 旧命令无法绕过 guard/Approval；无预授权的非交互写动作执行次数为 0。
- 重复 send/approve、错误仓库、会话不存在、已关闭、unknown 未处置均有稳定机器结果和可操作提示。
- 按快速开始操作，无须查询 SQLite 或原始 JSONL 即可完成恢复。

### SRF-07：完成故障矩阵、真实试用与回归收口

**依赖：** SRF-06；复用 SRF-00～06 的 fixture 和测试，不另建一套重复框架。

**主要位置：** Session/Effect/Approval/Workspace 的集成和端到端测试；现有 `benchmarks/`、评测报告入口；在本文记录最终任务状态与证据位置。

**开发步骤：**

1. 将各任务故障用例整理为机器可读矩阵，每个用例记录故障点、后端、预期状态、独立动作次数、恢复结果及事件完整性。测试父进程等待屏障后终止子进程，不用固定 sleep 猜测窗口。
2. 每个适用崩溃场景重复至少 3 次，覆盖消息提交/消费、模型响应、工具批次、执行意图、外部动作、结果/checkpoint/Memory 提交以及 Trace 导出。保留全部失败，不能只报告最佳一次。
3. 对同一 Session、同一 Task、同一 Workspace 分别做 8 个进程、100 轮竞争；每轮在全部竞争者返回前保持胜者所有权，确保“恰好一个成功”不被先释放后重获干扰。额外验证释放后可重新获取、不同 Workspace 能并行。
4. 在目标 Windows 主机及支持的 Docker 后端执行进程树清理、旧 worker 恢复、lease 接管、用户修改文件测试。不可运行的关键用例标记未验收，不用 skip 代表通过，也不扩大 Sandbox 安全承诺。
5. 汇总重复执行、unknown 自动重试、未授权动作、stale 提交、审批决定与事件缺失。用独立审计结果交叉核对数据库、Trace 和最终文件；扫描原始 token/凭据泄漏。
6. 锁定至少 1 个真实 Python 仓库的 revision、依赖、Issue 和验证命令，用真实 Provider 在 3 个独立干净工作区完成流程，均包含追加约束、审批和重启。验证公开测试与独立断言，记录 diff、用量、时延及所有失败。
7. 区分自动恢复成功、正确停止并等待人工处置、真正恢复失败；远端未知用量单独报告。无真实 Provider 凭据时保留“真实试用未验收”，不能用 Fake 替代后声明全部完成。
8. 运行第 3 节质量门禁，回归旧 Task/checkpoint/Trace、Memory/Cache 和旧 CLI；记录环境、命令、通过/失败/跳过及报告位置，再更新本文与第二阶段总计划的实际状态。

**交付物：** 故障/竞争矩阵机器结果、真实试用报告、完整回归结果、更新后的实际完成状态。

**验收：** 第 3 节全部满足；如果关键后端测试或真实试用未完成，明确保留未完成项，不以代码合并或单元测试通过代替闭环验收。

## 3. 本轮总体验收

- 已确认 Effect 因恢复重复执行为 0；unknown 自动重试为 0；未授权高风险执行为 0；stale owner 成功提交为 0。
- 固定崩溃场景每项至少 3 次符合预期；3 类所有权竞争分别 8 进程 × 100 轮，每轮恰好一个 owner。
- 对话、计划、工具批次、变更基线、预算及 Memory/Cache 可恢复；待审批请求和决定跨进程保持一致。
- 旧数据无损迁移且可回退；状态与关键事件事务一致，JSONL 可补导；凭据/原始 lease token 泄漏为 0。
- CLI 全流程、第二终端控制和旧入口迁移测试通过；真实仓库与真实 Provider 的 3 次独立试用完成并如实报告全部结果。

实现阶段运行：

```text
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
```

上述是待执行门禁，不是本次文档修改的测试结果。第二阶段完整的 30 任务/5 仓库评测、跨 Provider、Skills 与 Git 工作流仍按 [项目目标](PHASE_2_PROJECT_GOALS.md) 验收。

## 4. 提交顺序与工作量

首个提交只完成 SRF-00：契约、历史 fixture、故障基线和当前质量检查。后续每个编号可拆为模型/实现/集成提交，但完成状态按该编号的全部验收标准判定，不能只完成接口就标记结束。

SRF-03/04 首先以一个文件写工具完成 `prepare → claim → execute → commit/reconcile`、持久化审批和 owner guard 的端到端垂直切片，再扩展到命令、多工具批次和完整并发矩阵；不得先分别大范围改写 Runtime/Gateway 后才联调。

修订粗估：SRF-00～01 为 3～5 天，SRF-02～03 为 7～11 天，SRF-04～05 为 8～13 天，SRF-06～07 为 6～9 天，共 24～38 个开发日。进程树清理、旧库迁移或真实 Provider 试用未通过时继续修订，不用最初估算压缩验收。

## 5. 跨任务约束

### 5.1 事实来源与兼容

SQLite 保存权威状态和事件；checkpoint 是版本化快照，JSONL/CLI 是投影。保留原 Task ID、公开导入和旧数据读取路径；不允许旧执行入口绕过新控制面。

### 5.2 事务与恢复

事务内不调用 Provider、工具或文件系统。数据库提交不能证明外部动作一定未发生；已确认结果只回放，未知结果经 reconcile 核对或停止，不宣称通用命令具有全局 exactly-once 保证。

### 5.3 授权与输入

工具可用权限、具体动作审批和恢复重试授权分别判断。新输入的生效点以 Effect 认领事务为边界；普通 resume 不扩大权限，不隐式批准未知动作重试。

### 5.4 所有权与外部进程

数据库 fencing 只阻止旧持久化提交，Workspace 独占与进程清理负责阻止旧外部 writer。lease 过期后，须确认旧 writer 及受管进程树停止才允许新写入；不能确认则将 runtime condition 置为 recovery_required。暂停、等待审批和退出先清理再释放；ControlRequest 清理失败进入 cleanup_failed，不得报告安全接管。锁不约束用户编辑器，恢复还须核对文件前置条件。具体实现与测试由 SRF-03、SRF-04、SRF-07 负责。

## 6. SRF-00 实际交付记录

### 6.1 契约决策

契约已写入 [ADR-020](adr/ADR-020-session-runtime-foundation-contract.md)，固定了
Session/Task/Turn/Step/Execution/Effect/ControlRequest/RecoveryDisposition 的对象归属、
Task 业务结果与运行条件分离、Effect 状态、暂停与取消规则、完成与取消竞态规则，以及
`unknown` 不得由普通 resume 重试的恢复规则。

当前读写顺序的证据来自 `runtime.py`、`tools/gateway.py` 和 `persistence.py`：Gateway
先调用外部工具，Runtime 再独立保存 `tool_calls`，批次结束后再保存 Step、Memory 和
checkpoint。外部动作完成而结果未落库的窗口已明确记录为旧行为，不把 Trace 或数据库
缺行解释成动作未发生。

### 6.2 旧数据 fixture

固定输入位于 `tests/fixtures/session_legacy/`，由
`tests/e2e/test_session_runtime_baseline.py::test_session_legacy_fixture_is_readable_by_current_models`
读取。fixture 保留一个完成 Task、一个带未完成计划的 running Task、确认写入结果、
checkpoint、Memory/Cache snapshot 和 Trace；`runtime-v0.sqlite` 冻结旧 runtime/memory 表与
代表性数据，`manifest.json` 固定来源 commit、SHA-256 和表行数。数据库 fixture 由当前
版本直接读取验证，后续迁移测试必须复制该文件后升级。

### 6.3 故障屏障与旧行为基线

`tests/support/session_faults.py` 通过真实 `AgentRuntime → ToolGateway → Tool → SQLiteStore`
路径提供父进程可控的五个屏障，并把屏障和非幂等外部动作审计追加到独立 JSONL 后
`fsync`。五个位置各至少命中一次；外部动作完成后、结果提交前额外重复 3 次，均证明审计
文件和目标文件显示动作已发生，而 SQLite 没有对应工具结果。该测试是 SRF-04 安全恢复
回归的输入，不是恢复成功声明。

### 6.4 质量门禁记录

SRF-00 实施前的当前基线：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（110 files already formatted） |
| `mypy src` | 通过（63 source files） |
| `pytest -q` | 通过：223 passed, 1 skipped in 187.89s；跳过原因为 Windows 主机不支持 symbolic links |

SRF-00 实施后的同一组门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（118 files already formatted） |
| `mypy src` | 通过（63 source files） |
| `pytest -q` | 通过：242 passed, 1 skipped in 172.14s；跳过原因为 Windows 主机不支持 symbolic links |

SRF-00 契约修订后的同一组门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（118 files already formatted） |
| `mypy src` | 通过（63 source files） |
| `pytest -q` | 通过：262 passed, 1 skipped in 171.21s；跳过原因为 Windows 主机不支持 symbolic links |

SRF-00 原有 19 项测试与本次 20 项契约/旧库/真实调用链测试共 39 项全部通过。五个旧路径
故障屏障各命中一次，其中外部动作完成后崩溃窗口额外重复 3 次。完整门禁未发现回归；唯一
跳过项仍是 Windows 主机不支持 symbolic links。

## 7. SRF-01 实际交付记录

### 7.1 领域模型

新增 `src/patchloop/session/models.py` 和 `src/patchloop/execution/models.py`，提供可序列化
的 Session、Turn、SessionCheckpoint、Execution、Effect、Approval、ControlRequest 和
RecoveryDisposition。`src/patchloop/domain.py` 为旧 Task 增加 Session 关联、业务 outcome、
runtime condition 和并发 version；旧 `Task.status` 仍可反序列化并作为兼容投影。

业务结果与运行条件分离：终态 Task 不会回到 active，`recovery_required` 不能通过普通状态
转换离开；Effect 的身份由 Task/Step/批次位置及动作内容决定，Provider call ID 不作为唯一
执行身份。

### 7.2 服务端口、错误与 Fake Store

新增 `src/patchloop/persistence_contracts.py`，定义 Session/Runtime Store Protocol、
Runtime/Effect 端口、AdvanceResult、LeaseGuard，以及 `StaleVersion`、`LeaseConflict`、
`LeaseLost`、`ApprovalConflict`、`EffectIdentityConflict`、`RecoveryRequired` 等结构化
错误。Fake Store 覆盖输入幂等、版本校验、活动 Task 槽、Execution claim、Effect prepare/
claim/commit、审批、控制请求、恢复处置和 checkpoint 提交；不导入 SQLite、CLI 或具体
Provider，实现未接管现有生产执行入口。

### 7.3 验收证据

模型和 Store 测试位于 `tests/unit/test_session_models.py`、
`tests/unit/test_execution_models.py` 和 `tests/unit/test_persistence_contracts.py`；相关
定向测试 23 项通过。全量 `pytest -q` 在加入 SRF-01 后为 279 passed、1 skipped（跳过项
仍是 Windows 不支持 symbolic links），固定检索报告同步更新为当前源码候选路径；Week 05
检索快照由 `tests/unit/test_intelligence.py` 验证可复现。
