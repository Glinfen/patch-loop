# ADR-020：Session Runtime 基础契约与恢复边界

状态：已接受（SRF-00）

## 背景

当前 Runtime 以 Task 为持久化边界。一次模型响应可以包含多个工具调用，工具先由
`ToolGateway.execute` 执行，随后 Runtime 分别写入工具结果和 checkpoint。外部副作用、
结果记录与 checkpoint 提交之间没有共同事务，因此数据库中没有工具结果不等价于工具没有
执行。

## 决策

### 对象归属

| 对象 | 归属与含义 |
| --- | --- |
| Session | 长期用户会话，串行包含多个 Task；最多一个活动 Task |
| Task | 一个用户目标及其计划、预算、工具历史和最终报告；最多一个执行 owner |
| Turn | 一条用户或 Agent 交互记录，可关联 Task |
| Step | 一次模型决策及其工具批次 |
| Execution | 一次 `run` 或 `resume` 尝试，不等同于 Provider call ID |
| Effect | 一个可审计的工具动作；后续由稳定 Effect ID 表示 |
| ControlRequest | pause/cancel 控制请求及其清理确认，不等同于 Task 终态 |
| RecoveryDisposition | 对 unknown Effect 的核验、显式重试或放弃决定 |

### 状态与控制

- Session 只有 `open` 和 `closed`。关闭前必须没有活动 Task，关闭后只读。
- Task 业务结果与运行条件分开保存。业务结果使用 `active`、`completed`、`failed`、
  `cancelled`；后三者是终态，不能重新变为 `active`。为兼容旧数据，旧 `Task.status` 在迁移
  和旧 API 中保留为投影，不再作为新 Runtime 的唯一状态源。
- 活动 Task 的运行条件使用 `idle`、`running`、`pausing`、`paused`、
  `waiting_for_approval`、`recovery_required`、`ended`。`recovery_required` 不能通过普通
  状态转换离开，只能由持久化 RecoveryDisposition 原子处置为确认结果、创建显式重试或
  放弃。业务结果已为 `cancelled` 时仍可同时保留 `recovery_required`，表示目标已取消但
  外部动作仍需核对。
- Effect 使用 `prepared`、`waiting_for_approval`、`executing`、`succeeded`、`failed`、
  `denied`、`unknown`、`cancelled`。`unknown` 代表外部结果无法确认，不是失败，也不能
  普通重试；显式重试创建带 `retry_of_effect_id` 的新 Effect，原记录保持 unknown。
- `pause` 和 `cancel` 先写入 ControlRequest，再经历 `requested → acknowledged → settled`
  或 `cleanup_failed`。只有受管进程已经停止，或未决动作已进入可见的 unknown/
  recovery_required 处置路径，才报告安全停止。`complete` 与 `cancel` 业务结果的竞态按
  首个成功提交的终态事务决定，旧 owner 不得覆盖已取消的 Task。
- 普通 `resume` 不扩大授权、不隐式批准等待中的动作、不重跑 `unknown` Effect。恢复
  `unknown` 必须先提交 RecoveryDisposition：核验已有结果、放弃，或显式创建新的受审批
  Effect。

### 当前实现的读写顺序

| 顺序 | 当前位置 | 提交边界与风险 |
| --- | --- | --- |
| 1 | `AgentRuntime.run` | Task 转为 running，独立保存 Task |
| 2 | `AgentRuntime._execute` | 模型请求前建立 Step；模型响应只在内存中追加 |
| 3 | `ToolGateway.execute` | 策略检查后调用外部工具；文件/命令副作用已经可能发生 |
| 4 | `AgentRuntime._execute_or_replay` | 工具返回后独立 `record_tool_call`；这一步之前崩溃会留下动作无结果 |
| 5 | `AgentRuntime._execute` | 批次结束后独立记录 Step、Memory 观察和 checkpoint |
| 6 | `SQLiteStore.save_checkpoint` | checkpoint 通过另一条 SQLite 事务覆盖保存 |
| 7 | `AgentRuntime._complete` / `_fail` | 最终 Task 再次独立保存并写 Trace |

SQLite 事务只覆盖各自的单次 Store 方法；事务内不调用 Provider、工具或文件系统。事件
Trace 是 JSONL 投影，不是恢复时的唯一事实源。

### SRF-00 故障屏障

`tests/support/session_faults.py` 通过真实 `AgentRuntime → ToolGateway → Tool →
SQLiteStore` 调用链固定五个父进程可选择的旧路径屏障：工具派发后、动作开始前、外部动作
完成后、结果提交后、checkpoint 提交前。旧实现并没有持久化执行意图，因此第一个屏障明确
命名为 `tool_dispatched`；SRF-04 新路径再增加真正的 prepared/executing 提交屏障。每条
屏障记录先追加并 `fsync` 到独立审计 JSONL，再按配置让子进程以专用退出码结束。审计文件
用于证明外部动作是否发生，不能用 SQLite 结果行数替代。

SRF-00 特征化测试让五个屏障各至少命中一次，并在 `after_external_action` 屏障额外重复
三次：独立审计均证明非幂等追加动作已发生，而 `tool_calls` 没有结果行。这是旧行为基线；
它不代表恢复安全能力已完成，安全恢复断言留给 SRF-04。

### Legacy fixture

`tests/fixtures/session_legacy/` 固定当前版本可读取的最小旧数据输入，包含：

- 一个完成 Task：`legacy-completed-task`；
- 一个带未完成计划的 running Task：`legacy-running-task`；
- 已确认的 `legacy-write-call` 工具结果；
- step 1 checkpoint、Memory 1.0 snapshot、`legacy-epoch` Cache snapshot；
- 包含两个 Task 事件的 JSONL Trace。
- 当前旧表结构和代表性行的 `runtime-v0.sqlite`，以及记录来源 commit、SHA-256 和表行数的
  `manifest.json`。

后续迁移测试必须读取这些静态文件，不得用新 schema 在测试开始时重建同一份“旧数据”。

## 后果

SRF-00 先固定事实来源、状态转换和故障证据，不修改生产执行入口。SRF-01 负责将契约
转成领域模型，SRF-02 负责事务化存储和迁移，SRF-03/04 负责 owner fencing、审批和
`unknown` 恢复。直到这些任务完成，旧的“结果缺失即可重试”行为仍只能作为基线观测。
