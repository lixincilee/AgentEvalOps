# CI 门禁与报告冻结

这一页写给第一次接手这个仓库的人：`.github/workflows/regression.yml` 里的每一步在做什么、
什么时候会挡住合并、以及**哪些结论还没有被验证过**。

一句话：CI 不只跑测试。它还要在代码变化时回答一个更难的问题——
**这套评测本身的分数有没有退化？**

## 1. 三个 job，各管一段

| job | 什么时候跑 | 干什么 | 阻塞合并吗 |
| --- | --- | --- | --- |
| `test` | 每次 push 到 `main`、每个 PR、手动触发、每天定时 | `python -m pytest -q` | 阻塞 |
| `gate` | 同上（`needs: test`，测试先过） | 冻结候选发布，并和 `releases/v0.7.0` 比 | 阻塞 |
| `nightly-real` | **只在定时任务里**（`github.event_name == 'schedule'`） | 真实模型对比 + 真实并发压测 | 不阻塞 |

`nightly-real` 不阻塞合并有两重保险：它**在 PR 上根本不触发**；同时整个 job 标了
`continue-on-error: true`，所以供应商拥塞、限流、余额不足都不会把 workflow 弄红。

## 2. 触发、并发与权限

```yaml
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
  schedule:
    - cron: "0 18 * * *"
```

- `cron` 只认 UTC。`0 18 * * *` = 北京时间次日 02:00；这个点也正好落在
  DeepSeek 的空闲时段，单价是高峰的一半。
- `concurrency.group` 用 `github.workflow-github.ref`，`cancel-in-progress: true`：
  同一分支的新推送会取消旧运行。门禁回答的是「现在这版过不过」，旧版跑完也没人看。
- `permissions: contents: read`：CI 只读仓库，不需要任何写权限。

## 3. `gate` job 里那条命令拆开看

`gate` 只有一条实质命令：

```
python scripts/freeze_release.py --out .release-candidate --gate-against releases/v0.7.0
```

`freeze_release.py` 会跑五件事，每件都会把原始报告和从报告里抽出来的指标落盘
（出处：`scripts/freeze_release.py` 的 `main()` 与 `check_*` 函数）：

| 检查 | 实际命令 | 产物文件 | 写进索引的指标 |
| --- | --- | --- | --- |
| `pytest` | `python -m pytest -q --tb=no -p no:cacheprovider` | `pytest.txt` | `pytest.passed` / `pytest.failed` / `pytest.xfailed` / `pytest.skipped` |
| `faults` | `python scripts/run_faults.py --out ...` | `fault-run.json` | `fault.pass_rate` / `fault.cases` |
| `comparison:matrix` | `python scripts/compare_versions.py --offline --cases eval_cases/case_matrix_v2.jsonl` | `comparison-matrix.json` | `matrix.v1.pass_rate` / `matrix.v3_rules.pass_rate` |
| `comparison:rag_multiturn` | 同上，用例是 `rag_cases.jsonl` + `multiturn_cases.jsonl` | `comparison-rag-multiturn.json` | `rag_multiturn.v1.pass_rate` / `rag_multiturn.v3_rules.pass_rate` |
| `load_test` | `python scripts/load_test.py --users 20 --spawn-rate 5 --run-time 20s --port 8140` | `load-test.json` | `perf.http_error_rate` / `perf.task_success_rate` / `perf.p95_ms` / `perf.rps` / `perf.tokens_per_request` / `perf.cost_per_success_cny` |

跑完写出两份索引：`release.json`（机器读的指标索引）和 `README.md`（同一份内容的人读版）。
`release.json` 的 `kind` 必须是 `release_index`，否则门禁会把它当成「这不是一次冻结发布」。

三个刻意的设计：

- **为什么 `test` 里跑过 pytest，`gate` 里又跑一遍？** 因为 `release.json` 要把测试计数
  本身当成指标记下来（`pytest.passed` / `pytest.failed`）。没有计数就没有可对比的基线。
- **脏工作区拒绝冻结。** 指标锚不住版本号，后面的对比就都失去意义。放行要显式
  `--allow-dirty`，而且 `dirty=true` 会写进索引，门禁报告里看得见。
- **先写临时目录，全部跑完再搬过去。** 否则冻结到一半失败会留下一个半成品的
  `releases/<tag>/`，而它看起来像一份完整的基线。
- **检查跑不起来也算一次失败的检查。** 子进程启动失败（脚本不存在等）会被记成
  `exit_code: 127` 的失败检查，而不是让整个冻结崩掉 ——「这个检查跑不了」和
  「冻结失败」是两件事。所以索引里看到 127，先去查那个检查脚本在不在。

