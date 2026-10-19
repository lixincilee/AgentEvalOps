r"""冻结一次发布：跑齐离线检查，把产物与指标索引写进 ``releases/<tag>/``。

用法::

    D:\Python\python.exe scripts/freeze_release.py
    D:\Python\python.exe scripts/freeze_release.py --out .release-candidate --tag candidate --gate-against releases/v0.6.0

退出码：``0`` 全部通过（含门禁）；``1`` 有检查失败或门禁没过；``2`` 起不来。

为什么要有这个脚本：``reports/`` 是运行产物（gitignore 掉了），它记不住「上个版本长什么样」。
没有一份**提交进 git 的**历史指标，所谓「回归」就没有比较对象，所谓「版本对比」也只是
两张互不相干的截图。``releases/<tag>/release.json`` 就是那份比较对象。

两件刻意的设计：

* **脏工作区拒绝冻结**。指标锚不住版本号，后面所有对比都失去意义（要放行用 ``--allow-dirty``，
  但 ``dirty=true`` 会写进索引，门禁报告里看得见）。
* **先写临时目录，全部跑完再搬过去**。否则冻结到一半失败会留下一个半成品 ``releases/<tag>/``，
  而它会看起来像一份完整的基线。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
RELEASES_DIR = REPO_ROOT / "releases"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(REPO_ROOT / "src"))

INDEX_NAME = "release.json"
CASE_MATRIX = "eval_cases/case_matrix_v2.jsonl"
RAG_CASES = "eval_cases/rag_cases.jsonl"
MULTITURN_CASES = "eval_cases/multiturn_cases.jsonl"
FAULT_CASES = "eval_cases/fault_cases.jsonl"

#: 一次检查最多给多久。压测要起服务、跑 locust，比其它几个都慢。
TIMEOUTS = {"pytest": 1800.0, "load": 600.0, "trace": 900.0, "judge": 60.0}

PYTEST_COUNT = re.compile(r"(\d+)\s+(passed|failed|xfailed|xpassed|skipped|error|errors|deselected)")


class FreezeSetupError(RuntimeError):
    """冻结根本起不来：git 状态不对、参数不对。"""


def _git(*arguments: str) -> str | None:
    """跑一条 git 命令；取不到就返回 None，索引里写 null 比猜一个诚实。"""
    try:
        done = subprocess.run(
            ["git", *arguments], cwd=REPO_ROOT, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = done.stdout.strip()
    return output if done.returncode == 0 and output else None


def is_dirty() -> bool:
    return bool(_git("status", "--porcelain"))


def spec_version() -> str | None:
    """行为规格版本。取不到就算了，不为了一个展示字段让冻结失败。"""
    try:
        from agentevalops.spec import SPEC_VERSION

        return str(SPEC_VERSION)
    except Exception:
        return None


def _run(command: Sequence[str], timeout: float) -> dict[str, Any]:
    """跑一条子进程，返回 ``{exit_code, output, seconds}``。

    环境强制 UTF-8：locust 会把 cwd 下的 ``pyproject.toml`` 当默认配置读，而那份带中文注释，
    在 GBK 默认的 Windows 上会炸在解析前（``scripts/load_test.py`` 里踩过同一个坑）。
    """
    started = time.monotonic()
    try:
        done = subprocess.run(
            list(command), cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "output": f"超时（>{timeout:.0f}s）", "seconds": timeout}
    except OSError as exc:
        # 脚本本身不存在（比如在旧 tag 上跑新版冻结器）也要记成一次失败的检查，
        # 而不是让整个冻结崩掉——"这个检查跑不了" 和 "冻结失败" 是两件事。
        return {"exit_code": 127, "output": f"启动失败: {exc}", "seconds": 0.0}
    return {
        "exit_code": done.returncode,
        "output": (done.stdout or "") + (done.stderr or ""),
        "seconds": round(time.monotonic() - started, 3),
    }


def parse_pytest_counts(output: str) -> dict[str, int]:
    """从 pytest 的收尾行里抽计数。词表是 pytest 自己的（passed / failed / xfailed …）。

    复数归一化到单数：``0 errors`` 与 ``1 error`` 是同一个键。
    """
    counts: dict[str, int] = {}
    for number, word in PYTEST_COUNT.findall(output):
        key = word.rstrip("s") if word.endswith("s") and word != "passed" else word
        counts[key] = counts.get(key, 0) + int(number)
    return counts


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _column_pass_rate(report: dict[str, Any], column: str) -> float | None:
    """取某一列的行级通过率。离线模式下模型列是 ``None``，不是缺字段——别把它当 0。"""
    column_data = (report.get("versions") or {}).get(column)
    if not isinstance(column_data, dict):
        return None
    metrics = column_data.get("metrics") or {}
    value = metrics.get("pass_rate")
    return None if value is None else float(value)


def check_pytest(tmp: Path) -> dict[str, Any]:
    result = _run(
        [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
        TIMEOUTS["pytest"],
    )
    (tmp / "pytest.txt").write_text(result["output"], encoding="utf-8")
    counts = parse_pytest_counts(result["output"])
    failed = counts.get("failed", 0) + counts.get("error", 0)
    metrics = {
        "pytest.passed": counts.get("passed", 0),
        "pytest.failed": failed,
        "pytest.xfailed": counts.get("xfailed", 0),
        "pytest.skipped": counts.get("skipped", 0),
    }
    return {
        "name": "pytest",
        "status": "passed" if result["exit_code"] == 0 and failed == 0 else "failed",
        "exit_code": result["exit_code"],
        "seconds": result["seconds"],
        "artifact": "pytest.txt",
        "metrics": metrics,
        "detail": f"{counts.get('passed', 0)} passed / {failed} failed",
    }


def check_faults(tmp: Path) -> dict[str, Any]:
    out = tmp / "fault-run.json"
    result = _run(
        [sys.executable, str(SCRIPTS_DIR / "run_faults.py"), "--out", str(out)],
        TIMEOUTS.get("faults", 600.0),
    )
    report = _read_json(out) or {}
    metrics = {
        "fault.pass_rate": report.get("pass_rate"),
        "fault.cases": report.get("passed"),
    }
    return {
        "name": "faults",
        "status": "passed" if result["exit_code"] == 0 else "failed",
        "exit_code": result["exit_code"],
        "seconds": result["seconds"],
        "artifact": "fault-run.json" if report else None,
        "metrics": metrics,
        "detail": f"{report.get('passed', '?')}/{report.get('rows') and len(report.get('rows')) or '?'} 条故障用例",
    }


def check_comparison(name: str, cases: Sequence[str], tmp: Path, artifact: str) -> dict[str, Any]:
    out = tmp / artifact
    result = _run(
        [sys.executable, str(SCRIPTS_DIR / "compare_versions.py"), "--offline",
         "--cases", *cases, "--out", str(out)],
        TIMEOUTS.get("comparison", 600.0),
    )
    report = _read_json(out) or {}
    metrics = {
        f"{name}.v1.pass_rate": _column_pass_rate(report, "v1"),
        f"{name}.v3_rules.pass_rate": _column_pass_rate(report, "v3-rules"),
    }
    return {
        "name": f"comparison:{name}",
        "status": "passed" if result["exit_code"] == 0 else "failed",
        "exit_code": result["exit_code"],
        "seconds": result["seconds"],
        "artifact": artifact if report else None,
        "metrics": metrics,
        "detail": f"{len(report.get('rows') or [])} 行",
    }


def check_trace(tmp: Path) -> dict[str, Any]:
    """生成发布用的 trace、失败定位报告与 HTML 视图（规则臂，零成本）。

    trace 是「失败到底坏在哪一层」的证据；它不替代通过率，但必须和发布一起冻结，
    否则下个版本只能看到分数，看不到现场。
    """
    trace_dir = tmp / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    traces = trace_dir / "traces.jsonl"
    cases = trace_dir / "traces.cases.jsonl"
    report_md = trace_dir / "trace-report.md"
    report_json = trace_dir / "trace-report.json"
    view = trace_dir / "trace-view.html"

    run = _run(
        [
            sys.executable, str(SCRIPTS_DIR / "trace_run.py"),
            "--cases", CASE_MATRIX,
            "--faults", FAULT_CASES,
            "--multiturn", MULTITURN_CASES,
            "--out", str(traces),
            "--quiet",
        ],
        TIMEOUTS["trace"],
    )
    report_run = _run(
        [
            sys.executable, str(SCRIPTS_DIR / "trace_report.py"),
            "--traces", str(traces),
            "--cases", str(cases),
            "--out", str(report_md),
            "--json", str(report_json),
            "--quiet",
        ],
        TIMEOUTS["trace"],
    )
    # 视图是展示层：失败不阻断发布，但产物缺失要在 detail 里说出来。
    view_run = _run(
        [
            sys.executable, str(SCRIPTS_DIR / "trace_view.py"),
            "--traces", str(traces),
            "--out", str(view),
            "--title", "AgentEvalOps 发布 trace 视图",
            "--quiet",
        ],
        TIMEOUTS["trace"],
    )

    report = _read_json(report_json) or {}
    total = int(report.get("traces") or 0)
    failed = int(report.get("failed") or 0)
    stages = report.get("stages") or {}
    unclassified = int(stages.get("unclassified") or 0)
    pass_rate = None if total == 0 else (total - failed) / total
    ok = run["exit_code"] == 0 and report_run["exit_code"] == 0 and total > 0
    detail = f"{total} 条 trace / {failed} 条不通过 / 未定位 {unclassified}"
    if view_run["exit_code"] != 0:
        detail += "（HTML 视图生成失败）"

    return {
        "name": "trace",
        "status": "passed" if ok else "failed",
        "exit_code": 0 if ok else (report_run["exit_code"] or run["exit_code"] or 1),
        "seconds": round(run["seconds"] + report_run["seconds"] + view_run["seconds"], 3),
        "artifact": "trace/trace-report.md",
        "artifacts": [
            "trace/traces.jsonl",
            "trace/traces.cases.jsonl",
            "trace/trace-report.md",
            "trace/trace-report.json",
            "trace/trace-view.html",
        ],
        "metrics": {
            "trace.traces": total,
            "trace.failed": failed,
            "trace.pass_rate": pass_rate,
            "trace.unclassified": unclassified,
        },
        "detail": detail,
    }


def check_judge(
    tmp: Path,
    *,
    l3: Path = REPO_ROOT / "reports" / "l3-ragas.json",
    l4: Path = REPO_ROOT / "reports" / "l4-deepeval.json",
    l5: Path = REPO_ROOT / "reports" / "l5-promptfoo.json",
    review_summary: Path = REPO_ROOT / "quality" / "review-summary-v1.json",
) -> dict[str, Any]:
    """把三层判分报告与人工抽检证据复制进发布目录。

    L3/L4 需要真实模型、会花钱，冻结时**不重跑**：冻结的是「这次发布对应的报告」，
    不是「冻结时再花一次钱」。L5 是确定性的，报告同样直接归档。
    """
    judge_dir = tmp / "judge"
    judge_dir.mkdir(parents=True, exist_ok=True)
    layers = (
        ("L3", l3, "l3-ragas"),
        ("L4", l4, "l4-deepeval"),
        ("L5", l5, "l5-promptfoo"),
    )
    sources: list[dict[str, Any]] = []
    metrics: dict[str, float | None] = {}
    details: list[str] = []
    missing: list[str] = []
    errors = 0

    for layer, path, stem in layers:
        report = _read_json(path)
        if report is None or report.get("layer") != layer:
            missing.append(layer)
            continue
        for suffix in (".json", ".md"):
            source = path.with_suffix(suffix)
            if source.is_file():
                shutil.copyfile(source, judge_dir / f"{stem}{suffix}")
        pass_rate = report.get("pass_rate")
        worst = report.get("worst_pass_rate", pass_rate)
        repeat = int(report.get("repeat") or 1)
        layer_errors = int(report.get("errors") or 0)
        errors += layer_errors
        metrics.update(
            {
                f"judge.{layer}.pass_rate": pass_rate,
                f"judge.{layer}.worst_pass_rate": worst,
                f"judge.{layer}.repeat": repeat,
                f"judge.{layer}.errors": layer_errors,
                f"judge.{layer}.cost_cny": report.get("cost_cny"),
            }
        )
        sources.append(
            {
                "layer": layer,
                "path": str(path),
                "generated_at": report.get("generated_at"),
                "judge_model": report.get("judge_model"),
                "case_set": report.get("case_set"),
                "repeat": repeat,
            }
        )
        shown = "n/a" if pass_rate is None else f"{float(pass_rate):.1%}"
        details.append(f"{layer} {shown}（{repeat} 轮）")

    metrics["judge.errors"] = errors

    # 人工抽检是「Judge 分数是否可信」的证据，和三层报告一起归档；它不参与自动门禁。
    if review_summary.is_file():
        summary = _read_json(review_summary) or {}
        shutil.copyfile(review_summary, judge_dir / "review-summary-v1.json")
        metrics["judge.human.agreement"] = summary.get("agreement")
        metrics["judge.human.kappa"] = summary.get("kappa")
        metrics["judge.human.labeled"] = summary.get("labeled")
        details.append(
            "人工 "
            f"{summary.get('labeled', 0)}/{summary.get('selected', 0)}"
            f" / kappa {summary.get('kappa', 'n/a')}"
        )

    baseline = REPO_ROOT / "quality" / "judge_baseline.json"
    if baseline.is_file():
        shutil.copyfile(baseline, judge_dir / "judge_baseline.json")

    status = "passed" if not missing and errors == 0 else "failed"
    if missing:
        details.append("缺少报告：" + "、".join(missing))
    return {
        "name": "judge",
        "status": status,
        "exit_code": 0 if status == "passed" else 1,
        "seconds": 0.0,
        "artifact": "judge/l3-ragas.json" if l3.is_file() else None,
        "artifacts": [
            "judge/l3-ragas.json",
            "judge/l3-ragas.md",
            "judge/l4-deepeval.json",
            "judge/l4-deepeval.md",
            "judge/l5-promptfoo.json",
            "judge/l5-promptfoo.md",
            "judge/review-summary-v1.json",
            "judge/judge_baseline.json",
        ],
        "metrics": metrics,
        "sources": sources,
        "detail": " / ".join(details),
    }


def check_load(
    tmp: Path, *, port: int, users: int, run_time: str, repeat: int = 1
) -> dict[str, Any]:
    out = tmp / "load-test.json"
    command = [
        sys.executable, str(SCRIPTS_DIR / "load_test.py"), "--users", str(users),
        "--spawn-rate", str(max(1, users // 4)), "--run-time", run_time,
        "--port", str(port), "--report", str(out),
    ]
    if repeat > 1:
        command += ["--repeat", str(repeat)]
    result = _run(command, TIMEOUTS["load"])
    report = _read_json(out) or {}
    metrics = {
        "perf.http_error_rate": report.get("http_error_rate"),
        "perf.task_success_rate": report.get("task_success_rate"),
        "perf.p95_ms": report.get("p95_ms"),
        "perf.rps": report.get("rps"),
        "perf.tokens_per_request": report.get("tokens_per_request"),
        "perf.cost_per_success_cny": report.get("cost_per_success_cny"),
    }
    return {
        "name": "load_test",
        "status": "passed" if result["exit_code"] == 0 else "failed",
        "exit_code": result["exit_code"],
        "seconds": result["seconds"],
        "artifact": "load-test.json" if report else None,
        "metrics": metrics,
        "detail": (
            f"{report.get('arm', '?')} 臂 / P95 {report.get('p95_ms', '?')}ms"
            + ("" if repeat <= 1 else f"（{repeat} 次的中位数）")
        ),
        "provenance": report.get("provenance"),
        #: ``--repeat > 1`` 时的噪声（每次运行的极差与相对极差）。它回答的是
        #: 「这次测的指标到底有多准」——没有它就说不清某个漂移是真退化还是抖动。
        "repeat": report.get("repeat", 1),
        "variance": report.get("variance"),
    }


def collect_metrics(checks: Sequence[dict[str, Any]]) -> dict[str, float | None]:
    """把各检查的指标拍平成一张表。``None`` 表示「这次没测出来」，不是 0。"""
    metrics: dict[str, float | None] = {}
    for check in checks:
        for name, value in (check.get("metrics") or {}).items():
            metrics[name] = None if value is None else float(value)
    return dict(sorted(metrics.items()))


def render_markdown(index: dict[str, Any]) -> str:
    lines = [
        f"# 发布冻结：{index['tag']}",
        "",
        f"- 代码版本：`{index.get('commit') or '?'}`（`{index.get('describe') or '?'}`）",
        f"- 行为规格：v{index.get('spec_version') or '?'}",
        f"- 冻结时间：{index['frozen_at']}",
        f"- 工作区：{'**脏**（指标锚不住版本，谨慎引用）' if index.get('dirty') else '干净'}",
        "",
        "## 检查",
        "",
        "| 检查 | 结论 | 耗时 (s) | 说明 |",
        "| --- | --- | --- | --- |",
    ]
    for check in index["checks"]:
        mark = {"passed": "通过", "failed": "**失败**", "skipped": "跳过"}.get(
            str(check["status"]), str(check["status"])
        )
        lines.append(
            f"| `{check['name']}` | {mark} | {check['seconds']} | {check['detail']} |"
        )
    lines += ["", "## 指标", "", "| 指标 | 值 |", "| --- | --- |"]
    for name, value in index["metrics"].items():
        lines.append(f"| `{name}` | {'n/a' if value is None else value} |")
    lines += [
        "",
        "## 怎么用",
        "",
        "这份目录是**回归门禁的比较对象**：",
        "",
        "```",
        "python scripts/regression_gate.py --candidate <候选目录> --baseline "
        f"{index['tag']}",
        "```",
        "",
        "`release.json` 是机器读的，本文件是人读的。原始报告在同目录下。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结一次发布：跑齐离线检查并落盘指标索引")
    parser.add_argument("--out", type=Path, default=None, help="输出目录；默认 releases/<tag>")
    parser.add_argument("--tag", default=None, help="版本号；默认取 git describe --tags")
    parser.add_argument("--allow-dirty", action="store_true", help="放行脏工作区（会记进索引）")
    parser.add_argument("--gate-against", type=Path, action="append", default=[],
                        help="跑完顺手做回归门禁；可给多个基线")
    parser.add_argument("--skip-load", action="store_true", help="跳过并发压测（慢，约 1 分钟起）")
    parser.add_argument("--skip-trace", action="store_true", help="跳过 trace 生成（离线，但会比对比检查慢）")
    parser.add_argument("--skip-judge", action="store_true", help="跳过三层判分报告归档")
    parser.add_argument("--judge-l3", type=Path, default=REPO_ROOT / "reports" / "l3-ragas.json")
    parser.add_argument("--judge-l4", type=Path, default=REPO_ROOT / "reports" / "l4-deepeval.json")
    parser.add_argument("--judge-l5", type=Path, default=REPO_ROOT / "reports" / "l5-promptfoo.json")
    parser.add_argument("--repeat", type=int, default=1,
                        help="压测重复次数（量方差；每次重启服务，耗时按倍数增长）")
    parser.add_argument("--load-users", type=int, default=20, help="压测虚拟用户数")
    parser.add_argument("--load-run-time", default="20s", help="压测时长")
    parser.add_argument("--load-port", type=int, default=8140, help="压测用的服务端口")
    args = parser.parse_args(argv)

    tag = args.tag or _git("describe", "--tags") or "unknown"
    if "/" in tag or "\\" in tag:
        print(f"版本号不能含路径分隔符：{tag!r}")
        return 2

    dirty = is_dirty()
    if dirty and not args.allow_dirty:
        print(
            "工作区是脏的（有未提交改动），拒绝冻结：指标锚不住版本号，之后的对比都没意义。\n"
            "先提交，或用 --allow-dirty 放行（dirty=true 会写进索引，门禁报告里看得见）。"
        )
        return 2

    out = args.out or (RELEASES_DIR / tag)
    print(f"开始冻结 {tag} → {out}（工作区{'脏，已放行' if dirty else '干净'}）")

    with tempfile.TemporaryDirectory(prefix="agentevalops-freeze-") as raw_tmp:
        tmp = Path(raw_tmp)
        checks: list[dict[str, Any]] = [check_pytest(tmp)]
        checks.append(check_faults(tmp))
        checks.append(check_comparison("matrix", [CASE_MATRIX], tmp, "comparison-matrix.json"))
        checks.append(
            check_comparison("rag_multiturn", [RAG_CASES, MULTITURN_CASES], tmp,
                             "comparison-rag-multiturn.json")
        )
        if args.skip_trace:
            checks.append({
                "name": "trace", "status": "skipped", "exit_code": None,
                "seconds": 0.0, "artifact": None, "metrics": {},
                "detail": "本次跳过了 trace 生成（--skip-trace）",
            })
        else:
            checks.append(check_trace(tmp))

        if args.skip_judge:
            checks.append({
                "name": "judge", "status": "skipped", "exit_code": None,
                "seconds": 0.0, "artifact": None, "metrics": {},
                "detail": "本次跳过了三层判分报告归档（--skip-judge）",
            })
        else:
            checks.append(
                check_judge(
                    tmp,
                    l3=args.judge_l3,
                    l4=args.judge_l4,
                    l5=args.judge_l5,
                )
            )

        if args.skip_load:
            # 记一条 skipped，而不是干脆不出现：门禁要靠它区分「没跑」与「没数」。
            print("跳过并发压测（--skip-load）")
            checks.append({
                "name": "load_test", "status": "skipped", "exit_code": None,
                "seconds": 0.0, "artifact": None, "metrics": {},
                "detail": "本次跳过了并发压测（--skip-load）",
            })
        else:
            checks.append(
                check_load(tmp, port=args.load_port, users=args.load_users,
                           run_time=args.load_run_time, repeat=args.repeat)
            )

        marks = {"passed": "通过", "skipped": "跳过", "failed": "失败"}
        for check in checks:
            mark = marks.get(str(check["status"]), str(check["status"]))
            print(f"  [{mark}] {check['name']:<28} {check['seconds']:>7.1f}s  {check['detail']}")

        index = {
            "kind": "release_index",
            "tag": tag,
            "commit": _git("rev-parse", "--short", "HEAD"),
            "describe": _git("describe", "--tags"),
            "spec_version": spec_version(),
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "dirty": dirty,
            "checks": checks,
            "metrics": collect_metrics(checks),
            # 每个检查自己量出来的噪声。门禁拿它来判断「这个漂移值不值得追」。
            "variance": {
                check["name"]: check["variance"]
                for check in checks
                if check.get("variance")
            },
        }
        (tmp / INDEX_NAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp / "README.md").write_text(render_markdown(index), encoding="utf-8")

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            shutil.rmtree(out)
        shutil.move(str(tmp), str(out))

    # 只有 "failed" 才算失败：被跳过的检查（--skip-load）不该让冻结以非 0 退出。
    failed = [c for c in checks if c["status"] == "failed"]
    print(f"已冻结到 {out}")
    if args.gate_against:
        import regression_gate

        try:
            candidate = regression_gate.load_release(out)
            baselines = [regression_gate.load_release(path) for path in args.gate_against]
        except regression_gate.GateUsageError as exc:
            # 基线不存在是环境问题（还没冻结过基线？），不是质量结论——所以是 2 不是 1。
            # 不加这一层，用户看到的会是一个 traceback，退出码还带着 1 的误导。
            print(f"门禁起不来：{exc}")
            print("提示：基线要用 scripts/freeze_release.py 先冻结一次，或改 --gate-against 的路径。")
            return 2
        summary = regression_gate.run_gate(candidate, baselines)
        regression_gate.print_report(summary, candidate, baselines)
        if not summary["passed"]:
            print("回归门禁未通过。")
            return 1
    if failed:
        print("有检查未通过：" + "、".join(c["name"] for c in failed))
        return 1
    print("全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())