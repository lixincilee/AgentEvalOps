# 失败环节定位报告

- 生成时间：2026-10-20T14:14:03.444026+00:00
- 输入 trace：reports\traces-langfuse-llm-rag-full.jsonl（48 条）
- 判分依据：reports\traces-langfuse-llm-rag-full.cases.jsonl
- 结果：**0/48 条不通过**

## 按环节聚类

没有定位到任何失败环节。


## 逐条

没有不通过的用例。


## 口径

- 逐字段比对，只比对用例里**写了**的期望字段；`expected_error_code: null` 的意思是「不该有错误码」。
- 环节定位按固定优先级（见 `scripts/trace_report.py` 的模块文档），先硬事实（span 失败）
  再间接证据（无依据、兜底规划），最后才按对不上的字段名归类。`unclassified` 是要补规则，不是噪声。
- 这份报告不判「通过率」：那是 pytest 与 `compare_versions.py` 的职责，同一批 trace 不应该有两套分数。
