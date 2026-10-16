"""v1 / v2 / v3 三方对比：同一批用例、同一套判分、同一份版本清单。

Usage:
    python scripts/compare_versions.py --offline --limit 20
    python scripts/compare_versions.py --llm --yes --limit 20
    python scripts/compare_versions.py --out reports/v1-v2-v3.json
    python scripts/compare_versions.py --offline --repeat 5   # 反复跑，汇总中位数与方差

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

``--repeat N`` 把同一份用例跑 N 次再汇总：通过率 / 成本 / 时延取中位数，逐次的原始数字留在
``runs[]``，极差留在 ``variance``。**单次运行的通过率是抽样的**，拿它去判「版本退化」会把
模型的随机性读成代码的退化；这就是这个开关存在的唯一理由。
"""

from __future__ import annotations

import argparse
import copy
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
    BudgetExceededError,
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

SCRIPTS_DIR = Path(__file__).resolve().parent
# scripts/ 不是包：直接跑时 sys.path[0] 就是它，被 importlib 加载时不是。不显式加一次，
# `from variance import ...` 只在一种运行方式下成立（load_test.py 同款处理）。
sys.path.insert(0, str(SCRIPTS_DIR))

from variance import (  # noqa: E402
    difference_is_interpretable,
    spread_of,
    summarize_runs,
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


#: ``--repeat`` 时逐次记录并汇总的列级指标。名字直接对应报告里的取值位置，
#: 免得下游再去猜「这个 0.9671 是行级还是用例级」。
REPEAT_COLUMN_METRICS = (
    "pass_rate",
    "case_pass_rate",
    "cost",
    "total_tokens",
    "model_calls",
    "avg_latency_ms",
)


def _column_metric(report: dict[str, Any], column: str, metric: str) -> float | None:
    """从一次运行的报告里读某一列的某个指标；列不可用就是 None（不是 0）。"""
    payload = (report.get("versions") or {}).get(column)
    if not isinstance(payload, dict):
        return None
    if metric == "case_pass_rate":
        value = (payload.get("case_level") or {}).get("pass_rate")
    else:
        value = (payload.get("metrics") or {}).get(metric)
    return None if value is None else float(value)


def _compact_run(report: dict[str, Any], index: int) -> dict[str, Any]:
    """一次运行的摘要。

    整份报告每份都有几百行 ``rows``，N 份全存下来报告会大到没人看；这里只留「每次的数字」。
    明细留给代表运行——汇总报告顶层的 ``rows`` 就是它。
    """
    return {
        "run": index + 1,
        "run_id": report.get("run_id"),
        "generated_at": report.get("generated_at"),
        "columns": {
            key: {metric: _column_metric(report, key, metric) for metric in REPEAT_COLUMN_METRICS}
            for key in COLUMNS
        },
    }


def _failed_case_ids(report: dict[str, Any]) -> dict[str, list[str]]:
    """这一次运行里，每一列分别是哪几条用例没过。"""
    return {
        key: [
            row["case_id"]
            for row in report["rows"]
            if row.get(key) is not None and not row[key]["passed"]
        ]
        for key in COLUMNS
        if (report.get("versions") or {}).get(key) is not None
    }


def _flaky_cases(
    per_run: list[dict[str, dict[str, bool]]]
) -> dict[str, list[dict[str, Any]]]:
    """跨次运行里**结论翻过**的用例。

    只列翻过的：全部通过、全部失败的用例不进这里（那些不是墙头草）。按「通过的次数」升序，
    最先出事的排前面。这是 ``--repeat`` 最有价值的产出之一——**跑一次永远看不出来哪条用例
    是墙头草，而墙头草正是「版本对比时最容易被噪声说成退化」的那些用例**。
    """
    if len(per_run) < 2:
        return {}
    flaky: dict[str, list[dict[str, Any]]] = {}
    for key in COLUMNS:
        totals: dict[str, int] = {}
        passes: dict[str, int] = {}
        for run in per_run:
            for case_id, passed in (run.get(key) or {}).items():
                totals[case_id] = totals.get(case_id, 0) + 1
                passes[case_id] = passes.get(case_id, 0) + (1 if passed else 0)
        rows = [
            {"case_id": case_id, "passed_runs": passes.get(case_id, 0), "total_runs": total}
            for case_id, total in sorted(totals.items())
            if 0 < passes.get(case_id, 0) < total
        ]
        if rows:
            rows.sort(key=lambda item: (item["passed_runs"], item["case_id"]))
            flaky[key] = rows
    return flaky


def _representative_run(
    runs: list[dict[str, Any]], variance: dict[str, dict[str, Any]]
) -> int:
    """挑「离中位数最近」的那次运行当代表（各列行级通过率的绝对偏差之和最小）。

    顶层明细只可能来自**某一次真实运行**：编一份「平均出来的明细」等于造假。所以代表运行的
    原始数字原样保留，中位数另行写在 ``metrics`` 与 ``variance`` 里，两者都看得见。
    """
    best_index, best_score = 0, None
    for index, run in enumerate(runs):
        score = 0.0
        for key in COLUMNS:
            value = (run.get("columns") or {}).get(key, {}).get("pass_rate")
            median = (variance.get(f"{key}.pass_rate") or {}).get("median")
            if value is None or median is None:
                continue
            score += abs(float(value) - float(median))
        if best_score is None or score < best_score:
            best_index, best_score = index, score
    return best_index


def _interpretable_deltas(
    versions: dict[str, dict[str, Any] | None], variance: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any] | None]:
    """两两差值里，哪些比噪声大。

    判据在 ``variance.difference_is_interpretable``：``|差值| > max(两边的极差)``，故意保守。
    它不是判分，是**把「噪声」和「结论」分开**：报告里每条差值都跟着一句
    「这次差异能不能解释」，达不到就明说解释不了。
    """
    deltas = _pairwise_deltas(versions)
    out: dict[str, dict[str, Any] | None] = {}
    for pair, delta in deltas.items():
        if delta is None:
            out[pair] = None
            continue
        left, right = str(delta["left"]), str(delta["right"])
        entry: dict[str, Any] = {"left": left, "right": right}
        for metric in ("pass_rate", "case_pass_rate"):
            value = float(delta[metric])
            noise_left = spread_of(variance, f"{left}.{metric}")
            noise_right = spread_of(variance, f"{right}.{metric}")
            entry[f"{metric}_delta"] = value
            entry[f"{metric}_noise"] = max(noise_left, noise_right)
            entry[f"{metric}_interpretable"] = difference_is_interpretable(
                value, noise_left, noise_right
            )
        out[pair] = entry
    return out


