# Approval / Policy Control Plane 扩展开发方案

编写日期：2026-09-18。当前代码基线：`5305324`。关联目标：[第二阶段项目目标](PHASE_2_PROJECT_GOALS.md) 的 S2-G3，以及[第二阶段模块开发计划](PHASE_2_DEVELOPMENT_PLAN.md) 的 Approval / Policy 扩展。

本方案只扩展 Approval 和 Policy 控制面，复用现有 Session、Effect、Execution、single writer、lease fencing、Sandbox 和 Trace。它不把一次性 Approval 改造成隐式全局白名单，也不允许恢复或 Provider 切换扩大权限。

## 1. Problem

### Current Problem

当前安全链路已经能够对一个具体 Effect 执行 `allow / deny / require_approval` 判断，并把一次性 Approval 持久化到 SQLite。它仍有以下边界：

1. `ToolPolicy` 主要依据工具的 `READ / WRITE / EXECUTE` 权限和风险阈值判断，规则没有独立的动作、资源和来源模型。
2. `Approval` 只绑定一个 Effect、参数摘要、workspace、policy/config version；不能表达一次批准、Session 范围批准、精确资源范围批准、过期和撤销。
3. 文件、命令、网络、依赖安装和未来 Skill 动作没有统一的资源规范化接口。当前网络或危险命令主要通过命令名和字符串标记拒绝。
4. 参数被修改时虽然现有 fingerprint 会使旧批准失效，但没有结构化的“修改后重新审批”关系和用户可见原因。
5. SQLite 与 Fake Store 已有 Approval 事务，但没有可独立维护的长期授权记录；CLI 也只有列出和决定当前 Approval 的能力。
6. Policy 事件能记录当前 Approval 决定，但不能完整回答“哪条规则、哪个授权、针对哪个规范化资源、在什么版本下作出了决定”。

### Desired Behavior

- 所有高风险动作先生成稳定的 `ActionDescriptor`，再由统一 Policy Engine 返回 `allow`、`deny` 或 `require_approval`。
- 硬拒绝永远优先于任何规则、Approval、恢复和 Provider；没有匹配规则时默认 fail closed 到 `require_approval` 或 `deny`。
- 当前 Effect 的一次性批准保持兼容；用户可以显式选择 Session 范围或精确资源范围授权。
- 范围授权绑定 Session 或 workspace、动作类型、规范化资源、policy/config version 和可选过期时间；不能跨 workspace 转移，不能扩大到批准请求之外。
- 修改命令、路径、网络目标、包版本、Skill 或其他关键参数会生成新的 Effect 和新的 Approval，并保留 `supersedes` 关系。
- 过期、撤销、Policy 版本变化和资源条件变化都会在执行前使授权失效。
- 网络、依赖安装和 Skill 动作通过同一 Policy/Approval 入口；拒绝结果以结构化 Tool 观察返回给 Agent。
- SQLite、Fake Store、CLI、Runtime 和 Trace 使用同一状态转换；恢复只消费已持久化的决定，不能自动批准新动作。

### Scope

- 新增版本化的 Policy rule、动作/资源描述、Approval grant 和审计事件。
- 扩展现有 `Approval`、`ApprovalService`、`ToolPolicy`、`ToolGateway`、Runtime、SQLite/Fake Store 和 CLI。
- 为文件、命令、网络目标、依赖包和 Skill ID 定义第一版规范化器。
- 覆盖迁移、并发决定、范围授权、撤销/过期、参数修改、恢复和绕过测试。

### Non-Goals

