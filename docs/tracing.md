# Trace 口径（M8）

一句话：**trace 记录「这一轮真实发生了什么」，期望值不写进去**；两者的交叉引用发生在
`scripts/trace_report.py` 里。把期望值塞进被测对象的记录里，读的人就再也分不清哪一段是
证据、哪一段是断言。

## 1. 一条 trace 有哪些字段

`tracing.SCHEMA = "agentevalops.trace/v1"`，一行一条 JSON：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 一轮一个 |
| `started_at` | 墙上时间（UTC ISO） |
| `duration_ms` | 根 span 的时长 |
| `status` | 只要有**任何** span 是 `error`，整条 trace 就是 `error` |
| `run_id` / `case_id` / `version` | 归属：哪次运行、哪条用例、哪份代码 |
| `spans[]` | span 列表（见 §2） |

每个 span：`span_id` / `parent_id` / `name` / `kind` / `start_ms` / `duration_ms` /
`status` / `error_code` / `error_message` / `attributes`。

**时间口径**：`start_ms` 与 `duration_ms` 是相对 trace 起点的偏移，由 `time.perf_counter()`
得到（单调时钟，系统时间被改也不会算出负数）；`started_at` 才是墙上时间。压测里的
耗时统计与这里的偏移是两套东西，不要互相换算。

## 2. span 类型与各自记什么

| `kind` | 建在哪 | 关键属性 |
| --- | --- | --- |
| `turn` | `V3Agent.handle` | `message` / `intent` / `state` / `tool_name` / `error_code` / `plan_source` / `slot_order_id` / `slot_source` / `nodes` |
| `node` | `pipeline._traced` 包装的每个图节点 | `node`，以及该节点的结局（意图 / 状态 / 错误码 / 检索命中数） |
| `llm` | `planner.LLMPlanner.plan` | `model` / `prompt_version` / `system_prompt` / `user_message` / `response` / `usage` / `latency_ms` / `parse` / `plan_source` |
| `retriever` | `pipeline._retrieve_node` | `query` / `top_k` / `hits` / `doc_ids` / `scores` / `expired` / `grounded` |
| `tool` | `agent._call_tool` | `tool` / `arguments` / `success` / `error_code` / `result` |
| `state` | `pipeline._respond_node` | `session_id` / `order_id` / `source` / `written` / `last_order_id` |

为什么检索 span 建在**节点里**而不是 `KnowledgeBaseTool` 里：故障注入是包在工具外面的
（`FaultyTool` 在调内层之前就抛），插在工具内部的话，「检索工具坏了」那一轮的 trace 里
会一条检索痕迹都不剩——而它恰恰是最需要被看见的轮次。

`llm` span 只在**真的调了模型**的时候出现：规则臂（`RulePlannerV3`）跑出来的 trace 里没有
它。要拿到真实的 prompt / 回答 / token，加 `--llm --yes`：

```powershell
D:\Python\python.exe scripts/trace_run.py --cases eval_cases/case_matrix_v2.jsonl --limit 10 --llm --yes --budget 0.2 --out reports/traces-llm-matrix.jsonl
```

实测一条（`deepseek-flash`）：`latency_ms=3036.3`、`usage={'prompt_tokens': 203,
'completion_tokens': 16, 'cache_hit_tokens': 0, 'total_tokens': 219}`、
`response='{"intent":"order_status","order_id":"ORD-1001"}'`。花费规矩与另外两个会花钱的
脚本一致：没有 `--yes` 停在发请求之前（退出码 2）、缺 key 零成本退出、`--budget` 触到就停
（退出码 1）。数字见 `docs/stage2-evidence.md` §16.5。

## 3. 「零行为改变」是怎么保证的

1. 插桩点统一写成 `with span(...)`，没有激活的 tracer 时它 yield 一个 `_NullSpan`
   （`set` / `fail` 全是空操作）。业务代码里**不允许**出现「有 tracer / 没 tracer」的分支。
