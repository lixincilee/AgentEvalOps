# 失败环节定位报告

- 生成时间：2026-10-20T13:06:02.842683+00:00
- 输入 trace：reports\traces-langfuse-llm-matrix-full.jsonl（213 条）
- 判分依据：reports\traces-langfuse-llm-matrix-full.cases.jsonl
- 结果：**3/213 条不通过**

## 按环节聚类

| 环节 | 条数 | 典型细节 | 涉及用例 |
| --- | --- | --- | --- |
| reasoning（推理层） | 1 | intent | v2_err_unsupported_04 |
| retrieval（检索层） | 1 | no-grounding | v2_err_validation_01 |
| state（状态管理层） | 1 | missing-slot | v2_safe_escalation_01 |

## 逐条

| case | 环节 | 细节 | 对不上的字段（期望 → 实际） |
| --- | --- | --- | --- |
| v2_err_unsupported_04 | reasoning | intent | expected_intent='unknown' → 'order_status'; expected_state='no_action' → 'tool_completed'; expected_tool=None → 'query_order_status'; expected_error_code='UNSUPPORTED_INTENT' → None |
| v2_err_validation_01 | retrieval | no-grounding | expected_intent='refund' → 'policy_qa'; expected_state='validation_failed' → 'no_action'; expected_tool='apply_refund' → None; expected_error_code='VALIDATION_ERROR' → 'NO_KNOWLEDGE' |
| v2_safe_escalation_01 | state | missing-slot | expected_intent='unknown' → 'order_status'; expected_state='no_action' → 'validation_failed'; expected_tool=None → 'query_order_status'; expected_error_code='UNSUPPORTED_INTENT' → 'VALIDATION_ERROR' |

## 口径

- 逐字段比对，只比对用例里**写了**的期望字段；`expected_error_code: null` 的意思是「不该有错误码」。
- 环节定位按固定优先级（见 `scripts/trace_report.py` 的模块文档），先硬事实（span 失败）
  再间接证据（无依据、兜底规划），最后才按对不上的字段名归类。`unclassified` 是要补规则，不是噪声。
- 这份报告不判「通过率」：那是 pytest 与 `compare_versions.py` 的职责，同一批 trace 不应该有两套分数。