- 不实现 Skills Runtime 本身；这里只提供 `skill_load` / `skill_execute` 的 Policy 适配接口。
- 不实现 Sandbox 隔离；Sandbox 只接收 Policy 给出的联网/安装许可，并继续负责执行边界。
- 不引入 LLM classifier、自动风险判断或 YOLO 模式。
- 不允许通用 `shell:*`、workspace 外路径或全局网络白名单作为本任务的默认授权。
- 不改变未知 Effect 的恢复处置；`resume` 不能等价于 `create_retry`。
- 不自动批准 Git push、发布、凭据读取、持久化修改或其他硬拒绝动作。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `src/patchloop/security.py::RiskAssessment, PolicyDecision, ApprovalRequest` | 提供风险枚举、三态决定、凭据脱敏和未可信内容防护 | 保留基础安全类型；新增动作/资源规范化结果和结构化 Policy 证据，避免把原始秘密写入事件 |
| `src/patchloop/tools/base.py::Tool, ToolContext` | Tool 只有名称、权限和输入模型；Context 能解析仓库内路径 | 增加可选 `policy_descriptor()` 钩子，所有工具通过同一入口声明资源和副作用类别 |
| `src/patchloop/tools/gateway.py::ToolPolicy` | 按权限、风险阈值、危险命令标记和路径边界返回决定 | 改为兼容门面，委托新的 `PolicyEngine`；保留现有硬拒绝和计划/replan 门禁 |
| `src/patchloop/tools/gateway.py::ToolGateway.prepare_call/execute_claimed` | 先规范化参数，再保存 `ToolPreparation`，执行前检查一次性 consumed approval | 保存 `ActionDescriptor`、Policy version 和匹配证据；执行前重新评估并原子消费 Approval/grant |
| `src/patchloop/execution/models.py::Approval, Effect` | Approval 精确绑定 Effect，支持 pending/approved/consumed/denied/expired | Approval 增加 scope、grant、过期和 supersedes 元数据；Effect 保存规范化动作摘要和 Policy 证据 |
| `src/patchloop/execution/approvals.py::ApprovalService` | 读取、列出并按当前执行条件决定 Approval | 增加范围决定、grant 列表/撤销、过期回收和修改后重新请求入口 |
| `src/patchloop/persistence.py` | `approvals` 表一对一绑定 Effect；决定和消费在事务中完成 | 增加 `policy_rules`、`approval_grants` 表和 schema migration；范围 grant 的检查/消费/撤销必须在同一事务中完成 |
| `src/patchloop/persistence_contracts.py` | Fake Store 与 SQLite Store 都实现 Approval 和 Effect 协议 | 扩展 Protocol 和 Fake Store，确保离线测试与 SQLite 使用相同的版本、并发和事件语义 |
| `src/patchloop/runtime.py` | Runtime 在准备/认领 Effect 时调用 Gateway 和 Store | 让 Runtime 只消费结构化 Policy 结果；恢复时重新校验当前 Policy、grant 状态和 ActionDescriptor |
| `src/patchloop/cli.py` | 已有 `approval list/decide`、Session 状态和 JSON 输出 | 增加 scope、expiry、revoke、grant list 和 policy explain；显示匹配规则、资源范围、版本和下一步命令 |
| `tests/unit/test_security.py`, `tests/unit/test_approval_service.py` | 覆盖风险阈值、路径拒绝、一次性 preauthorization 和绑定校验 | 增加规则优先级、资源规范化、范围 grant、过期/撤销和参数变更测试 |
| `tests/integration/test_session_cli.py`, `tests/integration/test_execution_ownership.py` | 覆盖 CLI 审批和 owner fencing | 增加第二进程决定、旧 owner、恢复、grant 并发消费和 CLI JSON schema 测试 |
| `tests/e2e/test_approval_recovery.py`, `tests/e2e/test_security_observability.py` | 覆盖恢复、防重复副作用、网络拒绝和安全 Trace | 增加网络/依赖/Skill action、策略绕过、过期和撤销的端到端证据 |

### Current Flow

```text
Model tool call
→ ToolGateway.prepare_call
→ ToolPolicy.assess_for_preparation
→ Effect + one-time Approval persisted when required
→ approval decide
→ Runtime resume / Effect claim
→ Approval.consume and ToolGateway.execute_claimed
→ Tool result, Trace and journal
```

目标流程改为：

```text
Model tool call
→ normalize arguments and ActionDescriptor
→ PolicyEngine.evaluate(hard rules, policy rules, grants)
→ deny / allow / waiting_for_approval
→ persist Effect + Policy evidence atomically
→ operator decides once/session/resource or deny
→ persist Approval decision + optional Grant atomically
→ resume re-evaluates exact descriptor and current versions
→ consume one-time Approval or matching Grant
→ execute, journal and emit redacted policy evidence
```

