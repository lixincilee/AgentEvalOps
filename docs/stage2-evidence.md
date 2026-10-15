# 阶段二实测证据（M1 / M2）

所有数字都来自本机真实调用。原始 JSON 由脚本写入 `reports/`（已被 `.gitignore` 忽略，
它是运行产物），随时可以重跑生成。

**版本锚定**：仓库已 `git init`，下面每一份报告都来自**干净工作区**：
`code_version = e95ce18`、`is_reproducible = true`、`git describe --tags --dirty = v0.2.0`。

> `is_reproducible=true` 只保证**输入**可复现（代码版本 + prompt + 模型 + 用例集全部钉死），
> **不保证输出可复现**：同一个 manifest 的两次真实运行分数就不一样（7.2 节）；编排层自己
> 出过一次「数字对不上」的 bug（7.3 节）；供应商也不是恒速的（7.4 节）。

## 0. 端点事实（先搞清楚的坑）

| 事实 | 观测值 | 影响 |
| --- | --- | --- |
| 请求模型 vs 实际返回 | 请求 `deepseek-chat` → 返回 `deepseek-flash` | 供应商会做别名解析，trace 必须同时记 `requested_model` 与 `model` |
| 可用模型名 | `deepseek-flash`、`deepseek-v4-pro` | 官方价目页已确认：以**人民币元**计价，空闲时段为高峰价的一半 |
| 首次调用延迟 | 13.4 s（冷启动） | 压测前必须预热，否则 P95 全是冷启动噪声 |
| 健康窗口延迟 | 0.4 ~ 2.4 s（单条，串行） | 这就是 B 档真模型压测的基线量级 |
| **拥塞窗口延迟** | 同一端点、同一 Key，实测 **12.9 ~ 28.8 s/次**，并出现超时回退 | 版本对比必须在健康窗口内做，见 7.4 |

## 1. 证据一：单次连通性与成本核算

```bash
python scripts/smoke_llm.py --save reports/m1-smoke-real.json
```

| 项 | 值 |
| --- | --- |
| 请求 / 返回模型 | `deepseek-chat` / `deepseek-flash` |
| 用量 | prompt 21 / completion 36 / total 57 tokens（cache_hit 0） |
| 延迟 | 24694.6 ms —— 这一发正好落在拥塞窗口里（见 7.4） |
| 花费 | `CNY 0.000330`，`cost_is_complete=true` |
| 独立复核 | (21×2 + 36×8) ÷ 1e6 = 0.00033 元 ✓ |
| manifest_id | `9b91e683b422` |

**结论**：链路通、用量可读、成本口径能被一个独立算式复核，而不是「信不信脚本」。

## 2. 证据二：v1 规则集上 v1 vs v2（19 条）

```bash
python scripts/compare_planners.py --save reports/v1-vs-v2-real-19.json
```

| 指标 | v1 规则 | v2 模型 | 差 |
| --- | --- | --- | --- |
| 意图准确率 | 100.0% | 100.0% | +0.0% |
| 状态准确率 | 100.0% | 100.0% | +0.0% |
| 工具选择准确率 | 100.0% | 100.0% | +0.0% |
| 参数准确率 | 100.0% | 100.0% | +0.0% |
| 任务成功率 | 36.8% | 36.8% | +0.0% |

用量：prompt 2106 / completion 311 tokens（无缓存命中）；平均延迟 **2292 ms**；
19/19 来自模型；行为变化 0 条；花费 `CNY 0.006700`（独立复核：(2106×2 + 311×8) ÷ 1e6 ✓）；
manifest_id `244c53ab4840`。

**结论（重要）**：这 19 条是**为 v1 量身定做的**，v1 满分 ⇒ 这套用例**没有区分度**，
既不能证明 v2 更差、也不能证明 v2 更好。评测集的第一个质量标准不是「条数多」，
而是**能把版本分开**。这条结论直接催生了证据三。

## 3. 证据三：v1 漏判集上 v1 vs v2（8 条）

