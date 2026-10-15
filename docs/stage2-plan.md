# 阶段二计划：从「确定性规则 Agent」到「可版本化的 LLM 评测基线」

本文件是阶段二的施工图与验收单。阶段一（确定性规则 Agent + 138 个测试）已经完成，
它是本阶段的 **v1 基线版本**，也是判分第 1 层（L1）的实现。

## 1. 阶段二的目标

让「被测系统」第一次具备不确定性，同时建立**版本 / 运行 / 指标**数据模型。
完成标准是能产出一份 **v1（规则）/ v2（LLM）/ v3（LangGraph + RAG + 多轮）** 的三方对比报告，
以及一条可分享的 Trace。

> 关键认知：先有版本与运行的数据模型，才可能有「3 个版本回归对比」。
> 没有它，对比只能靠手工拼截图。

## 2. 现状盘点

| 能力 | 状态 |
| --- | --- |
| 3 个工具（查单 / 退款 / 工单）+ 工具契约 | 已完成（阶段一） |
| AuditRecord 审计记录（意图/状态/工具/参数/结果/错误） | 已完成 |
| 判分 L1：138 个确定性断言 | 已完成 |
| 设置与环境变量加载（config.py） | 本次完成 |
| LLM 抽象 + DeepSeek 适配器 + 离线假客户端 | 本次完成 |
| 版本清单 VersionManifest | 本次完成 |
| 运行数据模型 Run / RunMetrics | 本次完成 |
| 成本核算与预算熔断 UsageLedger | 本次完成 |
| RAG 检索工具 / 多轮会话状态 / LangGraph | 已完成（M3，v3） |
| Langfuse Trace | 未开始 |
| 用例集 | 已完成：主矩阵 240 条 + M3 检索 48 条 + 多轮 36 条 = **324 条**（见 §5） |
| Ragas / DeepEval / Promptfoo / Locust / CI | 未开始 |

## 3. 三个地基（已落地）

### 3.1 供应商可替换

agentevalops.llm 提供统一的 LLMClient 协议：

- DeepSeekClient：真实调用（OpenAI 兼容协议，openai SDK 懒加载）。
- ScriptedLLMClient：**离线、零成本、字节级可复现**，测试与 CI 默认用它。
- MeteredLLMClient：包一层，记录 token 与花费，并在超预算时熔断。

这样「换成真模型」只改工厂函数的一个参数，测试套件仍完全离线可跑。

### 3.2 版本清单

VersionManifest 固定住一次运行的全部身份信息：
code_version（git 短 sha，脏树加 -dirty）、prompt_version、prompt_hash、model_id、
temperature、retrieval_config_hash、case_set_version、case_set_hash、extra，
并派生出 12 位 manifest_id 作为版本指纹。

is_reproducible 明确回答「这次结果可不可复现」——脏树或非 git 目录一律 False。

### 3.3 运行数据模型

Run = run_id + 版本清单 + 起止时间 + 逐条 CaseResult，聚合出 RunMetrics：
pass_rate、by_layer、by_category、total_tokens、cost、
cost_per_success（单次成功任务成本）。Run.save/load 落盘为 UTF-8 JSON。

## 4. 判分分层

不是所有用例都该走 LLM Judge。分层是为了同时控住成本与抖动。

| 层 | 手段 | 覆盖 | 成本 | 简历对应指标 |
| --- | --- | --- | --- | --- |
| L1 | 纯规则断言 | 意图 / 工具 / 参数 / 错误码 / 越权 | 免费 | 工具选择、参数准确率 |
| L2 | 检索规则断言 | gold doc 命中、recall@k、该拒答时是否拒答 | 免费 | 上下文召回 |
| L3 | Ragas | 忠实性、上下文精确率/召回率 | LLM + Embedding | 忠实性 |
| L4 | DeepEval（GEval） | 答案质量、语气、任务完成 | LLM | 任务完成率 |
| L5 | Promptfoo | 提示注入 / 越权 / 泄露 | LLM | 安全拦截率 |
| L6 | Locust | P95 / 吞吐 / 错误率 / 成本 | LLM | 性能与成本 |

## 5. 用例矩阵（324 条已达成）

五份 JSONL 合计 **324 条**，期望值全部来自 `docs/agent-spec.md`（SPEC-JUDGE-2）：

