# DeepSeek V4 Flash Hard Suite 评测结果

## 运行条件

- 基线日期：2026-08-29
- 最终日期：2026-08-30
- Provider：`deepseek-v4-flash`
- 套件：`patchloop-coding-hard-v1`
- 任务数：5
- 外部验证：每项 1 个锁定隐藏测试文件，Agent 结束后才注入工作区
- 沙箱：`local`，只运行项目内受信任 fixture
- 采样：基线和最终配置各 1 轮

## 任务设计

| 任务 | 主要能力 | 隐藏测试重点 |
| --- | --- | --- |
| deep-config-merge | 递归合并与可变对象隔离 | 多层合并、输入不变性 |
| deterministic-dependency-order | 确定性拓扑排序 | 环、未知依赖、字典序 |
| sliding-window-rate-limiter | 状态型窗口算法 | key 隔离、边界、时间单调性 |
| atomic-inventory-reservation | 跨文件功能与事务语义 | 重复聚合、失败回滚、输入校验 |
| structured-log-redaction | 安全文本处理 | 大小写、多凭据、分隔符保持 |

## 优化前后

| 指标 | 基线 | 最终配置 | 变化 |
| --- | ---: | ---: | ---: |
| 独立成功率 | 2/5，40% | 5/5，100% | +60 个百分点 |
| Agent 完成率 | 2/5，40% | 5/5，100% | +60 个百分点 |
| 总步骤 | 69 | 42 | -39.13% |
| 工具调用 | 111 | 55 | -50.45% |
| 输入 Token | 430,303 | 243,509 | -43.41% |
| 输出 Token | 27,309 | 24,850 | -9.00% |
| 平均时延 | 52.98 秒 | 50.22 秒 | -5.22% |
| 估算费用 | 0.012746 美元 | 0.011476 美元 | -9.96% |

## 失败驱动优化

### 仓库被错误识别为空

基线工作区位于 `.patchloop` 目录下。只读文件枚举错误地检查完整绝对路径，只要任意祖先目录名是 `.patchloop` 就忽略文件，导致 `list_files` 和 `search_text` 返回空仓库。模型随后猜测 `src/`、`tests/` 和不存在的隐藏测试路径，三个任务因工具失败预算耗尽而没有写入代码。

修复后，忽略规则只检查相对于评测仓库的路径。代码评测工具集同时移除不必要的通用诊断命令，避免 Git 向上搜索到宿主项目。限流任务轨迹终止序号从 113 降至 46。

### 脱敏观察无法用于精确补丁

日志任务中的假凭据按安全策略被替换为 `[REDACTED]`。原有 `apply_patch` 和 `replace_text` 要求模型提供磁盘上的精确旧文本，因此脱敏后的观察无法命中真实文件。最终增加 `write_file`：只允许原子覆盖仓库内已存在的 UTF-8 文件，不创建文件，也不能越过路径边界。独立测试和变更范围判定继续阻止修改测试或额外文件。

日志任务由 28 步、37 次工具调用、失败，改善为 8 步、11 次工具调用并通过全部测试。计划工具同时兼容模型常用的 `in_progress` 状态别名，但仍在领域模型中规范化为 `running`。

## 证据与限制

原始报告：

- `benchmarks/results/code_benchmark_hard_baseline.json`
- `benchmarks/results/code_benchmark_hard_optimized.json`
- `benchmarks/results/code_benchmark_hard_redaction_fix.json`
- `benchmarks/results/code_benchmark_hard_final.json`

隐藏测试存放在仓库中，因此只保证单次 Agent 运行期间不可见，并不保证对模型训练数据或项目读者保密。当前仍是 5 个小型 Python 任务和每配置单次采样；100% 不能外推为未知大型仓库能力。下一阶段应增加跨目录重构、测试生成、依赖升级和故障恢复任务，并对最终配置重复 3～5 轮。
