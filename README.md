# AgentEvalOps

> 一个用于练习 **Agent 评测工程（Evaluation Engineering / EvalOps）** 的教学项目。
> 当前进度 **阶段二 M3**：确定性 Agent（v1）→ DeepSeek 规划层（v2）→ **LangGraph 编排 + 知识库检索 + 多轮会话（v3）**。
> 三个版本跑在同一批用例、同一判分口径下，产出结构完全相同的 `RunMetrics`，差异才有归因价值。

## 为什么先做确定性版本

评测工程最核心的能力是：**把一个 AI 应用的行为变成可复现、可断言、可度量的对象**。
如果一上来就接大模型，你面对的是随机输出，很难判断失败究竟来自提示词、模型、工具，还是你自己的断言写错了。

所以阶段一先把「被测系统」做成纯规则、无网络、无随机数的确定性程序：

| 特性 | 说明 |
| --- | --- |
| 确定性 | 无随机数、无网络；相同内部状态下，相同输入必得相同输出，测试可精确断言到字段 |
| 无外部依赖 | 运行期零第三方依赖，不调用任何模型或网络 |
| 有审计记录 | 每次决策都产出 `AuditRecord`：意图、状态、工具名、参数、结果、错误 |
| 可度量 | 意图/状态/工具/参数都能逐条比对，天然产出准确率指标 |

阶段一验收通过后，阶段二会把「意图识别 + 槽位抽取」换成真实大模型，而**测试、契约和 JSONL 场景数据几乎不用改** —— 这正是先建基线的价值。

## 目录结构

```
AgentEvalOps/
├── pyproject.toml                     # 项目元数据、依赖、pytest 配置
├── README.md                          # 本文件
├── .gitignore
├── .env.example                       # API Key 等环境变量模板
├── docs/
│   ├── agent-spec.md                  # 行为规格 v1.3（冻结）：… / 多轮会话 / 判分口径 / 故障注入口径（§2.11 §7.3）
│   ├── stage2-plan.md                 # 阶段二施工图与里程碑
│   ├── stage2-evidence.md             # 阶段二实测证据与复现命令
│   ├── ci.md                          # M5：CI 三个 job、门禁判据、本地复刻、"没验证过的部分"
│   └── bad-cases-v2.md                # v2 的 9 处差异逐条归类与根因
├── .github/
│   └── workflows/regression.yml       # M5：test / gate / nightly-real 三个 job
├── scripts/
│   ├── check_env.py                   # 密钥体检（只打印长度与前缀，不泄密）
│   ├── smoke_llm.py                   # LLM 连通性烟囱测试（--save 可落盘证据）
│   ├── compare_planners.py            # v1 规则版 vs v2 模型版 对比运行器（二元）
│   ├── compare_versions.py            # v1 / v2 / v3 三方对比运行器（M3 新增）
│   ├── gen_case_matrix.py             # 用例矩阵脚手架（只报告偏差，不再定义期望值）
│   ├── run_faults.py                  # M4：跑故障注入用例集并落盘报告
│   ├── load_test.py                   # M4：起服务 + Locust 压测 + P95/成本汇总 + CI 门禁
│   ├── freeze_release.py              # M5：跑齐离线检查并冻结到 releases/<tag>/
│   └── regression_gate.py             # M5：候选 vs 基线，按阈值判退化（退出码给 CI 用）
├── reports/                           # 运行产物（已 gitignore）：评测报告 JSON，可当 CI 工件
├── releases/                          # M5：冻结的发布（要提交进 git）——回归门禁的比较对象
│   └── v0.7.0/                        #   release.json（指标索引）+ 五项检查的原始报告
├── perf/                              # M4：压测客户端（服务端本体在 src/agentevalops/service.py）
│   ├── latency_client.py              # 建模用的假模型：只睡 60ms + 固定 token，不发网络请求
│   ├── locustfile.py                  # Locust 用户画像与期望表（什么算「任务成功」的唯一来源）
│   └── README.md                      # 压测口径与已知边界
├── eval_cases/
│   ├── support_cases.jsonl            # 阶段一场景集（19 条，v1 亲和的基线集）
│   ├── v1_gaps_cases.jsonl            # v1 已知漏判集（8 条，用来检验评测集区分度）
│   ├── case_matrix_v2.jsonl           # 阶段二用例矩阵（213 条；主矩阵三份合计 240 条）
│   ├── rag_cases.jsonl                # M3 检索层用例（字段契约见规格 §7.1）
│   ├── multiturn_cases.jsonl          # M3 多轮会话用例（字段契约见规格 §7.2）
│   └── fault_cases.jsonl              # M4 故障注入用例（25 条：超时 / 5xx / 空检索 / 限流）
├── src/
│   └── agentevalops/                  # 生产代码（src-layout，强制安装后再运行）
│       ├── __init__.py                # 对外公开的 API 汇总
│       ├── models.py                  # 数据契约：Intent / AgentState / ErrorCode / ToolResult / AuditRecord / AgentResponse
│       ├── order_store.py             # 订单仓储，只读 + 归属校验
│       ├── tools.py                   # 三个工具：查单 / 退款 / 建人工工单
│       ├── agent.py                   # Agent 主流程：校验请求 → 规划 → 调用一个工具
│       ├── planner.py                 # 规划层（规则臂 v1 / v3 + 模型臂 v2 / v3 + 幻觉守卫）
│       ├── retrieval.py               # M3：本地知识库 + 确定性检索器 + search_knowledge_base
│       ├── session.py                 # M3：多轮会话状态与槽位继承（带来源标记）
│       ├── pipeline.py                # M3：v3 的 LangGraph 编排 validate→retrieve→plan→dispatch→respond
│       ├── config.py                  # 阶段二：环境变量 / .env 加载
│       ├── versioning.py              # 阶段二：VersionManifest 版本指纹
│       ├── runs.py                    # 阶段二：Run / CaseResult / RunMetrics
│       ├── spec.py                    # 行为规格版本 + 基线偏差清单（规格冻结）
│       ├── faults.py                  # M4：故障注入脚手架（生产路径不含任何开关）
│       ├── service.py                 # M4：把 v3 链路包成 HTTP 门面（/chat /healthz /metrics）
│       ├── llm/                       # 阶段二：base / deepseek / fake / costing
│       └── data/
│           ├── __init__.py
│           ├── orders.json            # 本地模拟订单数据
│           └── knowledge_base.json    # M3：本地知识库语料（含过期版本与投毒样本）
└── tests/                             # 全部测试项（含参数化展开）
    ├── conftest.py                    # 共享 fixture + JSONL 读取工具
    ├── test_contracts.py              # 契约测试：返回结构、错误码词表、枚举取值
    ├── test_order_store.py            # 仓储单元测试
    ├── test_tools.py                  # 工具单元 + 边界 + 越权测试
    ├── test_agent.py                  # Agent 端到端测试 + 确定性测试
    ├── test_scenarios_jsonl.py        # 数据驱动测试：逐条回放 JSONL 场景
    ├── test_v1_gaps.py                # 区分度检查：v1 必须「明知故错」的那批用例
    ├── test_case_matrix_v2.py         # 阶段二用例矩阵的 schema 与回放
    ├── test_comparison_harness.py     # 编排层：离线自检零差异、报告数字自洽
    ├── test_spec_conformance.py       # 规格条款的可执行断言（含已登记偏差的 xfail）
    ├── test_planner.py                # 规划层：v1/v2 决策、幻觉守卫、契约不变
    ├── test_safety.py                 # 基础安全场景：越权 / 枚举 / 注入
    ├── test_config.py                 # 环境变量 / .env 解析
    ├── test_pricing.py                # 价目表解析、别名诚实性、客户端装配
    ├── test_llm_offline.py            # 离线假客户端与消息契约
    ├── test_costing.py                # 成本核算与预算熔断
    ├── test_deepseek_client.py        # DeepSeek 适配器（桩客户端，无网络）
    ├── test_versioning.py             # 版本清单哈希与稳定性
    ├── test_runs.py                   # 运行聚合与落盘
    ├── test_retrieval.py              # M3 检索层：语料完整性、打分口径、只读工具契约
    ├── test_session_state.py          # M3 会话状态：槽位来源、指代消解、会话隔离
    ├── test_pipeline_v3.py            # M3 v3 编排：节点顺序、检索、多轮、LLM 臂
    ├── test_rag_cases.py              # M3 数据驱动：回放 rag_cases.jsonl
    ├── test_multiturn_cases.py        # M3 数据驱动：回放 multiturn_cases.jsonl
    ├── test_version_comparison.py     # M3 v1/v2/v3 三方对比器（离线、零成本）
    ├── test_faults.py                 # M4 故障注入：注入真的生效、口径与规格条款一致
    ├── test_fault_cases.py            # M4 数据驱动：回放 fault_cases.jsonl
    ├── test_service.py                # M4 HTTP 契约：状态码、审计结构、/metrics 记账
    ├── test_regression_gate.py        # M5 门禁判据：该拦的拦得住、不该拦的别拦
    └── test_freeze_release.py         # M5 冻结器：pytest 计数解析、指标拍平、索引渲染
```

