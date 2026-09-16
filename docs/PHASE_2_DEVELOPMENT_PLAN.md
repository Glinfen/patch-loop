# PatchLoop 第二阶段模块开发计划

## 1. 计划目的

本计划把 [第二阶段项目目标](PHASE_2_PROJECT_GOALS.md) 映射为模块级开发工作。这里不继续拆分为类似 PCO-00、PCO-01 的细粒度任务；每个模块在正式启动时，再根据现状调查、接口设计和验收 fixture 编写自己的专项计划。

第一阶段的 [提示缓存架构重构计划](PROMPT_CACHE_ARCHITECTURE_REFACTOR_PLAN.md) 是本计划的入口工作。PCR-00 至 PCR-05 已有交付与 [验收记录](milestones/PCR_05_ACCEPTANCE.md)；真实 DeepSeek stable smoke 的未验证项按该记录保留。缓存和记忆进入维护状态，研发重心转向真实用户工作流。

截至 2026-09-15，[SRF](SESSION_RUNTIME_FOUNDATION_PLAN.md) 基础实现与本机门禁通过，但真实仓库三次试用失败、真实 Docker 未验收；[PGW](PROVIDER_GATEWAY_DEVELOPMENT_PLAN.md) 两协议与兼容路径已实现，最新离线矩阵与当前 Luna Chat profile 三次基础试用通过，完整多 Provider 真实矩阵仍未通过。[PPS](PROMPT_PREFIX_STABILITY_PLAN.md) 最新四个小型任务均完成、隐藏测试 20/20 通过，但收益门禁未通过，默认继续 legacy。

[append_only 开销优化 AOP-01～08](APPEND_ONLY_OPTIMIZATION_PLAN.md) 已于 2026-09-16 完成实现、离线 L0 门禁和有预算真实入口。本轮没有执行真实模型请求；旧 readiness 在源码提交后自动失效，后续 L1 必须先在提交后的同一修订重新生成并归档 L0 产物，再由执行者显式增加 `--execute-real`。其他模块仍只确定边界和顺序，各自启动前再编写专项方案。既有失败结果保留，真实 Docker 安全验证和不调用模型的开发测试可以继续。

## 2. 开发原则

- 以 Session 和用户工作流为主线，不以独立模块数量衡量进度。
- 先固定跨模块契约，再替换具体实现。
- 所有高风险副作用经过统一 Policy、Approval 和 Sandbox 路径。
- Provider、Skill、Tool 和 Sandbox 都通过能力声明接入，不在 Runtime 中堆积分支。
- 持久化状态必须版本化，任何中断恢复不得重复已确认副作用。
- local fallback 与隔离 Sandbox 明确区分，不扩大安全承诺。
- 每个模块同时交付单元测试、集成测试、失败场景和可观测事件。
- 真实端到端评测是阶段验收依据，合成 fixture 只用于快速回归。

## 3. 模块级开发任务

### Runtime Core 与领域契约

开发范围：

- 拆分过重的 `AgentRuntime`，明确 Session、Turn、Task、Step、Effect 和 Checkpoint 的边界。
- 建立可中断状态机和统一副作用提交协议。
- 规范用户消息、模型事件、工具事件、审批事件和恢复事件。
- 为 Provider、Approval、Skill、Sandbox 和 Workspace 提供端口接口，Runtime 只负责编排。
- 保持第一阶段任务、报告、Trace 和 checkpoint 的兼容读取路径。

模块产物：稳定领域模型、Runtime 编排接口、状态转换表、兼容策略和架构 ADR。

### Session Service

开发范围：

- 提供持久化多轮会话、用户追问、任务继续、中断、恢复、结束和会话列表。
- 管理当前目标、活动任务、待审批动作、上下文 epoch 和最近用户输入。
- 区分对话恢复、任务恢复和工具副作用恢复。
- 支持后续 Session fork 和会话迁移所需的稳定身份与事件序列，但暂不实现多 Agent。

模块产物：Session API、持久化 schema、恢复协议、CLI/TUI 接口和端到端恢复测试。

### Provider Gateway

开发范围：

- 将 DeepSeek 适配迁入统一 Provider Gateway，并新增独立协议实现验证抽象。
- 统一流式输出、reasoning、工具调用、结构化输出、取消、超时、重试、用量与缓存字段。
- 建立 Provider 能力矩阵和请求降级规则。
- 支持模型配置、凭据来源、企业兼容端点和本地服务。
- 对协议错误、限流、网络故障和部分流失败提供一致事件与恢复语义。