```bash
python scripts/compare_planners.py --cases eval_cases/v1_gaps_cases.jsonl --save reports/v1-vs-v2-gaps.json
```

用例样例（全部是**正常且合理**的请求，只是措辞不在 v1 关键词表里）：

| case_id | 消息 | v1 | v2 |
| --- | --- | --- | --- |
| `gap_status_track_en` | help me track order ORD-1001 | `no_action` | 查单成功 |
| `gap_status_cha_yixia` | ORD-1001 查一下 | `no_action` | 查单成功 |
| `gap_status_zenmeyang` | ORD-1002 现在怎么样了 | `no_action` | 查单成功 |
| `gap_refund_tui_wo_qian` | ORD-1002 重复扣款了，请退我钱 | `no_action` | 退款受理成功 |

| 指标 | v1 规则 | v2 模型 | 差 |
| --- | --- | --- | --- |
| 意图准确率 | 0.0% | **100.0%** | +100.0% |
| 状态准确率 | 0.0% | **100.0%** | +100.0% |
| 工具选择准确率 | 0.0% | **100.0%** | +100.0% |
| 参数准确率 | 0.0% | **100.0%** | +100.0% |
| 任务成功率 | 0.0% | **100.0%** | +100.0% |

用量：prompt 884 / completion 138 tokens；平均延迟 **1503 ms**；8/8 用例行为发生变化；
花费 `CNY 0.002872`；单次成功任务成本 `CNY 0.000359`；manifest_id `a36656c2df43`。

**结论**：v2 用约 1.5 s/条的代价，把 v1 在口语化表达上的系统性漏判补齐；
`AuditRecord` 契约未变，说明这次替换是**可回归、可对比**的。

> ⚠️ 这份对比**第一次**跑时正好撞在拥塞窗口里：8 条里有 1 条调用超时回退到 v1，
> v2 只有 87.5%。同一份用例、同一个 manifest，窗口恢复后重跑就是 100%。
> **这是运行环境抖动，不是版本退化**——把它当成 v2 的缺陷去查代码会白查半天。

## 4. 成本口径（不要跳过的坑）

| 场景 | 行为 |
| --- | --- |
| 模型有内置价目（`deepseek-flash` / `deepseek-v4-pro`） | 直接算出**真实成本**，`cost_is_complete=true` |
| 模型无价目 | 记 0 成本但标记 `cost_is_complete=false`，报告里声明「成本只算下限」 |
| 设置了 `AGENTEVALOPS_BUDGET` | 无价目时**直接报错**（`UnknownModelPriceError`），因为上限形同虚设 |
| 价目表里混了美元条目 | 账本直接报错，拒绝把元和美元加总 |

**165 条报告的成本怎么复核**（这一条差点被误判成记账 bug）：

| 项 | 计算 |
| --- | --- |
| 缓存未命中输入 | 17803 tokens × 2 元 = 35606 |
| 缓存命中输入 | 2432 tokens × 0.04 元 = 97.28 |
| 输出 | 2442 tokens × 8 元 = 19536 |
| 合计 | (35606 + 97.28 + 19536) ÷ 1e6 = **CNY 0.055239** ✓ |

只按「输入全价」反推会得到 0.060006 元，于是看起来「账本算错了」。**缓存命中折扣是
承重的**：同一段 system prompt 在 159 次调用里重复出现，命中部分按 1/50 的价格计费。

## 5. 复现步骤（从零到以上全部证据）

```bash
python -m pip install -e ".[llm]"                  # 装 openai SDK
python scripts/check_env.py                        # 确认 Key 已配置（不打印密钥）
python -m pytest -q                                # 全量测试，应为 562 passed
python scripts/compare_planners.py --offline --save reports/v1-vs-v2-offline.json  # 零成本自检
python scripts/smoke_llm.py --save reports/m1-smoke-real.json
python scripts/compare_planners.py --save reports/v1-vs-v2-real-19.json
python scripts/compare_planners.py --cases eval_cases/v1_gaps_cases.jsonl --save reports/v1-vs-v2-gaps.json
python scripts/compare_planners.py --cases eval_cases/case_matrix_v2.jsonl --save reports/v1-vs-v2-matrix-165.json
```