2. 当前 tracer 用 `ContextVar` 而不是全局变量：压测服务在线程池里跑，全局变量会让并发
   请求的 trace 互相污染。
3. `tests/test_tracing.py::test_instrumentation_does_not_change_behaviour` 逐字段比对
   「带/不带 tracer」的审计记录。做不到这一点，「加了可观测性」就变成了「换了一个被测
   系统」，M3 之前所有回归数字都要重跑。

## 4. 脱敏与截断口径

- **键名**命中 `api_key / secret / token / authorization / password / credential / cookie`
  → 整值替换成 `***`（不看值长什么样）。
- **值形状**命中 `sk-…`、`Bearer …`、`AKIA…`、邮箱、中国大陆手机号 → 替换。
- 属性上限：单值 400 字符、列表 20 项、递归深度 4。截断会留下 `…(+N chars)`，
  不假装原文就这么长。

一个真实的反例：`prompt_tokens` / `total_tokens` 里带 "token"，第一版规则把它们也脱成
`***`——**静默删掉了成本指标**。现在有白名单钉住：「带计数提示词的 `*_tokens` 不算凭据」，
并且有一条测试专门守它。

## 5. Langfuse 映射

`tracing.langfuse_payload(trace)` 是个**纯函数**，把 trace 摊成 Langfuse 的 span 列表。
纯函数是有意的：服务本机起不来（没有 Docker），但「发给 Langfuse 的到底是什么」这件事
必须能被测试钉住，不能因为服务不可用就没人检查。

| trace | Langfuse |
| --- | --- |
| span 名 | span 名 |
| `kind` / `trace_id` / `case_id` / `run_id` / `version` / `error_code` | `metadata` |
| `status=error` | `level=ERROR` + `status_message` |
| `retriever.search` 的 `query` / `doc_ids` | `input` / `output` |
| `tool.*` 的 `arguments` / `result` | `input` / `output` |
| `llm.plan` 的 prompt / `response` | `input` / `output` |
| `llm.plan` 的 `usage` | `usage_details`（input / output / total） |

出口状态记在 `emitter.status`，调用方要打印出来：

| 状态 | 含义 |
| --- | --- |
| `live` | 真发（SDK 可用 + 配了 key，或注入了 client） |
| `dry-run:no-sdk` / `dry-run:no-keys` | 不发送，把本来要发的载荷写进 dump 文件 |
| `error:<异常类名>` | 发送过程中出错；**不抛**——观测层不该把被测系统搞崩 |

## 6. 失败定位规则

规则表在 `scripts/trace_report.py` 的模块文档里，优先级从硬事实到间接证据：

| 优先级 | 证据 | 环节 / 细节 |
| --- | --- | --- |
| 1 | 模型 span 失败 | `reasoning` / `model-error:` |
| 2 | 检索 span 失败 | `retrieval` / `retriever-error:` |
| 3 | `state=validation_failed` 且 `tool_name` 为空 | `request` / `request-validation` |
| 4 | 工具 span 抛故障 | `tool` / `tool-error:` |
| 5 | `error_code=NO_KNOWLEDGE` | `retrieval` / `no-grounding` |
| 6 | 检索命中 0（`grounded=false`） | `retrieval` / `no-hit` |
| 7 | 走了兜底规划（`plan_source=*fallback`） | `reasoning` / `prompt-format` |
| 8 | 槽位 / 继承字段不符 | `state` / `slot-inheritance` |
| 9 | 工具返回失败（业务失败） | `tool` / `tool-failed:` |
| 10 | 意图不符 | `reasoning` / `intent` |
| 11 | 都不匹配 | `unclassified` |

第 3 条有个例外：**「意图认出来了、槽位没抽到」也是 `VALIDATION_ERROR`**，但它归
`state / missing-slot`——要去查的是槽位解析与会话继承，报成「请求校验」会把读的人指到
错误的代码上去。判据是 `tool_name` 有没有值（`dispatch` 在缺 `order_id` 时会记下本来要调
的工具）。

