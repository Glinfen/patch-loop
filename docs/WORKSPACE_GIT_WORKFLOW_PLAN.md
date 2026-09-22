# Workspace / Git 工作流专项开发方案

## 1. Problem

### Current Problem

PatchLoop 目前已经有以下基础能力：

- `ToolContext` 持有 `FileChangeTracker`，能够为通过文件工具产生的文本修改保存进程内基线并生成 unified diff；
- `ExecutionOwnershipManager` 能够按 Session → Task → Workspace 顺序取得 workspace writer lease，并用规范化路径生成 workspace ID；
- `Session` 已经绑定 `workspace_ref`，SQLite 已经保存 Session、Task、执行租约和 workspace lease；
- Git 相关命令目前只允许有限的只读诊断，`normalize_command` 明确拒绝 `commit`、`push`、`pull`、`clone`、`remote`、`submodule` 等持久化或发布操作。

但是，当前没有完整的 Workspace Manager，也没有持久化的 Git 基线、工作树模式、变更归属或提交前版本确认。`FileChangeTracker` 只知道当前进程捕获过的文本文件，无法区分用户在任务开始前已有的 dirty 变更和 Agent 后续变更，也无法在 Session 重启后继续审查或安全撤销。

### Desired Behavior

每个可写 Session 都通过一个持久化的 `WorkspaceHandle` 绑定到明确的仓库和运行模式：

1. `direct` 模式在用户当前工作区执行，但任务开始前冻结 dirty 基线；
2. `worktree` 模式从指定 revision 创建隔离 Git worktree，源工作区不被 Agent 修改；
3. 所有变更按用户原有、Agent、混合和未识别状态分类；
4. diff review、局部接受、局部撤销和验证后提交都使用稳定的 workspace 身份和版本前置条件；
5. 用户已有修改不能被覆盖，用户已有 staged 或 untracked 文件不能被隐式提交；
6. Git 写操作通过专用 Adapter、Policy 和 Approval 执行，不通过通用 Shell 绕过控制面。

### Scope

- 仓库发现、规范化路径、Git 状态和 revision 读取；
- direct/worktree 两种 Workspace 模式；
- dirty 基线、变更归属、diff review、局部接受和安全撤销；
- 测试后版本确认、提交前检查和显式 commit 生成；
- Workspace 持久化、Session 重启恢复、CLI JSON 输出和审计事件；
- 本地真实 Git 仓库的单元、集成和端到端测试。

### Non-Goals

- GitHub、GitLab 或其他托管平台的 PR 创建、评论和 push；
- 自动 stash、自动解决 merge conflict 或自动覆盖用户修改；
- 多 Agent 协同编辑同一个 worktree；
- 将 Git 操作重新开放为任意 Shell；
- 本任务不完成 Sandbox 的真实隔离验收。执行测试是否在 Docker 中运行仍由 Sandbox 模块负责。

## 2. Repository Investigation

| File / Symbol | Current Behavior | Required Change |
| --- | --- | --- |
| `src/patchloop/changes.py::FileChangeTracker` | 保存进程内的文本基线并生成 diff；只跟踪被当前 `ToolContext` 捕获的路径 | 增加可持久化基线和变更归属适配层；保留现有工具 diff 兼容性 |
| `src/patchloop/tools/base.py::ToolContext` | 每个 Context 新建 `FileChangeTracker`，只绑定 repository path | 从 Workspace Manager 注入 workspace handle、baseline 和 ownership ledger |
| `src/patchloop/execution/ownership.py::ExecutionOwnershipManager` | 管理 Session、Task、Workspace writer lease | Workspace 写入、review、revert、commit 前复用 `assert_owned`；不新增第二套 lease |
| `src/patchloop/execution/policy.py::PolicyAction.GIT` | 已有 Git action，但通用 `normalize_command` 拒绝持久化 Git 命令 | 增加专用 Workspace/Git action descriptor；保持通用 Shell 的拒绝规则 |
| `src/patchloop/session/models.py::Session` | Session 只保存 `workspace_ref` | 增加可选的 `workspace_id` 或通过 Store 关联 Workspace；旧 Session 延迟创建 direct workspace |
| `src/patchloop/persistence.py` | 已有 Session、Task、lease 和事件 schema，当前 runtime schema 为 6 | 新增 Workspace、变更归属、验证记录表，迁移到 schema 7 |
| `src/patchloop/cli.py::WorkspaceServices` | CLI 服务只暴露 Session、Approval、Recovery | 增加 WorkspaceService/GitAdapter，提供稳定 JSON 命令 |
| `src/patchloop/tools/write.py` | 文件工具在写入前捕获原始内容 | 写入成功后向 ownership ledger 记录 effect/path/target digest，失败不登记完成变更 |
| `tests/e2e/test_workspace_ownership.py` | 已验证同一 workspace 只有一个 writer | 增加 dirty baseline、模式隔离和提交边界测试 |

