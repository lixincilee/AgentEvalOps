# 并发压测（M4）

压的是 `agentevalops.service`：一个把 v3 链路（LangGraph + 检索 + 会话）包成 HTTP 的门面。
服务端本体在 `src/agentevalops/service.py`，这里只有客户端侧的东西。

## 跑起来

```powershell
# 契约测试：离线、零成本、不起服务器
D:\Python\python.exe -m pytest tests/test_service.py -q

# mock 模式（默认，零成本）：建模 60ms 延迟 + 固定 token，成本按真实价目折算
D:\Python\python.exe scripts/load_test.py --users 20 --spawn-rate 5 --run-time 20s
D:\Python\python.exe scripts/load_test.py --users 50 --spawn-rate 10 --run-time 20s --port 8124

# 纯规则臂（零延迟、零成本，用来区分「服务端开销」与「建模延迟」）
D:\Python\python.exe scripts/load_test.py --arm rules --users 20 --run-time 20s --port 8125

# 真实调用（会花钱）：必须 --yes，且环境里要有 DEEPSEEK_API_KEY
D:\Python\python.exe scripts/load_test.py --real --yes --users 5 --run-time 30s --budget 0.5

# 压方差：同一份配置连跑 5 次，每次都重启服务（新进程、新端口）
D:\Python\python.exe scripts/load_test.py --arm rules --users 20 --spawn-rate 5 --run-time 15s --repeat 5 --report reports/variance-rules-15s-5x.json
```

`--repeat N` 汇总出来的是一份报告，不是 N 份：延迟 / RPS / 成本取**中位数**，
`http_error_rate` 取**max**、`task_success_rate` 取**min**（最坏值）。理由很直接——
三次里一次 5% 错误率、两次 0%，取中位数就是 0%，**一次真实故障会被平均掉**。
每次运行的原始数字留在 `runs[]`，极差与相对极差留在 `variance[]`，汇总口径写在
`aggregate_policy` 里。两边请求数都取中位数，对账明细逐次留在 `locust.consistency_by_run`。

退出码：`0` 通过；`1` 门禁没过；`2` 根本起不来。门禁两条线都要过：

| 指标 | 判据 | 判的是什么 |
|---|---|---|
| `http_error_rate` | ≤ 1% | **传输层**：非 200、连接异常、超时、响应不是 JSON |
| `task_success_rate` | ≥ 99% | 这一轮的终态是不是**这条消息该有的结果** |

阈值写死在 `scripts/load_test.py` 里，不接受命令行放宽——能现场放宽的门禁等于没有。

## 什么算「成功」由期望表定义

`perf/locustfile.py` 顶部的 `EXPECTATIONS` 是唯一的判据来源：每条消息列出允许的
`(state, error_code)` 组合。**拒答（`no_action` + `NO_KNOWLEDGE`）是正确结果**，重复退款也是
（`RefundTool` 对同一 `(order_id, customer_id)` 幂等，第二次起返回同一个 REF 号且仍算成功）。
把这些算进错误率，等于说「系统越守规矩，错误率越高」。

报告里会同时给出 `terminal_states`（各终态次数）和 `error_codes`（各错误码次数）——失败数
必须能自己把话说清楚，而不是留一个孤零零的百分比。

## 两种模式差在哪

| | 模型臂 | 延迟 | token / 成本 |
|---|---|---|---|
| `mock` / `modeled` | `LatencyLLMClient` | `time.sleep(60ms)`，**建模值** | 固定 260 + 30，按真实价目折算 |
| `mock` / `rules` | `RulePlannerV3` | 无（纯 CPU） | 恒为 0 |
| `real` | `GroundedLLMPlanner` + `DeepSeekClient` | 供应商真实延迟 | 供应商报回的 usage，真实账单 |

`LatencyLLMClient` 一个字节都不发。报告里的 `provenance` 字段会明确写
`{"latency": "modeled", "tokens": "modeled"}`，读者不必猜这些数字是谁的。mock 模式的
RPS / P95 / 成本只能读作「该配置下**服务端**的行为」，不能读作「供应商的产能或报价」。

## 压测画像

`perf/locustfile.py` 按权重发 6 类请求：查单、退款、转人工、政策问答、拒答未知主题，
以及一组两轮的多轮请求（同一个 `session_id`，第二轮用「这个」指代第一轮的订单号，
压会话状态）。只有传输层失败才记 `response.failure()`；终态是否符合期望记进任务级统计，
两者在报告里分成两条线。

## 已知边界

- `/metrics` 是**进程内**计数。`uvicorn --workers N` 会让每个进程各有一份，脚本必须逐进程
  聚合；本脚本按单进程多线程跑（anyio 线程池默认 40 线程，那也是并发的真实上界）。
- 服务对**共享 agent 实例**加了一把锁（`pipeline` 把每轮状态挂在实例属性上，不串行会串台）。
  压测里模型臂一律传**工厂**：每线程一个 agent、共享一个 `SessionStore`。注意
  `V3Agent.__init__` 写的是 `sessions or SessionStore()`，而 `SessionStore` 有 `__len__`——
  **空的**共享 store 是假值，会被悄悄换掉，所以脚本里用了 `SharedSessionStore`（恒为真值）。
- 请求数对账取 **locust 停表那一刻**的服务端快照；停表后仍在飞的请求会落进
  `service_metrics_final`，差额写在报告的 `notes` 里。RPS 用 locust 实测窗口折算
  （首个到最后一个请求），所以会比 `-t` 的名义时长略高：locust 启动的那一秒不算分母。
  「差多少还算同一次运行」用的是相对判据（绝对值 5 与 0.5% 取大，见
  `inflight_tolerance()`）——固定的绝对值到了 200+ RPS 就会误报。
- **单次运行的性能数字不能用来比较版本**。同一份配置在本机跑三次实测 RPS
  `125.4 / 179.2 / 210.1`、P95 `220 / 130 / 100` ms，代码一个字没改。所以有了 `--repeat N`：
  背靠背 5 次实测 P95 相对极差 25%、RPS 8.9%（`docs/stage2-evidence.md` §13）。
  **注意两件事**：(1) `--repeat` 压的是**会话内**噪声，压不掉跨会话漂移，所以两个版本要在
  同一个会话里交替跑；(2) P99 在 N=5 时的相对极差是 118.9%（一个离群点就够了），门禁只认
  `perf.p95_ms`。
- `--budget` 靠轮询 `/metrics` 触发，粒度 1 秒；被打断的那次不会有 locust 统计 JSON，
  报告里 `budget_exceeded=true`、`task_success_rate=null`，门禁按不通过处理。
