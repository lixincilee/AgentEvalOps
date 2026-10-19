# 发布冻结：v0.9.0

- 代码版本：`4020550`（`v0.8.0-9-g4020550`）
- 行为规格：v1.3
- 冻结时间：2026-10-19T09:23:17.228014+00:00
- 工作区：干净

## 检查

| 检查 | 结论 | 耗时 (s) | 说明 |
| --- | --- | --- | --- |
| `pytest` | 通过 | 38.968 | 1278 passed / 0 failed |
| `faults` | 通过 | 5.547 | 25/25 条故障用例 |
| `comparison:matrix` | 通过 | 5.625 | 213 行 |
| `comparison:rag_multiturn` | 通过 | 5.25 | 120 行 |
| `trace` | 通过 | 10.844 | 310 条 trace / 7 条不通过 / 未定位 0 |
| `judge` | 通过 | 0.0 | L3 65.8%（3 轮） / L4 95.8%（3 轮） / L5 100.0%（1 轮） / 人工 20/20 / kappa 0.875 |
| `load_test` | 通过 | 38.203 | modeled 臂 / P95 160ms |

## 指标

| 指标 | 值 |
| --- | --- |
| `fault.cases` | 25.0 |
| `fault.pass_rate` | 1.0 |
| `judge.L3.cost_cny` | 1.524776 |
| `judge.L3.errors` | 0.0 |
| `judge.L3.pass_rate` | 0.657895 |
| `judge.L3.repeat` | 3.0 |
| `judge.L3.worst_pass_rate` | 0.605263 |
| `judge.L4.cost_cny` | 0.2903184 |
| `judge.L4.errors` | 0.0 |
| `judge.L4.pass_rate` | 0.958333 |
| `judge.L4.repeat` | 3.0 |
| `judge.L4.worst_pass_rate` | 0.958333 |
| `judge.L5.cost_cny` | 0.0 |
| `judge.L5.errors` | 0.0 |
| `judge.L5.pass_rate` | 1.0 |
| `judge.L5.repeat` | 1.0 |
| `judge.L5.worst_pass_rate` | 1.0 |
| `judge.errors` | 0.0 |
| `judge.human.agreement` | 0.95 |
| `judge.human.kappa` | 0.875 |
| `judge.human.labeled` | 20.0 |
| `matrix.v1.pass_rate` | 0.9671361502347418 |
| `matrix.v3_rules.pass_rate` | 0.9671361502347418 |
| `perf.cost_per_success_cny` | 0.00076 |
| `perf.http_error_rate` | 0.0 |
| `perf.p95_ms` | 160.0 |
| `perf.rps` | 145.516 |
| `perf.task_success_rate` | 1.0 |
| `perf.tokens_per_request` | 290.0 |
| `pytest.failed` | 0.0 |
| `pytest.passed` | 1278.0 |
| `pytest.skipped` | 1.0 |
| `pytest.xfailed` | 10.0 |
| `rag_multiturn.v1.pass_rate` | 0.375 |
| `rag_multiturn.v3_rules.pass_rate` | 1.0 |
| `trace.failed` | 7.0 |
| `trace.pass_rate` | 0.9774193548387097 |
| `trace.traces` | 310.0 |
| `trace.unclassified` | 0.0 |

## 怎么用

这份目录是**回归门禁的比较对象**：

```
python scripts/regression_gate.py --candidate <候选目录> --baseline v0.9.0
```

`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。