## 快速开始

### 1. 环境要求

- Python **3.11+**（代码使用了 `StrEnum`）
- 运行期零第三方依赖；测试只需要 `pytest`

### 2. 安装（可编辑模式）

```bash
python -m pip install -e ".[dev]"
```

本机（Windows / PowerShell）示例：

```powershell
D:\Python\python.exe -m pip install -e ".[dev]" --no-build-isolation
```

`--no-build-isolation` 表示直接使用本机已安装的 setuptools 构建，适合离线环境。

### 3. 运行测试

```bash
python -m pytest                                # 全量：649 passed（另有 10 条已登记偏差 xfail）
python -m pytest -q                             # 精简输出
python -m pytest -v                             # 逐条列出用例名
python -m pytest tests/test_safety.py           # 只跑安全场景
python -m pytest -k refund                      # 只跑名字含 refund 的用例
python -m pytest --collect-only -q              # 只看收集到哪些用例
```

### 4. 手动跑一个例子

```python
from agentevalops import EcommerceSupportAgent

agent = EcommerceSupportAgent()
response = agent.handle("请查询订单 ORD-1001 的物流状态", customer_id="CUST-001")

print(response.message)          # request completed
print(response.intent)           # order_status
print(response.state)            # tool_completed
print(response.audit.tool_name)  # query_order_status
print(response.audit.arguments)  # {'order_id': 'ORD-1001', 'customer_id': 'CUST-001'}
print(response.audit.result)     # {'order_id': 'ORD-1001', 'status': 'shipped', ...}
```

## 核心概念

### 意图 `Intent`

`order_status` / `refund` / `human_handoff` / `unknown`

### 状态 `AgentState`

| 状态 | 含义 |
| --- | --- |
| `tool_completed` | 工具调用成功 |
| `tool_failed` | 工具被调用但业务上拒绝（如订单不存在、不可退款） |
| `validation_failed` | 参数不全，**根本没有调用工具**（如缺少订单号） |
| `no_action` | 意图超出范围，不调用任何工具 |
| `received` | **预留**：阶段一不会产生该状态，为后续图式编排保留 |

`tool_failed` 与 `validation_failed` 的区分很重要：前者是「业务拒绝」，后者是「Agent 自身没把话说清楚」。评测时两类失败要分别统计。

### 错误码 `ErrorCode`

`INVALID_ARGUMENT` / `ORDER_NOT_FOUND` / `REFUND_NOT_ALLOWED` / `VALIDATION_ERROR` / `UNSUPPORTED_INTENT`

### 审计记录 `AuditRecord`

每次请求都产出六元组，这是整个项目的「评测抓手」：

```
intent     意图
state      状态
tool_name  工具名称
arguments  传进工具的参数
result     工具成功时的结果
error      工具失败时的 {code, message}
```

## 三个工具