退出码（三个脚本统一约定）：`0` = 通过；`1` = 真有检查失败或门禁没过；
`2` = 起不来（用法 / 环境问题，例如工作区脏、缺 `release.json`）。

## 4. 门禁判据

判据只有一处定义：`scripts/regression_gate.py` 的 `RULES` 表（第 75-95 行）。
下面这张表是逐字抄的，**不是**本节重新解释的版本：

| 指标 | 方向 | 绝对下限 / 上限 | 允许相对基线退化 | 强制？ |
| --- | --- | --- | --- | --- |
| `pytest.failed` | 越小越好 | 必须为 0 | 0 | 是 |
| `fault.pass_rate` | 越大越好 | ≥ 100% | 0 | 是 |
| `matrix.v3_rules.pass_rate` | 越大越好 | ≥ 90% | 1 个百分点 | 是 |
| `rag_multiturn.v3_rules.pass_rate` | 越大越好 | ≥ 90% | 1 个百分点 | 是 |
| `perf.http_error_rate` | 越小越好 | ≤ 1% | 0.5 个百分点 | 是 |
| `perf.task_success_rate` | 越大越好 | ≥ 99% | 0.5 个百分点 | 是 |
| `perf.p95_ms` | 越小越好 | 无 | 相对 50% | **否，只报不拦** |
| `perf.cost_per_success_cny` | 越小越好 | 无 | 相对 50% | **否，只报不拦** |

读这张表要注意六件事：

- **两条判据，缺一不可。** 绝对下限防的是「基线烂，于是烂变成了标准」；相对退化防的是
  「从 100% 掉到 91%，但没跌破绝对下限」。
- **判定顺序是先绝对下限、再相对退化。** 反过来会先给出「比基线掉了多少」，而那个数字
  在指标本身就不可接受时会分散注意力。
- **强制规则缺指标 = 阻断；但「这项检查本次没跑」降级为提醒。** 两者必须分开：
  前者是报告坏了（跑了却没产出这个数），后者是压根没测。所以 `--skip-load` 会在
  `release.json` 的 `checks[]` 里留一条 `status: "skipped"`，门禁看到它就把该指标的阻断
  降成提醒。**不这么做，一条会经常误报的门禁等于没有门禁**——它只是训练人忽略它。
- **`null` 不是 0。** `release.json` 里的 `null` 表示「这次没测出来」；把它当 0 会凭空
  造出一个失败。
- **基线里没有这个指标时，只判绝对下限**，不拿 0 当基线。
- **阈值写死在脚本里，不接受命令行放宽** ——「能被现场放宽的门禁等于没有门禁」。
  两条只报不拦的规则正是因为噪声：同一份配置单次运行的 RPS 差 1.7 倍、P95 差 2.2 倍，
  还没压出结论就拿来拦版本，拦的是噪声（见 `docs/stage2-evidence.md` 第 11 节）。

## 5. 在本地复刻 CI

本机的解释器是 `D:\Python\python.exe`；CI 里统一写 `python`（`ubuntu-latest` 上没有
`python.exe`）。下面命令逐条对应 workflow 里的步骤：

```powershell
cd F:\Agent学习\AgentEvalOps

# 装依赖（三个 job 都是这一条）
D:\Python\python.exe -m pip install -e ".[dev,llm,api,perf]"

# 1) 全量测试 = test job
D:\Python\python.exe -m pytest -q

# 2) 冻结候选发布 + 门禁 = gate job
D:\Python\python.exe scripts/freeze_release.py --out .release-candidate --gate-against releases/v0.7.0

# 3) 只判门禁，不重新冻结（候选目录已存在时用这个，秒级）
D:\Python\python.exe scripts/regression_gate.py --candidate .release-candidate --baseline releases/v0.7.0

# 4) 本机工作区通常是脏的，冻结会被拒绝；放行要显式加 --allow-dirty
D:\Python\python.exe scripts/freeze_release.py --out .release-candidate --allow-dirty --skip-load
```

关于第 4 条：它只用于本地快速迭代。`--skip-load` 会让 `perf.*` 指标缺失，但冻结会把
`load_test` 记成 `status: "skipped"`，门禁据此只给 4 条提醒、不阻断（见第 4 节）。

会花钱的两条（只有你想跑才跑）。它们都要 `--yes`，都会在缺 key 时零成本退出，
`--budget` 是花费熔断上限（元）：