`unclassified` 不是「没问题」，是「规则没覆盖」：它必须被当成待办。

## 7. 没有验证过的部分

这一节和 `docs/ci.md` 第 6 节同等重要。

| 声明 | 状态 |
| --- | --- |
| 有 trace 真的到过 Langfuse 服务 | **没有**。本机没有 Docker、也没有 Langfuse 账号。验证到的是载荷映射、dry-run 降级、注入假 client 时的调用序列 |
| `langfuse>=3.0` 的 API 形状（`start_span` / `update` / `end` 的参数名） | **未在真机验证**。适配层写成「出错就降级 + 落盘」，所以猜错也不会让评测崩，但会静默走 dry-run——**这个静默是已知风险** |
| HTML 视图在浏览器里的实际渲染 | **未逐像素核对**。结构（几何、转义、徽标）有测试钉住，肉眼效果需要在浏览器里打开确认 |
| trace 进了发布冻结产物 | **还没有**。`freeze_release.py` 目前只冻结五项检查的原始报告；把 trace 与定位报告一起冻结属于 M9 |
| 真实模型路径的 trace | **跑过了，但只有 16 条**（CNY 0.0057）。`llm.plan` 上带真实 prompt / 回答 / token / 延迟；方差未测（只跑了一次）。其余 294 条是规则臂——能定位状态与工具问题，回答不了 prompt 问题 |
| `--llm` 的接线（预算闸门、注入客户端也过账本、故障轮用真实规划器） | **有测试钉住**：`tests/test_trace_run.py` 16 条，全程不联网（注入 `ScriptedLLMClient`） |

## 8. 已经踩过的坑

1. **故障注入包在工具外面。** 检索 span 一开始建在 `KnowledgeBaseTool.execute` 里，
   于是「检索工具超时」那一轮 trace 里没有任何检索 span——最该看的轮次反而什么都没有。
   现在建在节点里（见 §2）。
2. **追加 vs 覆盖。** `JsonlEmitter` 是追加写；`trace_run.py` 默认**截断**输出文件。
   否则两次运行混进同一个文件，报告把同一批用例算两遍——而「跑了两遍」和「跑出两条不同
   结果」在文件里长得一模一样。
3. **预热轮。** 故障用例带 `warmup`（先在同一个会话里跑一轮铺垫）。预热**不记 trace**：
   记上就会多出一条没有期望值的「用例」，读的人分不清哪一轮该被判分。
4. **发送失败的状态被 dump 改写成 dry-run。** `error:RuntimeError` 被随后的
   `_write_dump` 改成了 `dry-run:RuntimeError`，调用方再也看不到「真发失败」这件事。
5. **显式错误码被异常类名盖掉。** `llm_span.fail("BUDGET_EXCEEDED", …)` 之后异常继续
   上行，`Tracer.span` 的兜底又用 `BudgetExceededError` 覆盖了它。现在显式标记优先。
6. **视图不渲染用户文本 = 转义测试是空测试。** 第一版视图只画 span 名字，
   `<script>` 测试「通过」是因为页面里根本没有用户文本——同时也说明它把最该看的东西
   （用户说了什么、检索到什么）藏起来了。
7. **校验失败有两种。** 见 §6 第 3 条：`missing-slot` 一开始被报成 `request-validation`，
   在 310 条 trace 的报告里一眼就看出来了（3 条状态缺口的定位全指向错误的环节）。
8. **预算 + 没有价目的模型 = 静默降级。** `UsageLedger` 对未计价模型抛
   `UnknownModelPriceError`，而它属于 `LLMError`，会被规划器当成「模型抽风」降级成
   `llm-fallback`——整个 run 静默地不再发请求、一个 token 都不花，通过率还很好看。
   现在 `trace_run.py --llm` 在发请求之前就拒掉这种组合（退出码 2），与
   `compare_versions.py` 同一道闸。