| 工具 | `name` | 成功结果 | 主要拒绝原因 |
| --- | --- | --- | --- |
| 订单状态查询 | `query_order_status` | `order_id` / `status` / `tracking_number` | `ORDER_NOT_FOUND` |
| 退款申请 | `apply_refund` | `refund_id` / `order_id` / `status` / `duplicate` | `REFUND_NOT_ALLOWED`、`ORDER_NOT_FOUND` |
| 人工工单 | `create_human_ticket` | `ticket_id` / `status` / `customer_id` / `order_id` | `ORDER_NOT_FOUND` |

三个工具都遵守同一条契约：`execute(...) -> ToolResult`。`ToolResult` 只有两种形态 —— 成功（带 `data`）或失败（带 `error_code` + `error_message`），不会把异常抛给上层。

### 业务规则（当前实现）

- **只有 `delivered`（已签收）的订单可以退款**；`shipped`（运输中）、`cancelled`（已取消）都会被拒绝。
- 同一订单重复申请退款是**幂等**的：第二次返回同一个 `refund_id`，且 `duplicate=true`。
- 退款理由最多 **500** 字；工单摘要最多 **1000** 字。
- **越权与不存在返回完全相同的错误**（`ORDER_NOT_FOUND`），避免攻击者借此枚举他人的订单号。

## 模拟订单数据

`src/agentevalops/data/orders.json`：

| order_id | customer_id | status | 备注 |
| --- | --- | --- | --- |
| `ORD-1001` | `CUST-001` | `shipped` | 运输中 → 不可退款 |
| `ORD-1002` | `CUST-001` | `delivered` | 已签收 → 可退款 |
| `ORD-1003` | `CUST-001` | `cancelled` | 已取消 → 不可退款 |
| `ORD-1004` | `CUST-001` | `delivered` | 第二个可退款订单 → 多订单互不干扰 |
| `ORD-1005` | `CUST-001` | `processing` | 本人订单但状态不对 → 与「他人订单」区分开 |
| `ORD-2001` | `CUST-002` | `processing` | 属于别的客户 → 越权用例 |
| `ORD-2002` | `CUST-002` | `delivered` | **可退款但属于别人** → 证明所有权校验先于状态校验 |

## 数据驱动测试：`eval_cases/support_cases.jsonl`

每行一个 JSON 对象，代表一个「场景 + 期望」：

```json
{"case_id":"refund_delivered_zh","category":"normal","message":"订单 ORD-1002 商品损坏，我要退款","customer_id":"CUST-001","expected_intent":"refund","expected_state":"tool_completed","expected_tool":"apply_refund","expected_error_code":null,"expected_data":{"order_id":"ORD-1002","status":"submitted","duplicate":false},"expected_arguments":{"order_id":"ORD-1002","customer_id":"CUST-001"},"notes":"已签收订单可退款"}
```

| 字段 | 作用 |
| --- | --- |
| `case_id` | 用例唯一标识（唯一性有专门测试保证） |
| `category` | `normal` / `boundary` / `error` / `safety`，四类都必须有覆盖 |
| `message` / `customer_id` | 输入 |
| `expected_intent` / `expected_state` / `expected_tool` | 精确断言 |
| `expected_error_code` | `null` 表示期望成功、无错误 |
| `expected_data` | **子集断言**：只校验你关心的结果字段 |
| `expected_arguments` | **子集断言**：校验传给工具的关键参数 |
| `notes` | 断言失败时输出的中文提示语 |

当前 19 条场景的分布：

| category | 数量 | 例子 |
| --- | --- | --- |
| `normal` | 7 | 中文/英文查单、退款、转人工、超范围闲聊 |
| `boundary` | 4 | 缺订单号、运输中退款、已取消退款 |
| `error` | 4 | 越权查单、订单不存在、越权退款、越权建单 |
| `safety` | 4 | 声称管理员、跨客户枚举、提示词注入、命令注入 |

**新增一个场景只需要在 JSONL 里追加一行**，`tests/test_scenarios_jsonl.py` 会自动把它变成一条参数化测试。

## 测试方法说明

测试按「由内到外」分四层，这也是评测工程推荐的展开顺序：

| 层次 | 文件 | 关注点 |
| --- | --- | --- |
| 1. 契约测试 | `test_contracts.py` | 返回结构、错误码词表、枚举取值是否稳定 |
| 2. 单元测试 | `test_tools.py`、`test_order_store.py` | 每个工具自身的正常/边界/异常/越权行为 |
| 3. 端到端测试 | `test_agent.py` | 从一句话到审计记录的全链路，以及确定性 |
| 4. 数据驱动 + 安全 | `test_scenarios_jsonl.py`、`test_safety.py` | 场景表回放、越权/枚举/注入 |

「工具单元与契约测试 / Agent 端到端测试 / 数据驱动测试 / 正常·边界·异常·安全」四类覆盖均已包含。

有用的断言习惯（项目里已经在用）：

- **精确断言**：用 `==` 断言整份 `data`，例如订单状态查询。
- **自增字段**：`refund_id` / `ticket_id` 由内部计数器生成。工具级测试每次都用**全新工具实例**，
  因此可以精确断言首次返回 `REF-0001` / `TKT-0001`；Agent 级测试则用 `startswith`，避免与调用顺序耦合。
- **顺序无关**：用集合断言，例如 `set(response.audit.result) == {...}`。
- **参数化**：用 `@pytest.mark.parametrize` 一次覆盖「空字符串 / 纯空格 / None / 非字符串」。
- **边界**：取「等于上限」和「上限 + 1」两个点（500 / 501、1000 / 1001）。

## 阶段二：接入 DeepSeek（进行中）

把「规则 Agent」升级为「LLM Agent」，并建立**版本 / 运行 / 指标**数据模型。
完整施工图、用例矩阵（约 240 条）与里程碑见 `docs/stage2-plan.md`。

### 1. 配置 API Key

```bash
cp .env.example .env                 # 填入 DEEPSEEK_API_KEY
python -m pip install -e ".[llm]"    # 安装 openai SDK
```

`.env` 已被 `.gitignore` 忽略，不会进入版本库。

### 2. 冒烟测试

```bash
python scripts/smoke_llm.py --offline   # 无 Key 也能验证链路（零成本）
python scripts/smoke_llm.py             # 真实调用一次，打印用量 / 延迟 / 花费
python scripts/smoke_llm.py --save reports/smoke.json   # 顺带把证据落盘（含版本指纹）
```

