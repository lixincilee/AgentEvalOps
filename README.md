# AgentEvalOps

> 一个用于练习 **Agent 评测工程（Evaluation Engineering / EvalOps）** 的教学项目。
> 当前处于 **阶段一**：先做「完全确定性的电商客服 Agent」，把契约、数据和测试基线建好，再谈接入大模型。

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
│   ├── agent-spec.md                  # 行为规格 v1.0（冻结）：意图粒度 / 槽位匹配 / 判分口径
│   ├── stage2-plan.md                 # 阶段二施工图与里程碑
│   ├── stage2-evidence.md             # 阶段二实测证据与复现命令
│   └── bad-cases-v2.md                # v2 的 9 处差异逐条归类与根因
├── scripts/
│   ├── check_env.py                   # 密钥体检（只打印长度与前缀，不泄密）
│   ├── smoke_llm.py                   # LLM 连通性烟囱测试（--save 可落盘证据）
│   ├── compare_planners.py            # v1 规则版 vs v2 模型版 对比运行器
│   └── gen_case_matrix.py             # 用例矩阵脚手架（只报告偏差，不再定义期望值）
├── reports/                           # 运行产物（已 gitignore）：评测报告 JSON，可当 CI 工件
├── eval_cases/
│   ├── support_cases.jsonl            # 阶段一场景集（19 条，v1 亲和的基线集）
│   ├── v1_gaps_cases.jsonl            # v1 已知漏判集（8 条，用来检验评测集区分度）
│   └── case_matrix_v2.jsonl           # 阶段二用例矩阵（分类别大规模扩充）
├── src/
│   └── agentevalops/                  # 生产代码（src-layout，强制安装后再运行）
│       ├── __init__.py                # 对外公开的 API 汇总
│       ├── models.py                  # 数据契约：Intent / AgentState / ErrorCode / ToolResult / AuditRecord / AgentResponse
│       ├── order_store.py             # 订单仓储，只读 + 归属校验
│       ├── tools.py                   # 三个工具：查单 / 退款 / 建人工工单
│       ├── agent.py                   # Agent 主流程：校验请求 → 规划 → 调用一个工具
│       ├── planner.py                 # 阶段二：规划层（RulePlanner=v1 / LLMPlanner=v2 + 幻觉守卫）
│       ├── config.py                  # 阶段二：环境变量 / .env 加载
│       ├── versioning.py              # 阶段二：VersionManifest 版本指纹
│       ├── runs.py                    # 阶段二：Run / CaseResult / RunMetrics
│       ├── spec.py                    # 行为规格版本 + 基线偏差清单（规格冻结）
│       ├── llm/                       # 阶段二：base / deepseek / fake / costing
│       └── data/
│           ├── __init__.py
│           └── orders.json            # 本地模拟订单数据
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
    └── test_runs.py                   # 运行聚合与落盘
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
python -m pytest                                # 全量：585 passed（另有 10 条已登记偏差 xfail）
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
| `ORD-2001` | `CUST-002` | `processing` | 属于别的客户 → 越权用例 |

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
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
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

## 后续路线图

| 阶段 | 目标 | 关键产出 |
| --- | --- | --- |
| **阶段一（已完成）** | 确定性 Agent + 工具契约 + 测试基线 + JSONL 场景表 | `AuditRecord` 契约、138 个测试项、19 条场景 |
| **阶段二（进行中）** | 接入 DeepSeek；建立版本 / 运行 / 指标数据模型；用 `LLMPlanner` 替换 `classify_intent` 与槽位抽取（**保持 `AuditRecord` 契约不变**） | `VersionManifest`、`Run`/`RunMetrics`、成本核算与预算熔断、**192 条用例集**、**v1 vs v2 真实对比报告**（`docs/stage2-evidence.md`）、tag `v0.2.0` |
| 阶段三：LangGraph 编排 | 显式状态机：意图节点 → 工具节点 → 人工中断节点 | 图式编排替代 if/else；节点级可观测 trace |
| 阶段四：RAG | 新增知识库检索工具（第 4 个工具），回答政策类问题 | 检索质量指标（命中率、引用正确率）与幻觉检测 |
| 阶段五：评测框架与看板 | 批量跑场景、算指标、存历史、卡 CI 门禁 | 回归看板、基线对比、失败样本归档 |

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
- **不处理多轮对话**：`handle()` 是无状态的单轮调用，没有会话上下文。

## 设计取舍记录

- **`src/` layout**：强制「安装后再导入」，避免测试意外跑在工作区根目录的散装代码上。
- **状态用枚举而非裸字符串**：拼错 `"tool_complet"` 会立刻报错，而不是静默通过。
- **错误码集中到 `ErrorCode`**：契约测试因此可以断言「所有拒绝都使用文档化的错误码」。
- **`OrderStore.get` 返回副本**：防止调用方顺手改坏仓储里的数据（有测试兜底）。
- **越权与不存在同码**：安全上防枚举，评测上也让二者无法区分。
