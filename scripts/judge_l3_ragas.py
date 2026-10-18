r"""L3：用 Ragas 给「检索 → 客服话术」打分。

    D:\Python\python.exe scripts/judge_l3_ragas.py --limit 12 --yes --budget 0.2

产物：``reports/l3-ragas.md`` + ``.json``（后者给门禁/版本对比读）。

为什么这里要花两次钱：一次是**合成回答**（把检索证据写成客户话术，v3 目前不生成），
一次是 **Ragas 判分**（拆陈述、逐条核对）。报告会把两笔分开记。

退出码：``0`` 全部通过 / ``1`` 有指标没过阈值或框架报错 / ``2`` 用法或环境问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

from agentevalops.config import Settings, load_settings  # noqa: E402
from agentevalops.judging.base import DEFAULT_THRESHOLDS, JudgeReport, utc_now  # noqa: E402
from agentevalops.judging.collect import collect_rag_samples, compose_answer, load_cases  # noqa: E402
from agentevalops.judging.llm import (  # noqa: E402
    JudgeUnavailableError,
    build_async_openai,
    build_ledger,
    build_metered_client,
)
from agentevalops.llm import resolve_price  # noqa: E402

DEFAULT_CASES = REPO_ROOT / "eval_cases" / "rag_cases.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="L3：Ragas 忠实性 + 上下文精度")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--limit", type=int, default=0, help="最多评多少条（0 = 全部）")
    parser.add_argument("--model", default=None, help="判分模型（默认取 settings.model）")
    parser.add_argument("--budget", type=float, default=None, help="花费熔断上限（元）")
    parser.add_argument("--yes", action="store_true", help="确认真实调用（会花钱）")
    parser.add_argument("--no-compose", action="store_true",
                        help="不合成回答（直接评 v3 的 message，预期无意义，仅用于对照）")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "l3-ragas.md")
    parser.add_argument("--json", type=Path, default=REPO_ROOT / "reports" / "l3-ragas.json")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, settings: Settings | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved = settings if settings is not None else load_settings(env_file=REPO_ROOT / ".env")
    if not args.yes:
        print("L3 会真实调用模型（合成回答 + 判分）：确认后加 --yes。已停在发请求之前。", file=sys.stderr)
        return 2
    if not resolved.llm_configured:
        print("未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env。", file=sys.stderr)
        return 2

    model = args.model or resolved.model
    budget = args.budget if args.budget is not None else resolved.budget
    ledger = build_ledger(resolved, budget)
    if budget is not None and resolve_price(model, ledger.pricing) is None:
        print(
            f"无法启用预算熔断：模型 {model!r} 没有价目，未计价的花费会让上限形同虚设。",
            file=sys.stderr,
        )
        return 2

    try:
        cases = load_cases(args.cases)
        if args.limit:
            cases = cases[: args.limit]
        composer_client = None if args.no_compose else build_metered_client(resolved, ledger)
        samples = collect_rag_samples(
            cases,
            composer=(
                None
                if composer_client is None
                else (lambda question, contexts: compose_answer(
                    question, contexts, composer_client, ledger=ledger
                ))
            ),
        )
        scored = [sample for sample in samples if sample.contexts]
        skipped = len(samples) - len(scored)
        judge_client = build_async_openai(resolved, ledger)
        from agentevalops.judging.ragas_layer import DEFAULT_METRICS, build_llm, evaluate_samples

        llm = build_llm(model, judge_client)
        rows = evaluate_samples(scored, llm=llm, metrics=DEFAULT_METRICS, ledger=ledger, judge_model=model)
    except JudgeUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    notes = [
        "faithfulness / context_precision 都由 LLM 判分，不是人工标注。",
        "context_recall / answer_relevancy 需要 embedding，DeepSeek 不提供，因此**未启用**。",
        (
            "回答由 judging/collect.py 的 answer-v1 prompt 现场合成：v3 的 policy_qa 只返回"
            "结构化证据（message=request completed），不生成客户话术。"
            if not args.no_compose
            else "本次用 --no-compose：评的是 v3 原始的 message（对照用，不构成质量结论）。"
        ),
        "没有检索依据的用例（refuse / 空检索）被跳过：没有上下文就谈不上忠实性，计入 skipped。",
    ]
    report = JudgeReport(
        layer="L3",
        judge_model=model,
        rows=rows,
        skipped=skipped,
        generated_at=utc_now(),
        case_set=str(args.cases.name),
        notes=notes,
    )
    _write(report, args.out, args.json)
    if not args.quiet:
        print(f"用例 {len(scored)} 条（跳过 {skipped} 条无依据样本）→ {len(rows)} 个指标行")
        print(f"通过率  : {report.pass_rate:.1%}（所有指标都过阈值才算通过）")
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
