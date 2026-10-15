"""25 条故障用例的数据驱动回放，外加两层集合守卫。

用例文件是唯一事实源：期望值只写在 `eval_cases/fault_cases.jsonl` 里，这里负责回放与判分。
守卫部分单独写，因为「用例都在、但一条也没真注入」是这套实验最可能出现的假证据。
"""

from __future__ import annotations

from collections import Counter

import pytest
from conftest import REPO_ROOT, read_jsonl

from agentevalops.faults import FaultSpec, build_faulted_agent
from agentevalops.models import AgentState, ErrorCode
from agentevalops.order_store import OrderStore
from agentevalops.pipeline import V3Agent

FAULT_PATH = REPO_ROOT / "eval_cases" / "fault_cases.jsonl"
OTHER_CASE_SETS = (
    "case_matrix_v2.jsonl",
    "rag_cases.jsonl",
    "multiturn_cases.jsonl",
    "support_cases.jsonl",
    "v1_gaps_cases.jsonl",
)

FAULT_CASES = read_jsonl(FAULT_PATH)

FAULT_CODES = {
    "fault_timeout",
    "fault_unavailable",
}


def test_the_fault_set_has_25_cases_with_unique_ids():
    assert len(FAULT_CASES) == 25
    ids = [case["case_id"] for case in FAULT_CASES]
    assert len(set(ids)) == len(ids)


def test_every_case_declares_a_fault_that_parses():
    """一条写错的声明就会让那份「一切正常」的故障报告失真，所以在收集阶段就炸。"""
    for case in FAULT_CASES:
        assert "fault" in case, case["case_id"]
        FaultSpec.parse(case["fault"])


def test_the_four_fault_categories_are_all_present():
    counts = Counter(case["category"] for case in FAULT_CASES)
    assert counts == {
        "fault_timeout": 7,
        "fault_unavailable": 6,
        "fault_empty_retrieval": 6,
        "fault_rate_limit": 6,
    }


def test_no_other_case_set_has_picked_up_a_fault_field():
    """SPEC-FAULT-1：故障用例独立成文件、独立判分。主矩阵里冒出 fault 就是口径被污染了。"""
    for name in OTHER_CASE_SETS:
        for case in read_jsonl(REPO_ROOT / "eval_cases" / name):
            assert "fault" not in case, (name, case.get("case_id"))


@pytest.mark.parametrize("case", FAULT_CASES, ids=[c["case_id"] for c in FAULT_CASES])
def test_fault_case_matches_agent_behaviour(case):
    agent = build_faulted_agent(case["fault"])
    session_id = case.get("session_id")
    for turn in case.get("warmup", []):
        agent.handle(
            turn["message"],
            customer_id=turn.get("customer_id", case["customer_id"]),
            session_id=session_id,
        )

    response = agent.handle(
        case["message"], customer_id=case["customer_id"], session_id=session_id
    )

    assert str(response.intent) == case["expected_intent"]
    assert str(response.state) == case["expected_state"]
    assert response.audit.tool_name == case["expected_tool"]
    error = response.audit.error or {}
    assert error.get("code") == case["expected_error_code"]
    if case["expected_plan_source"] is not None:
        assert agent.last_plan.source == case["expected_plan_source"]
    if "expected_slot_source" in case:
        assert agent.last_slot.source == case["expected_slot_source"]


@pytest.mark.parametrize(
    "case",
    [c for c in FAULT_CASES if c["category"] in FAULT_CODES],
    ids=[c["case_id"] for c in FAULT_CASES if c["category"] in FAULT_CODES],
)
def test_a_dependency_fault_reports_its_own_error_code(case):
    """期望值不许和声明打架：mode=timeout 却期望 NO_KNOWLEDGE，就是用例本身写错了。

    这两类用例的故障一定落在本轮要走的那条路上，所以错误码由 mode 唯一决定。
    """
    expected = (
        ErrorCode.TOOL_TIMEOUT if case["fault"]["mode"] == "timeout" else ErrorCode.TOOL_UNAVAILABLE
    )
    assert case["expected_error_code"] == expected
    assert case["expected_state"] == "tool_failed"


@pytest.mark.parametrize(
    "case",
    [c for c in FAULT_CASES if c["category"] == "fault_empty_retrieval"],
    ids=[c["case_id"] for c in FAULT_CASES if c["category"] == "fault_empty_retrieval"],
)
def test_an_empty_hit_list_only_changes_the_turns_that_retrieve(case):
    """注入必须局限于被注入的那一环：本轮是业务动作时，空检索什么都不该改变。"""
    if case["expected_intent"] == "policy_qa":
        assert case["expected_state"] == "no_action"
        assert case["expected_error_code"] == ErrorCode.NO_KNOWLEDGE
        assert case["expected_tool"] is None
    else:
        assert case["expected_state"] == "tool_completed"
        assert case["expected_error_code"] is None


@pytest.mark.parametrize(
    "case",
    [c for c in FAULT_CASES if c["category"] == "fault_rate_limit"],
    ids=[c["case_id"] for c in FAULT_CASES if c["category"] == "fault_rate_limit"],
)
def test_a_rate_limited_case_never_turns_the_request_into_a_failure(case):
    """SPEC-FAULT-4：降级之后请求该办成照样办成，该拒答照样拒答，唯独不许「失败」。"""
    assert case["expected_plan_source"] == "llm-fallback"
    assert case["expected_state"] in {"tool_completed", "no_action"}
    assert case["expected_state"] != "tool_failed"


def test_the_rate_limited_cases_are_the_only_ones_asserting_a_degraded_plan():
    for case in FAULT_CASES:
        degraded = case["expected_plan_source"] == "llm-fallback"
        assert degraded == (case["fault"]["target"] == "model"), case["case_id"]


def test_an_injected_empty_retrieval_reproduces_the_real_gap_exactly():
    """用例集里那条「语料本来就没有」的样本，注入前后必须逐字段一致（SPEC-FAULT-5）。"""
    case = next(c for c in FAULT_CASES if c["case_id"] == "flt_empty_retrieval_15")
    genuine = V3Agent(OrderStore()).handle(case["message"], customer_id=case["customer_id"])
    injected = build_faulted_agent(case["fault"]).handle(
        case["message"], customer_id=case["customer_id"]
    )
    assert genuine.to_dict() == injected.to_dict()
    assert genuine.state is AgentState.NO_ACTION