def _aggregate_invariants(
    versions: dict[str, dict[str, Any] | None], variance: dict[str, dict[str, Any]]
) -> dict[str, bool]:
    """汇总自己也要自检：中位数必须落在观测范围内。"""
    ok = True
    for key in COLUMNS:
        if versions.get(key) is None:
            continue
        for metric in REPEAT_COLUMN_METRICS:
            entry = variance.get(f"{key}.{metric}") or {}
            median = entry.get("median")
            low, high = entry.get("min"), entry.get("max")
            if median is None or low is None or high is None:
                continue
            if not (float(low) <= float(median) <= float(high)):
                ok = False
    return {"median_within_observed_range": ok}


def run_repeated_comparison(
    cases: list[dict[str, Any]],
    *,
    repeat: int = 1,
    **kwargs: Any,
) -> dict[str, Any]:
    """把同一份用例跑 ``repeat`` 次，汇总出中位数与方差。

    只加一层「循环 + 汇总」，判分逻辑一个字都不重写：每次老老实实调一次
    :func:`run_comparison`。三条口径写死在这里，理由和压测（``load_test.py --repeat``）同源：

    * 通过率 / 成本 / 时延取 **中位数**——单次运行的通过率就是抽样的，拿它判退化会把模型随机性
      读成代码退化。
    * ``--budget`` 是 **N 次合计** 的上限，不是每次各自的上限（否则实际花销是 N 倍预算）。
      每次开跑前把「剩余额度」传给那一次，跑完扣账；额度耗尽就停下。
    * 次数没跑满时**照样出报告**：跑完的那几次是真的，丢掉它们等于把已经花掉的钱烧掉。但
      ``repeat_completed`` 会写明跑了几次、``repeat_aborted`` 写明为什么停。
    """
    planned = max(1, int(repeat))
    budget = kwargs.get("budget")
    runs: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    per_run_pass: list[dict[str, dict[str, bool]]] = []
    aborted: dict[str, Any] | None = None
    first_error: BaseException | None = None
    spent = 0.0

    for index in range(planned):
        remaining = None if budget is None else float(budget) - spent
        if remaining is not None and remaining <= 0:
            aborted = {
                "after_run": index,
                "reason": "budget",
                "detail": (
                    f"累计花费已达上限（{budget}），第 {index + 1} 次没有再跑："
                    "把没跑的那些算成「通过」才是真的骗人。"
                ),
            }
            break
        call_kwargs = dict(kwargs)
        if remaining is not None:
            call_kwargs["budget"] = remaining
        # moment 同时钉死时钟和 run_id；重复运行时每次都钉同一秒会撞号，所以逐次放开。
        call_kwargs.pop("moment", None)
        try:
            report = run_comparison(cases, **call_kwargs)
        except BudgetExceededError as exc:
            # 熔断发生在**运行中间**：这一次的部分花费不在 cost_total 里（那是下限），
            # 所以 reason 单独记成 budget，别和后一种「跑着跑着崩了」混成一类。
            aborted = {
                "after_run": index,
                "reason": "budget",
                "detail": (
                    f"第 {index + 1} 次运行中途触发预算熔断（剩余额度 "
                    f"{remaining:.6f}）：{exc}"
                ),
            }
            break
        except RunSetupError:
            raise
        except Exception as exc:  # noqa: BLE001 - 任何一次崩掉都不该把前面几次的数据扔掉
            first_error = exc
            aborted = {
                "after_run": index,
                "reason": "error",
                "detail": f"第 {index + 1} 次运行抛了 {type(exc).__name__}: {exc}",
            }
            break
        reports.append(report)
        runs.append(_compact_run(report, index))
        per_run_pass.append(
            {
                key: {
                    row["case_id"]: bool(row[key]["passed"])
                    for row in report["rows"]
                    if row.get(key) is not None
                }
                for key in COLUMNS
            }
        )
        spent += sum(_column_metric(report, key, "cost") or 0.0 for key in COLUMNS)
        if planned > 1:
            shown = " ".join(
                f"{key} {value:.1%}"
                for key in COLUMNS
                for value in [_column_metric(report, key, "pass_rate")]
                if value is not None
            )
            print(f"  第 {index + 1}/{planned} 次: {shown}")

    if not reports:
        if first_error is not None:
            raise first_error
        raise RunSetupError(
            "一次都没跑成：" + (aborted["detail"] if aborted else "没有可用的运行结果。")
        )

    flat_runs = [
        {
            f"{key}.{metric}": _column_metric(report, key, metric)
            for key in COLUMNS
            for metric in REPEAT_COLUMN_METRICS
        }
        for report in reports
    ]
    variance = summarize_runs(
        flat_runs,
        [f"{key}.{metric}" for key in COLUMNS for metric in REPEAT_COLUMN_METRICS],
    )

    base_index = _representative_run(runs, variance)
    base = reports[base_index]
    versions = copy.deepcopy(base["versions"])
    for key in COLUMNS:
        payload = versions.get(key)
        if payload is None:
            continue
        row_stats = variance.get(f"{key}.pass_rate") or {}
        case_stats = variance.get(f"{key}.case_pass_rate") or {}
        if row_stats.get("median") is not None:
            payload["metrics"]["pass_rate_single_run"] = payload["metrics"]["pass_rate"]
            payload["metrics"]["pass_rate"] = row_stats["median"]
            payload["metrics"]["pass_rate_min"] = row_stats["min"]
            payload["metrics"]["pass_rate_max"] = row_stats["max"]
        if case_stats.get("median") is not None:
            payload["case_level"]["pass_rate_single_run"] = payload["case_level"]["pass_rate"]
            payload["case_level"]["pass_rate"] = case_stats["median"]
            payload["case_level"]["pass_rate_min"] = case_stats["min"]
            payload["case_level"]["pass_rate_max"] = case_stats["max"]
        payload["aggregate"] = {
            metric: variance[f"{key}.{metric}"]
            for metric in REPEAT_COLUMN_METRICS
            if (variance.get(f"{key}.{metric}") or {}).get("count")
        }

    aggregated = dict(base)
    aggregated.update(
        {
            "repeat": planned,
            "repeat_completed": len(reports),
            "repeat_aborted": aborted,
            "representative_run": base_index + 1,
            "runs": runs,
            "variance": variance,
            "aggregate_policy": {
                "central_statistic": "median",
                "central_metrics": list(REPEAT_COLUMN_METRICS),
                "detail_source": f"第 {base_index + 1} 次运行（离中位数最近的一次）",
                "budget_scope": "N 次合计" if budget is not None else None,
            },
            "flaky_cases": _flaky_cases(per_run_pass),
            "versions": versions,
            "deltas": _pairwise_deltas(versions),
            "interpretable_deltas": _interpretable_deltas(versions, variance),
            "summary": {
                **base["summary"],
                "repeat": planned,
                "repeat_completed": len(reports),
                "cost_total": spent,
                "cost_total_complete": aborted is None,
                "budget": budget,
                "failure_signature": {
                    key: len(ids) for key, ids in _failed_case_ids(base).items()
                },
            },
            "invariants": {
                **base["invariants"],
                **_aggregate_invariants(versions, variance),
            },
            "notes": list(base.get("notes") or [])
            + [
                f"这份报告汇总 {len(reports)}/{planned} 次运行：通过率 / 成本 / 时延取中位数"
                f"（逐次数字在 runs[]，极差在 variance 与 metrics.pass_rate_min/max）。"
                f"顶层明细（rows、未中项）来自第 {base_index + 1} 次运行，"
                f"是真实一次的结果，不是「平均出来的明细」。"
            ]
            + (
                []
                if aborted is None
                else [
                    f"重复未跑满（{aborted['reason']}）：{aborted['detail']}",
                    "被中断的那一次的花费没有计入 cost_total，所以那是个下限"
                    "（cost_total_complete=false）。",
                ]
            ),
        }
    )
    return aggregated


