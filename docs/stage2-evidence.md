# 阶段二实测证据（M1 / M2）

所有数字都来自本机真实调用，原始 JSON 在 `reports/` 下，可随时复现。
**注意**：`code_version` 目前是 `nogit`（仓库还没 `git init`），所以 `is_reproducible=false` --
版本指纹的其余字段（模型、prompt hash、用例集 hash）都是有效的。

## 0. 端点事实（先搞清楚的坑）

| 事实 | 观测值 | 影响 |
| --- | --- | --- |
| 请求模型 vs 实际返回 | 请求 `deepseek-chat` → 返回 `deepseek-flash` | 供应商会做别名解析，所以 trace 必须同时记 `requested_model` 与 `model` |
| 可用模型名 | `deepseek-flash`、`deepseek-v4-pro` | **不在** DeepSeek 官方价目表里，价格未知，禁止臆造 |
| 首次调用延迟 | 13.4 s（冷启动） | 压测前必须预热，否则 P95 全是冷启动噪声 |
| 稳定调用延迟 | 0.9 ~ 3.1 s（单条，串行） | 这就是 B 档真模型压测的基线量级 |

## 1. 证据一：单次连通性与成本核算

```bash
python scripts/smoke_llm.py --save reports/m1-smoke-real.json
```

| 项 | 值 |
| --- | --- |
| 请求 / 返回模型 | `deepseek-chat` / `deepseek-flash` |
| 用量 | prompt 21 / completion 34 / total 55 tokens（cache_hit 0） |
| 延迟 | 3060.6 ms |
| 花费 | `$0.000000`，`cost_is_complete=false`（`deepseek-flash` 无价目） |
| manifest_id | `d1bd37a81c4b` |

**结论**：链路通、用量可读、成本口径诚实（未定价就明确说「只能算下限」，绝不假装 0 成本）。

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

用量：prompt 2106 / completion 305 tokens；平均延迟 **909.7 ms**；规划来源 19/19 来自模型；
行为变化 0 条；manifest_id `4779ebb89104`。

**结论（重要）**：这 19 条是**为 v1 量身定做的**，v1 满分 ⇒ 这套用例**没有区分度**，
既不能证明 v2 更差、也不能证明 v2 更好。评测集的第一个质量标准不是「条数多」，而是
**能把版本分开**。这条结论直接催生了证据三。

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

用量：prompt 884 / completion 141 tokens；平均延迟 **1557.3 ms**；8/8 用例行为发生变化；
manifest_id `73f5adbe7e5a`。

**结论**：v2 用 1.56 s/条的代价，把 v1 在口语化表达上的系统性漏判补齐；
`AuditRecord` 契约未变，说明这次替换是**可回归、可对比**的。

## 4. 成本口径（不要跳过的坑）

未定价模型按 0 记账但会被标记：

| 场景 | 行为 |
| --- | --- |
| 只有 `AGENTEVALOPS_PRICING_JSON` 未声明该模型 | 记 0 成本，`cost_is_complete=false`，报告里声明「成本是下限」 |
| 设置了 `AGENTEVALOPS_BUDGET_USD` | **直接报错**（`UnknownModelPriceError`），因为无价目时预算上限形同虚设 |

所以：**在有真实价格之前，任何「单次成功任务成本」都只能mark为下限**，不能写进简历数字里。

## 5. 复现步骤（从零到以上三份证据）

```bash
python -m pip install -e ".[llm]"                  # 装 openai SDK
python scripts/check_env.py                        # 确认 Key 已配置（不打印密钥）
python -m pytest -q                                # 全量测试
python scripts/smoke_llm.py --save reports/m1-smoke-real.json
python scripts/compare_planners.py --offline --save reports/v1-vs-v2-offline.json
python scripts/compare_planners.py --save reports/v1-vs-v2-real-19.json
python scripts/compare_planners.py --cases eval_cases/v1_gaps_cases.jsonl --save reports/v1-vs-v2-gaps.json
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

用量：prompt 20235 / completion 2427 tokens；平均延迟 **917.2 ms**；165/165 来自模型；
行为变化 9 条；manifest_id `13ba753e02f4`。

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

## 7. 尚未完成（挡住简历数字的部分）

| 待办 | 卡在哪 |
| --- | --- |
| `git init` + 打 v1 基线 tag | 未执行；导致 `code_version=nogit`、`is_reproducible=false`，3 个版本回归对比无法锚定 |
| `deepseek-flash` / `deepseek-v4-pro` 真实单价 | 未知；不确认就只能报「成本下限」 |
| 240 条用例矩阵 / 35 条安全样本 | 已有 **192 条**（normal 69 / boundary 48 / safety 38 / error 37），安全样本 38 条已达标；距 240 还差约 48 条 |
| 规格冻结（`docs/agent-spec.md`） | **未做**：期望值目前来自 v1 实现，导致 9 条争议用例无法判定谁对 |
| Langfuse trace | 本机无 Docker，只能走 Cloud 或先退化为本地 JSON trace |
| 并发压测（Locust/k6） | 未开始，需先决定 A 档（mock 压编排层）/ B 档（真模型） |
