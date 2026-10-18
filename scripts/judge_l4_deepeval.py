r"""L4：DeepEval GEval 给「客服话术」打整体分。

    D:\Python\python.exe scripts/judge_l4_deepeval.py --limit 12 --yes --budget 0.2

判据二选一（见 ``judging/deepeval_layer.py``）：有检索依据判 ``grounded_answer``，
没依据判 ``refusal_quality``。每一行都带 GEval 的评分理由。

退出码同 L3：0 全过 / 1 有失败或错误 / 2 用法或环境。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# DeepEval 默认会发匿名遥测；评测机器不该因为跑测试往外发东西。
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("ERROR_REPORTING", "false")

from agentevalops.config import Settings, load_settings  # noqa: E402
from agentevalops.judging.base import JudgeReport, utc_now  # noqa: E402
from agentevalops.judging.collect import collect_rag_samples, compose_answer, load_cases  # noqa: E402
from agentevalops.judging.llm import (  # noqa: E402
    JudgeUnavailableError,
    build_ledger,
    build_metered_client,
)
from agentevalops.llm import resolve_price  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "eval_cases" / "rag_cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="L4：DeepEval GEval（LLM-as-a-Judge）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=0, help="最多评多少条（0 = 全部）")
    parser.add_argument("--model", default=None)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "l4-deepeval.md")
    parser.add_argument("--json", type=Path, default=REPO_ROOT / "reports" / "l4-deepeval.json")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = settings if settings is not None else load_settings(env_file=REPO_ROOT / ".env")
    if not args.yes:
        print("L4 会真实调用模型：确认后加 --yes。已停在发请求之前。", file=sys.stderr)
        return 2
    if not resolved.llm_configured:
        print("未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env。", file=sys.stderr)
        return 2
    model = args.model or resolved.model
    budget = args.budget if args.budget is not None else resolved.budget
    ledger = build_ledger(resolved, budget)
    if budget is not None and resolve_price(model, ledger.pricing) is None:
        print(f"无法启用预算熔断：模型 {model!r} 没有价目。", file=sys.stderr)
        return 2

    try:
        cases = load_cases(args.cases)
        if args.limit:
            cases = cases[: args.limit]
        client = build_metered_client(resolved, ledger)
        samples = collect_rag_samples(
            cases,
            composer=lambda question, contexts: compose_answer(
                question, contexts, client, ledger=ledger
            ),
        )
        from agentevalops.judging.deepeval_layer import build_model, evaluate_samples

        judge = build_model(client, name=model)
        rows = evaluate_samples(samples, model=judge, ledger=ledger, judge_model=model)
    except JudgeUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report = JudgeReport(
        layer="L4",
        judge_model=model,
        rows=rows,
        generated_at=utc_now(),
        case_set=str(args.cases.name),
        notes=[
            "GEval 的评分步骤写死在 deepeval_layer.CRITERIA 里，同一条回答在版本之间可比。",
            "有检索依据 → grounded_answer；没有依据 → refusal_quality。",
            "回答由 answer-v1 现场合成（v3 的 policy_qa 不生成客户话术）。",
            "这是 LLM 打分，不是人工标注：绝对分数不可当真理，趋势与相对差异才有意义。",
        ],
    )
    _write(report, args.out, args.json)
    if not args.quiet:
        print(f"用例 {len(samples)} 条 → {len(rows)} 个判分行")
        print(f"通过率  : {report.pass_rate:.1%}")
        print(f"花费    : CNY {ledger.cost:.4f}（调用 {ledger.calls} 次）")
        print(f"报告    : {args.out}")
    return 0 if report.pass_rate == 1.0 and not report.errored_rows else 1


def _write(report: JudgeReport, out: Path, json_path: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_markdown(), encoding="utf-8")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