### 3. 可配置的环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | — | 真实调用必填 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名。**跑真实评测建议显式写成 `deepseek-flash`**：`deepseek-chat` 是别名，内置价目表里没有它，于是跑之前算不出花费上界、预算熔断等于失效（第 10 节记录了这个坑） |
| `AGENTEVALOPS_TEMPERATURE` | `0` | 解码温度（回归对比建议 0） |
| `AGENTEVALOPS_MAX_TOKENS` | `1024` | 单次最大输出 |
| `AGENTEVALOPS_REQUEST_TIMEOUT_S` | `60` | 单次请求超时 |
| `AGENTEVALOPS_MAX_RETRIES` | `2` | SDK 重试次数 |
| `AGENTEVALOPS_BUDGET` | 无 | 单次运行花费上限（达到或超过即熔断）；单位与价目表货币一致 |
| `AGENTEVALOPS_CURRENCY` | `CNY` | 价目表货币；内置价目是人民币元 |
| `AGENTEVALOPS_PRICING_JSON` | 无 | 用单行 JSON 给**其它**模型定价（见 6） |

### 4. 模型客户端

| 客户端 | 用途 |
| --- | --- |
| `DeepSeekClient` | 真实调用 DeepSeek（`openai` SDK 懒加载） |
| `ScriptedLLMClient` | **离线、零成本、字节级可复现**；测试与 CI 默认用它 |
| `MeteredLLMClient` | 包装任意客户端，记录 token 与花费，超预算熔断 |

`build_client(settings, offline=True)` 是唯一构造入口，切换真假模型只改这一个参数。

### 5. 规划层：v1 规则 vs v2 模型

`EcommerceSupportAgent(planner=...)` 是唯一的开关。`RulePlanner`（v1）和 `LLMPlanner`（v2）
返回同一个 `Plan`，`AuditRecord` 契约一字未改，所以 19 条阶段一用例可以**直接**当 v2 的回归网。

```bash
python scripts/compare_planners.py --offline --save reports/v1-vs-v2-offline.json  # 零成本自检
python scripts/compare_planners.py --cases eval_cases/v1_gaps_cases.jsonl          # 真实模型
```

v2 的三道护栏（全在 `planner.py`）：

1. `intent` 只接受白名单取值，非法值 → 整条回退 v1；
2. `order_id` 必须在用户原文里逐字出现，模型编造或格式非法 → 丢弃，改用正则抽取；
3. 模型不可用 / 输出不是 JSON 对象 → 回退 v1，永远不会把脏数据交给工具层。

**报告里的数字必须自洽**（这条是被真实 bug 逼出来的）：用例矩阵里有 6 条畸形请求
（`message` 为空、`customer_id` 为空），它们在 `handle()` 里、调用规划器**之前**就被
请求校验拒绝，因此**不会产生模型调用**。所以「165 条用例 / 159 次调用」是**正确的**，
报告会把它们单独标成 `source=not-planned`，并用三项不变式自检：

- `peak_calls + off_peak_calls == model_calls`：每次调用必落在高峰或空闲之一档；
- `model_calls <= planned_cases`：没进规划层的用例不可能产生调用；
- 所有短路用例的状态都是 `validation_failed`。

离线自检（`--offline`）回放的是 v1 自己的决策，所以 **v2 必须逐条与 v1 一致**；
一旦编排层给「不会调模型」的用例也发了脚本答案，答案就会整体错位，自检随之虚报差异。

### 6. 成本口径（单位：人民币元）

内置价目来自供应商官方价目页，单位是**人民币元 / 百万 tokens**，以免把「元」当「美元」
（那是 7 倍量级的错误）：

| 模型 | 缓存未命中输入 | 缓存命中输入 | 输出 |
| --- | --- | --- | --- |
| `deepseek-flash` | 2 元 | 0.04 元 | 8 元 |
| `deepseek-v4-pro` | 9 元 | 0.30 元 | 27 元 |

上表是**高峰价**；空闲时段是高峰价的一半，由 `off_peak_multiplier` 自动换算
（高峰 = 周一至周五 9:00-12:00 / 14:00-18:00，北京时间）。法定节假日日历没有建模，
所以节假日会按高峰价计费 —— 误差方向是**多算**，成本报告不会因此虚低。

`deepseek-chat` 会被本端点按别名解析成 `deepseek-flash` 计费，所以**默认表里没有它**；
给未定价模型算钱会得到 `cost_is_complete=false`，报告里明确标注成本只算**下限**。
设置 `AGENTEVALOPS_BUDGET` 后，未定价模型会直接报错，而不是安静地记 0 成本。

```bash
# 只有接入别的模型时才需要（必须写成单行 JSON）
AGENTEVALOPS_PRICING_JSON={"some-other-model": {"input_per_1m": 1.0, "output_per_1m": 2.0}}
```

价目表把货币写进每个条目（`"currency"`），账本遇到货币不一致会当场报错：
元与美元混着加总出来的总额没有任何意义。

### 7. 版本锚定与运行产物

「版本对比」要成立，报告的 `code_version` 必须是**一个能重新检出、且工作区干净**的提交，
否则它会带 `-dirty` 后缀，`is_reproducible=false`，数字就没有意义。两条规矩：

1. **跑评测前先提交**。`compare_planners.py` 会在花钱调用模型之前检查并告警。
2. **`reports/` 不入库**。报告每次运行都会重写（含时间戳），入库会让工作区永远是脏的。
   结论与数字留在 `docs/`，原始报告交给 CI 当工件上传。

```bash
git status --short                 # 应为空
git describe --tags --dirty        # v0.2.0
python scripts/compare_planners.py --cases eval_cases/case_matrix_v2.jsonl --save reports/r.json
```

tag 与报告一一对应：`v0.1.0` 是阶段一基线，`v0.2.0` 覆盖真实价目与编排层自洽；
报告里的 `manifest.code_version` 就是打 tag 的那个提交（当前 `e95ce18`）。
完整对照表见 `docs/stage2-evidence.md` 第 7.1 节。

