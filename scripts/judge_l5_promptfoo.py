r"""L5：Promptfoo 红队（提示注入 / 越权 / 泄露）。

    D:\Python\python.exe scripts/judge_l5_promptfoo.py

不花钱（断言是确定性的），但需要 Node：优先使用仓库内 ``.promptfoo/`` 里固定版本的
promptfoo（``npm install --no-save --prefix .promptfoo promptfoo@0.121.3``），没有才回退
到 ``npx``。配置是**每次生成**的（落在 reports/，不入库）：用例改了，config 跟着改，
不会出现「YAML 里还留着上个月的 40 条、JSONL 里已经是 46 条」这种静默漂移。

退出码：0 全过 / 1 有失败 / 2 用法或环境（没有 node、promptfoo 跑不起来）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from agentevalops.judging.base import JudgeReport, utc_now  # noqa: E402
from agentevalops.judging.promptfoo_layer import (  # noqa: E402
    PROMPTFOO_VERSION,
    build_config,
    collect_safety_cases,
    parse_results,
)

DEFAULT_CASES = REPO_ROOT / "eval_cases" / "case_matrix_v2.jsonl"
PROVIDER = REPO_ROOT / "scripts" / "promptfoo_provider.py"


def resolve_promptfoo(version: str) -> list[str] | None:
    """返回启动 promptfoo 的命令前缀；找不到 Node 时返回 None。

    本地安装优先：``npx`` 首次运行会顺带拉 Playwright 浏览器（L5 用不到浏览器），
    还会在管道里卡住。仓库内的固定版本可重放，也不污染全局 npm。
    """
    local = REPO_ROOT / ".promptfoo" / "node_modules" / ".bin" / "promptfoo.cmd"
    if not local.is_file():
        local = REPO_ROOT / ".promptfoo" / "node_modules" / ".bin" / "promptfoo"
    if local.is_file():
        return [str(local)]
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx is None:
        return None
    return [npx, "--yes", f"promptfoo@{version}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="L5：Promptfoo 红队（确定性断言）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--version", default=PROMPTFOO_VERSION)
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "reports" / "promptfoo.config.json")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "l5-promptfoo.md")
    parser.add_argument("--json", type=Path, default=REPO_ROOT / "reports" / "l5-promptfoo.json")
    parser.add_argument("--raw", type=Path, default=REPO_ROOT / "reports" / "promptfoo-raw.json")
    parser.add_argument("--no-run", action="store_true", help="只生成 config，不执行")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cases = collect_safety_cases(args.cases)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"{args.cases} 里没有 category=safety 的用例。", file=sys.stderr)
        return 2

    config = build_config(cases, python_exe=sys.executable, provider_script=PROVIDER)
    args.config.parent.mkdir(parents=True, exist_ok=True)
    # JSON 是合法 YAML；省掉一个 pyyaml 依赖，也保证 config 可被 json.load 直接读。
    args.config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.no_run:
        print(f"已生成 {args.config}（{len(cases)} 条用例），未执行。")
        return 0

    env = dict(os.environ)
    env.setdefault("PROMPTFOO_DISABLE_TELEMETRY", "1")
    env.setdefault("PROMPTFOO_DISABLE_UPDATE", "1")
    env.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
    launcher = resolve_promptfoo(args.version)
    if launcher is None:
        print("找不到 promptfoo：请装 Node.js（>=18），或按 README 本地安装。", file=sys.stderr)
        return 2
    command = [
        *launcher,
        "eval",
        "-c",
        str(args.config),
        "-o",
        str(args.raw),
        "--no-progress-bar",
    ]
    try:
        done = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except FileNotFoundError:
        print("找不到 promptfoo：请装 Node.js（>=18），或按 README 本地安装。", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("promptfoo 超时（30 分钟）。", file=sys.stderr)
        return 2

    if not args.raw.exists():
        print("promptfoo 没有产出报告：", file=sys.stderr)
        print((done.stderr or done.stdout or "")[-2000:], file=sys.stderr)
        return 2
    payload = json.loads(args.raw.read_text(encoding="utf-8"))
    rows = parse_results(payload)
    report = JudgeReport(
        layer="L5",
        judge_model=f"promptfoo@{args.version}",
        rows=rows,
        generated_at=utc_now(),
        case_set=str(args.cases.name),
        notes=[
            "断言是确定性的：状态 + 错误码逐字比对，加泄露标记 not-contains。",
            "provider 直接 import V3Agent，不启 HTTP 服务；一条用例一次真实 handle()。",
            f"promptfoo 固定 {args.version}；config 每次从 JSONL 重新生成。",
        ],
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report.to_markdown(), encoding="utf-8")
    args.json.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if not args.quiet:
        print(f"safety 用例 {len(cases)} 条 → 通过率 {report.pass_rate:.1%}")
        print(f"报告    : {args.out}")
        if report.pass_rate < 1.0:
            print("未通过的用例会列在报告的「未通过」表里。")
    return 0 if report.pass_rate == 1.0 and not report.errored_rows else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
