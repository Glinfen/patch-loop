# PatchLoop 持久化 Session 与恢复闭环开发计划

## 1. 目标与范围

状态：SRF-00～SRF-06 已完成；SRF-07 的实现和本机质量门禁已完成，但整体验收仍为
**未验收**：当前主机没有可用 Docker 后端；真实 Provider 凭据和 `deepseek-flash` API 已验证可用，
模型兼容适配后完成了三次 Session 试用，但均在固定 20 步预算内未收敛。SRF-00 固定了执行契约、旧数据
输入和旧行为故障基线；SRF-01 完成领域模型、服务端口和 Fake Store；SRF-02 完成 Session
存储、事件与旧数据迁移；SRF-03 已将执行租约、Workspace 独占、guard、heartbeat 和受管
进程清理接入现有 `run/resume`；SRF-04 完成 Effect 的稳定身份、持久化审批、原子执行提交、
副作用核对和显式恢复处置；SRF-05 完成 SessionService、可推进 Runtime、多轮输入、控制、
checkpoint 恢复和跨 Task 上下文继承；SRF-06 完成 Session CLI、持久化审批与恢复操作、
旧入口迁移、稳定机器输出及快速开始文档。后续故障矩阵与真实试用按 SRF-07 实施。

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

**状态：** 已完成，提交 `60a5e95`。

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

**状态：** 已完成，提交 `4de3f69`。

**主要位置：** 新增 `src/patchloop/session/service.py`、`execution/driver.py`；修改 `runtime.py`、`persistence.py::RuntimeCheckpoint`，复用现有 MemoryManager/PromptCacheCoordinator；新增 `tests/integration/test_session_runtime.py`，扩展现有 checkpoint、Memory 和受管命令恢复测试。

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

**状态：** 已完成，提交 `0c2c2e9`。

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

**状态：** 开发完成，本机可执行门禁通过；真实 Docker 后端和真实 Provider 三次试用未验收。

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

上述命令仍是 SRF-07 的最终门禁要求；SRF-06 的实际结果记录在第 12 节。第二阶段完整的
30 任务/5 仓库评测、跨 Provider、Skills 与 Git 工作流仍按
[项目目标](PHASE_2_PROJECT_GOALS.md) 验收。

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

## 8. SRF-02 实际交付记录

### 8.1 Session Schema 与迁移

提交 `26b542b` 完成 Runtime schema 的统一初始化和版本迁移，新增 sessions、turns、
executions、effects、approvals、control_requests、recovery_dispositions、session_events、
workspace_leases 等表及必要索引、唯一约束和外键。Runtime 与 Memory schema 通过同一 SQLite
连接入口初始化，保留 WAL、foreign keys 和 busy timeout 配置；未来版本、不完整 schema 和
迁移中断均拒绝继续写入。

旧 `runtime-v0.sqlite` 迁移会先建立一致性备份，再将历史 Task、工具结果、checkpoint、
Memory、Cache 和 Trace 映射到 Session 模型。缺少执行意图的历史 running Task 保持
`recovery_required`，不会据此推断外部动作未发生。备份恢复、重复升级和迁移故障回滚均由
真实 SQLite 测试覆盖。

### 8.2 事务存储与事件

SQLiteStore 和 FakeStore 已实现 Session、Turn、Task、Effect、Approval、ControlRequest、
RecoveryDisposition 和 SessionCheckpoint 的领域化操作。Turn sequence、客户端提交 ID、
Effect 身份及审批决定具备幂等或冲突语义；`commit_effect` 在同一事务提交结果、观察、恢复
游标和 journal 事件。

数据库 journal 是 Session 状态事件的权威来源。JSONL exporter 支持中断续导、不完整尾行
修复、重复或乱序投影重写。持久化内容统一经过脱敏，凭据以引用形式保存，不把脱敏占位符
还原为工具参数。

### 8.3 验收证据

主要测试位于 `tests/unit/test_session_store.py`、
`tests/unit/test_session_store_transactions.py`、`tests/integration/test_session_migration.py`、
`tests/integration/test_session_events.py` 和 `tests/integration/test_session_backup.py`。SRF-02 的
全部实现已由后续 SRF-03 全量回归继续覆盖。

