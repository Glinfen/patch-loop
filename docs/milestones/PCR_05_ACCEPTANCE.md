# PCR-05 验收记录：依赖治理、回归与文档收口

## 交付内容

- 新增 AST 依赖边界测试，禁止 `memory`/`providers` 依赖 `prompt_cache`，并禁止 `prompt_cache` 依赖 `runtime`、`persistence` 或 `observability`。
- 新增 ADR-019，记录 `src` layout、Prompt Cache 包边界、纯协调器、依赖方向和兼容重导出策略。
- 新增 Prompt Cache 模块说明，明确推荐导入路径和 Runtime 生命周期。
- README 和提示缓存优化计划中的架构重构入口及当前状态已存在于工作区；第二阶段模块计划已将 PCR 作为入口。本次保留这些用户既有文档改动，不覆盖或重新提交它们。

## 确定性回归

PCR-00 基线仍由 `tests/unit/test_pcr00_behavior_baseline.py` 逐字节校验。当前 `CacheBenchmarkRunner(repeats=3)` 结果与基线一致：

- fixture fingerprint：`6d666620d936707fb35f2e8cbd4ce8b2c5f8458d93bd04bec703b4198a777983`
- deterministic fingerprint：`bc64bd00c5019898cfd866a10d4271d8032960986586a280fba792105d5f0613`
- 六个矩阵变体均保持任务正确率 `1.0`，full optimization 平均缓存命中率 `0.7681976591267755`。

## DeepSeek stable 场景

本次环境未提供 `DEEPSEEK_API_KEY` 或 `LLM_API_KEY`，因此没有发起真实 Provider 请求，避免在无凭据时伪造“真实场景”结果。DeepSeek 的 Provider 单元测试仍覆盖缓存字段保留、计价和不一致输入；stable Runtime 场景使用 Fake Provider 完成了请求布局、epoch 压缩、Trace 和 report 回归。获得凭据后应单独运行受控 stable smoke test，并只观察字段采集是否退化，不把远端命中率波动当作重构失败。

## 验证结果

```text
ruff check src tests       PASS
ruff format --check src tests  PASS
mypy src                    PASS
pytest -q                  221 passed, 1 skipped
```

跳过项为 Windows 主机不支持符号链接的工具测试。