```powershell
D:\Python\python.exe scripts/compare_versions.py --llm --yes --limit 40 --budget 0.5
D:\Python\python.exe scripts/load_test.py --real --yes --users 5 --run-time 30s --budget 0.5
```

不带 `--yes` 时脚本会停在发请求之前并返回 2（本机实测，输出为
「未确认：真实调用需要显式加 --yes。已停在发请求之前。」）。

## 6. 没有验证过的部分

这一节是这一页最重要的部分。下面每一条都是「听起来应该对、但还没有证据」的声明：

| 声明 | 状态 |
| --- | --- |
| 这份 workflow 能在 GitHub 上跑通 | **未验证**。本机没有 Docker、没有 gh CLI，`.github/` 从未被 GitHub 读过。做过的是：每条 `run` 命令在本机按同一顺序跑过 + YAML 能被 `yaml.safe_load` 解析 |
| `test` / `gate` 两个 job 会变绿 | **未验证**（同上） |
| `nightly-real` | **从未触发过**。真实模型的延迟、吞吐与账单都还没取到真值 |
| 冻结发布里的 `matrix.v2.pass_rate` / `matrix.v3.pass_rate` | 永远是 `n/a`：`freeze_release.py` 只跑 `--offline`（规则臂）。离线模式下模型列是 `None`，不是 0 |
| `releases/v0.7.0/` 基线 | **已入库**：随 tag `v0.7.0` 冻结并提交，`gate` job 的执法基线就是它。本机已验证「冻结候选 → 门禁」整条链路通过 |
| mock 臂压测的 P95 / token | 建模值，报告里标 `provenance: modeled`，不是供应商真值 |
| 测试条数 | 会随用例增加而变。冻结基线时实测 `1061 passed, 10 xfailed`（`python -m pytest -q`） |
| `.release-candidate/` 能被打包上传 | **未验证**：本机没有 GitHub，验证不了「以点开头的目录」在当前 action 版本下的行为。为了不依赖它，上传步骤显式写了 `include-hidden-files: true` |

## 7. 已经踩过的坑

1. **浅克隆没有 tag。** `freeze_release.py` 用 `git describe --tags` 锚版本号；
   `actions/checkout` 默认 `depth=1`，浅克隆里没有 tag，锚点会退化成 `unknown`。
   所以 `gate` job 显式写了 `fetch-depth: 0`。
2. **哪些目录进 git、哪些不进，是有分工的。** `reports/` 被 gitignore：它是运行产物，
   含时间戳，入库必然弄脏工作区，让下一次运行的 manifest 变成 `-dirty`；
   `.release-candidate/` 也被 gitignore：它是一次检查的中间产物，不是要归档的发布；
   只有 `releases/<tag>/` **要提交进 git** —— 它才是「上个版本长什么样」的比较对象。
   所以 artifact 上传 `.release-candidate/` 和 `reports/`，而 `releases/<tag>/` 走提交。
3. **脏工作区会拒绝冻结（退出码 2）。** 本机实测输出：`工作区是脏的（有未提交改动），
   拒绝冻结：指标锚不住版本号，之后的对比都没意义。`
4. **`--skip-load` 会让 `perf.*` 变成「跳过」而不是「失败」**，门禁据此降级为提醒
   （见第 4 节）。
5. **编码。** 用例与报告全是中文，`scripts/freeze_release.py` 在跑子进程时强制
   `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`：Windows 上 locust 会把带中文注释的
   `pyproject.toml` 当默认配置读，GBK 下会炸在解析之前。workflow 里也设了同样的变量。
6. **退出码 2 不等于失败。** 三个脚本都约定 `2 = 用法 / 环境问题`。CI 日志里看到 2，
   先查环境（工作区脏、缺 `release.json`、缺 key），不要当成质量结论。
7. **基线不存在时曾经会吐 traceback（已修）。** 门禁在 `freeze_release.py` 里是被直接
   调用的，`load_release` 抛的 `GateUsageError` 一开始没被捕获，退出码是误导性的 1。
   现在会打出「门禁起不来：…」并返回干净的 2。
8. **门禁真的会拦。** 本机实测：把 `RefundTool.refundable_statuses` 从 `{"delivered"}`
   改成 `set()`（一个很像「手滑收紧规则」的改动），冻结出的候选被拦下 4 条：
   `pytest.failed 51 > 0`、`fault.pass_rate 92% < 100%`、
   `matrix.v3_rules.pass_rate 86.9% < 90%`、
   `rag_multiturn.v3_rules.pass_rate 比基线退化 6.7% > 容忍 1%`。
   绝对下限与相对退化**两条判据都各自触发过**。