## 9. SRF-03 实际交付记录

### 9.1 执行所有权与写入 Fencing

提交 `adfc2b1` 新增 `src/patchloop/execution/ownership.py`，按 Session → Task → Workspace
顺序获取执行所有权，并在失败时逆序释放。Task lease 和 Workspace writer 使用不可复用 token
与递增 generation；acquire、renew、release、assert 均校验 owner、token 和 generation。
Workspace ID 由规范化真实路径生成，同一路径别名不能获得第二个 writer，不同 Workspace
之间不建立全局串行锁。

Task、Step、工具结果、checkpoint、artifact 元数据、Memory 批次和 WRITE/EXECUTE 工具入口
已经接入 lease guard；guard 校验与数据库写入处于同一事务。写工具还必须持有 Workspace
writer。旧 owner 在租约失效或接管后不能提交正常状态、结果和 checkpoint，也不能释放新
owner 的所有权。

### 9.2 Runtime、Heartbeat 与受管命令

现有 `run/resume` 已统一进入 Execution 生命周期，在 Provider 等待和工具运行期间由 heartbeat
续约。获取所有权失败时不会调用 Provider 或工具；续约失败后阻止新动作，并清理受管命令。

LocalProcessSandbox 和 DockerSandbox 会在命令启动后持久化可核验身份。Windows 使用 PID 与
进程创建时间并通过 Job Object 约束进程树；POSIX 使用独立进程组和启动标记；Docker 使用唯一
容器名及 `patchloop.command_id`、`patchloop.execution_id` labels。timeout、pause、cancel、
lease lost 和 Runtime 退出都会先终止并确认进程树或容器，再结算控制请求和释放租约。

恢复接管不会仅依赖数据库 lease 过期。新 owner 会先查询旧 Execution 或 Workspace 的活动
命令，核验并清理原进程身份；PID 已复用、容器 label 不匹配、Docker daemon 不可达或退出
无法确认时均失败关闭。清理失败会把命令标记为 `cleanup_failed`，将旧 Execution 和 Task
置为 `recovery_required`，并回滚刚获取的新 Execution，禁止新 writer 与旧 writer 重叠。

### 9.3 租约事件与安全诊断

Session journal 已增加 `lease.acquired`、`lease.contended`、`lease.renew_failed`、
`lease.lost`、`lease.released` 和 `lease.takeover`。事件包含作用域、资源、generation 和恢复
建议；owner 只输出 SHA-256 摘要，不记录原始 owner 或 lease token。

### 9.4 验收证据

所有权和恢复测试位于 `tests/integration/test_execution_ownership.py`、
`tests/integration/test_runtime_ownership.py`、
`tests/integration/test_execution_takeover_recovery.py`、
`tests/integration/test_managed_command_runtime.py` 和 `tests/e2e/test_workspace_ownership.py`。
覆盖双进程竞争、路径别名、heartbeat、stale guard、父进程终止、暂停/取消清理、跨 Task
Workspace 接管以及清理失败进入人工恢复。

2026-09-06 在 Windows 主机运行质量门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（140 files already formatted） |
| `mypy src` | 通过（70 source files） |
| `pytest -q` | 通过：414 passed, 1 skipped in 107.65s |

唯一跳过项为该 Windows 主机不支持 symbolic links。Windows Job Object 的真实父进程崩溃
测试已通过；当前主机没有 Docker daemon，因此 Docker 身份核验、清理和失败关闭由模拟测试
覆盖，真实 Docker 后端仍按 SRF-07 的目标环境验收执行。

## 10. SRF-04 实际交付记录

### 10.1 Effect 身份、准备与原子执行

提交 `60a5e95` 完成 SRF-04。Runtime 在调用任何工具后端前，先持久化完整模型响应、用量、
工具批次顺序和由 Task、Step、批次位置生成的稳定 Effect ID。恢复时读取原始响应批次和 Effect，
不重新询问 Provider，也不使用 Provider call ID 代替执行身份。

`src/patchloop/execution/effects.py` 和写工具预览层实现无副作用的 `prepare`：校验工具、参数、
路径、计划要求和 Policy，保存规范化参数摘要、动作类别、文件原始内容或不存在标记、原始摘要及
目标摘要。连续文件修改按批次投影前一个目标内容，恢复后仍能重建修改前基线和最终 diff。

