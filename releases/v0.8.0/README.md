# 发布冻结：v0.8.0

- 代码版本：`bff1171`（`v0.8.0`）
- 行为规格：v1.3
- 冻结时间：2026-10-16T09:44:01.485193+00:00
- 工作区：干净

## 检查

| 检查 | 结论 | 耗时 (s) | 说明 |
| --- | --- | --- | --- |
| `pytest` | 通过 | 27.89 | 1121 passed / 0 failed |
| `faults` | 通过 | 4.36 | 25/25 条故障用例 |
| `comparison:matrix` | 通过 | 4.797 | 213 行 |
| `comparison:rag_multiturn` | 通过 | 4.546 | 120 行 |
| `load_test` | 通过 | 96.297 | modeled 臂 / P95 140.0ms（3 次的中位数） |

## 指标

| 指标 | 值 |
| --- | --- |
| `fault.cases` | 25.0 |
| `fault.pass_rate` | 1.0 |
| `matrix.v1.pass_rate` | 0.9671361502347418 |
| `matrix.v3_rules.pass_rate` | 0.9671361502347418 |
| `perf.cost_per_success_cny` | 0.00076 |
| `perf.http_error_rate` | 0.0 |
| `perf.p95_ms` | 140.0 |
| `perf.rps` | 168.498 |
| `perf.task_success_rate` | 1.0 |
| `perf.tokens_per_request` | 290.0 |
| `pytest.failed` | 0.0 |
| `pytest.passed` | 1121.0 |
| `pytest.skipped` | 0.0 |
| `pytest.xfailed` | 10.0 |
| `rag_multiturn.v1.pass_rate` | 0.375 |
| `rag_multiturn.v3_rules.pass_rate` | 1.0 |

## 怎么用

这份目录是**回归门禁的比较对象**：

```
python scripts/regression_gate.py --candidate <候选目录> --baseline v0.8.0
```

`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。
