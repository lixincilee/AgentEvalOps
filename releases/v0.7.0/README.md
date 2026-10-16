# 发布冻结：v0.7.0

- 代码版本：`d7fe209`（`v0.7.0`）
- 行为规格：v1.3
- 冻结时间：2026-10-16T07:27:59.394458+00:00
- 工作区：干净

## 检查

| 检查 | 结论 | 耗时 (s) | 说明 |
| --- | --- | --- | --- |
| `pytest` | 通过 | 35.625 | 1061 passed / 0 failed |
| `faults` | 通过 | 7.719 | 25/25 条故障用例 |
| `comparison:matrix` | 通过 | 9.171 | 213 行 |
| `comparison:rag_multiturn` | 通过 | 9.5 | 120 行 |
| `load_test` | 通过 | 44.422 | modeled 臂 / P95 160ms |

## 指标

| 指标 | 值 |
| --- | --- |
| `fault.cases` | 25.0 |
| `fault.pass_rate` | 1.0 |
| `matrix.v1.pass_rate` | 0.9671361502347418 |
| `matrix.v3_rules.pass_rate` | 0.9671361502347418 |
| `perf.cost_per_success_cny` | 0.00076 |
| `perf.http_error_rate` | 0.0 |
| `perf.p95_ms` | 160.0 |
| `perf.rps` | 144.799 |
| `perf.task_success_rate` | 1.0 |
| `perf.tokens_per_request` | 290.0 |
| `pytest.failed` | 0.0 |
| `pytest.passed` | 1061.0 |
| `pytest.skipped` | 0.0 |
| `pytest.xfailed` | 10.0 |
| `rag_multiturn.v1.pass_rate` | 0.375 |
| `rag_multiturn.v3_rules.pass_rate` | 1.0 |

## 怎么用

这份目录是**回归门禁的比较对象**：

```
python scripts/regression_gate.py --candidate <候选目录> --baseline v0.7.0
```

`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。