`ToolPolicy` 使用 allow、deny、require_approval 三态。硬性权限和路径拒绝不能由 Approval
绕过；待审批 Effect、Approval、Task 和 Execution 状态在同一事务提交。Approval 精确绑定
Effect 内容摘要、参数摘要、workspace、policy version 和 config version，批准只能消费一次，
执行条件变化会使旧批准失效。

执行前 Runtime 再次检查输入、Policy、文件基线和所有权，然后由 Store 原子地将 Effect 认领为
executing 并消费批准；事务完成后才调用后端。后端返回后，`commit_effect` 在一个事务保存
Effect 终态、ToolResult、Provider 观察引用、checkpoint 事件水位和 journal 事件。提交中途故障
不会留下半结果或孤立事件。

### 10.2 Reconcile、拒绝与取消

Runtime 恢复入口会先 reconcile 未完成 Effect。prepared Effect 重新校验后执行；
waiting_for_approval 保持原请求且不调用后端；终态 Effect 回放持久结果。executing 且结果缺失时，
文件动作只有在当前内容与目标摘要完全一致时才补认成功；部分写入、用户再次修改或无法读取时均
转为 unknown，并使 Task 进入 recovery_required。测试和通用命令不按只读动作自动重跑，无法
证明外部结果时同样进入 unknown。

明确返回失败的后端结果保存为 failed，恢复时只回放，不与 unknown 混淆。准备阶段被 Policy
拒绝、文件基线变化或 Task 取消时不调用后端，而是原子保存 denied/cancelled Effect 及结构化
Provider 工具观察。观察明确包含后端未调用、Effect 状态和建议的后续动作，确保每个原模型工具
调用都有配对结果，Agent 可以更新计划或选择安全替代方案。

### 10.3 显式恢复处置

`src/patchloop/execution/recovery.py` 提供 RecoveryService，离开 recovery_required 必须提交带
操作人来源和证据的 RecoveryDisposition：

- `confirm_result` 校验原工具身份和参数摘要，补写已核验的 ToolResult，并把原 unknown Effect
  结算为 succeeded 或 failed；
- `abandon` 将 Task 终止为 cancelled，释放 Session 活动 Task 槽，但保留原 unknown Effect 和
  处置证据；
- `create_retry` 保留原 unknown Effect，创建具有新 ID 和 `retry_of_effect_id` 的 Effect，同时
  创建精确绑定的新 Approval 并进入 waiting_for_approval。

显式重试不会把 unknown 改回 prepared，也不会继承或重复使用旧批准。新 Approval 被明确批准后，
Runtime 将重试物化为新的持久化工具批次；恢复占位观察不计为新的工具失败，已批准的新 Effect
绕过针对隐式重复动作的阻断，但仍执行最新 Policy、参数、资源基线和所有权检查。端到端测试确认
旧 Effect 保持 unknown，后端只执行新的 retry call 一次。

### 10.4 测试与验收证据

SRF-04 定向矩阵覆盖 Effect prepare/claim/commit/reconcile、审批等待与重启、并发一次性消费、
条件变化导致授权失效、文件目标摘要核验、部分写入和用户修改、命令结果未知、明确失败回放、
结构化拒绝与取消，以及 RecoveryDisposition 的确认、放弃和显式重试。FakeStore 与 SQLiteStore
运行同一组恢复处置契约；真实 SQLite 另外覆盖 Effect、结果、审批、Task 和事件的事务故障回滚。

2026-09-07 在 Windows 主机运行 SRF-04 定向矩阵：

| 命令 | 结果 |
| --- | --- |
| `pytest -q tests/unit/test_execution_models.py tests/unit/test_effect_preparation.py tests/unit/test_approval_service.py tests/unit/test_recovery_service.py tests/unit/test_security.py tests/unit/test_session_store_transactions.py tests/e2e/test_effect_recovery.py tests/e2e/test_approval_recovery.py tests/integration/test_runtime_ownership.py tests/e2e/test_session_runtime_baseline.py` | 通过：92 passed in 23.82s |

