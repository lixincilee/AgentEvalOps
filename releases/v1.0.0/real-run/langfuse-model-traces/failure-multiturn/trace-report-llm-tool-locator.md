# 失败环节定位报告

- 生成时间：2026-10-20T13:07:21.150370+00:00
- 输入 trace：reports\traces-langfuse-llm-tool-locator.jsonl（1 条）
- 判分依据：reports\traces-langfuse-llm-tool-locator.cases.jsonl
- 结果：**1/1 条不通过**

## 按环节聚类

| 环节 | 条数 | 典型细节 | 涉及用例 |
| --- | --- | --- | --- |
| tool（工具层） | 1 | tool-error:TOOL_TIMEOUT | trace_tool_locator_timeout |

## 逐条

| case | 环节 | 细节 | 对不上的字段（期望 → 实际） |
| --- | --- | --- | --- |
| trace_tool_locator_timeout | tool | tool-error:TOOL_TIMEOUT | expected_state='tool_completed' → 'tool_failed'; expected_error_code=None → 'TOOL_TIMEOUT' |

## 口径

- 逐字段比对，只比对用例里**写了**的期望字段；`expected_error_code: null` 的意思是「不该有错误码」。
- 环节定位按固定优先级（见 `scripts/trace_report.py` 的模块文档），先硬事实（span 失败）
  再间接证据（无依据、兜底规划），最后才按对不上的字段名归类。`unclassified` 是要补规则，不是噪声。
- 这份报告不判「通过率」：那是 pytest 与 `compare_versions.py` 的职责，同一批 trace 不应该有两套分数。
