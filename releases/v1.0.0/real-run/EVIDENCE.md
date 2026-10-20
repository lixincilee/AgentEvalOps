# v1.0.0 真实模型运行证据

来源：GitHub Actions [run 39251627592](https://github.com/lixincilee/AgentEvalOps/actions/runs/39251627592)，
提交 `cff5b09`，触发方式 `workflow_dispatch paid_target=both`。
模型：`deepseek-flash`。

## 花费

| 部分 | 花费 (CNY) |
| --- | --- |
| L3 + L4 + L5 判分 | 0.605774 |
| 真实四臂对比 + 真实压测 | 0.110482 |
| **合计（2026-10-19；不含 10-20 trace 补跑）** | **0.716256** |

## 判分

| 层 | 通过率（中位/最差） | 轮次 | 框架错误 | 花费 (CNY) |
| --- | --- | --- | --- | --- |
| L3 Ragas | 64.5% / 60.5% | 2 | 0 | 0.5071 |
| L4 DeepEval | 95.8% / 95.8% | 2 | 0 | 0.0987 |
| L5 Promptfoo | 100.0% | 1 | 0 | 0.0000 |

`judge_gate.py`：8 条判据通过，0 条失败，L5 未提供（跳过 1 条）。

## 真实四臂对比（40 条/臂）

| 臂 | 通过率 | 模型调用 | 平均延迟 | 花费 (CNY) |
| --- | --- | --- | --- | --- |
| `v1` | 100.0% | 0 | 0 ms | CNY 0.000000 |
| `v2` | 97.5% | 40 | 1084 ms | CNY 0.012315 |
| `v3` | 100.0% | 40 | 1098 ms | CNY 0.015063 |
| `v3-rules` | 100.0% | 0 | 0 ms | CNY 0.000000 |

## 真实并发压测

| 指标 | 数值 |
| --- | --- |
| 模式 / 臂 | `real` / `deepseek` |
| users / run_time | 5 / 30s |
| 请求数 | 133 |
| RPS | 4.787 |
| P50 / P95 / P99 | 1000 / 1300 / 1500 ms |
| 平均延迟 | 1020.2 ms |
| HTTP 错误率 | 0.000% |
| 任务成功率 | 100.000% |
| tokens | 30182 |
| 花费 | CNY 0.083104 |
| 单次成功任务成本 | CNY 0.000624842 |

## Langfuse Trace

主机 `https://jp.cloud.langfuse.com`，项目 `cmvf9rprr002mad0k9ckxaig4`，3/3 条 trace 经
`GET /api/public/v2/observations` 核对存在。

| case | Langfuse trace id | 链接 |
| --- | --- | --- |
| `status_shipped_zh` | `39ff1895e6a037d8a50e02872b68635e` | [打开](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/39ff1895e6a037d8a50e02872b68635e) |
| `status_en_lowercase` | `39165702d388c696da0cfa7b2b749f49` | [打开](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/39165702d388c696da0cfa7b2b749f49) |
| `status_delivered_zh` | `45ebd452cc010149e08a9741c7db7d69` | [打开](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/45ebd452cc010149e08a9741c7db7d69) |

`status_shipped_zh` 的 span 树：

| type | name | parent |
| --- | --- | --- |
| CHAIN | `agent.turn` | `e5809adc20ab5bcb` |
| CHAIN | `node.validate` | `338807e0cec5a9bc` |
| CHAIN | `node.plan` | `338807e0cec5a9bc` |
| CHAIN | `node.dispatch` | `338807e0cec5a9bc` |
| TOOL | `tool.query_order_status` | `a230abd8659bb143` |
| CHAIN | `node.respond` | `338807e0cec5a9bc` |

## 口径边界

- 压测是 **5 用户 / 30 秒的低并发基线**，不是容量上限；供应商拥塞会显著改变 P95。
- L3/L4 是 2 轮重复，L3 两轮相差 7.9 个百分点；单轮结果不足以判退化。
- Langfuse 这 3 条是零成本规则臂，证明链路与父子层级；真实模型 span 见下一节。
- 本目录是**运行产物归档**；`release.json` 的离线检查与指标仍以 `v1.0.0` 冻结时为准。

## 真实模型 Trace（2026-10-20 补跑）

主矩阵 10 条 + RAG 6 条，共 16 条 trace / 16 次模型调用，全部到达 Langfuse 并带
`GENERATION` span，Prompt / 模型响应 / token 16/16 非空，span 树 16/16 与本地一致。
有效花费 CNY 0.0109；另有一次导出超时重跑（CNY 0.0070 未计入证据），实际模型花费 CNY 0.0179。
耗时以本地 trace 为准（Langfuse 批量导出的时间戳被压缩）。
明细与两条代表 trace 的 Prompt/span 树：
[`langfuse-model-traces/EVIDENCE.md`](langfuse-model-traces/EVIDENCE.md)。

## 真实模型失败定位与多轮 Trace（2026-10-20）

主矩阵 213 条 + 故障/难例/多轮共 310 条原始 trace（292 条去重），全部到达 Langfuse。
主矩阵出现 **3 条真实失败**，分别定位到推理、检索、状态层；工具层用 1 条明确标注的合成探针验证。
全部 36 条多轮用例（72 轮）真实模型判分 0 失败，第二轮 `slot_source=inherited` 有 trace 证据。
另有 **24/279** 个实际模型调用 turn 因输出不可解析或缺字段降级到规则臂（矩阵 18 + 多轮 6）；
任务仍通过，但通过率掩盖了模型协议问题。
花费 CNY 0.1297。明细：
[`langfuse-model-traces/failure-multiturn/EVIDENCE.md`](langfuse-model-traces/failure-multiturn/EVIDENCE.md)。
