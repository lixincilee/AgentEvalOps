"""Run the case set through v1 (rules) and v2 (model) and report the difference.

Usage:
    python scripts/compare_planners.py --offline            # 零成本自检：v2 必须与 v1 完全一致
    python scripts/compare_planners.py --limit 20           # 真实模型，先跑 20 条探路
    python scripts/compare_planners.py --save reports/v1-vs-v2.json

Every metric is computed against the case set's ``expected_*`` fields, so the report
answers "did v2 get closer to the specification", not "did v2 change".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentevalops.agent import EcommerceSupportAgent, pre_plan_rejection  # noqa: E402
from agentevalops.config import Settings, load_settings  # noqa: E402
from agentevalops.llm import (  # noqa: E402
    UsageLedger,
    build_client,
    build_pricing,
    resolve_price,
)
from agentevalops.models import AgentState  # noqa: E402
from agentevalops.planner import (  # noqa: E402
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    LLMPlanner,
    RulePlanner,
)
from agentevalops.versioning import (  # noqa: E402
    build_manifest,
    git_code_version,
    hash_cases,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = [REPO_ROOT / "eval_cases" / "support_cases.jsonl"]
# A case rejected before planning never reaches the model, so its outcome is not a
# planner source. Labelling it keeps it visible instead of inflating the "llm" count.
SOURCE_NOT_PLANNED = "not-planned"

# metric name -> (row key produced by run_case, Chinese label for the report)
METRICS = {
    "intent_accuracy": ("intent_ok", "意图准确率"),
    "state_accuracy": ("state_ok", "状态准确率"),
    "tool_accuracy": ("tool_ok", "工具选择准确率"),
    "argument_accuracy": ("argument_ok", "参数准确率"),
    "task_success_rate": ("task_success", "任务成功率"),
}


class RunSetupError(RuntimeError):
    """The run cannot start at all: bad credentials, or a budget with no price table."""


def load_cases(paths: list[Path]) -> list[dict]:
    cases: list[dict] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append(json.loads(line))
    return cases


def run_case(agent: EcommerceSupportAgent, case: dict) -> dict:
    response = agent.handle(case["message"], customer_id=case["customer_id"])
    error = response.audit.error
    expected_arguments = case.get("expected_arguments") or {}
    actual_arguments = response.audit.arguments or {}
    return {
        "intent": response.intent.value,
        "state": response.state.value,
        "tool_name": response.audit.tool_name,
        "error_code": None if error is None else error.get("code"),
        "arguments": actual_arguments,
        "intent_ok": response.intent.value == case["expected_intent"],
        "state_ok": response.state.value == case["expected_state"],
        "tool_ok": response.audit.tool_name == case["expected_tool"],
        "argument_ok": all(
            actual_arguments.get(key) == value for key, value in expected_arguments.items()
        ),
        "task_success": response.state.value == "tool_completed",
    }


def rate(rows: list[dict], key: str) -> float:
    return 0.0 if not rows else sum(row[key] for row in rows) / len(rows)


def planned_cases(cases: list[dict]) -> list[dict]:
    """Filter to the cases that actually reach the planner.

    The matrix deliberately includes malformed requests (blank message, blank customer
    id). Those are answered by request validation, so they have no plan and make no model
    call: 165 such cases legitimately produce 159 calls.
    """
    return [
        case
        for case in cases
        if pre_plan_rejection(case["message"], case["customer_id"]) is None
    ]


def offline_scripts(cases: list[dict]) -> list[str]:
    """Replay v1's own decisions so the harness itself can be verified at zero cost.

    The scripted client answers in strict call order, so only cases that reach the planner
    may be answered here. Handing a canned answer to a case that never calls the model
    shifts every later answer by one -- the desync that makes an offline run report dozens
    of imaginary v1/v2 differences.
    """
    planner = RulePlanner()
    scripts: list[str] = []
    for case in planned_cases(cases):
        plan = planner.plan(case["message"])
        scripts.append(
            json.dumps(
                {"intent": plan.intent.value, "order_id": plan.order_id},
                ensure_ascii=False,
            )
        )
    return scripts


def run_comparison(
    cases: list[dict],
    *,
    offline: bool = False,
    settings: Settings | None = None,
    budget: float | None = None,
    period: str | None = None,
    case_set_version: str = "",
    repo_root: Path = REPO_ROOT,
) -> dict:
    """Run both planners over ``cases`` and return the complete report.

    Pure with respect to the filesystem, so tests can run the whole harness in-process and
    assert on the numbers instead of on printed text.
    """
    if not cases:
        raise RunSetupError("没有用例可跑。")
    resolved = settings if settings is not None else load_settings(env_file=repo_root / ".env")
    if not offline and not resolved.llm_configured:
        raise RunSetupError(
            "未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env，或加 --offline 用脚本化模型。"
        )

    effective_budget = budget if budget is not None else resolved.budget
    ledger = UsageLedger(
        limit=effective_budget,
        pricing=build_pricing(resolved.model_pricing()),
        currency=resolved.currency,
        off_peak=None if period is None else period == "off_peak",
    )
    if effective_budget is not None and resolve_price(resolved.model, ledger.pricing) is None:
        raise RunSetupError(
            f"无法启用预算熔断：模型 {resolved.model!r} 没有价目，"
            "未计价的花费会让上限形同虚设。请在 .env 里用 AGENTEVALOPS_PRICING_JSON "
            "声明单价，或去掉 --budget 并接受成本只算下限。"
        )

    client = build_client(
        resolved,
        offline=offline,
        scripts=offline_scripts(cases) if offline else (),
        ledger=ledger,
    )

    store = EcommerceSupportAgent().store
    v1_agent = EcommerceSupportAgent(store, planner=RulePlanner())
    v2_planner = LLMPlanner(client)
    v2_agent = EcommerceSupportAgent(store, planner=v2_planner)

    rows: list[dict] = []
    latencies: list[float] = []
    sources: dict[str, int] = {}
    for case in cases:
        v1 = run_case(v1_agent, case)
        v2 = run_case(v2_agent, case)
        plan = v2_agent.last_plan
        planned = plan is not None
        source = SOURCE_NOT_PLANNED if plan is None else plan.source
        sources[source] = sources.get(source, 0) + 1
        # ``last_response`` describes the planner's most recent *call*. Reading it after a
        # short circuit would report the previous case's latency for a case that called
        # nothing, so it is only consulted when this case really was planned.
        response = v2_planner.last_response if planned else None
        latency = None if response is None else response.latency_ms
        if latency is not None:
            latencies.append(latency)
        rows.append(
            {
                "case_id": case["case_id"],
                "category": case.get("category"),
                "message": case["message"],
                "expected": {
                    "intent": case["expected_intent"],
                    "state": case["expected_state"],
                    "tool_name": case["expected_tool"],
                    "error_code": case.get("expected_error_code"),
                },
                "v1": v1,
                "v2": v2,
                "planned": planned,
                "v2_source": source,
                "v2_note": None if plan is None else plan.note,
                "v2_latency_ms": latency,
                "changed": (
                    v1["intent"],
                    v1["state"],
                    v1["tool_name"],
                    v1["error_code"],
                )
                != (
                    v2["intent"],
                    v2["state"],
                    v2["tool_name"],
                    v2["error_code"],
                ),
            }
        )

    v2_successes = sum(1 for row in rows if row["v2"]["task_success"])
    planned_count = sum(1 for row in rows if row["planned"])
    invariants = {
        # A recorded call is billed either at peak or off peak, never neither.
        "peak_plus_off_peak_equals_model_calls": (
            ledger.calls == ledger.peak_calls + ledger.off_peak_calls
        ),
        # Only a planned case can call the model. A request that times out records nothing,
        # so fewer is possible; more would mean an unaccounted call.
        "model_calls_do_not_exceed_planned_cases": ledger.calls <= planned_count,
        # A short circuit is malformed-request validation by construction.
        "short_circuits_are_validation_failures": all(
            row["v2"]["state"] == AgentState.VALIDATION_FAILED.value
            for row in rows
            if not row["planned"]
        ),
    }

    summary = {
        "cases": len(rows),
        "v1": {
            name: rate([row["v1"] for row in rows], key) for name, (key, _) in METRICS.items()
        },
        "v2": {
            name: rate([row["v2"] for row in rows], key) for name, (key, _) in METRICS.items()
        },
        "source_breakdown": sources,
        "planned_cases": planned_count,
        "short_circuited_cases": len(rows) - planned_count,
        "model_calls": ledger.calls,
        "not_planned_cases": [row["case_id"] for row in rows if not row["planned"]],
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "latency_samples": len(latencies),
        "cost": ledger.cost,
        "currency": ledger.currency,
        "peak_calls": ledger.peak_calls,
        "off_peak_calls": ledger.off_peak_calls,
        "v2_successes": v2_successes,
        "cost_per_success": (ledger.cost / v2_successes) if v2_successes else None,
        "cost_is_complete": ledger.cost_is_complete,
        "unpriced_models": sorted(ledger.unpriced_models),
        "tokens": {
            "prompt": ledger.prompt_tokens,
            "completion": ledger.completion_tokens,
        },
        "changed_cases": [row["case_id"] for row in rows if row["changed"]],
        "regressions": [
            row["case_id"]
            for row in rows
            if not row["v2"]["state_ok"] and row["v1"]["state_ok"]
        ],
        "invariants": invariants,
    }
    summary["deltas"] = {name: summary["v2"][name] - summary["v1"][name] for name in METRICS}

    manifest = build_manifest(
        model_id="rules-v1" if offline else resolved.model,
        prompt_text=SYSTEM_PROMPT,
        cases=cases,
        prompt_version=PROMPT_VERSION,
        case_set_version=case_set_version,
        temperature=resolved.temperature,
        repo_dir=repo_root,
        # The baseline and the candidate are both part of the run's identity: without these
        # two fields a report cannot be reproduced from its own manifest.
        extra={
            "baseline_planner": v1_agent.planner.name,
            "candidate_planner": v2_planner.name,
        },
    )

    return {
        "kind": "planner_comparison",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline" if offline else "real",
        "endpoint": None if offline else resolved.base_url,
        "case_set_hash": hash_cases(cases),
        "manifest": manifest.to_dict(),
        "summary": summary,
        "cases": rows,
    }


def print_report(report: dict) -> None:
    summary = report["summary"]
    currency = summary["currency"]
    print(f"用例数: {summary['cases']}（模式: {report['mode']}）")
    print("| 指标 | v1 规则 | v2 模型 | 差 |")
    print("| --- | --- | --- | --- |")
    for name, (_, label) in METRICS.items():
        print(
            f"| {label} | {summary['v1'][name]:.1%} | {summary['v2'][name]:.1%} "
            f"| {summary['deltas'][name]:+.1%} |"
        )
    print(
        f"| 平均延迟 | — | {summary['avg_latency_ms']:.0f} ms"
        f"（{summary['latency_samples']} 次调用样本） | — |"
    )
    print(f"| 花费 | {currency} 0 | {currency} {summary['cost']:.6f} | — |")
    if summary["cost_per_success"] is not None:
        print(
            f"| 单次成功任务成本 | — | {currency} {summary['cost_per_success']:.6f} | — |"
        )
    print(
        f"模型调用: {summary['model_calls']} 次"
        f"（其中 {summary['short_circuited_cases']} 条用例在规划前被拒绝，未产生调用）"
    )
    print(
        f"计费时段: 高峰 {summary['peak_calls']} 次 / 空闲 {summary['off_peak_calls']} 次"
    )
    print("规划来源:", summary["source_breakdown"])
    if summary["changed_cases"]:
        print(
            f"行为发生变化的用例（{len(summary['changed_cases'])}）:",
            ", ".join(summary["changed_cases"]),
        )
    if not summary["cost_is_complete"]:
        print("注意: 未定价模型", summary["unpriced_models"], "→ 成本只算下限")
    broken = [name for name, ok in summary["invariants"].items() if not ok]
    if broken:
        print("警告: 报告不变式被破坏 ->", ", ".join(broken))


def main() -> int:
    parser = argparse.ArgumentParser(description="v1 规则版 vs v2 模型版 规划层对比")
    parser.add_argument("--offline", action="store_true", help="用脚本化模型，零成本自检")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（控成本）")
    parser.add_argument("--cases", type=Path, nargs="+", default=DEFAULT_CASES)
    parser.add_argument("--save", type=Path, default=None, help="把完整报告写成 JSON")
    parser.add_argument("--budget", type=float, default=None, help="本次运行花费上限")
    parser.add_argument(
        "--period",
        choices=("peak", "off_peak"),
        default=None,
        help="把计费时段钉死（默认按调用时刻的北京时间自动判定）",
    )
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        print("没有用例可跑。")
        return 2

    # A dirty or missing revision makes the report unreproducible: the numbers would be
    # attached to a code state that nobody can check out again. Say so before spending money.
    code_version = git_code_version(REPO_ROOT)
    if code_version == "nogit":
        print("警告: 当前目录不是 git 仓库，报告里的 code_version=nogit、is_reproducible=false。")
    elif code_version.endswith("-dirty"):
        print(
            f"警告: 工作区有未提交改动（code_version={code_version}），"
            "本次结果 is_reproducible=false，不能作为版本对比的锚点。"
        )

    try:
        report = run_comparison(
            cases,
            offline=args.offline,
            budget=args.budget,
            period=args.period,
            case_set_version=", ".join(path.name for path in args.cases),
        )
    except RunSetupError as exc:
        print(exc)
        return 2

    print_report(report)

    if args.save is not None:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("报告已写入:", args.save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())