同日运行完整质量门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（149 files already formatted） |
| `mypy src` | 通过（73 source files） |
| `pytest -q` | 455 passed, 1 skipped, 1 failed in 182.36s |

唯一跳过项仍为 Windows 主机不支持 symbolic links。唯一失败项为
`tests/unit/test_intelligence.py::test_committed_retrieval_report_is_reproducible`：工作区中原有且未纳入
SRF-04 提交的 `benchmarks/results/week05_retrieval.json` 与当前检索候选不同。SRF-04 定向矩阵、
静态检查、格式检查和类型检查均通过；该 benchmark 快照差异保留为独立工作区改动，不作为
SRF-04 恢复能力通过的证据，也未被提交 `60a5e95` 修改。

## 11. SRF-05 实际交付记录

### 11.1 SessionService 与可推进 Runtime

提交 `4de3f69` 新增 `src/patchloop/session/service.py` 和
`src/patchloop/execution/driver.py`。SessionService 提供 create/list/get、append_message、
start_task、request_pause/request_cancel、resume 和 close；用户消息先提交为具有 Session sequence
和客户端提交 ID 的 Turn，再向调用方确认。Session 同一时间仍只允许一个活动 Task，关闭活动
Session 或向已关闭 Session 写入均被拒绝。

AgentRuntime 将原有同步执行循环拆成单次 `_advance_once` 边界，由 RuntimeDriver 为旧
`run/resume` 入口继续驱动。每个边界最多推进一个模型步骤，到达 waiting_for_approval、paused、
recovery_required 或业务终态时立即返回。模型调用前后、每个 Effect 认领前和恢复入口都会检查
持久化输入与 ControlRequest；Effect claim 将已消费输入 sequence 纳入同一事务的条件校验，避免
检查与认领之间的新约束越过边界。

模型返回后发现新用户输入时，原响应和用量先持久化，尚未执行的 Effect 随后结算为 cancelled，
并为每个原工具调用生成配对的结构化观察。pause/cancel 会先 acknowledged，清理并确认受管命令
退出、结算未决 Effect 后才 settled；普通 resume 遇到 unknown Effect 直接返回
recovery_required，不扩大授权或隐式重试。

### 11.2 Checkpoint、恢复与预算

RuntimeCheckpoint 已扩展 Session ID、输入水位、事件水位、待处理 Effect 批次、在途模型请求、
已累计响应步骤和未知远端用量步骤。模型响应持久化后立即保存 Effect 批次；Effect 成功、取消或
转为 unknown 时，在提交结果或恢复事件的同一事务中推进 checkpoint 事件水位并移除对应待处理
Effect。恢复按稳定 Step/Effect 身份回放 checkpoint 之后已经提交的观察，不再次执行已确认动作。

MemoryManager 继续以 `tool:<call-id>` 等事件 ID 幂等消费；恢复重放不会重复产生 Memory 事件。
PromptCacheCoordinator 从 checkpoint 快照恢复，Session 新输入和历史对话只追加到冻结前缀之后，
不改变稳定缓存前缀。

计划、requires_replan、replan 次数、工具历史、文件修改前基线、失败计数、Token、费用、Context、
Memory 和 Cache 指标均随 checkpoint 保存并恢复。`accounted_model_response_steps` 保证已落库响应的
用量只累计一次；请求已发出但没有持久化响应时记录 unknown usage，不把远端费用伪造为精确值，
TaskReport 通过 `model_usage_exact` 和 `unknown_model_usage_calls` 明确报告。活动耗时在持久边界累计，
暂停后的墙钟等待时间不计入 `max_seconds` 预算。

### 11.3 跨 Task 会话历史与授权隔离

新 Task 从 Session Turns 构建持久对话尾部，并由既有 ContextEngine 在请求时按 Context 预算裁剪。
完成结果以幂等 Assistant Turn 写回 Session，因此第二个 Task 可以看到前一轮用户约束和 Agent
结论。会话历史位于 Prompt Cache 动态尾部，不被并入新 Task 的冻结前缀。

同一个 AgentRuntime 切换 Task 时会清空前一 Task 的工具历史、变更跟踪、recent paths、重规划
状态和内存预算累计，并使用新 Task 自己的计划与预算。Effect、Approval 和 checkpoint 继续以
Task ID 隔离；前一 Task 已消费的一次性 Approval 不能授权第二个 Task，相同写操作也必须创建并
批准新的 Effect/Approval。

