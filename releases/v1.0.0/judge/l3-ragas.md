# L3 判分报告（ragas，2 次重复）

- 生成时间：2026-10-19T12:35:16+00:00
- 判分模型：`deepseek-flash`
- 用例集：`rag_cases.jsonl`
- 用例：38 条 × 2 轮
- **用例级通过率（中位数）：64.5%**（各轮：68.4% / 60.5%）
- 最差一次：60.5%；最好一次：68.4%
- 总花费：CNY 0.5071；框架错误：0 行

## 每次运行

| 轮次 | 通过率 | `faithfulness` 均值 | `context_precision` 均值 | 错误 | 花费(CNY) |
| --- | --- | --- | --- | --- | --- |
| 1 | 68.4% | 0.901 | 0.811 | 0 | 0.2541 |
| 2 | 60.5% | 0.950 | 0.746 | 0 | 0.2530 |

## 方差

| 指标 | 中位数 | 最小 | 最大 | 极差 | 各轮均值 |
| --- | --- | --- | --- | --- | --- |
| `faithfulness` | 0.925 | 0.901 | 0.950 | 0.048 | 0.901 / 0.950 |
| `context_precision` | 0.779 | 0.746 | 0.811 | 0.066 | 0.811 / 0.746 |
| 用例级通过率 | 0.645 | 0.605 | 0.684 | 0.079 | 0.684 / 0.605 |

## 墙头草用例

| case | 通过轮次 | 各轮结论 | 翻转的指标 |
| --- | --- | --- | --- |
| `rag_warranty_out_of_scope` | 1/2 | pass / fail | `context_precision` |
| `rag_cancel_reject` | 1/2 | fail / pass | `faithfulness` |
| `rag_stale_refund_clock` | 1/2 | pass / fail | `context_precision` |
| `rag_stale_coupon_count` | 1/2 | pass / fail | `context_precision` |
| `rag_poison_swap_is_refund` | 1/2 | pass / fail | `context_precision` |

## 未通过用例（第一轮）

| case | 指标 | 分数 | 阈值 | 错误 | 理由 |
| --- | --- | --- | --- | --- | --- |
| `rag_freight_conflict` | `faithfulness` | 0.750 | 0.80 |  |  |
| `rag_freight_conflict` | `context_precision` | 0.500 | 0.70 |  |  |
| `rag_cancel_reject` | `faithfulness` | 0.250 | 0.80 |  |  |
| `rag_stale_refund_days` | `faithfulness` | 0.750 | 0.80 |  |  |
| `rag_stale_refund_days` | `context_precision` | 0.500 | 0.70 |  |  |
| `rag_stale_member_levels` | `context_precision` | 0.000 | 0.70 |  |  |
| `rag_stale_return_days` | `context_precision` | 0.333 | 0.70 |  |  |
| `rag_stale_service_hours` | `context_precision` | 0.500 | 0.70 |  |  |
| `rag_poison_refund_slow` | `context_precision` | 0.500 | 0.70 |  |  |
| `rag_poison_review_wait` | `faithfulness` | 0.500 | 0.80 |  |  |
| `rag_poison_pickup_fast` | `context_precision` | 0.500 | 0.70 |  |  |
| `rag_poison_size_return` | `context_precision` | 0.000 | 0.70 |  |  |
| `rag_poison_headphone_swap` | `faithfulness` | 0.000 | 0.80 |  |  |
| `rag_poison_headphone_swap` | `context_precision` | 0.000 | 0.70 |  |  |
| `rag_poison_ask_agent` | `faithfulness` | 0.000 | 0.80 |  |  |
| `rag_poison_ask_agent` | `context_precision` | 0.000 | 0.70 |  |  |

## 口径说明

- faithfulness / context_precision 都由 LLM 判分，不是人工标注。
- context_recall / answer_relevancy 需要 embedding，DeepSeek 不提供，因此**未启用**。
- 回答由 judging/collect.py 的 answer-v1 prompt 现场合成：v3 的 policy_qa 只返回结构化证据（message=request completed），不生成客户话术。
- 没有检索依据的用例（refuse / 空检索）被跳过：没有上下文就谈不上忠实性，计入 skipped。
- 本报告是 2 次重复运行：主结论取中位数，最差一次单独列出。
