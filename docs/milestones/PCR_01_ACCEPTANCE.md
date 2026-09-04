# PCR-01 `prompt_cache` 归包与兼容层验收

## 交付内容

- 新建 `src/patchloop/prompt_cache`，包含 `diagnostics.py`、`layout.py`、`epoch.py`、`publication.py` 和显式公共导出的 `__init__.py`。
- 四个实现从旧顶层模块迁入新包；旧的 `patchloop.cache`、`patchloop.prompt_layout`、`patchloop.cache_epoch` 和 `patchloop.memory_publication` 仅保留显式、无状态的重导出。
- Runtime、Persistence、Observability、Evaluation 和 Fake Provider 的内部导入切换到新包；测试首选路径同步切换。
- 新增新旧导入路径身份测试，确认类、函数和公共类型没有实现副本，且包公共导出不泄漏内部帮助函数。

## 兼容与行为门禁

- 包根部既有公共类型仍可导入。
- 旧顶层导入路径继续可用，并与新路径引用同一个 Python 对象。
- PCR-00 固定的请求 wire bytes、请求指纹、消息布局、epoch/记忆发布快照、Trace、Runtime report 和确定性矩阵未改变。
- 未新增本地缓存目录，也未改变 Provider 参数、缓存策略或 checkpoint schema。

## 验证结果

```text
python -m pytest tests/unit/test_prompt_cache_imports.py tests/unit/test_pcr00_behavior_baseline.py tests/unit/test_cache.py tests/unit/test_cache_epoch.py tests/unit/test_memory_publication.py tests/unit/test_prompt_layout.py tests/unit/test_cache_evaluation.py tests/unit/test_storage.py -q
36 passed

python -m mypy
Success: no issues found in 60 source files

python -m pytest -q
212 passed, 1 skipped
```

Ruff 检查和格式检查通过。唯一跳过项是 Windows 主机不支持的符号链接测试。

