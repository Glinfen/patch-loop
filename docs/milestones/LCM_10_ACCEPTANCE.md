# LCM-10 消融实验与失败驱动优化验收

## 结论

PatchLoop 已于 2026-09-03 完成 LCM-10。新增 `MemoryAblationRunner` 和 `experiment-memory` 命令，在同一份锁定长上下文清单上比较六种记忆策略，按失败探针分类并只选择频次最高的两个类别进行定向优化。

运行命令：

```text
patchloop experiment-memory --manifest benchmarks/memory_tasks.json --root . \
  --repeats 3 --output benchmarks/results/lcm10_memory_ablation.json
```

## 消融结果

确定性三轮结果保存在 [lcm10_memory_ablation.json](../../benchmarks/results/lcm10_memory_ablation.json)。所有变体的结果指纹均稳定，完整 Memory 2.0 达到 24/24，TaskMemory V1 达到 21/24。

| 变体 | 成功率 | 关键事实召回 | 陈旧事实率 | 说明 |
| --- | ---: | ---: | ---: | --- |
| `recent_only` | 0/24 | 0.000 | 0.000 | 早期事实和失败情景不可见 |
| `task_memory_v1` | 21/24 | 1.000 | 1.000 | 会把已替代 API 值带入上下文 |
| `hierarchical_no_semantic` | 3/24 | 0.154 | 0.000 | 语义事实无法跨长历史召回 |
| `hierarchical_no_episodic` | 18/24 | 0.846 | 0.000 | 失败策略和情景证据丢失 |
| `hierarchical_no_compression` | 24/24 | 1.000 | 0.000 | 本清单未观察到压缩导致的质量失败 |
| `hierarchical_memory` | 24/24 | 1.000 | 0.000 | 完整 Memory 2.0 |

六种变体共享任务生成器、探针和通过条件；关闭组件的 harness 只改变对应长期层，避免通过不同任务或不同判定制造差异。

## 失败分类与优化范围

失败按实际未通过的任务探针统计，而不是按单个成功样例推断。三轮合计排名最高的类别为：

1. `early_fact_not_recalled`：39 次。
2. `episodic_fact_not_recalled`：9 次。

因此优化阶段只启用与这两类直接相关的语义/情景记忆组件。`stale_fact_recalled`、`semantic_fact_not_recalled` 等较低频类别保留在报告中，未进行无数据依据的全面重构；压缩关闭也未产生质量失败，暂不优化压缩器。

## 验收门禁

- 六种变体均可通过同一条 CLI 命令运行。
- 每种变体重复三轮，成功率最小值、最大值和结果指纹一致。
- 完整 Memory 2.0 不低于 TaskMemory V1，并将陈旧事实率降为 0。
- 优化目标数量不超过 2，且每个目标都能在失败 taxonomy 中找到实际样例。
- `ruff check .`、`ruff format --check .`、`mypy src/patchloop` 和全量 pytest 通过。
