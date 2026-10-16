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

#: 一次检查最多给多久。压测要起服务、跑 locust，比其它几个都慢。
TIMEOUTS = {"pytest": 1800.0, "load": 600.0}

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


def check_load(tmp: Path, *, port: int, users: int, run_time: str) -> dict[str, Any]:
    out = tmp / "load-test.json"
    result = _run(
        [sys.executable, str(SCRIPTS_DIR / "load_test.py"), "--users", str(users),
         "--spawn-rate", str(max(1, users // 4)), "--run-time", run_time,
         "--port", str(port), "--report", str(out)],
        TIMEOUTS["load"],
    )
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
        "detail": f"{report.get('arm', '?')} 臂 / P95 {report.get('p95_ms', '?')}ms",
        "provenance": report.get("provenance"),
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
        mark = "通过" if check["status"] == "passed" else "**失败**"
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
        if args.skip_load:
            print("跳过并发压测（--skip-load）")
        else:
            checks.append(
                check_load(tmp, port=args.load_port, users=args.load_users,
                           run_time=args.load_run_time)
            )

        for check in checks:
            mark = "通过" if check["status"] == "passed" else "失败"
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
        }
        (tmp / INDEX_NAME).write_text(
            json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (tmp / "README.md").write_text(render_markdown(index), encoding="utf-8")

        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists():
            shutil.rmtree(out)
        shutil.move(str(tmp), str(out))

    failed = [c for c in checks if c["status"] != "passed"]
    print(f"已冻结到 {out}")
    if args.gate_against:
        import regression_gate

        candidate = regression_gate.load_release(out)
        baselines = [regression_gate.load_release(path) for path in args.gate_against]
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