### Current Flow

~~~text
Session/Task
→ ToolContext(repository)
→ FileChangeTracker.capture
→ Write Tool
→ in-memory diff
~~~

目标流程：

~~~text
Session
→ WorkspaceService.open(mode, base_revision)
→ GitAdapter.discover + capture baseline
→ ExecutionOwnershipManager.assert_owned
→ Policy/Approval descriptor
→ Tool / Git operation
→ ChangeOwnershipLedger
→ review / verify / revert / commit
→ persisted audit event
~~~

## 3. External Research

| Project | Relevant Design | Adopt | Do Not Adopt |
| --- | --- | --- | --- |
| [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage) | Git 相关工具可以按具体命令授予或拒绝；`git diff` 等只读命令可以单独列入允许列表 | 将 Git 能力拆成稳定的 action/resource，review 和 commit 使用显式权限 | 不把 `Bash(git:*)` 或任意 Shell 作为 Workspace API |
| [Codex Worktrees](https://developers.openai.com/en/docs/environments/git-worktrees) 与 [Code Review](https://developers.openai.com/zh-Hans/docs/code-review) | worktree 从选定分支创建；review 按 uncommitted、staged、commit、branch 等范围查看仓库状态，且 review 不修改工作树 | 采用稳定的 review scope、隔离 worktree 和源工作区不变的承诺 | 不在本任务中实现远程任务迁移、云执行或托管平台集成 |
| [Qwen Code Code Review](https://qwenlm.github.io/qwen-code-docs/en/users/features/code-review/) | PR review 使用临时 worktree、lease、自动清理和可恢复的 review 状态；`/rewind` 按操作点恢复文件 | 采用可追踪的 worktree 生命周期、清理失败状态和可恢复 review 记录 | 不复制多 Agent review pipeline，也不把 review 结论当作 commit 授权 |
| [OpenCode Permissions](https://opencode.ai/v2/docs/permissions) | 权限以 action/resource/effect 规则表达；Git 命令的参数会影响匹配范围 | 让 `workspace.review`、`workspace.revert`、`workspace.commit` 使用规范化资源和 fail-closed 匹配 | 不使用宽泛的全局 Git allow 规则 |

### Lessons for PatchLoop

1. Worktree 是隔离变更的边界，但它必须有稳定的 owner、路径和清理状态，不能只是一次临时 `git worktree add`。
2. Review 的输入必须是 Git 状态和明确的 diff scope，而不是只展示 Agent 本轮写过的文件。
3. Git 写操作应以 action/resource 进入 Policy；commit 的资源范围、源 revision 和 workspace 必须参与 Approval 指纹。
4. 恢复和撤销必须保留中断前的事实，遇到用户后来修改过的文件时停止并报告冲突，不能静默覆盖。

## 4. Design

### Selected Approach

新增 `patchloop.workspace` 端口层，由 `WorkspaceService` 编排 Workspace 生命周期，`GitAdapter` 负责受控 Git 命令和状态解析，`WorkspaceStore` 负责 SQLite 持久化，`ChangeOwnershipLedger` 负责每个路径的用户/Agent 归属。

Workspace 以 Session 为长期边界，以当前 execution 的 workspace writer lease 为写入前置条件。Runtime 不直接调用 Git；所有 Git 写操作都由 WorkspaceService 产生明确的 Policy descriptor，并使用现有 ApprovalService 和事件 Journal。

### Key Decisions

1. **Workspace 模式**
   - `direct`：effective root 就是用户选择的仓库；任务开始时保存 dirty 基线，禁止 commit 混入未识别或用户 staged 变更。
   - `worktree`：默认要求源仓库无 dirty 变更，从固定 `base_revision` 创建 `.patchloop/worktrees/<workspace_id>` 下的 worktree；源工作区保持原状态。
   - 第一版不自动 stash，也不把 dirty 源仓库复制到 worktree。用户需要保留 dirty 内容时使用 `direct` 模式。

2. **仓库身份和嵌套仓库**
   - `GitAdapter.discover` 从用户指定目录向上解析 `.git` 文件或目录，并返回 `repository_root`、`git_dir`、`common_dir`、`head_revision`、branch/detached 状态。
   - 嵌套 Git 仓库必须由调用方显式选择；父仓库的 Workspace 不把嵌套仓库内容当作可独立提交的普通目录。
   - `.git`、`.patchloop` 和仓库外路径不能成为普通文件变更 selector。

3. **基线和变更归属**
   - 基线保存 `HEAD`、branch/ref、index/worktree 状态、未跟踪文件清单、每个路径的 digest 和全局 status digest。
   - direct 模式中基线已有路径标记为 `user_preexisting`；Agent 第一次写入后记录 `agent_expected_digest`。
   - 如果同一文件在 Agent 修改后再次被用户或外部进程修改，路径标记为 `mixed`；撤销和 commit 对 mixed 路径 fail-closed。
   - worktree 模式中基线默认为 clean HEAD，worktree 内产生的变更初始归属为 Agent。

4. **撤销策略**
   - Agent-only 路径只有在当前 digest 等于最近一次 Agent 预期 digest 时才能自动撤销。
   - Agent 新建文件撤销时删除文件；Agent 修改原有文件恢复基线内容；Agent 删除文件恢复原文件。
   - 用户原有或 mixed 路径只生成冲突报告，不执行覆盖、checkout 或 reset。

5. **Review 与 commit**
   - review scope 固定为 `all`、`agent`、`user`、`mixed`，默认展示 `agent + mixed` 并显式标出用户基线。
   - `verify` 记录执行根目录、HEAD、工作树 digest、选定测试命令摘要和结果摘要。
   - commit 前必须满足：持有 workspace writer lease、源 HEAD 未变化、无 mixed 路径、Policy/Approval 仍匹配、最近一次 verify 未过期。
   - direct 模式使用临时 Git index，只把已接受的 Agent 路径加入提交，绝不修改用户当前 index；如果无法精确构造临时 index，则拒绝 commit。
   - worktree 模式可以创建 `patchloop/<session_id>` 分支或输出 detached commit；本任务不 push。

6. **安全和失败语义**
   - Git Adapter 只接受参数数组，禁止 shell 拼接、重定向和环境变量注入。
   - Git 命令超时、仓库状态变化、worktree 清理失败、revision 不一致和 lease 丢失均返回结构化错误，并将 Workspace 置为 `recovery_required`。
   - 不自动删除用户目录或用户修改；清理只允许删除由 WorkspaceManager 创建且身份匹配的 worktree。

### Target Flow

~~~text
workspace open
→ discover repository and nested-repo boundary
→ acquire workspace writer lease
→ capture HEAD/index/worktree baseline
→ create or bind direct/worktree effective root
→ create Session-bound WorkspaceHandle
→ execute Tool/Git action through Policy + Approval
→ record path ownership and event
→ review diff
→ verify exact revision and digest
→ accept/revert selected Agent paths
→ prepare or create commit
→ release lease and clean owned worktree
~~~

## 5. Change Map

| File | Symbol | Change |
| --- | --- | --- |
| `src/patchloop/workspace/models.py` | new models | `WorkspaceMode`、`WorkspaceStatus`、`RepositoryInfo`、`WorkspaceHandle`、`WorkspaceBaseline`、`ChangeRecord`、`VerificationRecord` |
| `src/patchloop/workspace/git.py` | `GitAdapter` | Git discovery、status porcelain v2 parsing、revision/ref/worktree operations、temporary index 和 typed errors |
| `src/patchloop/workspace/service.py` | `WorkspaceService` | open/close/status/diff/accept/revert/verify/commit lifecycle and lease checks |
| `src/patchloop/workspace/ownership.py` | `ChangeOwnershipLedger` | baseline classification、effect/path association、digest comparison and mixed-change detection |
| `src/patchloop/workspace/__init__.py` | public ports | export stable interfaces and errors |
| `src/patchloop/changes.py` | `FileChangeTracker` | 保留现有 API；增加持久化 baseline 导入、target digest 和 binary/unknown 状态适配 |
| `src/patchloop/tools/base.py` | `ToolContext` | 接收可选 `WorkspaceHandle`/ledger；旧调用保持 direct 兼容 |
| `src/patchloop/tools/write.py` | write tools | mutation 成功后写入 ownership ledger；不改变现有原子写语义 |
| `src/patchloop/execution/policy.py` | Git/workspace descriptors | 增加 workspace-specific action/resource normalization，保持通用 Git 持久化命令拒绝 |
| `src/patchloop/persistence.py` | schema 7 | 增加 `workspaces`、`workspace_changes`、`workspace_verifications` 及索引和迁移 |
| `src/patchloop/persistence_contracts.py` | `SessionStore`/Workspace protocol | 增加 Workspace CRUD、变更记录、verification 和 commit projection 接口 |
| `src/patchloop/cli.py` | `workspace_app`, Session options | 增加 `workspace status/show/diff/accept/revert/verify/commit/close` 和 `--workspace-mode` |
| `tests/unit/test_workspace_git.py` | new | parser、路径、revision、temporary index 和命令安全测试 |
| `tests/unit/test_workspace_ownership.py` | new | 用户/Agent/mixed 分类和安全撤销测试 |
| `tests/integration/test_workspace_service.py` | new | SQLite migration、Session restart、lease fencing 和 worktree lifecycle |
| `tests/e2e/test_workspace_workflow.py` | new | 真实临时 Git 仓库的 direct/worktree/review/verify/commit 流程 |
| `tests/e2e/test_workspace_ownership.py` | existing | 补充 dirty baseline、旧 owner 和不同 workspace 场景 |

## 6. Implementation Tasks

### TASK-01: Git Adapter 与仓库模型

**Goal**

提供无 Shell 拼接、可解析、可测试的 Git 端口，得到稳定的仓库身份和工作树状态。

**Files / Symbols**

~~~text
src/patchloop/workspace/models.py
src/patchloop/workspace/git.py
tests/unit/test_workspace_git.py
~~~

**Implementation**

1. 定义 repository root、git dir、common dir、HEAD、branch、detached、index/worktree dirty、untracked 和 nested repository 模型。
2. 使用 `git rev-parse`、`git status --porcelain=v2 -z --untracked-files=all`、`git worktree list --porcelain` 的参数数组调用；每个调用设置 timeout 并保留脱敏 stderr。
3. 解析 quoted path、rename/copy、untracked、ignored 和 submodule 状态；解析失败返回 typed `GitParseError`。
4. 为 Windows 大小写、路径分隔符、`.git` gitfile 和 worktree admin 路径增加测试。

**Interface Changes**

~~~python
class GitAdapter(Protocol):
    def discover(self, path: Path) -> RepositoryInfo: ...
    def status(self, repository: Path) -> RepositoryState: ...
    def revision(self, repository: Path) -> RevisionRef: ...
    def create_worktree(
        self, repository: Path, target: Path, revision: str, branch: str | None
    ) -> WorktreeInfo: ...
    def remove_worktree(self, repository: Path, target: Path) -> None: ...
~~~

**Tests**

- clean、dirty、detached HEAD、untracked、rename/delete 状态解析；
- Git 不存在、非仓库、超时和非零退出；
- nested repo 和 Windows path normalization；
- 命令参数不能注入 Shell 元字符。

**Acceptance Criteria**

- 同一仓库的路径别名得到同一 repository identity；
- 状态解析不丢失用户 staged、unstaged 和 untracked 信息；
- 不执行任何通用 Shell 或发布 Git 命令。

### TASK-02: Workspace 持久化与 Session 关联

**Goal**

让 Workspace 在进程重启后仍能恢复模式、基线、effective root 和当前状态。

**Files / Symbols**

~~~text
src/patchloop/persistence.py
src/patchloop/persistence_contracts.py
src/patchloop/workspace/models.py
tests/integration/test_workspace_service.py
tests/unit/test_storage.py
~~~

**Implementation**

1. 将 runtime schema 从 6 迁移到 7，增加 Workspace、path change 和 verification 表；payload JSON 继续作为完整模型来源，增加 workspace/session/status 索引。
2. 保存 repository root、effective root、mode、base revision、baseline digest、worktree path、lease owner、status、cleanup 状态和 timestamps。
3. 保存每个 path 的 baseline digest、当前 digest、Agent expected digest、归属、effect IDs 和 review 状态。
4. 旧 Session 没有 Workspace 记录时，在第一次打开时创建 `legacy_direct` Workspace，不修改旧 Task/checkpoint payload。

**Interface Changes**

~~~python
class WorkspaceStore(Protocol):
    def create_workspace(self, handle: WorkspaceHandle) -> WorkspaceHandle: ...
    def get_workspace(self, workspace_id: str) -> WorkspaceHandle: ...
    def save_baseline(self, baseline: WorkspaceBaseline) -> None: ...
    def save_change(self, change: ChangeRecord) -> None: ...
    def save_verification(self, verification: VerificationRecord) -> None: ...
~~~

**Tests**

- fresh v7 schema 和 v6 → v7 migration；
- 旧 Session lazy direct workspace；
- payload round-trip、索引查询、重复写幂等；
- 进程重启后继续 review/revert/verify。

**Acceptance Criteria**

- migration 不丢失 Session、Task、Effect、Approval、lease 和事件；
- Workspace 状态变化具有唯一版本和事件顺序；
- 恢复时发现 effective root、HEAD 或 lease 不匹配会进入 recovery，而不是继续写入。

### TASK-03: Workspace 生命周期与 worktree 管理

**Goal**

实现 direct/worktree 两种模式，并复用现有 workspace writer lease。

**Files / Symbols**

~~~text
src/patchloop/workspace/service.py
src/patchloop/execution/ownership.py
tests/integration/test_workspace_service.py
tests/e2e/test_workspace_workflow.py
~~~

**Implementation**

1. `open` 先 discover、校验 Session workspace、取得 writer lease，再捕获 baseline；顺序失败时逆序释放。
2. direct 模式绑定源目录并记录 dirty 基线；worktree 模式要求源仓库 clean，使用固定 base revision 创建受管路径。
3. worktree 记录 owner、session、lease generation、创建命令摘要和 cleanup 状态；只允许删除身份匹配的受管 worktree。
4. `close`、Session cancel、parent crash recovery 和 lease takeover 都调用同一个清理路径；清理失败将 Workspace 标为 recovery_required。

**Interface Changes**

~~~python
class WorkspaceService:
    def open(
        self,
        session_id: str,
        repository: Path,
        *,
        mode: WorkspaceMode,
        base_revision: str | None = None,
    ) -> WorkspaceHandle: ...
    def close(self, workspace_id: str) -> None: ...
    def status(self, workspace_id: str) -> WorkspaceSnapshot: ...
~~~

**Tests**

- direct clean/dirty open；
- dirty 源仓库拒绝 worktree open；
- source worktree 在 Agent 写入后保持原状态；
- worktree cleanup、重复 close、owner mismatch、lease takeover 和进程重启；
- Windows 路径和 Git worktree gitfile。

**Acceptance Criteria**

- 同一 Workspace 只有一个 writer；不同 Workspace 可以并行；
- worktree 源仓库没有 Agent 写入、切 branch 或隐式 stash；
- 任何无法确认 owner 的目录都不被自动删除。

### TASK-04: 变更归属、diff review 与局部撤销

**Goal**

把用户原有变更和 Agent 变更分开，并提供 fail-closed 的 review/revert。

**Files / Symbols**

~~~text
src/patchloop/workspace/ownership.py
src/patchloop/changes.py
src/patchloop/tools/base.py
src/patchloop/tools/write.py
tests/unit/test_workspace_ownership.py
~~~

**Implementation**

1. 从 Git baseline 和 FileChangeTracker effect 记录构建 path ledger；支持 create/modify/delete、untracked、binary/unknown。
2. 计算 `all`、`agent`、`user`、`mixed` diff，并为每个 path 输出 baseline/current/expected digest。
3. 实现 `accept(paths)` 只改变 review 状态；实现 `revert(paths)` 时检查 current digest，mixed 或 digest 不匹配返回冲突，不执行写入。
4. 对 binary 或无法读取的文件仅允许 Git-level review/revert，禁止使用文本快照覆盖。

**Interface Changes**

~~~python
class ChangeOwnershipLedger:
    def record_effect(
        self, effect_id: str, path: Path, before: FileState, after: FileState
    ) -> None: ...
    def diff(self, scope: DiffScope) -> WorkspaceDiff: ...
    def revert(self, paths: Sequence[str]) -> RevertReport: ...
~~~

**Tests**

- 用户已有修改与 Agent 修改不同文件；
- 同一文件后续发生用户修改，正确分类为 mixed；
- Agent-only 新建、修改、删除可撤销；
- digest 变化、外部编辑、binary 文件和 symlink/junction 场景拒绝覆盖；
- Session 重启后 ledger 与 diff 一致。

**Acceptance Criteria**

- 用户基线内容在所有 Agent review/revert 操作后保持字节级不变；
- mixed 路径永远不会被自动恢复或提交；
- diff scope 能解释每个路径为何属于 user、agent 或 mixed。

### TASK-05: Policy、Approval、验证和 commit

**Goal**

把高风险 Git 操作接入现有控制面，确保提交只包含已审查且仍然匹配的 Agent 变更。

**Files / Symbols**

~~~text
src/patchloop/execution/policy.py
src/patchloop/workspace/service.py
src/patchloop/execution/approvals.py
tests/e2e/test_workspace_workflow.py
tests/unit/test_policy_engine.py
~~~

**Implementation**

1. 为 workspace open、revert、verify、commit 生成 action/resource descriptor；资源包含 workspace ID、effective root、path selector、base revision 和 current diff digest。
2. read-only status/diff 可按现有低风险策略执行；revert、worktree create/delete 和 commit 必须按 Policy/Approval 处理。
3. `verify` 保存测试命令摘要、返回状态、HEAD、worktree digest 和 policy/config version；任何变更后使旧 verification 失效。
4. direct commit 使用临时 `GIT_INDEX_FILE`，只加入 accepted Agent paths；worktree commit 使用受管 worktree 分支或 detached commit。
5. commit 前重新检查 lease、HEAD、mixed 状态、approval grant、verification 和 resource digest；不匹配则返回 stale/approval-required。

**Interface Changes**

~~~python
class WorkspaceService:
    def verify(self, workspace_id: str, result: VerificationInput) -> VerificationRecord: ...
    def prepare_commit(self, workspace_id: str, message: str) -> CommitPlan: ...
    def commit(self, workspace_id: str, plan_id: str) -> CommitResult: ...
~~~

**Tests**

- deny 时 Git backend 不被调用；
- 参数、workspace、HEAD、diff 或 policy version 改变后旧 approval 失效；
- direct 临时 index 不改变用户 staged/unstaged 状态；
- 用户 staged 文件和 mixed 文件不会进入 commit；
- source HEAD 移动、lease 丢失和 verification 过期时拒绝 commit；
- commit/revert/verify 事件可审计且不泄漏凭据。

**Acceptance Criteria**

- 没有通用 Shell 绕过路径；
- 每个 commit 都能回溯到 workspace、base revision、accepted paths、verification 和 approval；
- 未经用户显式批准，系统不执行 commit、branch 修改或 worktree 删除。

### TASK-06: CLI、审查输出和回归验收

**Goal**

提供稳定的非交互 CLI 和真实临时仓库验收矩阵。

**Files / Symbols**

~~~text
src/patchloop/cli.py
tests/integration/test_workspace_cli.py
tests/e2e/test_workspace_workflow.py
benchmarks/results/workspace_git_acceptance.json
~~~

**Implementation**

1. 增加 `workspace status/show/diff/accept/revert/verify/commit/close` 命令，所有命令支持 `--repo`、`--json` 和稳定错误码。
2. Session 创建/启动增加 `--workspace-mode direct|worktree` 和明确的 base revision 选项。
3. JSON 输出包含 workspace ID、effective root、mode、HEAD、dirty summary、path ownership、verification 和 recovery advice。
4. 生成机器可读验收报告；记录 Git 版本、OS、测试仓库 revision、每个 case 的 expected/observed/result 和未验证原因。

**Tests**

- CLI JSON schema、human output、错误码和重复命令幂等；
- direct dirty baseline、worktree isolation、review/revert/verify/commit 全流程；
- Session restart、old owner、lease takeover、cleanup failure；
- 现有 Session、Approval、Policy、ownership 和全量回归。

**Acceptance Criteria**

- 陌生开发者可以只用 CLI 查看当前 Workspace、Agent diff 和待审批操作；
- 报告明确区分 passed、failed、skipped 和 unverified；
- 既有测试不因新增 Workspace schema 或 CLI 破坏。

## 7. Implementation Order

~~~text
TASK-01 Git Adapter 与模型
→ TASK-02 Workspace 持久化
→ TASK-03 Workspace 生命周期
→ TASK-04 变更归属与 review/revert
→ TASK-05 Policy/Approval、verify、commit
→ TASK-06 CLI 与端到端验收
~~~

TASK-01 完成模型和错误协议后，TASK-02 可以先落地迁移；TASK-04 的 ownership ledger 可以与 TASK-03 的 worktree 清理实现并行，但在合并前必须使用同一个 WorkspaceHandle 和 lease contract。TASK-05 依赖 TASK-04 的 path digest，TASK-06 在每个任务完成后持续补充，不应等到最后才首次测试。

## 8. Verification

定向测试：

~~~bash
python -m pytest -q \
  tests/unit/test_workspace_git.py \
  tests/unit/test_workspace_ownership.py \
  tests/integration/test_workspace_service.py \
  tests/integration/test_workspace_cli.py \
  tests/e2e/test_workspace_workflow.py \
  tests/e2e/test_workspace_ownership.py
~~~

质量门禁：

~~~bash
ruff check src tests
mypy src/patchloop
git diff --check
python -m pytest -q
~~~

真实临时 Git 仓库至少覆盖：

1. clean direct workspace；
2. dirty direct workspace，用户修改与 Agent 修改不同文件；
3. 同一文件后续发生用户修改，自动撤销被拒绝；
4. clean source 创建 worktree，Agent 修改不影响 source；
5. source HEAD 移动、lease 丢失、进程重启和 worktree cleanup；
6. accepted Agent changes commit，用户 staged/untracked/mixed changes 不进入 commit；
7. nested repository、detached HEAD、Windows path/case 规则。

通过标准：

- 用户原有文件和 index 在 Workspace 操作前后保持一致；
- source worktree 在 isolated 模式下保持一致；
- 无未授权 Git 写操作、无 mixed 路径被自动覆盖、无错误提交用户文件；
- 所有 commit 都有可验证的 base revision、diff digest、verification 和 approval 证据；
- 所有失败、清理失败和未验证环境都写入机器报告。

## 9. Blockers

~~~text
None
~~~

真实 Sandbox 环境不是本专项的实现前置条件。Workspace/Git 的本地真实 Git 测试可以先执行；涉及 Docker 执行的测试继续由 Sandbox 专项单独标记为未验证。
