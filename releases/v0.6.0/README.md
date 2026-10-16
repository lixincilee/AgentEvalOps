# 发布冻结：v0.6.0

- 代码版本：`c61d0b3`（`v0.6.0-1-gc61d0b3`）
- 行为规格：v1.3
- 冻结时间：2026-10-16T13:00:08.386676+00:00
- 工作区：干净

## 检查

| 检查 | 结论 | 耗时 (s) | 说明 |
| --- | --- | --- | --- |
| `pytest` | 通过 | 39.984 | 1025 passed / 0 failed |
| `faults` | 通过 | 8.328 | 25/25 条故障用例 |
| `comparison:matrix` | 通过 | 9.203 | 213 行 |
| `comparison:rag_multiturn` | 通过 | 8.141 | 120 行 |
| `load_test` | 通过 | 48.375 | modeled 臂 / P95 190ms |

## 指标

| 指标 | 值 |
| --- | --- |
| `fault.cases` | 25.0 |
| `fault.pass_rate` | 1.0 |
| `matrix.v1.pass_rate` | 0.9671361502347418 |
| `matrix.v3_rules.pass_rate` | 0.9671361502347418 |
| `perf.cost_per_success_cny` | 0.00076 |
| `perf.http_error_rate` | 0.0 |
| `perf.p95_ms` | 190.0 |
| `perf.rps` | 133.831 |
| `perf.task_success_rate` | 1.0 |
| `perf.tokens_per_request` | 290.0 |
| `pytest.failed` | 0.0 |
| `pytest.passed` | 1025.0 |
| `pytest.skipped` | 0.0 |
| `pytest.xfailed` | 10.0 |
| `rag_multiturn.v1.pass_rate` | 0.375 |
| `rag_multiturn.v3_rules.pass_rate` | 1.0 |

## 怎么用

这份目录是**回归门禁的比较对象**：

```
python scripts/regression_gate.py --candidate <候选目录> --baseline v0.6.0
```

`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。
