# 发布冻结：v1.0.0

- 代码版本：`ea7e13c`（`v0.9.0-12-gea7e13c`）
- 行为规格：v1.3
- 冻结时间：2026-10-19T13:57:56.601143+00:00
- 工作区：干净

## 检查

| 检查 | 结论 | 耗时 (s) | 说明 |
| --- | --- | --- | --- |
| `pytest` | 通过 | 38.078 | 1293 passed / 0 failed |
| `faults` | 通过 | 5.078 | 25/25 条故障用例 |
| `comparison:matrix` | 通过 | 5.203 | 213 行 |
| `comparison:rag_multiturn` | 通过 | 5.234 | 120 行 |
| `trace` | 通过 | 11.047 | 310 条 trace / 7 条不通过 / 未定位 0 |
| `judge` | 通过 | 0.0 | L3 64.5%（2 轮） / L4 95.8%（2 轮） / L5 100.0%（1 轮） / 人工 20/20 / kappa 0.875 |
| `load_test` | 通过 | 36.985 | modeled 臂 / P95 140ms |

## 指标

| 指标 | 值 |
| --- | --- |
| `fault.cases` | 25.0 |
| `fault.pass_rate` | 1.0 |
| `judge.L3.cost_cny` | 0.507073 |
| `judge.L3.errors` | 0.0 |
| `judge.L3.pass_rate` | 0.644737 |
| `judge.L3.repeat` | 2.0 |
| `judge.L3.worst_pass_rate` | 0.605263 |
| `judge.L4.cost_cny` | 0.09870056 |
| `judge.L4.errors` | 0.0 |
| `judge.L4.pass_rate` | 0.958333 |
| `judge.L4.repeat` | 2.0 |
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
| `perf.cost_per_success_cny` | 0.000760529 |
| `perf.http_error_rate` | 0.0 |
| `perf.p95_ms` | 140.0 |
| `perf.rps` | 157.701 |
| `perf.task_success_rate` | 1.0 |
| `perf.tokens_per_request` | 290.0 |
| `pytest.failed` | 0.0 |
| `pytest.passed` | 1293.0 |
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
python scripts/regression_gate.py --candidate <候选目录> --baseline v1.0.0
```

`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。


## 真实模型运行证据（2026-10-19）

- GitHub Actions：[run 39251627592](https://github.com/lixincilee/AgentEvalOps/actions/runs/39251627592)，提交 `cff5b09`。
- 详细报告与 trace 链接：[`real-run/EVIDENCE.md`](real-run/EVIDENCE.md)。
- 机器可读摘要：[`real-run/evidence.json`](real-run/evidence.json)。
- 16 条真实模型 Langfuse trace（Prompt / 模型响应 / token / span 树）：
  [`real-run/langfuse-model-traces/EVIDENCE.md`](real-run/langfuse-model-traces/EVIDENCE.md)，
  有效花费 CNY 0.0109；计入一次导出失败重跑后，实际模型花费 CNY 0.0179。
- 失败定位与多轮 trace：主矩阵 213 条 + 多轮 72 条等共 310 条原始 trace，
  3 条真实失败分别定位到推理/检索/状态层，工具层另有合成探针；24/279 个模型调用 turn 发生规则臂降级。
  明细：[`real-run/langfuse-model-traces/failure-multiturn/EVIDENCE.md`](real-run/langfuse-model-traces/failure-multiturn/EVIDENCE.md)，
  花费 CNY 0.1297。
- 真实模型方差：60 条四类分层切片 × v2/v3 × 3 次，两列通过率均 98.3%、极差 0.0pp、墙头草 0；
  唯一失败固定为 `v2_err_unsupported_04`。3 轮合计 CNY 0.129874。
  明细：[`real-run/langfuse-model-traces/variance-repeat3/EVIDENCE.md`](real-run/langfuse-model-traces/variance-repeat3/EVIDENCE.md)。
- RAG 48 + 故障 25 全量模型臂：RAG 48/48、gold 38/38、Top-1 28/28、过期标记 13/13；
  故障 25/25（19 llm + 6 llm-fallback）；Langfuse 73/73、树 73/73、成功生成 Prompt/输出/usage 67/67。
  新增花费 CNY 0.0246。明细：[`real-run/langfuse-model-traces/rag-fault-full/EVIDENCE.md`](real-run/langfuse-model-traces/rag-fault-full/EVIDENCE.md)。
- 2026-10-19 真实运行合计 **CNY 0.7163**；真实压测 P95 **1300 ms**、
  RPS **4.787**、任务成功率 **100.0%**、
  单次成功成本 **CNY 0.000624842**。