## 6. 证据四：165 条用例矩阵上的 v1 vs v2（真实模型）

```bash
python scripts/compare_planners.py --cases eval_cases/case_matrix_v2.jsonl --save reports/v1-vs-v2-matrix-165.json
```

| 指标 | v1 规则 | v2 模型 | 差 |
| --- | --- | --- | --- |
| 意图准确率 | 100.0% | 96.4% | **-3.6%** |
| 状态准确率 | 100.0% | 94.5% | **-5.5%** |
| 工具选择准确率 | 100.0% | 96.4% | **-3.6%** |
| 参数准确率 | 100.0% | 98.8% | -1.2% |
| 任务成功率 | 48.5% | **50.3%** | **+1.8%** |

用量：prompt 20235 / completion 2442 tokens；平均延迟 **852 ms**（159 次调用样本）；
行为变化 9 条；花费 `CNY 0.055239`；单次成功任务成本 `CNY 0.000666`；
manifest_id `d7dfb5b725e1`。

### 6.1 为什么是「159 次调用 / 165 条用例」

用例矩阵里有 6 条畸形请求，它们在 `handle()` 里、调用规划器**之前**就被请求级校验拒绝，
因此**根本不产生模型调用**：

| case_id | 消息 | customer_id | 结果 |
| --- | --- | --- | --- |
| `v2_bnd_whitespace_only` | 5 个空格 | CUST-001 | `validation_failed` |
| `v2_bnd_tab_only` | 两个制表符 | CUST-001 | `validation_failed` |
| `v2_err_validation_04` | 空字符串 | CUST-001 | `validation_failed` |
| `v2_err_validation_05` | 3 个空格 | CUST-001 | `validation_failed` |
| `v2_err_validation_06` | 正常消息 | 空字符串 | `validation_failed` |
| `v2_err_validation_07` | 正常消息 | 3 个空格 | `validation_failed` |

所以报告把它们单独记成 `source=not-planned`（`source_breakdown = {llm: 159, not-planned: 6}`），
并用三项不变式自检（见 7.3）：每次调用必落在高峰/空闲之一档、模型调用数不超过进入规划层的
用例数、所有短路用例状态都是 `validation_failed`。

### 6.2 9 条差异的归类

**9 条差异全部落在规格灰区，没有一条是「模型明确犯错」**：

| 类别 | 条数 | 说明 |
| --- | --- | --- |
| A：期望值编码了 v1 的正则边界缺陷 | 3 | 中文/数字紧贴订单号时 v1 认不出，v2 正确抽出 → **v2 更好** |
| B：意图粒度未定义 | 4 | 「意图明确但缺订单号」：v1 判 `no_action`，v2 判查单后报缺参数 |
| C：安全语义未定义 | 2 | 两者都无越权副作用，差别只在状态名 |
| **真正的 v2 错误** | **0** | — |

逐条归类与根因见 `docs/bad-cases-v2.md`。

**这条结论比分数本身重要**：这些期望值是从 v1 的**实现**导出的，所以它度量的是
「与基线的差异」，不是「正确性」。下一步必须先**冻结规格**，再重跑对比；
在此之前任何 v1/v2 分数都不能解读为「谁更正确」。

## 7. 版本锚定、运行方差与两次真实故障

### 7.1 锚定结果

| 项 | 值 |
| --- | --- |
| 提交 | `e95ce18` |
| tag | `v0.2.0`（`v0.1.0` 是阶段一基线） |
| `git describe --tags --dirty` | `v0.2.0`（**没有 `-dirty` 后缀**） |
| `VersionManifest.code_version` | `e95ce18` |
| `VersionManifest.is_reproducible` | `true` |
| 报告里的被测版本身份 | `extra = {"baseline_planner": "rules-v1", "candidate_planner": "llm-v2"}` |

