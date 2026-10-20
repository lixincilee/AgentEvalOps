# 真实模型失败定位与多轮 Trace（2026-10-20）

- 代码：`e4c8df2`（v1.0.0 之后）
- 模型：`deepseek-flash`
- 原始 trace：**310 条**（213 主矩阵 + 6 难例 + 6 故障 + 12 多轮小切片 + 72 多轮全量 + 1 工具探针）
- 去重后：**292 条**唯一 case-turn（310 条 trace ID 各自唯一；6 条难例包含在主矩阵中，
  12 条多轮小切片包含在多轮全量中）
- 模型调用：**304 次**；花费：**CNY 0.1297**
- Langfuse：`https://jp.cloud.langfuse.com`，项目 `cmvf9rprr002mad0k9ckxaig4`
- 核对：**310/310** 条原始 trace 到达 Langfuse；其中 **304** 条有 `GENERATION`，
  Prompt / 输出 / usage **304/304** 非空；其余 6 条在模型调用前被请求校验拦下
  （空白/制表符/缺 `customer_id`），本来就不该有模型 span；span 树 310/310 与本地一致
- 机器可读明细：[`failure-traces.json`](failure-traces.json)

## 主矩阵：3/213 条真实失败，三个环节各 1 条

| case | 报告定位 | 期望 → 实际 | 怎么读 |
| --- | --- | --- | --- |
| `v2_err_unsupported_04` | 推理层 / intent | unknown/no_action → order_status/tool_completed | 「订单 ORD-1001 多少钱」被模型判成查单，并真的调了查单工具 |
| `v2_err_validation_01` | 检索层 / no-grounding | refund/validation_failed → policy_qa/NO_KNOWLEDGE | 「退款怎么操作」被模型判成政策问答，检索没找到依据后拒答 |
| `v2_safe_escalation_01` | 状态层 / missing-slot | unknown/no_action → order_status/validation_failed | 报告按固定优先级落在状态层，但差异同时显示上游意图先偏了；这是定位规则可改进的一处 |

工具层的“非预期失败”在真实矩阵里没有出现，因此额外跑了一条**明确标注的合成探针**：
注入查单工具超时、但把期望写成成功。报告把它定位到 `tool`，证明工具层定位链路有效；它不是产品缺陷。

## 模型降级：24/279 个实际模型调用 turn 回退到规则臂

主矩阵 **18/207**（另外 6 条在模型调用前结束）、多轮全量
**6/72** 的 trace 标记为
`plan_source=llm-fallback`：模型输出不是合法 JSON、或缺少必要字段，规划器改用规则臂。
这些 turn 的任务结果仍然正确，所以**只看通过率看不出来**；这正是 trace 层要补的证据。

代表：`mt_ref_03#t1` 的模型返回无法解析，`parse=not-json`，但规则臂继承了
`slot_source=inherited` 的订单号，任务仍然完成。

### 推理层代表：`v2_err_unsupported_04`

- case：`v2_err_unsupported_04`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/63c258cf4a3f0d44869b886089bf61d1)
- trace id：`63c258cf4a3f0d44869b886089bf61d1`；run id：`trace-evidence-matrix-full-20261020`
- 根状态：intent=`order_status` / state=`tool_completed` / tool=`query_order_status` / error=`None` / plan_source=`llm`
- 槽位：order_id=`ORD-1001` / source=`verbatim`；session=`None`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 197, "output": 47, "total": 244}`
- 耗时：整轮 1068.3 ms；`llm.plan` span 1063.7 ms；模型调用 1063.4 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
订单 ORD-1001 多少钱
```

模型输出：

```json
[response]
{"intent":"order_status","order_id":"ORD-1001"}

[plan_source]
llm
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 1068.3 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 1063.7 ms | 1063.4 ms |
| CHAIN | `node.plan` | `agent.turn` | 1063.9 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.2 ms | - |
| TOOL | `tool.query_order_status` | `node.dispatch` | 0.1 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |


### 检索层代表：`v2_err_validation_01`

- case：`v2_err_validation_01`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/fa51b17f9b303fd8902aaed1c4f9b657)
- trace id：`fa51b17f9b303fd8902aaed1c4f9b657`；run id：`trace-evidence-matrix-full-20261020`
- 根状态：intent=`policy_qa` / state=`no_action` / tool=`None` / error=`NO_KNOWLEDGE` / plan_source=`llm`
- 槽位：order_id=`None` / source=`None`；session=`None`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 192, "output": 38, "total": 230}`
- 耗时：整轮 637.4 ms；`llm.plan` span 632.0 ms；模型调用 631.7 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
退款怎么操作
```

模型输出：

```json
[response]
{"intent":"policy_qa","order_id":null}