| 文件 | 条数 | 组成 |
| --- | --- | --- |
| `eval_cases/case_matrix_v2.jsonl` | 213 | normal 64 / boundary 56 / error 47 / safety 46 |
| `eval_cases/support_cases.jsonl` | 19 | 阶段一基线集（v1 亲和） |
| `eval_cases/v1_gaps_cases.jsonl` | 8 | v1 已知漏判集（区分度回归网） |
| `eval_cases/rag_cases.jsonl` | 48 | M3 检索层：rag 22 / rag_stale 8 / rag_poison 8 / rag_refuse 10 |
| `eval_cases/multiturn_cases.jsonl` | 36 | M3 会话层：multiturn 32 / multiturn_refuse 4（共 72 轮） |
| **合计** | **324** | 其中安全样本 50 条，另有 M3 投毒样本 8 条（目标 35 条） |

三类用例的进度：

| 类别 | 目标 | 现状 |
| --- | --- | --- |
| RAG 检索 | 45 | **已完成 48 条**（M3）：有答案 / 应拒答 / 多文档 / 冲突文档 / 过期文档 / 投毒隔离 |
| 多轮状态 | 35 | **已完成 36 条**（M3）：指代消解 / 槽位继承 / 话题切换 / 中途改主意 / 缺槽位 |
| 故障注入 | 25 | 未开始（M5）：需要注入能力——工具超时 / 工具 500 / 检索超时 / 空检索 / 模型限流 |

> 新增的 48 条写在 M2.5 规格冻结**之后**，因此**没有引入任何新的基线偏差**
> （偏差集合仍是 SPEC-SLOT-1 三条 + SPEC-INTENT-2 四条）。
> 另外 `scripts/gen_case_matrix.py` 与 JSONL 的一致性有了守卫：
> 两者的期望值一旦漂移，`test_the_generator_cannot_silently_overwrite_the_frozen_spec` 当场失败。

### 安全样本 4 类（35 条）

| 类别 | 条数 | 要点 |
| --- | --- | --- |
| 直接提示注入 | 10 | 覆盖指令、越狱、编码绕过 |
| 间接注入（语料投毒） | 10 | 把攻击指令写进知识库文档，等检索捞出来 —— RAG Agent 独有攻击面 |
| 越权操作 | 8 | 跨客户读单 / 退款 / 建单，身份边界 |
| 敏感信息泄露 | 7 | 套系统提示词、套他人 PII、套内部字段 |

## 6. 三个版本怎么来

| 版本 | 定义 | 状态 |
| --- | --- | --- |
| v1 | 规则版 Agent（阶段一产物） | 已存在 |
| v2 | LLM 版（DeepSeek + prompt v2），单轮、无检索 | 已完成（`v0.3.0` / `v0.4.0`） |
| v3 | LangGraph 编排 + RAG 检索 + 多轮会话 + prompt v3；规划器可换规则臂（`RulePlannerV3`）或模型臂（`GroundedLLMPlanner`） | **M3，tag `v0.5.0`** |

对比报告必须引用三个 manifest_id，并说明每个版本改了什么。
v1 与 v3 的规则臂都能**离线**跑（零成本、零方差），所以三方对比先对齐「检索与多轮链路成不成立」，
再花一次钱把模型臂跑出来做归因——两步混在一起，出了差异就说不清是链路错还是模型随机。

## 7. 性能测试的坑

直接压测接真 LLM 的 Agent，量到的主要是**供应商限流**，不是你的代码。分两档：

- A 档（编排层）：mock 掉模型与检索，高并发压你的路由/状态机 —— 这是能优化的 P95。
- B 档（真实端到端）：5~10 VU 真模型，只报真实延迟与 cost_per_success。

报告中必须写明 P95 属于哪一档，否则经不起追问。

## 8. CI 门禁策略

- L1 / L2 硬断言：要求 **100% 通过**，否则阻断。
- L3 / L4 软指标：用**通过率阈值**（如不低于 90%）+ 重复 N 次取多数，不追求满分。
- 单次运行设花费预算上限（UsageLedger.limit），超限直接失败。
- PR 只跑 50 条 golden set；全量 240 条 nightly 跑。

## 9. 里程碑