模块产物：Provider 协议、能力声明、Adapter 集成测试、Fake 流式服务器和兼容性矩阵。

### Approval 与 Policy Control Plane

开发范围：

- 把同步审批回调升级为持久化、可恢复的审批状态机。
- 统一工具、路径、命令、网络、依赖安装和 Skill 动作的策略判断。
- 支持一次性、Session 范围和精确资源范围授权。
- 将拒绝、修改后批准、过期和撤销作为一等事件。
- 保证非交互模式、恢复流程和 Provider 切换不能绕过策略。

模块产物：Policy 模型、Approval Store、CLI/TUI 审批流、审计事件和绕过回归测试。

### Skills Runtime

开发范围：

- 定义 Skill 清单、入口、版本、资源、依赖、触发和信任来源。
- 支持内置、用户级和项目级 Skill 的发现、安装、更新、禁用和检查。
- 实现按需选择与渐进式资源加载，控制上下文和提示缓存影响。
- 允许 Skill 使用受控脚本与模板，但所有副作用仍经过 Tool、Policy 和 Sandbox。
- 记录加载理由、资源使用和版本，防止仓库 Skill 隐式提权。

模块产物：Skill schema、注册表、安装器、加载器、信任策略、CLI 和安全测试集。

### Tool 与扩展协议

开发范围：

- 统一内置 Tool、Skill Tool 和未来 MCP Tool 的注册与能力描述。
- 增强工具结果的流式输出、截断、取消、幂等键和副作用分类。
- 为依赖安装、构建、格式化、静态分析和 Git 操作提供受控工具，而不是放宽通用 Shell。
- 改进工具参数错误反馈，使 Agent 可以修正调用而不进入无进展循环。
- 保持工具 Schema 稳定排序和 Provider 能力适配。

模块产物：扩展注册协议、工具生命周期、标准错误模型、受控开发工具集和协议测试。

### Sandbox 与 Execution Backend

开发范围：

- 编写明确威胁模型，定义 Docker Sandbox 和 local fallback 的安全承诺。
- 隔离文件、网络、环境、进程、资源和临时目录。
- 处理路径穿越、链接逃逸、TOCTOU、后台进程和清理失败。
- 将联网、依赖安装和构建脚本连接到 Approval 与 Policy。
- 建立可替换 Execution Backend，为未来远程执行保留接口。

模块产物：威胁模型、Sandbox contract、Docker 实现、local fallback 声明、攻击性测试和安全报告。

### Workspace 与 Git Workflow

开发范围：

- 建立仓库发现、dirty 状态、分支、worktree 和嵌套仓库模型。
- 支持隔离 worktree 与显式直接工作区两种运行模式。
- 区分 Agent 变更和用户原有变更，防止覆盖与误提交。
- 提供 diff review、局部接受、撤销 Agent 变更、测试后版本确认和 commit 生成。
- 为 Issue、PR 和代码审查集成提供接口边界，但不直接耦合单一托管平台。

模块产物：Workspace Manager、Git Adapter、变更所有权协议、审查 CLI/TUI 和真实仓库测试。

### Repository Intelligence 与 Context

开发范围：

- 将现有 Python AST 和轻量语义检索扩展为增量、可失效的仓库索引。
- 明确文件名、文本、符号、引用、测试、配置和依赖关系的检索接口。
- 增加至少一种额外语言或通用语言服务适配路径，用于验证边界。
- 让 Context、Memory 和 Prompt Cache 服务 Session，而不是各自维护重复状态。
- 测量无关上下文、重复读取、定位失败和索引陈旧，不只报告 Recall。

模块产物：索引生命周期、语言适配接口、上下文选择协议、失效机制和真实仓库定位评测。

### Persistence 与 Concurrency

开发范围：

- 扩展 SQLite schema 以保存 Session、Turn、Approval、Skill、Workspace 和执行租约。
- 固定单 Session 和单 Workspace 写入所有权，防止并发 run/resume 破坏状态。
- 使用事务、版本和 fencing 保护 task、checkpoint、工具结果与记忆批次。
- 建立迁移、备份、损坏检测和兼容读取策略。
- 保持存储接口可替换，但本阶段仍以本地 SQLite 为默认实现。

模块产物：版本化 schema、存储 Protocol、并发语义、迁移测试、故障恢复和一致性测试。

### CLI/TUI 与配置体验

开发范围：