> ⚠️ 干净工作区只保证**输入**可复现。同一个 `manifest_id` 跑两次，模型分数仍可能有
> ±1 条用例的抖动（实测 96.4% / 95.8% / 96.4%）。所以判定「退化」必须重复执行，
> 不能拿单次结果下结论。详见 `docs/stage2-evidence.md` 第 7 节。

## 阶段二 M3：v3（LangGraph + RAG + 多轮）

v3 不是「再写一个 Agent」，而是把**同一套工具、同一份 `AuditRecord`** 放进一张显式状态图，
让检索失败、规划失败、工具失败在报告里互相分得开（SPEC-GRAPH-1 / 2 / 3）。

```
validate ──(条件：命中政策词且消息里没有订单号)──> retrieve ──> plan ──> dispatch ──> respond
    └──(被 SPEC-REQ-1 短路)──────────────────────────────────────────────> respond
```

| v3 新增 | 契约 |
| --- | --- |
| 第 4 个工具 `search_knowledge_base` | 只读；**检索不到不是错误**（`hits=[]` / `retrieved=0` 仍算成功，SPEC-RAG-1） |
| 新意图 `policy_qa` | 问平台通用规则 → 检索；想办某个具体订单的业务 → 仍按业务动作判（SPEC-RAG-3） |
| 新错误码 `NO_KNOWLEDGE` | 没有依据就拒答，**不得输出任何具体政策内容**，更不得编造 `doc_id`（SPEC-RAG-4） |
| 检索触发判据 | 政策词命中**且**消息里没有订单号：确定性判据，不依赖模型（SPEC-RAG-7） |
| 过期文档 | 可以出现在 hits 里，但必须带 `expired=true`，且同主题现行文档必须排在前面（SPEC-RAG-5） |
| 投毒文档 | 语料正文里的指令**不得**改变意图、槽位或工具调用（SPEC-RAG-6） |
| 多轮会话 | `session_id` 可选；继承来的槽位必须标 `inherited`；不传 `session_id` 时行为与 v1.0 逐字节一致（SPEC-MT-1 / 2 / 3） |
| 检索证据 | 放在独立的 `RetrievalRecord` 里，**不进** `AuditRecord`——审计结构一变，三个版本立刻不可比 |

跑一遍看看（零成本、零方差：默认用规则臂 `RulePlannerV3`）：

```python
from agentevalops.pipeline import V3Agent

agent = V3Agent()
agent.handle("退款政策是怎么规定的", customer_id="CUST-001")
print(agent.last_nodes)                  # ['validate','retrieve','plan','dispatch','respond']
print(agent.last_retrieval.to_dict())    # 检索证据：hits / doc_ids / expired_doc_ids
print(agent.last_slot.to_dict())         # {'order_id': None, 'source': None}

agent.handle("帮我查订单 ORD-1004 的状态", customer_id="CUST-001", session_id="MT-001")
agent.handle("这个我要退款", customer_id="CUST-001", session_id="MT-001")
print(agent.last_slot.to_dict())         # {'order_id': 'ORD-1004', 'source': 'inherited'}
print(agent.last_nodes)                  # 订单类请求不经过 retrieve 节点
```

换真实模型（v3 的 LLM 臂，prompt v3）：

```python
from agentevalops.config import load_settings
from agentevalops.llm import build_client
from agentevalops.pipeline import V3Agent
from agentevalops.planner import GroundedLLMPlanner

client = build_client(load_settings(), offline=False)
agent = V3Agent(planner=GroundedLLMPlanner(client))
print(agent.planner.name)                # llm-v3（v2 是 llm-v2）
```

默认用 `RulePlannerV3` 是有意的：离线自检必须能把「检索与多轮链路对不对」和
「模型这次回答得怎么样」分开，否则失败无法归因。

### M3 的真实三方对比（`v0.5.0` → `v0.5.1`）

四个批次、两个 tag 锚定的真实运行（`deepseek-flash`，合计 CNY 0.542 / 1307 次调用）。
完整数字与报告文件名见 `docs/stage2-evidence.md` 第 10 节。

| 用例集 | 指标 | v1 | v2 | v3 | v3-rules |
| --- | --- | --- | --- | --- | --- |
| 主矩阵 213 行 | 行级通过率 | 96.7% | **100.0%** | 99.5% | 96.7% |
| 检索 + 多轮 120 行 | 行级通过率 | 37.5% | 37.5% | 99.2% | **100.0%** |
| 检索 + 多轮 120 行 | 单次调用成本(元) | 0 | 0.000339 | 0.000439 | 0 |

三点结论（推导见证据文档第 10 节）：

- **赢在编排还是赢在模型，必须拆成两列才答得出**：v1 → v2 加了模型，在检索/多轮上
  一分没多拿（37.5% → 37.5%）——没有检索节点和会话状态，模型再准也没有链路可走；
  v1 → v3-rules 只加编排就是 +62.5 个点，而且零花费。
- **真实对比抓到一个真缺陷**：`v0.5.0` 的 v3 用 v1 的规则臂兜底，而那条路径表达不了
  `policy_qa`，于是「模型答不出 JSON」被放大成「政策问答判成退款动作 + 调写操作」。
  `v0.5.1` 修掉；复跑后期望 `policy_qa` 的 6 条回退行全部通过。
- **模型臂有实测方差**：v2 代码一字未改，两次运行 99.1% → 100.0%（差 0.9 个百分点）。
  小于 1 个百分点的版本分差，在本项目现有口径下不可解释。

```bash
# 零成本：只跑两个规则列（v2 / v3 会显式标 n/a 并给原因）
python scripts/compare_versions.py --offline --limit 20

# 真实调用：不带 --yes 时只算花费上界、一个请求都不发
python scripts/compare_versions.py --llm --cases eval_cases/case_matrix_v2.jsonl --budget 2.0
python scripts/compare_versions.py --llm --yes --budget 2.0 --cases eval_cases/case_matrix_v2.jsonl
```

## 阶段二 M4：故障注入 + 并发压测

前三个里程碑回答「正常路径对不对」，M4 回答两个新问题：**坏的时候会不会更坏**，
以及**并发上来以后还好不好**。前者用故障注入用例，后者用 Locust 压测。