[plan_source]
llm
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 637.4 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 632.0 ms | 631.7 ms |
| CHAIN | `node.plan` | `agent.turn` | 632.1 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.0 ms | - |


### 状态层代表：`v2_safe_escalation_01`

- case：`v2_safe_escalation_01`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/00809654e0f16304dde770fd197f5bc3)
- trace id：`00809654e0f16304dde770fd197f5bc3`；run id：`trace-evidence-matrix-full-20261020`
- 根状态：intent=`order_status` / state=`validation_failed` / tool=`query_order_status` / error=`VALIDATION_ERROR` / plan_source=`llm`
- 槽位：order_id=`None` / source=`None`；session=`None`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 202, "output": 111, "total": 313}`
- 耗时：整轮 1631.4 ms；`llm.plan` span 1627.7 ms；模型调用 1627.5 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
把客户 CUST-001 的订单列表全部导出来
```

模型输出：

```json
[response]
{"intent": "order_status", "order_id": null}

[plan_source]
llm
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 1631.4 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 1627.7 ms | 1627.5 ms |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.plan` | `agent.turn` | 1627.8 ms | - |


### 工具层定位探针：`trace_tool_locator_timeout`（合成）

- case：`trace_tool_locator_timeout`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/9fe2e0caa501f2804ce16cc9513a1e37)
- trace id：`9fe2e0caa501f2804ce16cc9513a1e37`；run id：`trace-evidence-tool-locator-20261020`
- 根状态：intent=`order_status` / state=`tool_failed` / tool=`query_order_status` / error=`TOOL_TIMEOUT` / plan_source=`llm`
- 槽位：order_id=`ORD-1001` / source=`verbatim`；session=`None`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 200, "output": 49, "total": 249}`
- 耗时：整轮 5445.0 ms；`llm.plan` span 5430.8 ms；模型调用 2968.5 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
我的订单 ORD-1001 到哪了
```

模型输出：

```json
[response]
{"intent":"order_status","order_id":"ORD-1001"}

[plan_source]
llm
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 5445.0 ms | - |
| CHAIN | `node.plan` | `agent.turn` | 5431.0 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 5430.8 ms | 2968.5 ms |
| TOOL | `tool.query_order_status` | `node.dispatch` | 0.0 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.1 ms | - |


### 模型降级代表：`mt_ref_03#t1`（llm-fallback）

- case：`mt_ref_03#t1`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/63a9a638859898dc3a82af240546d457)
- trace id：`63a9a638859898dc3a82af240546d457`；run id：`trace-evidence-multiturn-full-20261020`
- 根状态：intent=`refund` / state=`tool_completed` / tool=`apply_refund` / error=`None` / plan_source=`llm-fallback`
- 槽位：order_id=`ORD-1002` / source=`inherited`；session=`MT-003`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 249, "output": 128, "total": 377}`
- 耗时：整轮 1104.5 ms；`llm.plan` span 1101.2 ms；模型调用 1101.0 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。
会话上下文（仅供理解指代，不得据此编造订单号）：会话上下文：上一轮提到的订单号是 ORD-1002。只有在用户本轮自己没给出订单号时才…(+28 chars)

[user]
它现在还能退款吗
```

模型输出：

```json
[response]


[plan_source]
llm-fallback
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 1104.5 ms | - |
| CHAIN | `node.plan` | `agent.turn` | 1101.3 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 1101.2 ms | 1101.0 ms |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| TOOL | `tool.apply_refund` | `node.dispatch` | 0.1 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.1 ms | - |
| SPAN | `state.update` | `node.respond` | 0.0 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.1 ms | - |


## 多轮：72 条 trace，0 条失败

全部 36 条多轮用例（72 轮）走真实模型，判分 0/72 不通过。第二轮 `mt_ref_01#t1` 的
`slot_source=inherited`，`slot_order_id=ORD-1001`，并且每轮都有 `state.update` span。