- 围绕 Session 重组命令和交互界面。
- 流式展示模型、计划、工具、测试、预算和审批状态。
- 支持中途输入、中断、恢复、diff review、Skill 管理和 Provider 选择。
- 统一项目、用户、环境变量和命令行配置的优先级与来源说明。
- 保留稳定的非交互模式、退出码和 JSON 输出供 CI 使用。

模块产物：交互式终端入口、非交互 CLI、配置 schema、错误恢复提示和可用性测试。

### Observability 与 Evaluation

开发范围：

- 统一 Session、Provider、Approval、Skill、Sandbox、Workspace 和工具事件。
- 为关键链路提供稳定 Trace ID、事件版本、指标和脱敏策略。
- 建立不少于 30 项真实端到端任务，覆盖多个仓库和任务类型。
- 增加多次采样、Provider 对照、恢复、审批和 Sandbox 对抗评测。
- 输出首次通过率、工具成功率、恢复成功率、无效动作、上下文质量、成本和时延。
- 根据高频真实失败安排优化，不根据单个成功案例扩大结论。

模块产物：事件协议、metrics/replay 更新、真实任务清单、评测运行器、机器报告和阶段验收报告。

### Packaging、文档与发布

开发范围：

- 提供可重复安装、配置检查和一条命令启动的本地演示路径。
- 编写架构图、Session 时序图、权限与 Sandbox 说明、Provider 和 Skill 开发指南。
- 生成评测图表、演示脚本、技术文章素材和中英文项目说明。
- 建立版本、变更日志、兼容策略和发布检查清单。
- 所有公开结果链接到可复现配置与机器产物。

模块产物：发行包、Quickstart、架构与扩展文档、演示材料、评测可视化和发布记录。

## 4. 跨模块契约

各模块的专项计划必须遵守以下共享边界：

| 契约 | 要求 |
| --- | --- |
| Session 所有权 | Session 是用户交互边界，Task 是其中一次目标执行，Turn 是一次用户或 Agent 交互 |
| 副作用 | 文件写入、命令、网络、安装和 Git 修改必须经过 Tool、Policy、Approval、Sandbox 与事件记录 |
| 能力声明 | Provider、Tool、Skill 和 Sandbox 先声明能力，Runtime 不猜测支持情况 |
| 持久化 | 可恢复状态使用版本化 schema，不依赖 Python 对象地址、pickle 或进程内单例 |
| 安全 | 仓库、工具输出和项目 Skill 默认是不可信内容，不得直接成为高权限指令 |
| 可观测性 | 每个外部动作都有稳定身份、输入摘要、结果、耗时、权限决策和脱敏事件 |
| 兼容性 | 第一阶段 Task、checkpoint、Trace 和 CLI 数据有明确读取或迁移路径 |
| 测试 | 单元测试验证局部契约，集成测试验证边界，真实任务验证最终价值 |

## 5. 模块依赖与建议顺序

模块专项计划建议按以下依赖关系启动，但允许在契约稳定后并行实施：

```text
Runtime Core / 领域契约
        │
        ├── Session Service ── Persistence / Concurrency
        ├── Provider Gateway
        └── Approval / Policy
                  │
                  ├── Skills Runtime ── Tool 扩展协议
                  └── Sandbox / Execution Backend
                               │
Workspace / Git ── Repository Intelligence / Context
                               │
                       CLI/TUI 集成体验
                               │
                   Observability / Evaluation
                               │
                     Packaging / 文档 / 发布
```

优先级判断以“是否阻塞真实 Session 闭环”为准。pilot-04 已暴露压缩开销，并在本轮代码调研中确认工作记忆截断导致字段增量失效；用户因此将 append_only 优化指定为真实模型评测的前置任务。该专项有明确离线出口，不扩展为无限期算法研究；模块设计和离线开发继续推进。多 Agent 和 Web UI 仍不进入本阶段关键路径。

SRF 和 PGW 已有实现继续作为公共基础，不重新开发同名状态机、request journal 或存储。Observability 和故障评测贯穿每一步；后续模块必须复用 single writer、fencing 和审批路径。

### 5.1 当前开发批次与模块顺序

下表是模块级路线，不作为低成本执行模型的完整开发指令；只有 AOP 已在本轮给出详细任务、接口和验收。其余模块启动前按 PLANNING_GUIDE 形成专项计划。