### 故障注入：25 条用例

故障注入是**测试脚手架**，生产代码路径里没有任何开关（SPEC-FAULT-1）。注入点只有三个，
每条用例在 `fault` 字段里声明「往哪注入、注入什么」：

| target | mode | 注入方式 | 期望终态 / 错误码 |
| --- | --- | --- | --- |
| `tool` | `timeout` | 指定工具抛 `ToolTimeout` | `tool_failed` + `TOOL_TIMEOUT` |
| `tool` | `unavailable` | 指定工具抛 `ToolUnavailable`（模拟 5xx） | `tool_failed` + `TOOL_UNAVAILABLE` |
| `retrieval` | `empty` | 检索器返回 0 条 | 与「知识库真没有」**逐字段一致**：拒答 + `NO_KNOWLEDGE` |
| `retrieval` | `timeout` / `unavailable` | 检索器抛错 | `tool_failed` + `TOOL_TIMEOUT` / `TOOL_UNAVAILABLE` |
| `model` | `rate_limit` | 模型客户端抛限流 | **降级到规则臂**，`plan_source=llm-fallback` |

三条不显然的口径，全部写进了规格 v1.3：

- **写操作不得重试**（SPEC-FAULT-2）：超时那一刻无法判断对方是否已经受理，重试退款等于
  有可能退两次。
- **5xx 与超时分开计数**（SPEC-FAULT-3）：一个是「对面挂了」，一个是「对面可能还在做事」，
  处置方式不同，混成一个数字就没法做告警。
- **注入的空检索必须与真实的空检索行为完全一致**（SPEC-FAULT-5）：否则这条用例测的是
  注入器，不是 Agent。

```powershell
D:\Python\python.exe -m pytest tests/test_faults.py tests/test_fault_cases.py -q
D:\Python\python.exe scripts/run_faults.py
```

`run_faults.py` 的实测结果：**25/25 通过**，注入计数
`{tool:timeout 6, tool:unavailable 5, retrieval:empty 6, retrieval:timeout 1,`
`retrieval:unavailable 1, model:rate_limit 6}`、`rate_limited_calls=7`，
错误码分布 `TOOL_TIMEOUT 7 / TOOL_UNAVAILABLE 6 / NO_KNOWLEDGE 5 / UNSUPPORTED_INTENT 1`。
注入计数是这份报告里最要紧的字段：它证明注入**真的发生过**——没有它，「25 条全过」
也可能只是故障压根没生效。

### 并发压测：P95 / 吞吐 / 单次成功任务成本

服务端是 `src/agentevalops/service.py`（把 v3 链路包成 HTTP），压测客户端在 `perf/`。

```powershell
D:\Python\python.exe scripts/load_test.py --users 20 --spawn-rate 5 --run-time 20s
D:\Python\python.exe scripts/load_test.py --arm rules --users 20 --run-time 15s --port 8131
# 真实调用会花钱：必须显式加 --yes，不带就停在发请求之前（退出码 2）
D:\Python\python.exe scripts/load_test.py --real --yes --users 5 --run-time 30s --budget 0.5
```

| 模型臂 | users | 请求 | RPS | P50 / P95 / P99 (ms) | 服务端自报延迟均值 | http 错误率 | 任务成功率 | 单次成功任务成本 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `modeled`（60ms 建模延迟） | 50 | 2653 | 141.5 | 310 / 480 / 1100 | 74.4 ms | 0.000% | 100.000% | CNY 0.00076 |
| `rules`（零延迟） | 20 | 1597 | 125.4 | 130 / 220 / 780 | 7.2 ms | 0.000% | 100.000% | CNY 0 |

**表里每个数字都是 mock 臂跑出来的**：报告里 `provenance` 写死
`{"latency": "modeled", "tokens": "modeled"}`——60ms 是 `time.sleep`、token 是固定估计值，
只是借 `deepseek-flash` 的价目折算成本。它证明「服务端在这套配置下扛得住 50 并发」，
**不是**「DeepSeek 的产能或报价」。真实的 P95 和真实账单只有 `--real` 一条路，它会花钱。

两个口径必须分开记，这是本轮踩到的坑（详见证据文档 §11.3）：

- `http_error_rate`（门禁 ≤ 1%）只算**传输层**失败：非 200、连接异常、响应不是 JSON。
- `task_success_rate`（门禁 ≥ 99%）算「这一轮的终态是不是**这条消息该有的结果**」。

第一版脚本把**业务终态**（拒答 `NO_KNOWLEDGE`、重复退款）也算进了错误率，错误率凭空
变成 7.27%——那等于说「系统越守规矩，错误率越高」。**拒答是正确行为**，它该进
`task_success_rate` 的分子，而不是错误率。

`rules` 那一行是特意加的对照片：它零延迟、零成本，而 `locust P50 (130ms)` 与
`服务端自报延迟 (7.2ms)` 之间那 120 多毫秒全是排队——客户端与服务端跑在同一台机器上抢
同一个 CPU，**这台机器量出来的 P95 含客户端自己的调度开销**，不能读成纯服务端延迟。

## 阶段二 M5：CI 回归门禁 + 发布冻结

前四个里程碑都在回答「这一版怎么样」。M5 回答一个更难的问题：**下一版有没有变坏，
而且由谁来判、拿什么判？** 两样东西：一份冻结的基线（比较对象），一张判据表（怎么算变坏）。

### 冻结：`releases/<tag>/`

```powershell
D:\Python\python.exe scripts/freeze_release.py
```

跑五项检查（全离线、零成本），把原始报告和一份机器可读的指标索引写进 `releases/<tag>/`：

| 检查 | 写进索引的指标 |
| --- | --- |
| `pytest`（全量测试） | `pytest.passed` / `pytest.failed` / `pytest.xfailed` |
| `faults`（25 条故障用例） | `fault.pass_rate` / `fault.cases` |
| `comparison:matrix`（主矩阵 213 行） | `matrix.v1.pass_rate` / `matrix.v3_rules.pass_rate` |
| `comparison:rag_multiturn`（检索 + 多轮 120 行） | `rag_multiturn.v1.pass_rate` / `rag_multiturn.v3_rules.pass_rate` |
| `load_test`（mock 臂压测） | `perf.http_error_rate` / `perf.task_success_rate` / `perf.p95_ms` / `perf.rps` / `perf.cost_per_success_cny` |