### 11.4 测试与验收证据

主要新增覆盖位于 `tests/integration/test_session_runtime.py`，并扩展
`tests/e2e/test_checkpoint_resume.py`、`tests/e2e/test_layered_memory_runtime.py`、
`tests/integration/test_managed_command_runtime.py` 和 `tests/unit/test_persistence_contracts.py`。
覆盖 Provider 阻塞期间追加输入、claim 竞争窗口、陈旧批次取消与观察配对、pause/cancel 清理、
unknown 恢复障碍、多 Effect 提交后崩溃、Memory 幂等回放、Prompt Cache 前缀、用量精确累计、
未知远端用量、暂停计时、第二 Task 历史继承和一次性授权隔离。

2026-09-07 在 Windows 主机运行 SRF-05 定向矩阵：

| 命令 | 结果 |
| --- | --- |
| `pytest -q tests/integration/test_session_runtime.py tests/integration/test_managed_command_runtime.py tests/e2e/test_checkpoint_resume.py tests/e2e/test_effect_recovery.py tests/e2e/test_approval_recovery.py tests/unit/test_persistence_contracts.py tests/unit/test_prompt_cache_coordinator.py tests/unit/test_memory_manager.py` | 通过：53 passed in 24.34s |

同日运行质量门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（152 files already formatted） |
| `mypy src` | 通过（75 source files） |
| `pytest -q --deselect=tests/unit/test_intelligence.py::test_committed_retrieval_report_is_reproducible` | 通过：475 passed, 1 skipped, 1 deselected in 238.65s |

唯一跳过项仍为 Windows 主机不支持 symbolic links。唯一 deselected 项仍是工作区中既有且未纳入
SRF-05 提交的 `benchmarks/results/week05_retrieval.json` 快照差异；SRF-05 没有覆盖或提交该文件。
因此 SRF-05 的代码、定向矩阵和除该已知快照项之外的全仓回归已通过，但该结果不替代 SRF-07
要求的重复故障矩阵、真实 Docker 后端和真实 Provider 试用验收。

## 12. SRF-06 实际交付记录

### 12.1 Session、Approval 与 Recovery CLI

提交 `0c2c2e9` 在 `src/patchloop/cli.py` 增加 `session` 和 `approval` 命令组，并统一从当前
目录或 `--repo` 指定目录解析 workspace-local SQLite。`session create/list/show/start/send/`
`pause/cancel/resume/close/enter/recover` 已接入 SessionService、ApprovalService 和
RecoveryService；CLI 不直接拼接 SQLite 执行状态写入。

`create` 和 `enter` 不获取 Execution；`start` 与 `resume` 才进入运行时所有权生命周期。
`enter` 的 `/exit` 只退出界面，Ctrl+C 则提交 pause、等待并展示清理状态。真实第二 CLI 进程可在
Provider 阻塞期间提交 Turn 或 pause；竞争 resume 返回结构化冲突，不会启动第二个执行者。

### 12.2 状态投影与机器接口

Session 查询会聚合活动目标、对话、计划及变化、模型/工具边界、测试结果、diff、审批、unknown
Effect、Execution generation、租约到期时间和脱敏 owner 摘要，并给出可复制的下一步命令。输出
不包含原始 owner 或 lease token。

JSON 输出固定 `schema_version`、Session/Task/Execution ID、状态、最新 sequence、待审批项、恢复
项、错误类别和下一步命令。退出码区分完成、执行失败、用法错误、等待审批、暂停、冲突、需要恢复
和取消；JSON 模式不会混入交互提示。`send` 使用客户端提交 ID 保持重试幂等，重复或冲突决定沿用
存储层的结构化结果。

### 12.3 旧入口、安全审批与显式重试

旧 `run/resume/cancel/status` 已转到共同 Session 服务或查询投影。删除了
`_approval_handler(non_interactive)` 自动返回 True 的路径；`non_interactive` 只保留兼容字段，
不再授予写入或执行权限。缺少精确 Approval 时，旧 `run` 同样返回请求 ID、
`waiting_for_approval` 和退出码 10，工具后端执行次数为 0。CI 必须显式执行
`approval list → approval decide → resume`，批准在执行前重新核对绑定并只消费一次。

