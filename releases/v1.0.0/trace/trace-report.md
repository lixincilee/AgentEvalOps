# 失败环节定位报告

- 生成时间：2026-10-19T13:57:18.655176+00:00
- 输入 trace：C:\Users\2018lxc\AppData\Local\Temp\agentevalops-freeze-b0kavxax\trace\traces.jsonl（310 条）
- 判分依据：C:\Users\2018lxc\AppData\Local\Temp\agentevalops-freeze-b0kavxax\trace\traces.cases.jsonl
- 结果：**7/310 条不通过**

## 按环节聚类

| 环节 | 条数 | 典型细节 | 涉及用例 |
| --- | --- | --- | --- |
| reasoning（推理层） | 4 | intent | v2_bnd_status_word_only, v2_bnd_status_no_order, v2_err_unsupported_01, v2_err_unsupported_06 |
| state（状态管理层） | 3 | missing-slot | v2_bnd_cjk_adjacent_left, v2_bnd_cjk_adjacent_right, v2_bnd_digit_prefix |

## 逐条

| case | 环节 | 细节 | 对不上的字段（期望 → 实际） |
| --- | --- | --- | --- |
| v2_bnd_cjk_adjacent_left | state | missing-slot | expected_state='tool_completed' → 'validation_failed'; expected_error_code=None → 'VALIDATION_ERROR' |
| v2_bnd_cjk_adjacent_right | state | missing-slot | expected_state='tool_completed' → 'validation_failed'; expected_error_code=None → 'VALIDATION_ERROR' |
| v2_bnd_digit_prefix | state | missing-slot | expected_state='tool_completed' → 'validation_failed'; expected_error_code=None → 'VALIDATION_ERROR' |
| v2_bnd_status_word_only | reasoning | intent | expected_intent='order_status' → 'unknown'; expected_state='validation_failed' → 'no_action'; expected_tool='query_order_status' → None; expected_error_code='VALIDATION_ERROR' → 'UNSUPPORTED_INTENT' |
| v2_bnd_status_no_order | reasoning | intent | expected_intent='order_status' → 'unknown'; expected_state='validation_failed' → 'no_action'; expected_tool='query_order_status' → None; expected_error_code='VALIDATION_ERROR' → 'UNSUPPORTED_INTENT' |
| v2_err_unsupported_01 | reasoning | intent | expected_intent='order_status' → 'unknown'; expected_state='validation_failed' → 'no_action'; expected_tool='query_order_status' → None; expected_error_code='VALIDATION_ERROR' → 'UNSUPPORTED_INTENT' |
| v2_err_unsupported_06 | reasoning | intent | expected_intent='order_status' → 'unknown'; expected_state='validation_failed' → 'no_action'; expected_tool='query_order_status' → None; expected_error_code='VALIDATION_ERROR' → 'UNSUPPORTED_INTENT' |

## 口径

- 逐字段比对，只比对用例里**写了**的期望字段；`expected_error_code: null` 的意思是「不该有错误码」。
- 环节定位按固定优先级（见 `scripts/trace_report.py` 的模块文档），先硬事实（span 失败）
  再间接证据（无依据、兜底规划），最后才按对不上的字段名归类。`unclassified` 是要补规则，不是噪声。
- 这份报告不判「通过率」：那是 pytest 与 `compare_versions.py` 的职责，同一批 trace 不应该有两套分数。