| 顺序 / 模块 | 当前基础与缺口 | 下一交付范围 | 依赖与出口 |
| --- | --- | --- | --- |
| 1. AOP 开销优化 | AOP-01～08 与离线 L0 已完成；真实收益尚未验证 | 后续仅执行显式、有预算的 L1，两场景各一对 | L1 异常立即停批；默认布局不自动切换 |
| 2. Approval / Policy 扩展 | 已有持久化批准/拒绝和精确动作绑定 | 限时/Session/资源范围授权、修改参数后重新审批、网络与依赖动作统一入口 | 复用 ApprovalService 与 Effect；任何新授权都需可撤销、可审计，先离线绕过测试 |
| 3. Sandbox 隔离验收与补齐 | 已有 Docker/local 后端和清理；真实 Docker 未通过 | 威胁模型、真实后端攻击性验证、网络/安装策略、跨平台路径及进程清理 | 可与 AOP/Policy 的独立部分并行；模型可替身，不冒充真实 Docker 隔离证据 |
| 4. Workspace / Git 工作流 | FileChangeTracker 保存初始内容并输出 diff，尚无完整 Workspace Manager | dirty 基线、Session worktree、用户/Agent 变更归属、局部审查撤销、验证版本绑定提交 | 依赖 SRF 所有权与 Policy；必须先证明不会覆盖或误提交用户修改 |
| 5. Skills Runtime | 无完整发现/安装/加载模块 | 元数据/版本/信任来源、按需加载、受控脚本与资源、选择和执行审计 | 依赖 Policy 与 Sandbox 契约；资源加载追加到当前会话，权限不来自 Skill 文本 |
| 6. CLI/TUI 集成 | 已有 Session CLI、审批和恢复展示 | Provider/权限/Sandbox/Skill/工作区可见，统一中途输入、diff、审批与错误恢复 | 集成上列模块；陌生开发者 10 分钟启动首会话的可用性验收 |
| 7. 真实评测与发布 | 已有 runner、首个真实仓库任务和小 fixture | 固定至少 5 仓库/30 任务、每项至少 3 次，独立测试与状态判分、费用/时延/安全统计 | append_only 前置优化与产品工作流就绪；全场景和量化目标同时通过 |

Tool 扩展、索引/Context、持久化和 Trace 随各模块的实际需求配套演进，不独立开展与当前工作流无关的重构。Workspace 与 Skills 的专项设计可在 Policy 契约明确后并行，实施顺序优先保护真实仓库变更。

### 5.2 真实模型评测恢复条件

```text
AOP 实现与离线 L0 → 有预算的两场景各一对 L1 → 达标后 PPS 三对 L2
                                        │
Policy + Sandbox + Workspace + Skills + CLI 集成
                                        ↓
                         SRF 真实流程 / Provider 完整矩阵
                                        ↓
                         第二阶段 ≥30 项 × ≥3 次 L3
```

- AOP-08 实施轮只运行 Fake/loopback 与离线门禁，模型请求数为 0；L1/L2/L3 均未执行。
- L0 只能证明代码结构和固定动作开销改善，不能证明真实 warm 命中或模型质量。详细门槛见 AOP 第 4.7 节。
- L1/L2 未达标则不扩量；新的失败、取消请求与未知费用必须记录，不能选择性补跑最佳结果。
- L1 已锁定 2026-09-16 OpenAI 公布的 `gpt-5.6-luna` 非零价格；批次总预算需在启动前固定，源码/fixture/Provider 价格策略漂移均需重验。该估算口径仍不能替代供应商最终账单。
- AOP 的 70% warm 目标属于专项收益门禁；第二阶段量化目标保持原文。有限真实验证的启动条件、PPS 默认发布条件和第二阶段结束条件分别记录，避免互相替代。
- L1 的可执行命令、预检和恢复方式统一记录在 README；默认命令不加载凭据，只有 `--execute-real` 可以创建真实任务。partial 批次保留原证据并停止扩量，不能在原目录选择性续跑为“完整批次”。

## 6. 阶段出口

第二阶段不是所有模块代码合并后自动完成。必须同时满足：

- [第二阶段项目目标](PHASE_2_PROJECT_GOALS.md) 的最终验收场景完整通过。
- 量化目标由固定、机器可读、可重复的端到端评测支持。
- Session 重启恢复、Approval、Sandbox、Skill 和 Git 工作区经过失败与对抗场景验证。
- 至少两个独立 Provider 协议族运行同一组核心工具契约测试。
- 文档准确区分已实现能力、实验结果、限制和未来计划。

每个模块启动前再新增对应专项开发计划，包含现状调查、ADR、任务拆分、迁移方案、自动化验收和回退策略。本总计划只维护模块边界、依赖关系和第二阶段总体方向。