`session recover` 会先显示原 Effect 结果未知以及重试可能重复外部副作用。创建新重试必须使用
`--acknowledge-duplicate-risk`，或在 human 模式完成交互确认；该确认还会写入
RecoveryDisposition 并由存储事务再次校验。普通 resume 和普通审批不能替代重试授权。新重试
具有新的 Effect ID、`retry_of_effect_id` 和独立的一次性 Approval，原 unknown Effect 及证据
保持不变。

### 12.4 文档与验收证据

README 快速开始已改为 Session 主流程，覆盖独立仓库准备、创建与启动、第二终端追加约束、审批、
进程重启、unknown 恢复、diff/报告查看，以及旧非交互行为变化和旧数据库升级/回退步骤。数据库
升级使用事务和自动一致性备份；回退要求先停止所有 writer，再通过带 manifest 校验的恢复函数
保留升级后数据库并恢复旧快照。

SRF-06 主要自动化覆盖位于 `tests/integration/test_session_cli.py`，并扩展
`tests/integration/test_cli.py`、`tests/unit/test_recovery_service.py`、
`tests/e2e/test_effect_recovery.py` 和 Store 契约测试。2026-09-07 在 Windows 主机运行定向矩阵：

| 命令 | 结果 |
| --- | --- |
| `pytest -q tests/unit/test_intelligence.py::test_committed_retrieval_report_is_reproducible tests/integration/test_cli.py tests/integration/test_session_cli.py tests/integration/test_session_runtime.py tests/unit/test_approval_service.py tests/unit/test_recovery_service.py tests/unit/test_persistence_contracts.py` | 通过：73 passed in 31.27s |

同日运行完整质量门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（153 files already formatted） |
| `mypy src` | 通过（75 source files） |
| `pytest -q` | 通过：502 passed, 1 skipped in 195.22s |

唯一跳过项为当前 Windows 主机不支持 symbolic links。固定检索报告已随当前源码候选更新，
`test_committed_retrieval_report_is_reproducible` 已恢复通过。真实 Provider 三次独立试用、真实 Docker
后端、固定崩溃重复矩阵和 8 进程 × 100 轮竞争仍属于 SRF-07，SRF-06 不以 Fake Provider 或模拟
后端替代这些最终验收。

## 13. SRF-07 实际交付记录

### 13.1 故障矩阵与所有权竞争

`tests/fixtures/session_fault_matrix.json` 固定 11 个故障场景，覆盖消息提交与消费、模型响应、
工具批次、执行意图、外部动作、结果、checkpoint、Memory 和 Trace 导出。每个场景独立执行
3 次并保留全部结果，共 33 次全部通过。父进程通过进程间 Event 等待精确屏障后终止 worker，
不使用固定 sleep 猜测崩溃窗口。机器结果位于
`benchmarks/results/srf07_fault_matrix_step2.json`。

Session、Task 和 Workspace 三类所有权均完成 8 进程 × 100 轮竞争。每轮在全部竞争者返回前
保持胜者租约，结果均为恰好一个成功、七个冲突、零错误；释放后可以重新获取，八个不同
Workspace 可以并行获取。机器结果位于
`benchmarks/results/srf07_ownership_contention_step3.json`。

### 13.2 目标后端与安全审计

Windows 目标主机上的进程树清理、旧 worker 接管、lease takeover 和用户修改文件场景均通过。
当前主机没有 Docker CLI 或可用 daemon，因此三个真实 Docker 场景在
`benchmarks/results/srf07_backend_acceptance_step4.json` 中明确记录为 `unverified`，不以 skip、
模拟测试或 Windows 结果代替通过。

独立安全审计交叉核对外部 JSONL、最终文件、SQLite journal 和 Trace。已确认 Effect 重复执行、
unknown 自动重试、未授权动作、stale owner 成功提交、审批决定失败、事件缺失、跨来源不一致、
凭据泄漏和原始 lease token 泄漏九项计数均为 0。机器结果位于
`benchmarks/results/srf07_safety_audit_step5.json`。

