# PCR-02 缓存用量累计器验收

## 交付内容

- 新增 `patchloop.prompt_cache.usage.CacheUsageAccumulator` 与 `CacheUsageAccumulatorSnapshot`。
- 缓存命中/未命中、写入、已报告/未报告、不一致调用和命中率计算集中到累计器；Runtime 不再持有独立的 `_cache_*` 数值字段。
- 使用可选 Token 总量区分“Provider 未报告”与“Provider 明确报告为 0”。写入 Token 也保留同样语义。
- Runtime 普通模型调用和 epoch 压缩调用共用同一累计入口；report 与 checkpoint 继续使用原有字段名和 JSON 形状。
- 支持从 PCR-02 快照恢复，也支持从旧 checkpoint 的分散字段恢复并继续累计。

## 覆盖范围

`tests/unit/test_prompt_cache_usage.py` 固定以下语义：

- 字段缺失、部分字段、混合命中和输入 Token 不一致。
- 命中/未命中/写入均明确为零。
- 仅 write 的调用仍计入写入报告，但缓存命中统计保持未报告。
- 快照恢复、旧 checkpoint 字段恢复和恢复后继续累计。
- 不允许快照把缺失值与已报告值混淆。

## 验证结果

```text
python -m pytest tests/unit/test_prompt_cache_usage.py tests/unit/test_cache.py tests/unit/test_cache_epoch.py tests/unit/test_memory_publication.py tests/e2e/test_checkpoint_resume.py -q
16 passed

python -m mypy
Success: no issues found in 61 source files

python -m pytest -q
212 passed, 1 skipped
```

Ruff 检查和格式检查通过。唯一跳过项是 Windows 主机不支持的符号链接测试。
