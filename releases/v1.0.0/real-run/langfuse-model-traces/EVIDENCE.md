# 真实模型 Langfuse Trace（16 条，2026-10-20）

- 代码：`6a2c143`（v1.0.0 发布后）
- 模型：`deepseek-flash`
- 切片：主矩阵 10 条 + RAG 6 条，共 **16 条 trace / 16 次模型调用**
- 有效花费：CNY 0.0067（矩阵）+ CNY 0.0042（RAG）= **CNY 0.0109**；另有一次 Langfuse
  导出超时重跑（模型调用 CNY 0.0070，未计入有效证据），实际模型花费合计 **CNY 0.0179**
- Langfuse：`https://jp.cloud.langfuse.com`，项目 `cmvf9rprr002mad0k9ckxaig4`
- 核对：`GET /api/public/v2/observations` 返回 **16/16**，每条都有 `GENERATION` span
- Prompt / 模型输出 / usage：**16/16 非空**
- 父子层级：**16/16 与本地 trace 一致**（`metadata.parent_name` 对本地 `parent_id`）
- 机器可读明细：[`model-traces.json`](model-traces.json)

## 主矩阵代表：`v2_status_zh_01`

- trace：[`v2_status_zh_01` 在 Langfuse 中打开](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/2d5696d177bdf0efea4978661d09265f)
- trace id：`2d5696d177bdf0efea4978661d09265f`；run id：`trace-20261020T020722Z`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 203, "output": 42, "total": 245}`
- 一轮 trace 总耗时（本地）：5700.0 ms；`llm.plan` span：5684.3 ms；
  模型调用计时：2972.9 ms

Prompt 输入（截断 1200 字）：

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
{"intent": "order_status", "order_id": "ORD-1001"}

[plan_source]
llm
```

| type | name | parent（本地树） | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 5700.0 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.1 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 5684.3 ms | 2972.9 ms |
| CHAIN | `node.plan` | `agent.turn` | 5684.5 ms | - |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |
| TOOL | `tool.query_order_status` | `node.dispatch` | 0.0 ms | - |


## RAG 代表：`rag_refund_window`

- trace：[`rag_refund_window` 在 Langfuse 中打开](https://jp.cloud.langfuse.com/project/cmvf9rprr002mad0k9ckxaig4/traces/e2554060d905cc581ee04cea76319516)
- trace id：`e2554060d905cc581ee04cea76319516`；run id：`trace-20261020T020840Z`
- 模型：`deepseek-flash`；prompt 版本：`v3`；usage：`{"input": 199, "output": 36, "total": 235}`
- 一轮 trace 总耗时（本地）：5436.2 ms；`llm.plan` span：5424.3 ms；
  模型调用计时：2860.4 ms

Prompt 输入（截断 1200 字）：

```text
[system]
你是电商客服系统的意图识别模块。只输出一个 JSON 对象，不要输出任何解释或代码块标记。
字段：
- "intent"：order_status / refund / human_handoff / policy_qa / unknown 之一
- "order_id"：用户消息中出现的订单号，形如 ORD-1001；没有就填 null
规则：order_id 必须原样出现在用户消息里，禁止编造；无法判断意图时填 unknown。
policy_qa 用于询问平台通用规则、条款、时效或费用标准（知识库能回答的通用事实）；若用户想办理某个具体订单的业务，仍按业务动作判：查单=order_status、退款=refund、要人工=human_handoff。

[user]
退款政策里无理由退款的期限是怎么规定的
```

模型输出：

```json
[response]
{"intent": "policy_qa", "order_id": null}

[plan_source]
llm
```

| type | name | parent（本地树） | span 耗时（本地） | 模型调用计时（本地） |
| --- | --- | --- | --- | --- |
| CHAIN | `agent.turn` | `trace 根` | 5436.2 ms | - |
| CHAIN | `node.validate` | `agent.turn` | 0.0 ms | - |
| RETRIEVER | `retriever.search` | `node.retrieve` | 0.4 ms | - |
| CHAIN | `node.plan` | `agent.turn` | 5424.4 ms | - |
| CHAIN | `node.retrieve` | `agent.turn` | 0.5 ms | - |
| CHAIN | `node.dispatch` | `agent.turn` | 0.0 ms | - |
| GENERATION | `llm.plan` | `node.plan` | 5424.3 ms | 2860.4 ms |
| CHAIN | `node.respond` | `agent.turn` | 0.0 ms | - |


## 口径边界

- 这 16 条补齐了此前缺失的 Prompt / 模型响应 / token 证据；训练与线上系统本身不依赖 Langfuse。
- 16 条只代表两个切片，不是 310 条全量 trace 的模型臂；方差仍未测。
- 发送时本机代理失效，改为直连 `jp.cloud.langfuse.com`，并把 SDK 超时从默认 5 秒提高到 60 秒。
- Langfuse v2 observations 在 SDK 5.0.2 批量导出时会压缩 `startTime` / `endTime`（同一毫秒），
  `latency` 也常为 `null`；因此本文耗时统一取**本地 trace 的 `span.duration_ms`**。
  `llm.plan` 的 span 耗时包含客户端初始化与 SDK 重试，模型本身的调用计时取
  `span.attributes.latency_ms`，两者分别列出。
