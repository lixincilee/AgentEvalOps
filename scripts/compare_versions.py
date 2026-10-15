"""v1 / v2 / v3 三方对比：同一批用例、同一套判分、同一份版本清单。

Usage:
    python scripts/compare_versions.py --offline --limit 20
    python scripts/compare_versions.py --llm --yes --limit 20
    python scripts/compare_versions.py --out reports/v1-v2-v3.json

四列的定义在任何模式下都不变，免得「谁是谁」悄悄漂移：

    v1        RulePlanner + EcommerceSupportAgent            规则基线，永远可用
    v2        LLMPlanner（prompt v2）+ EcommerceSupportAgent  只有 --llm 可用
    v3        V3Agent + GroundedLLMPlanner（prompt v3）        只有 --llm 可用
    v3-rules  V3Agent + RulePlannerV3（v3 的规则臂）            永远可用

为什么把 v3 拆成两列：v3 的卖点是「检索 + 多轮 + 图编排」，这些在规则臂上也能跑（零成本），
而 prompt v3 与 policy_qa 只有模型臂才有。合成一列就答不了「v3 赢在编排还是赢在模型」；
拆开之后 v3-rules 对 v1 看编排，v3 对 v3-rules 看模型。

判分口径：passed 按 SPEC-JUDGE-1 的四项（intent / state / tool_name / error_code）全中才算过。
这比 compare_planners.py 的 task_success（只看 state 是否 tool_completed）严格：
被正确拒绝的越权请求在这里算「通过」，而不是「没成功」。
多轮用例集（§7.2）被拆成「一行 = 一轮」；用例级口径另见 report["versions"][列]["case_level"]。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentevalops.agent import EcommerceSupportAgent, pre_plan_rejection  # noqa: E402
from agentevalops.config import Settings, load_settings  # noqa: E402
from agentevalops.llm import (  # noqa: E402
    Usage,
    UsageLedger,
    approx_tokens,
    build_client,
    build_pricing,
    estimate_cost,
    resolve_price,
)
from agentevalops.models import AgentResponse, AgentState  # noqa: E402
from agentevalops.order_store import OrderStore  # noqa: E402
from agentevalops.pipeline import V3Agent  # noqa: E402
from agentevalops.planner import (  # noqa: E402
    PROMPT_VERSION,
    PROMPT_VERSION_V3,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_V3,
    GroundedLLMPlanner,
    LLMPlanner,
    RulePlanner,
    RulePlannerV3,
)
from agentevalops.retrieval import DEFAULT_MIN_COVERAGE, DEFAULT_TOP_K  # noqa: E402
from agentevalops.runs import CaseResult, RunMetrics, new_run_id  # noqa: E402
from agentevalops.spec import SPEC_VERSION  # noqa: E402
from agentevalops.versioning import (  # noqa: E402
    build_manifest,
    git_code_version,
    hash_cases,
    sha256_text,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = [REPO_ROOT / "eval_cases" / "case_matrix_v2.jsonl"]
REPORTS_DIR = REPO_ROOT / "reports"

#: 报告的列。summary / versions / deltas / rows 里出现的键必须与它一致。
COLUMNS = ("v1", "v2", "v3", "v3-rules")

COLUMN_LABELS = {
    "v1": "v1 规则基线（RulePlanner）",
    "v2": "v2 模型规划（LLMPlanner / prompt v2）",
    "v3": "v3 图编排 + 检索 + 多轮（GroundedLLMPlanner / prompt v3）",
    "v3-rules": "v3 图编排的规则臂（RulePlannerV3，零成本）",
}

#: 离线模式下两列各自不可用的原因。写进报告，而不是把它们从结果里删掉——
#: 静默省略会让人误以为「这个版本就是没跑」，而实际发生的是「这个版本没法零成本跑」。
OFFLINE_UNAVAILABLE = {
    "v2": "v2 是模型规划器（prompt v2），零成本跑不了：要跑请加 --llm --yes",
    "v3": "v3 的模型臂是 GroundedLLMPlanner（prompt v3），零成本跑不了：要跑请加 --llm --yes",
}

#: 与 compare_planners.py 同名同义：在规划前被拒绝的用例不是任何规划器的决定。
SOURCE_NOT_PLANNED = "not-planned"

#: 真实调用的输出长度上界，取 LLMPlanner 的默认 max_tokens，只用于打印预估花费。
OUTPUT_TOKEN_HINT = 128


class RunSetupError(RuntimeError):
    """本次运行根本起不来：用例集不合法、缺凭证，或者没确认就要真实调用。"""


def load_cases(paths: list[Path]) -> list[dict[str, Any]]:
    """读 JSONL 用例集，多轮用例集按轮拆行（每轮一行）。

    行模型是「一条消息 -> 一次 handle()」，三个版本的 handle 都吃一条消息；
    多轮用例靠同一个 session_id 串起来，所以拆行不会丢上下文。
    """
    units: list[dict[str, Any]] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            case = json.loads(line)
            turns = case.get("turns")
            if turns is None:
                if "message" not in case:
                    raise RunSetupError(
                        f"{path.name}:{line_number} 既没有 message 也没有 turns，认不出用例形状"
                    )
                units.append(
                    {
                        **case,
                        "source": path.name,
                        "turn": None,
                        "parent_case_id": case["case_id"],
                    }
                )
                continue
            for index, turn in enumerate(turns):
                units.append(
                    {
                        "case_id": f"{case['case_id']}#t{index}",
                        "parent_case_id": case["case_id"],
                        "source": path.name,
                        "category": case.get("category"),
                        "customer_id": case.get("customer_id"),
                        "session_id": case.get("session_id"),
                        "notes": case.get("notes"),
                        "turn": index,
                        **turn,
                    }
                )
    return units


class VersionRunner:
    """把三个版本的调用入口对齐成一个签名，并留下「这次是谁做的决定」。"""

    def __init__(
        self,
        key: str,
        *,
        agent: EcommerceSupportAgent | None = None,
        graph: V3Agent | None = None,
        ledger: UsageLedger | None = None,
    ) -> None:
        if (agent is None) == (graph is None):
            raise ValueError("VersionRunner 必须且只能给 agent 或 graph 一个")
        self.key = key
        self.agent = agent
        self.graph = graph
        self.ledger = ledger
        self.planner = graph.planner if graph is not None else agent.planner
        self.last_plan = None
        self.last_retrieval = None
        self.last_slot = None
        self.last_nodes: list[str] = []
        self.last_latency_ms: float | None = None

    @property
    def planner_name(self) -> str:
        return self.planner.name

    def invoke(
        self, message: str, *, customer_id: str, session_id: str | None = None
    ) -> AgentResponse:
        if self.graph is not None:
            response = self.graph.handle(
                message, customer_id=customer_id, session_id=session_id
            )
            self.last_plan = self.graph.last_plan
            self.last_retrieval = self.graph.last_retrieval
            self.last_slot = self.graph.last_slot
            self.last_nodes = list(self.graph.last_nodes)
        else:
            # v1 / v2 没有会话概念：多轮用例上它们只能看到当前这一句（这正是要比的差距）。
            response = self.agent.handle(message, customer_id=customer_id)
            self.last_plan = self.agent.last_plan
            self.last_retrieval = None
            self.last_slot = None
            self.last_nodes = []
        recorded = getattr(self.planner, "last_response", None)
        self.last_latency_ms = None if recorded is None else recorded.latency_ms
        return response


def _ledger_snapshot(ledger: UsageLedger | None) -> dict[str, Any]:
    """按行归因花费：某一行用例的账 = 跑它前后账本读数的差。"""
    if ledger is None:
        return {"calls": 0, "cost": 0.0, "total_tokens": 0}
    return {"calls": ledger.calls, "cost": ledger.cost, "total_tokens": ledger.total_tokens}


def _retrieval_ok(unit: dict[str, Any], retrieval: Any) -> bool | None:
    """RAG 用例的命中判定（L2 观测项，不进 passed）。

    passed 只认 SPEC-JUDGE-1 的四项，检索质量单独报，免得两种失败混成一个数字。
    """
    gold = unit.get("expected_gold_doc_ids")
    top = unit.get("expected_top_doc_id")
    if gold is None and top is None:
        return None
    if retrieval is None:
        return False
    actual = set(retrieval.doc_ids)
    ok = all(doc_id in actual for doc_id in (gold or ()))
    return ok and (top is None or retrieval.top_doc_id == top)


def run_unit(runner: VersionRunner, unit: dict[str, Any]) -> dict[str, Any]:
    """跑一行用例，并按 SPEC-JUDGE-1 判分。"""
    before = _ledger_snapshot(runner.ledger)
    response = runner.invoke(
        unit["message"],
        customer_id=unit["customer_id"],
        session_id=unit.get("session_id"),
    )
    after = _ledger_snapshot(runner.ledger)

    error = response.audit.error
    arguments = response.audit.arguments or {}
    error_code = None if error is None else error.get("code")
    expected_arguments = unit.get("expected_arguments") or {}
    slot = runner.last_slot
    retrieval = runner.last_retrieval
    expected_slot = unit.get("expected_slot_source")
    # 只有图编排才有会话槽位与检索证据。没有这条链路的列记 n/a，而不是记 0 分：
    # 「能力不存在」和「能力存在但没做到」是两种失败，混成一个数字就再也分不开了。
    has_graph = runner.graph is not None

    cell: dict[str, Any] = {
        "intent": response.intent.value,
        "state": response.state.value,
        "tool_name": response.audit.tool_name,
        "error_code": error_code,
        "arguments": arguments,
        "order_id": arguments.get("order_id"),
        "planner": runner.planner_name,
        "plan_source": None if runner.last_plan is None else runner.last_plan.source,
        "intent_ok": response.intent.value == unit["expected_intent"],
        "state_ok": response.state.value == unit["expected_state"],
        "tool_ok": response.audit.tool_name == unit["expected_tool"],
        "error_ok": error_code == unit.get("expected_error_code"),
        "arguments_ok": all(
            arguments.get(name) == value for name, value in expected_arguments.items()
        ),
        "order_ok": (
            None
            if unit.get("expected_order_id") is None
            else arguments.get("order_id") == unit["expected_order_id"]
        ),
        "slot_source": None if slot is None else slot.source,
        "slot_ok": (
            None
            if expected_slot is None or not has_graph
            else slot is not None and slot.source == expected_slot
        ),
        "retrieval": None if retrieval is None else retrieval.to_dict(),
        "retrieval_ok": _retrieval_ok(unit, retrieval) if has_graph else None,
        "nodes": runner.last_nodes,
        "latency_ms": runner.last_latency_ms,
        "model_calls": after["calls"] - before["calls"],
        "cost": after["cost"] - before["cost"],
        "total_tokens": after["total_tokens"] - before["total_tokens"],
    }
    cell["passed"] = bool(
        cell["intent_ok"] and cell["state_ok"] and cell["tool_ok"] and cell["error_ok"]
    )
    return cell


def _failures(cell: dict[str, Any]) -> tuple[str, ...]:
    names = {
        "intent": cell["intent_ok"],
        "state": cell["state_ok"],
        "tool_name": cell["tool_ok"],
        "error_code": cell["error_ok"],
    }
    return tuple(name for name, ok in names.items() if not ok)


def _fingerprint(cell: dict[str, Any] | None) -> tuple[str, ...] | None:
    if cell is None:
        return None
    return (cell["intent"], cell["state"], cell["tool_name"], cell["error_code"])


def _divergent_columns(cells: dict[str, Any]) -> list[str]:
    """哪些列的这一行与「多数列的结论」不同——直接回答「谁在哪条上不一样」。"""
    seen = {key: _fingerprint(cells[key]) for key in COLUMNS if cells[key] is not None}
    if len(seen) < 2:
        return []
    counts = Counter(seen.values())
    best = max(counts.values())
    reference = next(seen[key] for key in COLUMNS if key in seen and counts[seen[key]] == best)
    return [key for key in COLUMNS if key in seen and seen[key] != reference]


def _case_level(column: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """用例级口径（§7.2）：一条用例的所有轮次都过，这条用例才算过。"""
    grouped: dict[str, list[bool]] = {}
    for row in rows:
        if row[column] is None:
            continue
        grouped.setdefault(row["parent_case_id"], []).append(row[column]["passed"])
    passed = sum(1 for outcomes in grouped.values() if all(outcomes))
    return {
        "cases": len(grouped),
        "passed": passed,
        "pass_rate": (passed / len(grouped)) if grouped else 0.0,
    }


def build_runners(
    *,
    offline: bool,
    settings: Settings,
    clients: dict[str, Any] | None = None,
    budget: float | None = None,
    period: str | None = None,
) -> dict[str, VersionRunner | None]:
    """构造四个列。不可用的列返回 None，绝不拿规则臂冒充模型列。

    每个模型列一个独立账本，成本才能按列归因；否则 v2 的花费会算到 v3 头上。
    """
    pricing = build_pricing(settings.model_pricing())
    off_peak = None if period is None else period == "off_peak"
    providers = clients or {}

    def new_ledger() -> UsageLedger:
        return UsageLedger(
            limit=budget, pricing=pricing, currency=settings.currency, off_peak=off_peak
        )

    runners: dict[str, VersionRunner | None] = {
        "v1": VersionRunner("v1", agent=EcommerceSupportAgent(OrderStore(), planner=RulePlanner())),
        "v3-rules": VersionRunner("v3-rules", graph=V3Agent(OrderStore(), planner=RulePlannerV3())),
        "v2": None,
        "v3": None,
    }
    if offline:
        return runners

    v2_ledger = new_ledger()
    v3_ledger = new_ledger()
    v2_planner = LLMPlanner(providers.get("v2") or build_client(settings, ledger=v2_ledger))
    v3_planner = GroundedLLMPlanner(
        providers.get("v3") or build_client(settings, ledger=v3_ledger)
    )
    runners["v2"] = VersionRunner(
        "v2",
        agent=EcommerceSupportAgent(OrderStore(), planner=v2_planner),
        ledger=v2_ledger,
    )
    runners["v3"] = VersionRunner(
        "v3", graph=V3Agent(OrderStore(), planner=v3_planner), ledger=v3_ledger
    )
    return runners


def _column_payload(
    key: str, rows: list[dict[str, Any]], runner: VersionRunner
) -> dict[str, Any]:
    cells = [(row, row[key]) for row in rows if row[key] is not None]
    metrics = RunMetrics.from_results(
        CaseResult(
            case_id=row["case_id"],
            passed=cell["passed"],
            layer="L1",
            category=row["category"] or "normal",
            failures=_failures(cell),
            latency_ms=cell["latency_ms"] or 0.0,
            cost=cell["cost"],
            total_tokens=cell["total_tokens"],
        )
        for row, cell in cells
    )
    sources = Counter(
        SOURCE_NOT_PLANNED if not row["planned"] else (cell["plan_source"] or "unknown")
        for row, cell in cells
    )
    latencies = [cell["latency_ms"] for _, cell in cells if cell["latency_ms"] is not None]
    return {
        "label": COLUMN_LABELS[key],
        "planner": runner.planner_name,
        "available": True,
        "unavailable_reason": None,
        "metrics": metrics.to_dict(),
        "case_level": _case_level(key, rows),
        "failures_by_field": {
            field: sum(1 for _, cell in cells if not cell[f"{field}_ok"])
            for field in ("intent", "state", "tool", "error")
        },
        # 没有会话 / 检索链路的列记 None：这不是 0 分，而是「这一列没有这个环节」。
        "slot_source_failures": (
            sum(1 for _, cell in cells if cell["slot_ok"] is False)
            if runner.graph is not None
            else None
        ),
        "retrieval_failures": (
            sum(1 for _, cell in cells if cell["retrieval_ok"] is False)
            if runner.graph is not None
            else None
        ),
        "source_breakdown": dict(sorted(sources.items())),
        "model_calls": sum(cell["model_calls"] for _, cell in cells),
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "latency_samples": len(latencies),
        "unpriced_models": [] if runner.ledger is None else sorted(runner.ledger.unpriced_models),
    }


def _pairwise_deltas(
    versions: dict[str, dict[str, Any] | None]
) -> dict[str, dict[str, float] | None]:
    """两两差值；任一侧不可用就是 None——这是「显式缺失」，不是「省略」。

    键用冒号分隔而不是连字符：列名 v3-rules 自己带连字符，"v1-v3-rules" 没法可靠地切开，
    下游一旦按分割结果做门禁就会把 v3 和 v3-rules 弄混。差值里也再写一遍 left/right。
    """
    deltas: dict[str, dict[str, float] | None] = {}
    for index, first in enumerate(COLUMNS):
        for second in COLUMNS[index + 1 :]:
            left, right = versions[first], versions[second]
            deltas[f"{first}:{second}"] = (
                None
                if left is None or right is None
                else {
                    "left": first,
                    "right": second,
                    "pass_rate": right["metrics"]["pass_rate"] - left["metrics"]["pass_rate"],
                    "case_pass_rate": (
                        right["case_level"]["pass_rate"] - left["case_level"]["pass_rate"]
                    ),
                    "cost": right["metrics"]["cost"] - left["metrics"]["cost"],
                    "total_tokens": (
                        right["metrics"]["total_tokens"] - left["metrics"]["total_tokens"]
                    ),
                }
            )
    return deltas


def run_comparison(
    cases: list[dict[str, Any]],
    *,
    offline: bool = True,
    confirm: bool = False,
    settings: Settings | None = None,
    clients: dict[str, Any] | None = None,
    budget: float | None = None,
    period: str | None = None,
    case_set_version: str = "",
    repo_root: Path = REPO_ROOT,
    moment: str | None = None,
) -> dict[str, Any]:
    """跑完所有列并返回完整报告。

    除了读 .env，对文件系统是纯的：测试可以把整份报告跑在进程里断言数字，而不是断言打印文本。
    moment 同时钉死时钟和 run_id，让「同一批用例跑两次得到同一份报告」这件事可以被断言。
    """
    if not cases:
        raise RunSetupError("没有用例可跑。")
    if not offline and not confirm:
        raise RunSetupError("真实调用会花钱：确认后加 --yes，或改用 --offline（零成本，只跑规则列）。")
    resolved = settings if settings is not None else load_settings(env_file=repo_root / ".env")
    if not offline and not resolved.llm_configured:
        raise RunSetupError(
            "未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env，或加 --offline 只跑零成本的规则列。"
        )

    effective_budget = budget if budget is not None else resolved.budget
    pricing = build_pricing(resolved.model_pricing())
    if (
        not offline
        and effective_budget is not None
        and resolve_price(resolved.model, pricing) is None
    ):
        raise RunSetupError(
            f"无法启用预算熔断：模型 {resolved.model!r} 没有价目，"
            "未计价的花费会让上限形同虚设。"
        )

    runners = build_runners(
        offline=offline,
        settings=resolved,
        clients=clients,
        budget=effective_budget,
        period=period,
    )

    rows: list[dict[str, Any]] = []
    for unit in cases:
        cells: dict[str, Any] = {}
        for key in COLUMNS:
            runner = runners[key]
            cells[key] = None if runner is None else run_unit(runner, unit)
        rows.append(
            {
                "case_id": unit["case_id"],
                "parent_case_id": unit.get("parent_case_id", unit["case_id"]),
                "source": unit.get("source"),
                "turn": unit.get("turn"),
                "category": unit.get("category"),
                "message": unit["message"],
                "planned": pre_plan_rejection(unit["message"], unit["customer_id"]) is None,
                "expected": {
                    "intent": unit["expected_intent"],
                    "state": unit["expected_state"],
                    "tool_name": unit["expected_tool"],
                    "error_code": unit.get("expected_error_code"),
                    "order_id": unit.get("expected_order_id"),
                    "slot_source": unit.get("expected_slot_source"),
                    "gold_doc_ids": unit.get("expected_gold_doc_ids"),
                    "top_doc_id": unit.get("expected_top_doc_id"),
                },
                "divergent_columns": _divergent_columns(cells),
                **cells,
            }
        )

    versions = {
        key: None if runners[key] is None else _column_payload(key, rows, runners[key])
        for key in COLUMNS
    }
    unavailable = {key: OFFLINE_UNAVAILABLE[key] for key in COLUMNS if runners[key] is None}
    planned_rows = sum(1 for row in rows if row["planned"])

    manifest_extra = {
        f"column.{key}": runners[key].planner_name
        for key in COLUMNS
        if runners[key] is not None
    }
    # 两个 prompt 的哈希都进清单：v2 / v3 的差里既有 prompt 也有编排，别把它们混成一个字段。
    manifest_extra["prompt_v2_hash"] = sha256_text(SYSTEM_PROMPT)
    manifest_extra["prompt_v3_hash"] = sha256_text(SYSTEM_PROMPT_V3)
    manifest = build_manifest(
        model_id="rules-v1+rules-v3" if offline else resolved.model,
        prompt_text=SYSTEM_PROMPT_V3,
        cases=cases,
        prompt_version=PROMPT_VERSION_V3,
        case_set_version=case_set_version,
        temperature=resolved.temperature,
        retrieval_config={"top_k": DEFAULT_TOP_K, "min_coverage": DEFAULT_MIN_COVERAGE},
        repo_dir=repo_root,
        extra=manifest_extra,
    )

    invariants = {
        # 规则列一分钱都不该花：花了说明离线路径上偷偷调了模型。
        "offline_columns_are_zero_cost": all(
            versions[key] is None or versions[key]["metrics"]["cost"] == 0.0
            for key in ("v1", "v3-rules")
        ),
        # 不可用的列必须是 None，而不是消失：缺列和「跑了没结果」不能长得一样。
        "unavailable_columns_stay_empty": all(
            row[key] is None for row in rows for key in COLUMNS if runners[key] is None
        ),
        "model_calls_do_not_exceed_planned_rows": all(
            versions[key] is None or versions[key]["model_calls"] <= planned_rows
            for key in COLUMNS
        ),
        # 每个真跑过的列都要在清单里留下规划器名字，否则报告没法自证它比的是什么。
        "every_available_column_is_in_the_manifest": all(
            f"column.{key}" in manifest_extra for key in COLUMNS if runners[key] is not None
        ),
        # 规划前被拒的用例必然是校验失败，任何列都不例外。
        "short_circuits_are_validation_failures": all(
            row[key]["state"] == AgentState.VALIDATION_FAILED.value
            for row in rows
            for key in COLUMNS
            if not row["planned"] and row[key] is not None
        ),
    }

    return {
        "kind": "version_comparison",
        "spec_version": SPEC_VERSION,
        "prompts": {"v2": PROMPT_VERSION, "v3": PROMPT_VERSION_V3},
        "generated_at": moment or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": new_run_id(manifest, moment=moment),
        "mode": "offline" if offline else "real",
        "endpoint": None if offline else resolved.base_url,
        "case_set_version": case_set_version,
        "case_set_hash": hash_cases(cases),
        "manifest": manifest.to_dict(),
        "columns": list(COLUMNS),
        "versions": versions,
        "unavailable": unavailable,
        "deltas": _pairwise_deltas(versions),
        "summary": {
            "cases": len({row["parent_case_id"] for row in rows}),
            "rows": len(rows),
            "turn_rows": sum(1 for row in rows if row["turn"] is not None),
            "planned_rows": planned_rows,
            "short_circuited_rows": len(rows) - planned_rows,
            "sources": sorted({row["source"] for row in rows if row["source"]}),
            "divergent_rows": [row["case_id"] for row in rows if row["divergent_columns"]],
        },
        "invariants": invariants,
        "rows": rows,
    }


def print_report(report: dict[str, Any], *, max_divergent: int = 12) -> None:
    versions = report["versions"]
    summary = report["summary"]
    available = [versions[key] for key in COLUMNS if versions[key] is not None]
    currency = available[0]["metrics"]["currency"] if available else "CNY"

    print(
        f"用例: {summary['cases']} 条 / {summary['rows']} 行"
        f"（模式: {report['mode']}，规格: {report['spec_version']}）"
    )
    print("| 列 | 规划器 | 行级通过率 | 用例级通过率 | 花费 | tokens | 模型调用 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for key in COLUMNS:
        payload = versions[key]
        if payload is None:
            print(f"| {key} | 不可用 | n/a | n/a | n/a | n/a | n/a |")
            continue
        metrics = payload["metrics"]
        print(
            f"| {key} | {payload['planner']} | {metrics['pass_rate']:.1%} "
            f"| {payload['case_level']['pass_rate']:.1%} | {currency} {metrics['cost']:.6f} "
            f"| {metrics['total_tokens']} | {payload['model_calls']} |"
        )
    for key, reason in report["unavailable"].items():
        print(f"{key} 不可用: {reason}")
    for pair, delta in report["deltas"].items():
        if delta is None:
            print(f"{pair} 差值: n/a（该对比有列不可用）")
            continue
        print(
            f"{pair} 差值: 行级通过率 {delta['pass_rate']:+.1%}"
            f" / 用例级 {delta['case_pass_rate']:+.1%}"
            f" / 花费 {currency}{delta['cost']:+.6f} / tokens {delta['total_tokens']:+d}"
        )
    print(f"结论不一致的行: {len(summary['divergent_rows'])}")
    shown = 0
    for row in report["rows"]:
        if not row["divergent_columns"]:
            continue
        if shown >= max_divergent:
            print(f"  ...（另有 {len(summary['divergent_rows']) - shown} 行，见报告 JSON）")
            break
        shown += 1
        expected = row["expected"]
        print(
            f"  {row['case_id']} 分叉列={row['divergent_columns']} "
            f"期望={expected['intent']}/{expected['state']}"
            f"/{expected['tool_name']}/{expected['error_code']}"
        )
    for key in COLUMNS:
        payload = versions[key]
        if payload is None:
            continue
        misses = {name: count for name, count in payload["failures_by_field"].items() if count}
        if misses:
            print(f"{key} 未中项: {misses}")
        if payload["slot_source_failures"] is None:
            print(f"{key} 没有会话与检索链路：槽位来源、检索命中记 n/a（不是 0 分）")
        else:
            if payload["slot_source_failures"]:
                print(f"{key} 槽位来源不符: {payload['slot_source_failures']} 轮")
            if payload["retrieval_failures"]:
                print(f"{key} 检索未命中 gold: {payload['retrieval_failures']} 行")
    if not report["invariants"]["offline_columns_are_zero_cost"]:
        print("警告: 规则列产生了花费，离线路径上一定有人调了模型。")
    broken = [name for name, ok in report["invariants"].items() if not ok]
    if broken:
        print("警告: 报告不变式被破坏 ->", ", ".join(broken))


def estimate_run_cost(
    cases: list[dict[str, Any]],
    settings: Settings,
    *,
    period: str | None = None,
    model_columns: int = 2,
) -> float | None:
    """真实调用的花费上界估计：每条用例一次调用，输出按 max_tokens 顶格算。

    只用于打印提醒，不参与判分。真实回答是短 JSON，通常远低于这个数，所以把它当上界读。
    没有价目时返回 None——宁可说「不知道」，也不给一个假数字。
    """
    planned = [
        case
        for case in cases
        if pre_plan_rejection(case["message"], case["customer_id"]) is None
    ]
    if not planned:
        return 0.0
    usage = Usage(
        prompt_tokens=sum(
            approx_tokens(SYSTEM_PROMPT_V3) + approx_tokens(case["message"])
            for case in planned
        ),
        completion_tokens=OUTPUT_TOKEN_HINT * len(planned),
    )
    per_column = estimate_cost(
        settings.model,
        usage,
        build_pricing(settings.model_pricing()),
        off_peak=period == "off_peak",
    )
    return None if per_column is None else per_column * model_columns


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v1 / v2 / v3 三方对比（规划 + 检索 + 多轮）")
    parser.add_argument("--offline", action="store_true", help="零成本：只跑规则列（默认行为）")
    parser.add_argument("--llm", action="store_true", help="真实调用模型：会花钱，需要 --yes")
    parser.add_argument("--yes", action="store_true", help="确认这次真实调用可以花钱")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 行（控成本）")
    parser.add_argument(
        "--cases", type=Path, nargs="+", default=DEFAULT_CASES, help="JSONL 用例集，可给多个"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="报告路径（默认写到 reports/ 下带时间戳）"
    )
    parser.add_argument("--budget", type=float, default=None, help="本次运行的花费上限（熔断）")
    parser.add_argument(
        "--period", choices=("peak", "off_peak"), default=None, help="钉死计费时段"
    )
    args = parser.parse_args(argv)

    if args.llm and args.offline:
        print("--llm 与 --offline 互斥：前者花钱，后者零成本。")
        return 2
    offline = not args.llm

    try:
        cases = load_cases(args.cases)
    except RunSetupError as exc:
        print(exc)
        return 2
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        print("没有用例可跑。")
        return 2

    if not offline:
        preview = load_settings(env_file=REPO_ROOT / ".env")
        estimate = estimate_run_cost(cases, preview, period=args.period)
        amount = (
            "未知（该模型没有价目，成本只能算下限）"
            if estimate is None
            else f"{preview.currency} {estimate:.6f}"
        )
        print(
            f"本次花费上界: {amount}（{len(cases)} 行 × 2 个模型列，"
            f"输出按上限 {OUTPUT_TOKEN_HINT} token 计；真实回答是短 JSON，通常远低于此值）"
        )
        if not args.yes:
            print("未确认：真实调用需要显式加 --yes。已停在发请求之前。")
            return 2

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
            offline=offline,
            confirm=args.yes,
            budget=args.budget,
            period=args.period,
            case_set_version=", ".join(path.name for path in args.cases),
        )
    except RunSetupError as exc:
        print(exc)
        return 2

    print_report(report)

    out = args.out or (
        REPORTS_DIR / f"version-comparison-{report['run_id']}-spec{SPEC_VERSION}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告已写入:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