| 里程碑 | 交付 | 验收标准 |
| --- | --- | --- |
| M1（本次） | 版本模型 + 运行模型 + LLM 抽象 + 成本核算 | 全套测试离线通过；无需 Key 即可跑 |
| M2 | LLM 版意图识别 + 槽位抽取，接入 DeepSeek | 一条真实 Trace；v1 vs v2 对比报告；L1 全部迁移且通过率有数字 |
| **M2.5（本次）** | **规格冻结**：写 `docs/agent-spec.md` v1.0，裁决 9 条 v1/v2 争议，把偏差变成可执行清单 | 期望值不再来自任何实现（SPEC-JUDGE-2）；基线偏差由 strict `xfail` 盯住；版本对比有了规范标尺 |
| M3 | RAG 检索工具 + 多轮状态 + LangGraph | 检索类用例可判分；多轮用例通过 |
| **M2.6（本次）** | **用例集扩到 240**：新增 3 条模拟订单 + 48 条用例（含 4 条 INVALID_ARGUMENT 边界、12 条安全样本） | 三份 JSONL 合计 240 条；新增用例零基线偏差；全量 `649 passed` |
| M4 | Ragas/DeepEval + Judge 校准 + 50 条 golden set 跑 PR | Judge 与人工抽检一致率有数字；全量 240 条跑完出指标 |
| **M3（本次）** | **RAG 检索工具 + 多轮会话状态 + LangGraph 编排**；规格升到 v1.2；产出 **v3** | 检索类 48 条 + 多轮类 36 条可判分；v1/v2/v3 三方对比器离线零差异；全量 `912 passed` |
| M4 | Ragas/DeepEval + Judge 校准 + 50 条 golden set 跑 PR | Judge 与人工抽检一致率有数字；全量用例跑完出指标 |
| M5 | 故障注入（工具超时 / 工具 500 / 空检索）+ Locust 并发压测 | 注入类用例可判分；P95 / 吞吐 / 单次成功任务成本有数字 |

## 10. 环境前置（当前状态）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| DeepSeek API 可达 | 已实测打通 | `scripts/smoke_llm.py` 真实调用成功；本端点会把 `deepseek-chat` 解析成 `deepseek-flash` 返回 |
| DEEPSEEK_API_KEY | 已配置 | 项目根目录 .env（已被 .gitignore 忽略）；`scripts/check_env.py` 返回 OK |
| git 仓库 | 非 git 仓库 | code_version 目前是 nogit，is_reproducible=False |
| Docker | 未安装 | Langfuse 只能用 Cloud，或先退化为本地 JSON trace |
| Node / npm | v24.18.0 / 11.16.0 | Promptfoo 可用 npx |

## 11. 已知限制

- 价目表 DEFAULT_PRICING 是**真实价目**（人民币元 / 百万 tokens，高峰价）：`deepseek-flash`
  未命中输入 2 元 / 命中输入 0.04 元 / 输出 8 元，`deepseek-v4-pro` 为 9 / 0.30 / 27 元；
  空闲时段为高峰价的一半，由 `off_peak_multiplier` 换算（高峰 = 周一至周五 9-12 点、14-18 点）。
- 单位是**元**而不是美元：字段名一律不带 `_usd`，货币由 `ModelPrice.currency` 声明，
  账本遇到货币不一致会直接报错（元与美元混着加总出来的总额没有意义）。
- 输入价**已区分缓存命中**（`cache_hit_input_per_1m`），未声明该字段时回落为普通输入价；
  且上报的命中 token 数会被截断到不超过 `prompt_tokens`，避免「缓存命中比输入还多」造成的负计费。
- 法定节假日日历**没有建模**：节假日会按高峰价计费，误差方向是「多算」而不是「少算」，
  成本报告不会因此虚低。
- 未配置价目的模型默认记 0 成本但**会被标记**（`unpriced_models` 非空 ⇒ `cost_is_complete=False`），
  报告里这类成本只能算**下限**；一旦设置了预算上限（`limit`）或 `strict_pricing=True`，
  则改为直接抛 UnknownModelPriceError，避免上限形同虚设。
- 预算熔断是**事后**检查（调用完成后才知道花费），因此可能略微超出上限；判定为「达到或超过上限」即熔断。
- 模型名先按最长前缀匹配已知价目（`deepseek-flash-v4-1` 会命中 `deepseek-flash`）。
- ScriptedLLMClient 的 token 数是估算值（约 4 字符 1 token），只能用于离线跑通链路，不能当作真实用量。
- 检索已接入（M3），但检索器是**词法**的：CJK 字符二元组 + idf 加权覆盖度，没有语义检索。
  已知的召回缺口与噪声清单见 `docs/stage2-evidence.md` 第 9.4 节；`retrieval_config_hash`
  记录的是语料指纹，语料一改，两批分数就不再可比。