tag 与报告一一对应，拿到报告就能回到产生它的代码状态：

| 报告 | 用例 | code_version | manifest_id |
| --- | --- | --- | --- |
| `m1-smoke-real.json` | 1 | `e95ce18` | `9b91e683b422` |
| `v1-vs-v2-offline.json` | 19 | `e95ce18` | `566e947acc0c` |
| `v1-vs-v2-real-19.json` | 19 | `e95ce18` | `244c53ab4840` |
| `v1-vs-v2-gaps.json` | 8 | `e95ce18` | `a36656c2df43` |
| `v1-vs-v2-matrix-165.json` | 165 | `e95ce18` | `d7dfb5b725e1` |
| `v1-vs-v2-matrix-165-r2.json` | 165 | `31433a7` | `4d598346aff3`（旧价目改动之前跑的第二采样，成本字段是旧口径） |

为了让「干净」这个状态能维持住，做了两件必要的事：

1. **`reports/` 改成 `.gitignore`。** 报告每次运行都会重写（含时间戳），一旦入库，
   工作区就永远是脏的，于是下一次运行的 `code_version` 会带 `-dirty`、
   `is_reproducible` 恒为 `false`——版本对比永远锚不住。结论留在 `docs/`，原始报告交给 CI 当工件。
2. **跑对比前先检查工作区。** `compare_planners.py` 在花钱调用模型**之前**就会告警：
   非 git 仓库（`nogit`）或脏树（`xxx-dirty`）都意味着本次结果不能当锚点。

### 7.2 同一个版本跑三次，分数不一样

同一个 `manifest_id`（`4d598346aff3`）、同一份 prompt、`temperature=0`，165 条用例跑了三次：

| 采样 | 意图准确率 | 与 v1 的差异条数 | 平均延迟 | completion tokens |
| --- | --- | --- | --- | --- |
| run1（锚定前） | 96.4% | 9 | 917.2 ms | 2427 |
| run2 | 95.8% | **10** | 704.6 ms | 2445 |
| run3 | 96.4% | 9 | 723.2 ms | 2427 |

多出来的那一条是 `v2_err_unsupported_04`（「订单 ORD-1001 多少钱」）：run2 里 v2 判成
`order_status` 跑去查单，run1/run3 判 `unknown` 拒绝。也就是 **9 条稳定差异 + 1 条偶发差异**。
这是**供应商侧的采样随机性，不是我们代码的**。

2026-10-15 在 `v0.2.0` 上重跑第四次：意图 96.4%、9 条差异、平均延迟 852 ms、
completion 2442 tokens——与 run1/run3 一致，说明这 9 条是稳定差异。

**结论（最容易踩的坑）**：

- `manifest_id` 标识的是**版本**，不是**输出**。两次运行 manifest 完全相同，分数却差了 0.6 个百分点。
  所以版本对比**必须报告重复执行的方差**，否则会把模型抖动当成版本退化去查半天。
- 单次 165 条的噪声水平约为 ±1 条用例。要判断「v2 是否真的退化」，
  每个版本必须重复跑 N 次取均值/多数票（已列入待办）。

### 7.3 编排层的一次真实 bug：165 条用例只统计出 159 次调用

重跑 165 条时报告出现了两个对不上的数字：`cases=165` 但 `peak_calls=159`（少了 6 次），
而成本 `0.055127` 元按高峰价反推也对不上。第一反应是计价记账写错了，**实际不是**：

| 排查步骤 | 结论 |
| --- | --- |
| 用可控时钟单独记录 165 次调用 | `calls=165`、`peak+off=165`，记账代码正确 |
| 按 `缓存未命中×2 + 缓存命中×0.04 + 输出×8` 重新核算 | 恰好等于 `0.055127` 元，**成本是对的** |
| 统计每行 `v2_source` 与延迟 | 165 行全是 `llm`、延迟全非零 —— 但内层客户端只被调用了 **159** 次 |

根因是两个 bug 叠在一起：

