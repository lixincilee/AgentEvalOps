# RAG 48 条与故障 25 条全量模型臂证据（2026-10-20）

## 1. 这次补了什么

此前 310 条真实模型 trace 覆盖主矩阵、多轮和 6 条故障代表样本，但 RAG 48 条只跑过 6 条，
故障 25 条只跑过 6 条。本节把两个缺口补齐：

- **RAG 48 条全量**：rag 22 / rag_stale 8 / rag_poison 8 / rag_refuse 10。
- **故障 25 条全量**：工具超时 7 / 工具不可用 6 / 空检索 6 / 模型限流 6。

两批都使用 `deepseek-flash`、真实 Langfuse trace 和同一套字段级判分。合计 **73 条 trace**，
新增花费 **CNY 0.0246**（RAG 0.0177 + 故障 0.0069）。

## 2. 运行命令

```powershell
$env:LANGFUSE_TIMEOUT = "60"

.venv-trace\Scripts\python.exe scripts\trace_run.py `
  --cases eval_cases\rag_cases.jsonl `
  --llm --yes --budget 0.15 --langfuse `
  --run-id rag-full-20261020 `
  --out reports\traces-langfuse-llm-rag-full.jsonl

.venv-trace\Scripts\python.exe scripts\trace_run.py `
  --faults eval_cases\fault_cases.jsonl `
  --llm --yes --budget 0.10 --langfuse `
  --run-id faults-full-20261020 `
  --out reports\traces-langfuse-llm-faults-full.jsonl
```

定位报告：

```powershell
D:\Python\python.exe scripts\trace_report.py `
  --traces reports\traces-langfuse-llm-rag-full.jsonl `
  --cases reports\traces-langfuse-llm-rag-full.cases.jsonl `
  --out reports\trace-report-llm-rag-full.md `
  --json reports\trace-report-llm-rag-full.json

D:\Python\python.exe scripts\trace_report.py `
  --traces reports\traces-langfuse-llm-faults-full.jsonl `
  --cases reports\fault-cases-model-arm.jsonl `
  --out reports\trace-report-llm-faults-full.md `
  --json reports\trace-report-llm-faults-full.json
```

## 3. RAG 48 条

**结果：48/48 通过，0 条定位失败。**

| 类别 | 用例 | 有检索依据 | 拒答（NO_KNOWLEDGE） | 模型 JSON 可解析 |
| --- | ---: | ---: | ---: | ---: |
| rag | 22 | 22 | 0 | 22 |
| rag_stale | 8 | 8 | 0 | 8 |
| rag_poison | 8 | 8 | 0 | 8 |
| rag_refuse | 10 | 0 | 10 | 10 |
| **合计** | **48** | **38** | **10** | **48** |

| 指标 | 结果 |
| --- | ---: |
| 期望 gold 文档命中 | **38 / 38** |
| 期望 Top-1 文档命中 | **28 / 28** |
| 期望过期文档标记命中 | **13 / 13** |
| 模型调用 | 48 |
| tokens | 11,535 |
| 模型延迟中位数 / P95 / 最大 | 893.67 / 1102.66 / 4432.38 ms |
| 花费 | **CNY 0.0177** |

所有 10 条 `rag_refuse` 都是**检索 0 命中后拒答**，不是检索链路异常；所有 `rag_stale` 的旧版
文档都被标记为过期，`rag_poison` 的 gold 文档也在候选集中。模型 48/48 输出可解析，没有
`llm-fallback`。

## 4. 故障 25 条

**结果：25/25 的意图、状态、工具、错误码和 `plan_source` 与模型臂期望一致。**

| 类别 | 用例 | 实际结果 |
| --- | ---: | --- |
| 工具超时 | 7 | 7 条 `TOOL_TIMEOUT` |
| 工具不可用 | 6 | 6 条 `TOOL_UNAVAILABLE` |
| 空检索 | 6 | 5 条 `NO_KNOWLEDGE` + 1 条业务动作不受检索故障影响 |
| 模型限流 | 6 | 6 条 `llm-fallback`，任务仍完成 |

**判定口径的关键点**：`eval_cases/fault_cases.jsonl` 里 19 条非限流故障的
`expected_plan_source` 写的是 `rules`，那是规则臂契约。模型臂自然应该是 `llm`；如果直接拿
源用例判，`trace_report.py` 会报 19 条失败，但差异**只有这一个字段**。因此本目录保留了
`fault-cases.jsonl`（源用例），另生成 `fault-cases-model-arm.jsonl`：只把 19 条改为
`expected_plan_source=llm`，6 条限流的 `llm-fallback` 不变。按模型臂视图重跑后是 **0/25 失败**。

| 指标 | 结果 |
| --- | ---: |
| 期望字段匹配 | **25 / 25** |
| trace 内成功模型生成 | 19 |
| 预期限流错误生成 | 6 |
| CLI 计费模型调用 | 21（含 2 次成功的 warmup；不计入 25 条 trace 结果） |
| tokens | 4,430 |
| 成功生成延迟中位数 / P95 / 最大 | 887.52 / 1298.04 / 2561.06 ms |
| 花费 | **CNY 0.0069** |

6 条限流用例的 `llm.plan` 是预期 `ERROR`（HTTP 429），随后规则臂降级并完成任务；
这不是产品失败，也不应该被通过率隐藏。

## 5. Langfuse 回查

回查使用 `GET /api/public/v2/observations`，字段选择
`core,basic,time,io,model,usage,prompt,metrics,trace_context`，并比较本地与远端的 span 边集合。

| 检查 | 结果 |
| --- | ---: |
| trace 到达 | **73 / 73** |
| 根 span `agent.turn` | 73 / 73 |
| span 树边集合与本地一致 | 73 / 73 |
| `GENERATION` span | 73 |
| 其中成功生成 | 67 |
| 其中预期限流错误 | 6 |
| Prompt 非空 | 73 / 73 |
| 输出非空 | 73 / 73 |
| 成功生成 usage 非空 | 67 / 67 |
| 成功生成模型为 `deepseek-flash` | 67 / 67 |
| `llm.plan` 父节点为 `node.plan` | 73 / 73 |
| 未匹配项 | **0** |

机器可读明细见 `langfuse-verification.json`；每条 trace 的本地 trace id、远端 trace id、
span 数、树结构、Prompt / 输出 / usage 检查都在里面。

## 6. 文件

| 文件 | 内容 |
| --- | --- |
| `rag-traces.jsonl` | RAG 48 条本地 trace |
| `rag-cases.jsonl` | RAG 48 条期望值 |
| `trace-report-rag.md` / `.json` | RAG 定位报告（0/48 失败） |
| `fault-traces.jsonl` | 故障 25 条本地 trace |
| `fault-cases.jsonl` | 源故障用例（规则臂期望） |
| `fault-cases-model-arm.jsonl` | 模型臂评测视图（只改 19 条 `expected_plan_source`） |
| `trace-report-faults.md` / `.json` | 故障定位报告（模型臂视图 0/25 失败） |
| `langfuse-verification.json` | 73 条 trace 的 API 回查与逐条匹配结果 |
| `summary.json` | 机器可读汇总 |

## 7. 边界

- 这是模型臂**单次运行**，不提供通过率方差；方差证据见 `../variance-repeat3/`。
- 故障用例的 `rules → llm` 是评测视图，不是产品行为变化；源用例没有修改。
- 6 条限流 `GENERATION` 错误是注入的预期故障，不应计入产品失败数。
- RAG 只覆盖项目构造的本地知识库，不代表外部企业知识库；检索质量也不等于答案忠实性。