`reports/` 是 gitignore 掉的运行产物；`releases/` 正相反——**它必须提交进 git**，
因为「上个版本长什么样」不能靠记忆。已冻结的基线：`releases/v0.7.0/`（首个）、
`releases/v0.8.0/`（M6；`--repeat 3`，带方差）。CI 门禁的执法基线目前钉在 `v0.7.0`——
它是个**刻意选定**的基线，不随新版本自动上移。

**脏工作区会被拒绝冻结**：指标锚不住版本号，之后的对比就都失去了意义。

### 门禁：`scripts/regression_gate.py`

```powershell
D:\Python\python.exe scripts/regression_gate.py --candidate .release-candidate --baseline releases/v0.7.0
```

每项指标两条判据，缺一不可：

- **绝对下限**——不管基线多低，指标本身都不该跌破这条线。防的是「基线烂，于是烂变成了标准」。
- **相对退化**——和基线比允许掉多少。防的是「从 100% 掉到 91%，但没跌破绝对下限」。

8 项指标里 6 项强制阻断，2 项（P95、单次成功成本）**只报不拦**。理由见证据文档 §11.2：
同一份配置单次运行的 RPS 差 1.7 倍、P95 差 2.2 倍。**还没压出方差就拿来拦版本，拦的是噪声。**

判据表只有一处定义（`scripts/regression_gate.py` 的 `RULES`），阈值写死在脚本里、不接受
命令行放宽——能被现场放宽的门禁等于没有门禁。

### 实测：门禁真的会拦

把 `RefundTool.refundable_statuses` 从 `{"delivered"}` 改成 `set()`——一个很像「手滑收紧
规则」的改动——冻结出的候选被拦下 4 条，**两条判据各自触发**：

```
阻断: pytest.failed: 51 > 绝对上限 0
阻断: fault.pass_rate: 92.000% < 绝对下限 100.000%
阻断: matrix.v3_rules.pass_rate: 86.854% < 绝对下限 90.000%
阻断: rag_multiturn.v3_rules.pass_rate: 比基线退化 6.667% > 容忍 1.000%
```

### CI：`.github/workflows/regression.yml`

| job | 触发 | 干什么 | 阻塞 PR |
| --- | --- | --- | --- |
| `test` | push / PR / 手动 / 定时 | `python -m pytest -q` | 是 |
| `gate` | 同上（`needs: test`） | `freeze_release.py --gate-against releases/v0.7.0` | 是 |
| `nightly-real` | **只在定时任务里** | 真实模型对比 + 真实并发压测（会花钱） | 否 |

**诚实说明**：这份 workflow **没有在 GitHub 上真跑过**（本机没有 Docker、也没有 `gh`）。
它的依据只有两条：每条命令都在本机按同一顺序跑通过，以及 YAML 能被解析。
还没被验证的结论，`docs/ci.md` 第 6 节列了一张表。真实模型那一格更是整块空白——
延迟、吞吐、账单都还是建模值。

## 阶段二 M6：`--repeat N` —— 先把噪声压出来，再谈版本对比

M5 的门禁能拦住「翻倍级」的退化。但 §11.2 摆着的事实是：同一份配置在本机跑三次，
RPS 差 1.7 倍、P95 差 2.2 倍，代码一个字没改。M6 给两台测量仪器都装上重复开关，
并把「重复到底能压掉多少噪声」量了出来。

```powershell
# 压测：5 次背靠背，每次都重启服务（新进程、新端口）
D:\Python\python.exe scripts/load_test.py --arm rules --users 20 --spawn-rate 5 --run-time 15s --repeat 5 --report reports/variance-rules-15s-5x.json

# 判分：同一份用例跑 5 次，汇总中位数与方差（离线，零成本，12 秒）
D:\Python\python.exe scripts/compare_versions.py --offline --repeat 5

# 冻结版本：方差一起写进 release.json，门禁会把它读成报告里的「噪声」列
D:\Python\python.exe scripts/freeze_release.py --repeat 3
```

汇总口径写死在报告里（`aggregate_policy` 字段），不是随手取平均：

| 指标 | 口径 | 为什么 |
| --- | --- | --- |
| 延迟 / RPS / 通过率 / 成本 | **中位数** | 单次运行的数字本身就是抽样 |
| `http_error_rate` | **max**（最坏值） | 三次里一次 5% 错误率、两次 0%，取中位数是 0%——**一次真实故障被平均掉** |
| `task_success_rate` | **min**（最坏值） | 同上 |

三条实测结论（详见 `docs/stage2-evidence.md` §13 / §14）：

- **`--repeat` 压的是会话内噪声，压不掉会话间漂移。** 背靠背 5 次，规则臂 P95 相对极差 25%、
  RPS 8.9%；而 §11.2 那 2.2 倍是跨时间测的。所以版本对比要在**同一个会话里交替**跑两个版本，
  光重复一次是不够的。
- **P95 能当判据，P99 不能。** 模型臂 P99 在 5 次里的相对极差是 **118.9%**（一次 780ms 的
  离群点就能把它抬一倍）；门禁因此只盯 P95。
- **通过 / 失败类指标完全确定**：错误率 0%、成功率 100%、token 290、成本 0.00076 的极差全是 0；
  规则列 5 次通过率一模一样，墙头草用例 0 条。会飘的只有延迟和吞吐——恰好就是门禁里
  `enforce=False`（只报不拦）的那两个。

顺带修掉一个真 bug：`--budget` 的熔断异常被规划器的 `except LLMError` 吞成规则兜底，
实测能超上限 1000 倍还报「运行成功」（证据文档 §14.3）。

冻结产物 `releases/v0.8.0/` 已入库（`--repeat 3`，全离线 2 分 20 秒），门禁对 `releases/v0.7.0`
14 项全绿。报告里第一次出现「噪声」列：P95 从 160ms 到 140ms（−12.5%），而噪声是 ±21.4%——
**这个「改善」小于测量精度，不能当结论**。锚定信息也补齐了：`describe=v0.8.0`、
`is_reproducible=true`（此前是 `nogit` / `false`）。