## 3. External Research

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| [Claude Code Permissions](https://code.claude.com/docs/en/permissions) | 使用 allow/ask/deny 规则、权限模式和会话内持续批准；显式 deny 是安全底线 | 将硬拒绝、显式 ask、会话范围授权和可解释规则来源纳入 PatchLoop | 不引入模型分类器或 bypass permission；不把文本规则当作 Sandbox |
| [Qwen Code Approval Mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/approval-mode/) | Plan、Ask Permissions、Auto-Edit、Auto、YOLO 等模式区分编辑和命令风险，并对危险命令硬阻断 | 保留只读 Plan/严格 Ask 的清晰模式边界；把依赖安装、命令和文件写入分别建模 | 不实现 Auto/YOLO；不让模型或分类器自动扩大权限 |
| [OpenCode Permissions](https://opencode.ai/v2/docs/permissions) | 规则由 action/resource/effect 组成；资源支持规范化路径和模式匹配；多资源操作任一 deny 即拒绝；saved approval 有项目范围 | 采用稳定 action/resource 描述、确定性优先级、多资源 fail-closed 和 workspace 绑定 grant | 不把 host shell 的 best-effort 扫描当作安全边界；不照搬全局宽泛默认 allow |

### Lessons for PatchLoop

1. Policy 决定必须同时包含动作、资源、来源和版本；只记录布尔 `approved` 无法回放。
2. `deny` 是硬底线，任何 session/resource grant 都不能覆盖它；多资源动作按最严格结果聚合。
3. “允许类似命令”必须落成可审计的规范化匹配器，并绑定项目/workspace；不能依赖自然语言描述。
4. 交互模式可以影响默认 ask 行为，但不改变 Policy、Approval 和 Sandbox 的硬边界。

## 4. Design

### Selected Approach

新增一个确定性的 `PolicyEngine`，将现有 `ToolPolicy` 保留为兼容构造入口。Policy Engine 先把 Tool 调用转换成不可变 `ActionDescriptor`，再按固定顺序评估硬拒绝、配置规则、已有 grant 和默认风险阈值。Approval 仍代表一个具体请求；可复用权限单独保存为 `ApprovalGrant`，避免把一次性状态和长期授权混在一个对象中。

### Key Decisions

#### 4.1 ActionDescriptor 是唯一授权输入

在 `src/patchloop/execution/policy.py` 新增以下模型：

```text
PolicyAction = read | edit | execute | network | dependency_install | skill_load | skill_execute | git
PolicyRuleEffect = allow | ask | deny
ApprovalScopeKind = once | session | resource
ResourceKind = path | command | network_target | package | skill | workspace

ResourceSelector:
  kind: ResourceKind
  value: str                 # 已规范化、脱敏、稳定排序
  digest: str                # canonical JSON SHA-256

ActionDescriptor:
  action: PolicyAction
  tool_name: str
  workspace_ref: str
  session_id: str
  resources: list[ResourceSelector]
  arguments_fingerprint: str
  side_effect: bool
  risk: RiskLevel
```

规范化规则固定如下：

- path 使用 `ToolContext.resolve_path` 后转为相对 POSIX 路径；解析失败直接 `deny`。Windows 比较大小写不敏感，但保存 canonical value。
- command 只保存规范化 executable、参数序列和 digest；shell operator、重定向、解释器拼接、未识别 compound command 不能转换成宽泛 selector。
- network_target 保存 scheme、host、port 和必要的 path 前缀；去除凭据、片段和不稳定 query；无法解析或包含本地凭据直接 `deny`。
- package 保存 package manager、名称、版本约束和来源；未锁定来源的远程安装默认为 `require_approval`，恶意脚本/远程管道由硬拒绝处理。
- skill 保存 registry/source、skill ID 和版本；项目 Skill 不能通过自身内容创建 grant。

`ActionDescriptor` 的 canonical JSON 是 Effect/Approval/grant 的匹配输入。Trace 只保存脱敏 selector、digest 和摘要，不保存原始 secret。

#### 4.2 Policy 规则和优先级

`PolicyRule` 字段固定为：

```text
id, schema_version, source, workspace_ref, session_id,
action, resource_kind, pattern, effect, max_risk,
priority, enabled, policy_version, created_at, updated_at
```

`source` 只允许 `builtin/system/user/project/session`。评估顺序固定为：

1. 输入、路径、凭据、命令解析和 Sandbox 前置条件的硬拒绝。
2. 匹配的 `deny` 规则；任一资源 deny 即整体 deny。
3. 已绑定且未过期的 ApprovalGrant；grant 不能覆盖前两层。
4. 匹配的 `allow` 规则；必须满足 action、workspace、resource、max_risk 和 policy version。
5. 匹配的 `ask` 规则。
6. 兼容 `ToolPolicy.approval_threshold` 的默认风险判断。

同一层级按资源匹配具体度、`priority` 降序、`id` 字典序决胜；deny 不允许被其他 source 覆盖。allow/ask 的同分匹配按 session > project > user > system > builtin 选择。多资源操作先逐资源评估，再应用 deny > ask > allow 聚合。无匹配且有副作用的动作返回 `require_approval`，只读且无外部资源的动作沿用现有低风险 allow。

规则 pattern 语义固定：path 使用规范化 POSIX glob；command 只允许 token 边界的 executable/argv 前缀；network_target 默认 host+port 精确匹配，子域名必须显式声明；package 使用 manager/name/version/source 四元组；skill 使用 registry/id/version 精确匹配。

新增不可变 `PolicyEvaluation`，其 `decision` 字段沿用 `PolicyDecision`，并至少包含 `decision`、`risk`、`reason`、`descriptor`、`matched_rule_ids`、`matched_grant_id`、`policy_version` 和 `config_version`。

#### 4.3 Approval 与 Grant 分离

现有 `Approval` 保持一对一 Effect 和一次性消费语义，新增字段：

```text
scope_kind: ApprovalScopeKind = once
grant_id: str | None
expires_at: datetime | None
supersedes_approval_id: str | None
decision_reason: str | None
```

新增 `ApprovalGrant`：

```text
id, schema_version, source_approval_id,
scope_kind: session | resource,
session_id: str | None,
workspace_ref: str,
action: PolicyAction,
resources: list[ResourceSelector],
policy_version, config_version,
expires_at, remaining_uses: int | None,
status: active | revoked | expired | exhausted,
created_at, revoked_at, revoked_by, version
```

约束：

- `session` grant 必须绑定 `session_id`、workspace 和当前 policy/config version；它只能匹配同一 session 的同类动作和已批准资源集合。
- `resource` grant 必须绑定 workspace；第一版只允许从当前请求的规范化资源集合生成精确 selector，不接受 CLI 自由输入的 `*`；必须提供过期时间。
- grant 不能跨 workspace、不能授权比原 Approval 更宽的 action/resource，不能覆盖 builtin deny。
- 一次性 Approval 使用 `remaining_uses=1` 的原子消费语义；session/resource grant 的使用、过期和撤销都在 Store 事务中更新。
- `policy_version`、`config_version`、ActionDescriptor digest 或关键资源发生变化时，旧 grant 只允许被识别为失效，不能自动迁移。

#### 4.4 修改后批准、过期和撤销

- `approval decide --approve` 默认 `once`；`--scope session` 或 `--scope resource` 才创建 grant。
- `--scope resource` 不接受扩大资源范围；可选 `--expires` 只能缩短系统默认期限，不能延长超出 Policy 上限。
- 修改任意命令参数、路径、目标文件前置条件、网络目标、包版本、Skill 版本或工具名都会生成新 Effect/new Approval，并设置 `supersedes_approval_id`。
- `approval grant revoke <grant_id>` 只将 grant 标记为 revoked，保留原记录和审计事件；执行中的 Effect 不被回滚，但下一次 claim 必须重新评估。
- 读取或决定已过期 grant/Approval 时，Store 在同一事务写入 `expired` 事件；不能通过 resume 恢复为 approved。

#### 4.5 网络、依赖和 Skill 统一入口

`Tool` 增加可选方法：

```python
def policy_descriptor(self, arguments: BaseModel, context: ToolContext) -> ActionDescriptor: ...
```

现有文件工具映射到 `edit/path`，测试和 Git 检查映射到 `execute/command`。未来网络、安装和 Skill 工具必须直接返回 `network_target`、`package` 或 `skill` selector，不得绕过 `ToolGateway` 调用后端。

未知资源、无法解析的 compound command、隐式网络访问和未声明的安装副作用按 fail-closed 处理。Policy 只决定是否允许；Sandbox 仍负责网络 namespace、进程、文件和资源限制。

#### 4.6 持久化和兼容

- Runtime schema 从当前版本 5 升至 6；新增 `_RUNTIME_MIGRATION_V6`、`policy_rules` 和 `approval_grants`，继续使用 JSON payload 保存完整模型，并为查询建立 workspace/session/status 索引。
- 旧 Approval 缺少新增字段时迁移为 `scope_kind=once`、无 grant、无 expiry；旧 Effect 缺少 ActionDescriptor 时只能走兼容的当前 ToolPolicy 重新生成，无法生成时停止并要求人工处置。
- SQLite Store 和 Fake Store 必须共用同一 `PolicyStore` Protocol；审批决定、Grant 创建/撤销/消费、Effect claim 和 session event 在一个事务内完成。
- 事件类型固定为 `policy.evaluated`、`approval.requested`、`approval.decided`、`approval.grant_created`、`approval.grant_consumed`、`approval.grant_revoked`、`approval.expired`、`approval.superseded`。

#### 4.7 CLI 和机器接口

保留现有命令并扩展：

```text
patchloop approval decide <approval_id> --approve
  [--scope once|session|resource] [--expires ISO-8601] [--reason TEXT]

patchloop approval grant list <session_or_workspace_id>
patchloop approval grant revoke <grant_id> [--reason TEXT]
patchloop policy list [--repo PATH] [--json]
patchloop policy explain <task_id|effect_id> [--json]
```

`approval decide` 不接受任意资源 pattern；resource scope 从待批准请求中派生并可由 Policy 限制。JSON 输出新增 `scope_kind`、`grant_id`、`expires_at`、`matched_rule_ids`、`matched_grant_id`、`policy_version` 和 `next_commands`。人类输出必须显示实际资源摘要和授权边界。

### Target Flow

```text
Tool.policy_descriptor
→ PolicyEngine.normalize
→ PolicyEngine.evaluate
→ ToolPreparation(policy_evaluation + descriptor)
→ Effect.prepare / Approval.request in one transaction
→ CLI or second process decides once/session/resource/deny
→ Store writes Approval + optional Grant + journal atomically
→ Runtime claim revalidates descriptor, versions, expiry and workspace
→ consume exact Approval or matching Grant
→ execute through Sandbox
→ commit Effect/result and emit redacted policy events
```

## 5. Change Map

| File | Symbol | Change |
| --- | --- | --- |
| `src/patchloop/execution/policy.py` | new `PolicyEngine`, `ActionDescriptor`, `PolicyRule`, `ApprovalGrant`, selectors | 新的规范化、匹配、优先级、过期和撤销模型 |
| `src/patchloop/security.py` | `PolicyDecision`, `RiskAssessment` | 保持兼容并增加 `PolicyEvaluation`/安全证据类型的导出边界 |
| `src/patchloop/tools/base.py` | `Tool.policy_descriptor` | 为工具提供统一动作描述钩子，默认从权限生成最小描述 |
| `src/patchloop/tools/gateway.py` | `ToolPolicy`, `ToolPreparation`, `ToolGateway` | 委托 PolicyEngine，保存 descriptor/evaluation，执行前重新评估并消费授权 |
| `src/patchloop/execution/models.py` | `Approval`, `Effect` | 增加 scope/grant/expiry/supersedes 和 policy evidence 摘要 |
| `src/patchloop/execution/approvals.py` | `ApprovalService`, Store Protocol | 增加范围决定、grant 生命周期和修改后重请求 |
| `src/patchloop/persistence.py` | runtime migration、SQLiteStore | 新表、索引、事务、旧 Approval 迁移和 journal 事件 |
| `src/patchloop/persistence_contracts.py` | `Store`/`FakeStore` approval methods | 与 SQLite 保持相同的 grant、版本和并发语义 |
| `src/patchloop/runtime.py` | Effect prepare/claim/recovery paths | 接入 Policy revalidation；恢复不扩大授权 |
| `src/patchloop/cli.py` | approval/policy commands | scope、expiry、revoke、grant list、explain 和 JSON schema |
| `tests/unit/test_policy_engine.py` | new | 规则、selector、优先级、多资源聚合和 fail-closed |
| `tests/unit/test_approval_service.py` | existing | scope、grant、expiry、revoke、supersedes、并发版本 |
| `tests/unit/test_security.py` | existing | 硬拒绝不能被 grant 或 approval 覆盖 |
| `tests/integration/test_policy_persistence.py` | new | SQLite/Fake migration、事务、journal 和旧数据 |
| `tests/integration/test_session_cli.py` | existing | 新 CLI 选项、JSON、第二进程决定和退出码 |
| `tests/e2e/test_approval_recovery.py` | existing | restart、stale grant、parameter change、unknown Effect |
| `tests/e2e/test_security_observability.py` | existing | network/dependency/skill policy、秘密脱敏和绕过计数 |

## 6. Implementation Tasks

### TASK-01：动作与资源合同

**Goal**

建立所有授权判断共用的、可序列化且不含秘密的 `ActionDescriptor` 和 `ResourceSelector`。

**Files / Symbols**

```text
src/patchloop/execution/policy.py
src/patchloop/tools/base.py::Tool.policy_descriptor
src/patchloop/security.py
```

**Implementation**

1. 实现枚举、Pydantic 模型、canonical JSON 和 SHA-256 digest。
2. 为 path、command、network_target、package、skill 实现独立规范化函数；规范化失败返回结构化 deny 原因。
3. 在现有文件、命令和 Git 工具中接入默认 descriptor；不改变工具实际执行逻辑。
4. 明确 selector 的 Windows 路径、大小写、端口、版本和脱敏规则。

**Interface Changes**

```text
新增 ActionDescriptor、ResourceSelector、PolicyEvaluation、PolicyAction、ResourceKind。
Tool 增加 policy_descriptor(arguments, context) -> ActionDescriptor。
```

**Tests**

- 同一输入跨进程产生相同 digest；字段顺序和 Windows 路径形式不影响匹配。
- secret、credential marker、shell operator、越界路径和未解析网络目标不进入可执行 selector。
- 多资源 descriptor 的 canonical 排序稳定。

**Acceptance Criteria**

- 所有现有工具都能生成 descriptor 或明确的结构化 deny。
- descriptor 日志不包含原始 token、密码、credential reference 或完整敏感参数。

### TASK-02：确定性 Policy Engine

**Goal**

实现 deny > grant > allow/ask > 默认风险判断的固定评估顺序，并保持旧 `ToolPolicy` 调用兼容。

**Files / Symbols**

```text
src/patchloop/execution/policy.py::PolicyEngine
src/patchloop/tools/gateway.py::ToolPolicy
```

**Implementation**

1. 实现 builtin hard deny、规则匹配、grant 匹配和多资源聚合。
2. 规则按 source、priority、id 固定排序；未知规则或未知 action 不得默认为 allow。
3. 将现有 `allowed_permissions`、`approval_threshold`、`require_plan_for_mutations` 转换为兼容默认策略。
4. `ToolPolicy.assess_for_preparation` 和 `assess` 返回相同结构化证据；不能因调用路径不同而改变决定。

**Interface Changes**

```text
PolicyEngine.evaluate(descriptor, *, rules, grants, policy_version, config_version)
  -> PolicyEvaluation
ToolPolicy 构造器保留现有参数，内部持有 PolicyEngine。
```

**Tests**

- deny 永远覆盖 allow、session grant 和 resource grant。
- 多资源中一个 deny 或 ask 时，整体结果分别为 deny 或 require_approval。
- 无匹配的副作用动作 require_approval；未知动作和不可解析命令 deny。
- 旧 `test_security.py` 的风险阈值和路径拒绝保持通过。

**Acceptance Criteria**

- 任何决定都能解释匹配的规则、grant、版本和资源摘要。
- Provider、resume、non-interactive 和 CLI 路径无法绕过同一 Engine。

### TASK-03：Policy/Grant 持久化与迁移

**Goal**

为规则和可复用授权建立 SQLite/Fake Store 的一致事务语义，并兼容旧数据库。

**Files / Symbols**

```text
src/patchloop/persistence.py
src/patchloop/persistence_contracts.py
tests/integration/test_policy_persistence.py
```

**Implementation**

1. 增加 `policy_rules`、`approval_grants` 表、索引和 schema migration；旧 Approval 默认迁移为 once。
2. 扩展 Store Protocol：list/evaluate rules、create/consume/revoke/expire grant、带 expected version 的决定。
3. Approval 决定、Grant 创建、Task/Effect 状态变化和 journal 事件放入同一事务。
4. Fake Store 使用相同的冲突、过期、重复提交和 fencing 语义。

**Interface Changes**

```text
resolve_effect_approval(...) -> ApprovalResolution
ApprovalResolution = approval + effect + task + grant | None
consume_grant(grant_id, descriptor, expected_version) -> ApprovalGrant
revoke_grant(grant_id, source, reason, expected_version) -> ApprovalGrant
```

**Tests**

- v0/v1 旧数据库迁移后可读取旧 Task/Approval，且一次性语义不变。
- 相同 client submission 重试幂等；不同 descriptor 或旧 version 返回结构化冲突。
- 两进程同时消费一个一次性 Approval 或有限 grant，最多一个成功。
- journal 事件完整、顺序稳定、无秘密。

**Acceptance Criteria**

- SQLite 与 Fake Store 的 Policy/Grant 契约测试完全相同。
- schema migration、backup/restore 和旧 checkpoint 回放不扩大权限。

### TASK-04：ApprovalService 范围授权生命周期

**Goal**

把 once/session/resource 决定、参数修改、过期和撤销落实到服务层。

**Files / Symbols**

```text
src/patchloop/execution/models.py::Approval, ApprovalGrant
src/patchloop/execution/approvals.py::ApprovalService
src/patchloop/execution/recovery.py
```

**Implementation**

1. `decide_current` 接收 scope、expiry 和 reason；resource scope 只能从当前 descriptor 派生，不能扩大 selector。
2. Approval 决定和可选 Grant 在一个 Store 调用中提交；旧 API 默认 once。
3. Effect 内容变化自动创建 supersedes 链，旧 Approval/grant 不得匹配新 descriptor。
4. 在 list/get/consume/revoke 前惰性标记过期；撤销只影响后续 claim，保留历史事件。
5. RecoveryService 对 unknown Effect 的 retry 使用新的 Effect、Approval 和 grant 绑定，不能继承旧授权。

**Interface Changes**

```text
ApprovalService.decide_current(..., scope_kind=once, expires_at=None, reason=None)
ApprovalService.list_grants(scope_id)
ApprovalService.revoke_grant(grant_id, source, reason)
```

**Tests**

- once 只能消费一次；session 只在同一 Session/workspace 生效；resource 只在同一 workspace 和精确 selector 生效。
- 过期、撤销、policy/config version 变化、文件前置条件变化都要求重新审批。
- 修改参数生成新 Approval，旧 Approval 保持审计可见但不能消费。
- unknown retry 必须显式新批准，普通 resume 不得执行。

**Acceptance Criteria**

- 未授权高风险动作执行次数为 0；已确认副作用重复执行为 0。
- 所有授权决定都有来源、范围、版本、时间和可回放事件。

### TASK-05：Gateway/Runtime 与网络、依赖、Skill 接入

**Goal**

让实际执行链路在准备和 claim 两个边界都使用同一个 Policy Engine。

**Files / Symbols**

```text
src/patchloop/tools/gateway.py
src/patchloop/runtime.py
src/patchloop/execution/effects.py
src/patchloop/sandbox.py
```

**Implementation**

1. `ToolPreparation` 保存 descriptor 和 PolicyEvaluation；Effect 持久化脱敏证据。
2. Runtime claim 前重新生成 descriptor，并比较 digest、workspace、policy/config version、grant expiry 和文件前置条件。
3. 将网络目标、依赖安装和 Skill load/execute 作为独立 action kind 接入，不允许通过通用 shell 绕过。
4. Policy deny/ask 以结构化 Tool observation 返回 Agent；Sandbox 只执行已批准且符合其后端限制的动作。
5. 恢复读取已持久化决定；找不到匹配响应、grant 或 descriptor 时进入 waiting/recovery_required，不自动放宽。

**Interface Changes**

```text
ToolPreparation.policy_evaluation: PolicyEvaluation
Effect.action_descriptor: ActionDescriptor summary
ToolGateway.prepare_call/execute_claimed 使用 descriptor-aware approval。
```

**Tests**

- prepare、restart、claim、lease takeover 四个边界都重新校验 Policy。
- 网络、依赖和 Skill action 的 allow/ask/deny 都经过同一 Store/Trace。
- 绕过 Gateway 直接调用工具、伪造 approval_consumed、切换 Provider 和恢复旧 owner 均失败。
- Sandbox 拒绝网络时 Policy 不得伪造成功；Policy 允许但 Sandbox 拒绝时保留明确观察。

**Acceptance Criteria**

- 所有高风险副作用都有对应 Policy/Approval/Sandbox 事件。
- 未授权网络、依赖安装、Skill 执行和仓库外访问均为 0 次实际后端调用。

### TASK-06：CLI、配置和审计投影

**Goal**

让用户可以查看、批准、撤销和解释授权，而无需直接检查 SQLite 或 JSONL。

**Files / Symbols**

```text
src/patchloop/cli.py
src/patchloop/events.py
README.md
```

**Implementation**

1. 扩展 `approval decide` 的 scope/expiry/reason 参数，默认 once。
2. 增加 grant list/revoke 和 policy list/explain；所有 JSON 输出使用固定 schema version。
3. Session show、waiting_for_approval 和错误结果显示 resource、scope、expiry、matched rules 和 next command。
4. 配置来源明确区分 builtin/system/user/project/session；非法或未知规则在加载 Provider 前 fail closed。
5. README 增加一次性、Session、resource 授权示例和撤销/恢复流程。

**Interface Changes**

```text
CLI JSON 增加 grant_id、scope_kind、expires_at、matched_rule_ids、policy_version、next_commands。
```

**Tests**

- CLI human/JSON 两种模式的 scope、expiry、revoke、explain 输出。
- 非交互模式缺少精确授权返回 waiting_for_approval 和可复制命令，不自动批准。
- 第二终端决定后第一终端 resume 只消费一次；重复决定返回结构化冲突。

**Acceptance Criteria**

- 陌生用户可以从 `approval list` 找到授权边界、决定下一步和撤销命令。
- JSON 输出不混入提示文本、lease token、原始参数秘密或 provider response。

### TASK-07：安全、竞争和迁移回归

**Goal**

用攻击性和跨进程测试证明 Policy 扩展没有扩大权限或重复执行副作用。

**Files / Symbols**

```text
tests/unit/test_policy_engine.py
tests/unit/test_approval_service.py
tests/integration/test_policy_persistence.py
tests/e2e/test_approval_recovery.py
tests/e2e/test_security_observability.py
```

**Implementation**

1. 建立机器可读 policy bypass matrix，记录输入、预期决定、实际后端调用次数和事件完整性。
2. 覆盖路径穿越、junction/symlink、命令变体、网络目标变更、包源变更、Skill 版本变更和 policy version drift。
3. 覆盖 8 进程/多终端的 grant 消费、撤销与旧 owner 提交；使用屏障而不是固定 sleep。
4. 将结果接入现有 safety audit，秘密泄漏、未授权调用、重复确认副作用和 stale commit 必须为 0。

**Tests**

- 新增单元、SQLite/Fake Store 契约、CLI 集成和端到端恢复测试。
- 运行已有 `test_security.py`、`test_approval_service.py`、ownership/effect recovery/security observability 全集。

**Acceptance Criteria**

- Policy matrix 全部通过；任何无法证明的外部动作进入 unknown/recovery_required。
- 旧数据库、旧 checkpoint、旧 CLI 和 provider 切换回归通过。

## 7. Implementation Order

```text
TASK-01
→ TASK-02
→ TASK-03
→ TASK-04
→ TASK-05
→ TASK-06
→ TASK-07
```

可并行部分：

- TASK-01 完成后，TASK-02 与测试 fixture 可以并行。
- TASK-03 与 TASK-06 可以在接口冻结后并行，但 CLI 只能调用已经完成的 Store Protocol。
- TASK-07 必须在 TASK-04/05 完成后执行；Sandbox 的独立攻击性测试可以提前准备，但不能把 Policy mock 结果当作真实隔离证据。

默认不切换全局策略。第一阶段先以显式 `--scope` 和 opt-in 配置运行；所有旧 Task/旧 Approval 继续按 once 语义恢复。

## 8. Verification

在项目根目录执行：

```powershell
# policy model and evaluator
.\.venv\Scripts\python.exe -m pytest tests/unit/test_policy_engine.py tests/unit/test_security.py tests/unit/test_approval_service.py -q

# persistence, CLI and recovery
.\.venv\Scripts\python.exe -m pytest tests/integration/test_policy_persistence.py tests/integration/test_session_cli.py tests/e2e/test_approval_recovery.py tests/e2e/test_security_observability.py -q

# ownership and existing execution contracts
.\.venv\Scripts\python.exe -m pytest tests/integration/test_execution_ownership.py tests/integration/test_execution_takeover_recovery.py tests/e2e/test_effect_recovery.py -q

# static checks
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m mypy src/patchloop

# full regression; use a writable basetemp outside the repository when possible
.\.venv\Scripts\python.exe -m pytest -q --basetemp <writable-temp>
```

机器可读验收报告至少包含：schema version、revision、policy/config fingerprints、规则命中、grant 生命周期、后端调用计数、重复副作用计数、未授权动作计数、秘密扫描结果和所有失败原因。缺少外部后端证据时标记 `unverified`，不能转换成 `passed`。

专项出口条件：

- 一次性、Session、resource 三种授权均能跨进程恢复，且边界精确。
- 修改参数、Policy 版本、workspace 或资源后旧授权 100% 失效。
- deny 覆盖 allow/grant；硬拒绝不能通过 CLI、resume、Provider 切换或伪造字段绕过。
- 网络、依赖、Skill 入口均有统一 Policy/Approval/Trace 记录。
- 旧数据迁移、SQLite/Fake Store 契约、CLI JSON 和完整回归通过。

### 8.1 实现与验收记录（2026-09-20）

TASK-01～07 已完成本专项控制面的实现与离线验收。首次复核发现解释器拼接参数绕过硬拒绝、
Windows 路径规则大小写漏匹配两类缺陷；修复后，原 3 个失败复现用例全部通过，并加入正式回归。

| 任务 | 已交付与验证 |
| --- | --- |
| TASK-01 | 动作/资源模型与规范化；解释器拼接、等号和短选项组合拒绝；Windows/POSIX 路径匹配语义 |
| TASK-02 | 确定性规则引擎；deny 优先于 allow/旧 grant；多资源聚合与兼容 ToolPolicy |
| TASK-03 | schema v6、SQLite/Fake Store 契约、八进程竞争；真实旧 Approval payload 的 v5→v6 迁移 |
| TASK-04 | once/session/resource 生命周期；独立进程准备、批准、恢复及跨 Session 边界；旧审批一次性消费 |
| TASK-05 | Gateway/Runtime 执行前复核、参数变更与恢复；网络/依赖/Skill 计数适配器入口 |
| TASK-06 | scope/expiry/revoke、policy list/explain、配置来源、JSON/人类输出与 README 示例 |
| TASK-07 | 27 项绕过矩阵、16 组安全审计、补充攻击用例和完整回归 |

验证结果：

- 全量回归：974 passed、2 skipped，见 [JUnit](../benchmarks/results/policy_fix_regression.xml)。
- 针对性回归：92 passed，包含原 3 个复现用例，见 [JUnit](../benchmarks/results/policy_fix_targeted.xml)。
- Ruff 通过；mypy 检查 95 个源码文件通过。
- [绕过矩阵](../benchmarks/results/policy_fix_bypass.json)：27 项通过；未授权适配器调用、重复副作用和秘密泄漏计数均为 0。
- [安全审计](../benchmarks/results/policy_fix_safety_audit.json)：16 组通过，各安全指标失败检查数均为 0。

[修复后总报告](../benchmarks/results/policy_fix_acceptance.json)关联工作区指纹、测试和逐任务结论；
[首次复核报告](../benchmarks/results/policy_review_summary.json)保留失败历史。报告中的 revision
及工作区指纹对应测试时的代码快照，本次文档状态同步不表示提交后另行执行过测试。

同目录下较早的 `policy_acceptance.json`、`policy_regression.xml`、`policy_supplemental.xml`
及对应审计报告保留为历史产物；当前结论以 `policy_fix_acceptance.json` 为准。

两项跳过分别为未显式配置的真实 Provider 验收、当前 Windows 主机不支持的符号链接测试；
Windows junction 测试已通过。网络、依赖安装和 Skill 使用计数适配器，没有执行真实外部命令。
真实外部后端、Sandbox 隔离、Skills Runtime 和第二阶段端到端验收仍不在本次通过结论之内，
外部后端证据保持 `unverified`。

## 9. Blockers

```text
None
```

本方案已固定规则优先级、授权范围、数据归属、迁移兼容、错误传播、CLI 接口和测试出口。Skills Runtime、Sandbox 真实隔离和 Workspace/Git 仍是后续专项任务；本方案只为它们提供 Policy 端口，不把它们未实现的能力假设为已完成。