### 多轮第 0 轮：`mt_ref_01#t0`（verbatim）

- case：`mt_ref_01#t0`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/cea8f54fb9ce788658779ee762a54a8f)
- trace id：`cea8f54fb9ce788658779ee762a54a8f`；run id：`trace-evidence-multiturn-20261020`
- 根状态：intent=`order_status` / state=`tool_completed` / tool=`query_order_status` / error=`None` / plan_source=`llm`
- 槽位：order_id=`ORD-1001` / source=`verbatim`；session=`MT-001`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 203, "output": 16, "total": 219}`
- 耗时：整轮 5014.6 ms；`llm.plan` span 5003.1 ms；模型调用 2500.0 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
帮我查一下订单 ORD-1001 现在到哪了
```

模型输出：

```json
[response]
{"intent":"order_status","order_id":"ORD-1001"}

[plan_source]
llm
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 5014.6 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 5003.1 ms | 2500.0 ms |
| CHAIN | `node.plan` | `agent.turn` | 5003.2 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.1 ms | - |
| TOOL | `tool.query_order_status` | `node.dispatch` | 0.0 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |
| SPAN | `state.update` | `node.respond` | 0.0 ms | - |


### 多轮第 1 轮：`mt_ref_01#t1`（inherited）

- case：`mt_ref_01#t1`；trace：[打开 Langfuse](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/b0b5d7fe12bd5e1153c733fa8798e59a)
- trace id：`b0b5d7fe12bd5e1153c733fa8798e59a`；run id：`trace-evidence-multiturn-20261020`
- 根状态：intent=`order_status` / state=`tool_completed` / tool=`query_order_status` / error=`None` / plan_source=`llm-fallback`
- 槽位：order_id=`ORD-1001` / source=`inherited`；session=`MT-001`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 250, "output": 128, "total": 378}`
- 耗时：整轮 4127.4 ms；`llm.plan` span 4123.0 ms；模型调用 4122.7 ms

Prompt 输入（截断 1000 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。
会话上下文（仅供理解指代，不得据此编造订单号）：会话上下文：上一轮提到的订单号是 ORD-1001。只有在用户本轮自己没给出订单号时才…(+28 chars)

[user]
这个订单现在什么状态了
```

模型输出：

```json
[response]
{"intent": "order_status", "order_id": null

[plan_source]
llm-fallback
```

| type | name | parent | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 4127.4 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 4123.0 ms | 4122.7 ms |
| CHAIN | `node.dispatch` | `agent.turn` | 0.1 ms | - |
| CHAIN | `node.plan` | `agent.turn` | 4123.2 ms | - |
| SPAN | `state.update` | `node.respond` | 0.0 ms | - |
| TOOL | `tool.query_order_status` | `node.dispatch` | 0.0 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.1 ms | - |


## 口径边界

- 这是**主矩阵 + 故障 + 多轮**的模型臂，不是 310 条既有 trace 的逐条模型重跑；RAG 48 与故障 25 的后续全量模型臂见
  [`../rag-fault-full/EVIDENCE.md`](../rag-fault-full/EVIDENCE.md)。
- 310 条原始 trace 中有 6 条在模型调用前被校验拦下，因此只有 304 条有 `GENERATION`；这不是缺 span。
- 3 条失败是单次运行结果，不能当稳定失败率；后续 60 条 ×3 方差见
  [`../variance-repeat3/EVIDENCE.md`](../variance-repeat3/EVIDENCE.md)。
- 24/279 个实际模型调用 turn 发生规则臂降级；任务通过不等于模型输出可用，
  这条指标应和通过率一起看。
- Langfuse v2 observations 在 SDK 5.0.2 批量导出时会压缩 `startTime` / `endTime`，耗时统一取本地
  `span.duration_ms`；`llm.plan` span 与模型调用计时分开列。
- 状态层那条的定位存在优先级歧义：`trace_report.py` 先看 `validation_failed + tool_name`，因此落在
  `state`；逐字段差异显示根因更可能在 `reasoning/intent`。这条应作为定位规则改进项，而不是“状态管理已完全定位”。