### 13.3 真实仓库试用与恢复分类

真实试用清单锁定 `https://github.com/Glinfen/patch-loop.git` 的 revision
`0c2c2e9f8685d8f980f4b759a04ce7d18606553a`、Python 3.12、精确依赖版本、Typer 0.9
兼容性 Issue、公开测试和仓库外独立断言。执行器为三次样本分别创建不可覆盖的干净工作区，
并编排 Session 启动、追加约束、精确审批、新进程恢复、diff、用量和时延采集；所有失败均保留。

2026-09-13 使用加密传输到 AutoDL 的显式凭据重新预检。DeepSeek 官方 `/models` 实时目录返回
`deepseek-flash`，使用该模型的 Chat Completions 基础请求和工具调用请求均返回 HTTP 200；工具响应
包含可解析的 `tool_calls`、函数名和 JSON 参数。旧 Provider 白名单最初拒绝该 ID，脱敏证据保存在
`.patchloop/srf07_real_provider_trials_deepseek_flash_failed.json`。适配后使用来源和目标 commit 均已
核验的本地裸镜像绕过 AutoDL 到 GitHub 的 HTTP/2/443 超时，在三个独立干净工作区重新执行。

三轮均创建 Task 并实际调用模型；Trial 1、2 到达审批边界并在批准后由新进程恢复，Trial 3 在初始
执行阶段结束。三轮分别执行 20 个模型步骤、34/40/35 次工具调用，最终都因 `step budget exceeded
(20)` 失败且没有代码变更。脱敏报告保存在
`.patchloop/srf07_real_provider_trials_deepseek_flash_v4_failed.json`。Fake Provider 结果没有写入这些
报告；本次结果证明配置、工具调用和恢复链路可达，但不能计为真实试用通过。

为区分固定预算与 Agent 实现因素，2026-09-13 在同一服务器、同一锁定 revision、同一 Typer 0.9
任务和同一 DeepSeek Flash 后端上运行 Claude Code 2.1.270 对照。按 DeepSeek 官方 Claude Code
映射使用 `sonnet` 客户端模型后，20 turn、无权限拒绝的样本同样以 `error_max_turns` 结束且没有
diff；把诊断预算提高到 60 turn 后，Claude Code 报告 66 turn、65 次工具调用并完成修改。公开
验收测试 3 项和仓库外独立断言均通过；全量测试为 499 passed、5 failed、1 skipped，其中 5 个
失败在未修改的锁定版本上逐项复现，因此不属于该补丁回归。原始 JSONL、stderr、计时、diff 和
脱敏汇总保存在 `.patchloop/claude_code_eval/`。该结果说明 20 步/turn 对这个模型和任务确实过紧，
也说明模型在扩大预算后具备完成能力；但 Claude turn 与 PatchLoop step 并非等价单位，不能据此
单独判定两个 Agent 的执行效率相同。

总验收报告将 11 个故障场景分类为自动恢复 7 项、正确停止等待人工处置 4 项、真正恢复失败
0 项。模型响应已返回但未持久化的 1 个场景单独记录远端用量未知；真实 Provider 因未执行而
没有产生远端用量记录，不能把 0 次未知调用解释为真实用量已核准。分类及组件状态位于
`benchmarks/results/srf07_acceptance_summary.json`。

### 13.4 最终质量门禁与结论

2026-09-08 在 Windows 11、Python 3.12.13 环境运行完整质量门禁：

| 命令 | 结果 |
| --- | --- |
| `ruff check src tests` | 通过 |
| `ruff format --check src tests` | 通过（169 files already formatted） |
| `mypy src` | 通过（81 source files） |
| `pytest -q` | 通过：522 passed, 1 skipped in 119.79s |

唯一跳过项为该 Windows 主机不支持 symbolic links。完整命令、退出码、耗时和输出保存在
`benchmarks/results/srf07_quality_gates_step8.json`。故障矩阵、竞争、安全审计和本机回归均通过；
由于真实 Docker 后端和真实 Provider 三次试用仍未验收，SRF-07 总状态保持 `unverified`，不得
标记为整体完成。待具备对应环境后应复用现有清单和执行器补跑，而不是修改报告状态。