def print_variance(report: dict[str, Any], *, max_flaky: int = 8) -> None:
    """打印 ``--repeat`` 的方差块：中位数与极差、能不能解释、以及墙头草用例。"""
    planned = int(report.get("repeat") or 1)
    if planned <= 1:
        return
    variance = report.get("variance") or {}
    completed = report.get("repeat_completed", planned)
    aborted = report.get("repeat_aborted")
    print("=" * 54)
    print("重复运行方差")
    print("=" * 54)
    print(
        f"次数                      {completed}/{planned}"
        + ("" if aborted is None else f"（{aborted['reason']}：{aborted['detail']}）")
    )
    print("列级中位数（极差 / 相对极差）:")
    for key in COLUMNS:
        if (report["versions"].get(key)) is None:
            continue
        parts = []
        for metric in ("pass_rate", "case_pass_rate"):
            entry = variance.get(f"{key}.{metric}") or {}
            median = entry.get("median")
            if median is None:
                continue
            label = "行级" if metric == "pass_rate" else "用例级"
            relative = entry.get("spread_pct")
            relative_shown = "n/a" if relative is None else f"{relative:.1%}"
            parts.append(
                f"{label} {median:.1%}（极差 {entry['spread']:.1%} / 相对 {relative_shown}）"
            )
        if parts:
            print(f"  {key:9s} " + "  ".join(parts))
    print("差值与噪声（|差值| > 噪声才算解释得了）:")
    for pair, entry in (report.get("interpretable_deltas") or {}).items():
        if entry is None:
            print(f"  {pair}: n/a（该对比有列不可用）")
            continue
        verdict = "可解释" if entry["pass_rate_interpretable"] else "不可解释（低于噪声）"
        print(
            f"  {pair}: 行级 {entry['pass_rate_delta']:+.1%}"
            f"（噪声 {entry['pass_rate_noise']:.1%}）-> {verdict}"
        )
    flaky = report.get("flaky_cases") or {}
    if flaky:
        total = sum(len(items) for items in flaky.values())
        print(f"墙头草用例（跨次结论翻转）: {total} 条")
        for key, items in flaky.items():
            for item in items[:max_flaky]:
                print(
                    f"  {key}: {item['case_id']} "
                    f"{item['total_runs']} 次里过 {item['passed_runs']} 次"
                )
            if len(items) > max_flaky:
                print(f"  {key}: ...（另有 {len(items) - max_flaky} 条，见报告 JSON）")
    else:
        print("墙头草用例（跨次结论翻转）: 无——每一列的结论在所有次运行里都一致")


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
    parser.add_argument(
        "--budget", type=float, default=None, help="花费上限（熔断）；--repeat 时是 N 次合计"
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="把同一份用例跑 N 次，汇总中位数与方差（默认 1；版本对比建议 ≥3）",
    )
    parser.add_argument(
        "--period", choices=("peak", "off_peak"), default=None, help="钉死计费时段"
    )
    args = parser.parse_args(argv)

    if args.llm and args.offline:
        print("--llm 与 --offline 互斥：前者花钱，后者零成本。")
        return 2
    if args.repeat < 1:
        print("--repeat 必须 ≥ 1（1 就是原来的行为）。")
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
            else f"{preview.currency} {estimate * max(1, args.repeat):.6f}"
        )
        print(
            f"本次花费上界: {amount}（{len(cases)} 行 × 2 个模型列 × {max(1, args.repeat)} 次，"
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

    comparison_kwargs: dict[str, Any] = dict(
        offline=offline,
        confirm=args.yes,
        budget=args.budget,
        period=args.period,
        case_set_version=", ".join(path.name for path in args.cases),
    )
    try:
        if args.repeat > 1:
            report = run_repeated_comparison(
                cases, repeat=args.repeat, **comparison_kwargs
            )
        else:
            report = run_comparison(cases, **comparison_kwargs)
    except RunSetupError as exc:
        print(exc)
        return 2

    print_report(report)
    print_variance(report)

    out = args.out or (
        REPORTS_DIR / f"version-comparison-{report['run_id']}-spec{SPEC_VERSION}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告已写入:", out)
    if report.get("repeat_aborted"):
        print("未通过（数据不完整）：" + report["repeat_aborted"]["detail"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