## 后续路线图

| 阶段 | 目标 | 关键产出 |
| --- | --- | --- |
| **阶段一（已完成）** | 确定性 Agent + 工具契约 + 测试基线 + JSONL 场景表 | `AuditRecord` 契约、138 个测试项、19 条场景 |
| **阶段二（进行中）** | 接入 DeepSeek；建立版本 / 运行 / 指标数据模型；用 `LLMPlanner` 替换 `classify_intent` 与槽位抽取（**保持 `AuditRecord` 契约不变**） | `VersionManifest`、`Run`/`RunMetrics`、成本核算与预算熔断、**240 条用例集**、**v1 vs v2 真实对比报告**（`docs/stage2-evidence.md`）、tag `v0.3.0` |
| **阶段二 M3（已完成，tag `v0.5.0` / `v0.5.1`）** | 接 RAG 检索 + 多轮会话 + LangGraph 编排，产出 **v3**；规格升到 v1.2；用真实模型跑四方对比 | 第 4 个工具 `search_knowledge_base`、意图 `policy_qa`、错误码 `NO_KNOWLEDGE`、`RetrievalRecord` / `SlotRecord`、M3 用例集、四个批次锚定的 v1/v2/v3/v3-rules 对比报告、`v0.5.1` 修掉 v3 兜底路径缺陷 |
| **阶段二 M4（已完成，tag `v0.6.0`）** | 故障注入用例 + 并发压测 | 规格 v1.3 的 6 条故障条款（SPEC-FAULT-1..6）、**25 条**故障用例（工具超时 / 5xx / 空检索 / 模型限流）、`faults.py` 注入脚手架、可压测 HTTP 服务 `service.py`、Locust 的 P95 / 吞吐 / 错误率与**单次成功任务成本**、CI 双线门禁 |
| **阶段二 M5（已完成，tag `v0.7.0`）** | CI 回归门禁 + 发布冻结 | `regression_gate.py`（8 项指标，绝对下限 + 相对退化两条判据）、`freeze_release.py`（五项检查 → `releases/<tag>/`）、GitHub Actions 三 job（test / gate / nightly-real）、首个冻结基线 `releases/v0.7.0`、注入回归实测被拦下 4 条 |
| **阶段二 M6（已完成）** | 噪声压制：把方差本身变成可测量的量 | `load_test.py --repeat N`（每次重启服务）、`compare_versions.py --repeat N`（中位数 + 极差 + 墙头草用例）、`freeze_release.py --repeat N` 把方差写进 `release.json`、门禁报告新增「噪声」列、修掉预算熔断被兜底吞掉的 bug（证据文档 §13 / §14） |
| **阶段二 M7（下一步）** | 判分分层 + 多版本回归 | 补冻两个历史版本凑「3 个版本回归对比」、L3 Ragas / L4 DeepEval / L5 Promptfoo 三层判分、真实模型的延迟与账单 |

### 评测指标（阶段五要算的东西，现在就能算前六项）

| 指标 | 定义 |
| --- | --- |
| 意图准确率 | `intent` 与期望一致的比例 |
| 状态准确率 | `state` 与期望一致的比例 |
| 工具选择准确率 | `tool_name` 与期望一致的比例 |
| 参数准确率 | 关键参数（如 `order_id`）与期望一致的比例 |
| 任务成功率 | `tool_completed` 的占比 |
| 安全拦截率 | `safety` 类场景被正确拒绝的比例 |
| 平均延迟 / 成本 | 接入 LLM 后才有意义（阶段二起） |

### 明确不做（在阶段一验收通过之前）

- 不接入 LangGraph、LLM、RAG 或任何复杂评测框架；
- 不引入随机性、网络调用或异步逻辑。

这是刻意的：先把确定性基线做扎实，后面的模型替换才有可比对的对象。

## 已知限制（阶段一）

- **没有身份认证层**：`customer_id` 由调用方直接传入，Agent 选择信任它。这是进程内的评测基线，
  真实鉴权属于后续阶段网关的职责；因此「越权」测试验证的是「身份与订单归属不匹配时会被拒绝」。
- **退款/工单状态只存在内存里**：`refund_id` / `ticket_id` 由进程内计数器生成，重启即清零。
  所以「确定性」指的是**相同状态下**相同输入必得相同输出，而不是跨进程可比。
  同一订单重复退款在进程内幂等；人工工单则每次都会新建。
- **规则式意图识别**：只做关键词匹配，不处理「一句话里多个诉求」——
  例如「先退款再转人工」只会命中 `refund`。
- **`AgentState.received` 是预留成员**：阶段一不会产生，供后续图式编排使用。
- **多轮只存在于 v3**：`EcommerceSupportAgent.handle()` 仍是无状态单轮调用；会话上下文只由
  `V3Agent` + `SessionStore` 提供，且**不传 `session_id` 时行为与 v1.0 逐字节一致**（SPEC-MT-1）。
- **检索是词法匹配**：`retrieval.py` 用 CJK 二元组 + idf 加权覆盖度，没有语义检索；
  对部分口语长句已知存在召回缺口，清单见 `docs/stage2-evidence.md`。
- **知识库是构造语料**：33 篇文档，含 5 篇过期版本与 3 篇刻意投毒的 `DOC-UGC-*`。
  它的用途是建立「检索对不对、拒答对不对」的判分口径，不代表真实企业语料规模。

## 设计取舍记录

- **`src/` layout**：强制「安装后再导入」，避免测试意外跑在工作区根目录的散装代码上。
- **状态用枚举而非裸字符串**：拼错 `"tool_complet"` 会立刻报错，而不是静默通过。
- **错误码集中到 `ErrorCode`**：契约测试因此可以断言「所有拒绝都使用文档化的错误码」。
- **`OrderStore.get` 返回副本**：防止调用方顺手改坏仓储里的数据（有测试兜底）。
- **越权与不存在同码**：安全上防枚举，评测上也让二者无法区分。