1. **`handle()` 没清空 `last_plan`。** 6 条畸形请求在规划前被拒绝，报告却读到了**上一条用例**
   的 `last_plan`，于是把它们记成 `source=llm`，延迟也显示成上一条的。
2. **离线自检的脚本答案错位。** `offline_scripts()` 给全部 165 条都发了答案，但只有 159 条
   会真正调用模型；脚本客户端是**按调用顺序**回放的，多发的 6 条让后面每条答案整体错位，
   于是「v2 必须与 v1 完全一致」的自检**虚报了 58 条差异**。

修复（提交 `e95ce18`）：

- `agent.handle()` 入口先 `self.last_plan = None`；抽出 `pre_plan_rejection()` 让 agent 与
  编排层共用同一份判定，两边规则不可能再不一致；
- `offline_scripts()` 只给会进入规划层的用例发答案；
- 抽出纯函数 `run_comparison()`，报告新增 `planned_cases` / `short_circuited_cases` /
  `model_calls` / `not_planned_cases` / `latency_samples`，并加三项不变式自检；
- 平均延迟只统计**真实调用样本**，不再把被拒用例的 0 拉进分母；
- 新增 `tests/test_comparison_harness.py`：离线自检必须逐条复现 v1，且报告里每个数字都要能互相对上。

**教训**：`159 ≠ 165` 这种「两个数字对不上」的告警，比任何单项指标都值钱。
如果报告只是打印一张好看的指标表，这个 bug 会一直躺在那里——而它已经让一份报告
对 6 条用例标注了错误的来源和延迟。

### 7.4 供应商拥塞窗口：同一份用例，延迟差 30 倍

2026-10-15 上午重跑时遇到一段明显的服务降级（同一端点、同一 Key、同一份用例）：

| 观测 | 健康窗口 | 拥塞窗口 |
| --- | --- | --- |
| 单次调用延迟 | 0.4 ~ 2.4 s | 12.9 ~ 28.8 s |
| 调用失败（超时后回退 v1） | 0 / 159 | 6 / 19（一次性）、1 / 8 |
| 165 条总耗时 | 139 s | 45 分钟仍未跑完（已中止） |

两次降级之间只隔了几分钟，且中间单发一次冒烟又慢到 24.7 s——**说明它是突发的**。

**结论**：

- 版本对比**必须在健康窗口内做**，并且要在结论里记录当时的延迟量级；
- 拥塞期的分数会**因为超时回退而系统性偏低**，看上去就像「新版本退化了」；
- 这正是需要「重复执行 + 门禁 + 失败样本归档」的原因：单次结果不足以判定退化。

## 8. 尚未完成（挡住简历数字的部分）

| 待办 | 状态 |
| --- | --- |
| ~~`git init` + 打基线 tag~~ | **已完成**：`e95ce18` / tag `v0.2.0` / `is_reproducible=true` |
| ~~真实单价~~ | **已完成**：`deepseek-flash` 2/0.04/8 元、`deepseek-v4-pro` 9/0.30/27 元（高峰价，空闲对半） |
| 240 条用例矩阵 / 35 条安全样本 | 已有 **192 条**（normal 69 / boundary 48 / safety 38 / error 37），安全样本 38 条已达标；距 240 还差约 48 条 |
| 规格冻结（`docs/agent-spec.md`） | **未做**：期望值目前来自 v1 实现，导致 9 条争议用例无法判定谁对（最高优先，比调 prompt 重要） |
| 重复执行 N 次取稳定值 | **未做**：单次运行有 ±1 条用例的采样噪声（7.2），需要 `--repeat N` 支持 |
| Langfuse trace | 本机无 Docker，只能走 Cloud 或先退化为本地 JSON trace |
| 并发压测（Locust/k6） | 未开始，需先决定 A 档（mock 压编排层）/ B 档（真模型） |
| `--repeat` 与报告快照入库 | 未开始：`reports/` 是运行产物；「可展示的报告」建议冻结到 `releases/<tag>/` 再入库 |