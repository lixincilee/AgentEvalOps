"""Data-driven multi-turn evaluation: replay eval_cases/multiturn_cases.jsonl.

SPEC-MT-1..6. Each case is a conversation, not a request: the case passes only when every
turn passes, in order, on one agent instance. The set is also checked for *sensitivity* --
a multi-turn suite that would pass without any session state is measuring nothing.
"""

from __future__ import annotations

import re

import pytest
from conftest import REPO_ROOT, read_jsonl

from agentevalops.models import SLOT_INHERITED, SLOT_VERBATIM, AgentState, ErrorCode, Intent
from agentevalops.pipeline import V3Agent
from agentevalops.session import SLOT_INTENTS

CASES_PATH = REPO_ROOT / "eval_cases" / "multiturn_cases.jsonl"
CASES = read_jsonl(CASES_PATH)
CASE_IDS = [case["case_id"] for case in CASES]
TURN_PARAMS = [
    pytest.param(case, id=case["case_id"])
    for case in CASES
]

REQUIRED_FIELDS = {"case_id", "category", "customer_id", "session_id", "turns", "notes"}
REQUIRED_TURN_FIELDS = {
    "message",
    "expected_intent",
    "expected_state",
    "expected_tool",
    "expected_error_code",
    "expected_order_id",
    "expected_slot_source",
}
CATEGORY_MINIMUMS = {"multiturn": 30, "multiturn_refuse": 3}
ORDER_ID_PATTERN = re.compile(r"ORD-\d{4}")


def _turn_count() -> int:
    return sum(len(case["turns"]) for case in CASES)


def _replay(case) -> list:
    """Run one case end to end and return the responses, in order."""
    agent = V3Agent()
    return [
        agent.handle(
            turn["message"],
            customer_id=case["customer_id"],
            session_id=case["session_id"],
        )
        for turn in case["turns"]
    ]


def _slots(case) -> list:
    """Replay one case and return the resolved slot per turn."""
    agent = V3Agent()
    slots = []
    for turn in case["turns"]:
        agent.handle(
            turn["message"],
            customer_id=case["customer_id"],
            session_id=case["session_id"],
        )
        slots.append(agent.last_slot)
    return slots


def _turn_matches(turn, response, slot) -> bool:
    if response.intent.value != turn["expected_intent"]:
        return False
    if response.state.value != turn["expected_state"]:
        return False
    if response.audit.tool_name != turn["expected_tool"]:
        return False
    error_code = None if response.audit.error is None else response.audit.error["code"]
    if error_code != turn["expected_error_code"]:
        return False
    if slot.order_id != turn["expected_order_id"]:
        return False
    return slot.source == turn["expected_slot_source"]


# --- 用例集自身的约束 --------------------------------------------------------


def test_the_case_file_exists_and_is_not_empty():
    assert CASES_PATH.is_file()
    assert len(CASES) >= 35
    assert _turn_count() >= 70


def test_case_ids_are_unique_and_prefixed():
    assert len(CASE_IDS) == len(set(CASE_IDS))
    for case_id in CASE_IDS:
        assert case_id.startswith("mt_"), case_id


def test_every_case_declares_the_full_schema():
    for case in CASES:
        assert set(case) == REQUIRED_FIELDS, case["case_id"]
        for turn in case["turns"]:
            assert set(turn) == REQUIRED_TURN_FIELDS, case["case_id"]


def test_every_case_has_at_least_two_turns():
    for case in CASES:
        assert len(case["turns"]) >= 2, case["case_id"]


@pytest.mark.parametrize("category", sorted(CATEGORY_MINIMUMS))
def test_each_category_meets_its_minimum(category):
    assert sum(1 for case in CASES if case["category"] == category) >= (
        CATEGORY_MINIMUMS[category]
    )


def test_session_ids_are_unique_across_cases():
    """SPEC-MT-6：不同用例必须用不同 session_id，否则一条用例会继承另一条的残留状态。"""
    session_ids = [case["session_id"] for case in CASES]
    assert len(session_ids) == len(set(session_ids))


def test_the_declared_slot_sources_are_closed_over():
    for case in CASES:
        for turn in case["turns"]:
            assert turn["expected_slot_source"] in {None, SLOT_VERBATIM, SLOT_INHERITED}, (
                case["case_id"]
            )


def test_a_verbatim_slot_is_really_in_the_message():
    """SPEC-MT-2：原文来源必须真在原文里，不能是「看起来像原文」。"""
    for case in CASES:
        for turn in case["turns"]:
            if turn["expected_slot_source"] != SLOT_VERBATIM:
                continue
            assert turn["expected_order_id"] in turn["message"], case["case_id"]


def test_an_inherited_slot_is_never_printed_in_its_own_message():
    """SPEC-MT-2：继承只在消息本身没有订单号时生效。"""
    for case in CASES:
        for turn in case["turns"]:
            if turn["expected_slot_source"] != SLOT_INHERITED:
                continue
            assert not ORDER_ID_PATTERN.search(turn["message"]), case["case_id"]


def test_an_inherited_slot_points_at_the_last_slot_of_the_same_case():
    """继承的必须是**上一轮**的订单号，不是更早某轮的、更不是别的用例的。"""
    for case in CASES:
        last_seen = None
        for turn in case["turns"]:
            if turn["expected_slot_source"] == SLOT_INHERITED:
                assert last_seen is not None, case["case_id"]
                assert turn["expected_order_id"] == last_seen, case["case_id"]
            if turn["expected_slot_source"] == SLOT_VERBATIM:
                last_seen = turn["expected_order_id"]


def test_a_turn_that_consumes_no_slot_declares_no_slot():
    """SPEC-MT-2：policy_qa / unknown 轮不解析槽位，哪怕会话里已经有订单号。"""
    for case in CASES:
        for turn in case["turns"]:
            intent = Intent(turn["expected_intent"])
            if intent in SLOT_INTENTS:
                continue
            assert turn["expected_order_id"] is None, case["case_id"]
            assert turn["expected_slot_source"] is None, case["case_id"]


def test_refusal_turns_use_the_dedicated_error_code():
    for case in CASES:
        if case["category"] != "multiturn_refuse":
            continue
        refused = [
            turn
            for turn in case["turns"]
            if turn["expected_error_code"] == ErrorCode.NO_KNOWLEDGE.value
        ]
        assert refused, case["case_id"]
        for turn in refused:
            assert turn["expected_state"] == AgentState.NO_ACTION.value
            assert turn["expected_tool"] is None


def test_the_set_covers_every_behaviour_it_claims_to():
    sources = [turn["expected_slot_source"] for case in CASES for turn in case["turns"]]
    assert sources.count(SLOT_VERBATIM) >= 10
    assert sources.count(SLOT_INHERITED) >= 8
    states = {turn["expected_state"] for case in CASES for turn in case["turns"]}
    assert AgentState.VALIDATION_FAILED.value in states
    assert AgentState.NO_ACTION.value in states
    intents = {turn["expected_intent"] for case in CASES for turn in case["turns"]}
    assert Intent.POLICY_QA.value in intents


# --- 回放 --------------------------------------------------------------------


@pytest.mark.parametrize("case", TURN_PARAMS)
def test_every_turn_of_the_case_matches(case):
    responses = _replay(case)
    slots = _slots(case)
    for index, (turn, response, slot) in enumerate(
        zip(case["turns"], responses, slots, strict=True), 1
    ):
        hint = f"{case['case_id']}#{index}: {case['notes']}"
        assert response.intent.value == turn["expected_intent"], hint
        assert response.state.value == turn["expected_state"], hint
        assert response.audit.tool_name == turn["expected_tool"], hint
        error_code = None if response.audit.error is None else response.audit.error["code"]
        assert error_code == turn["expected_error_code"], hint
        assert slot.order_id == turn["expected_order_id"], hint
        assert slot.source == turn["expected_slot_source"], hint


def test_the_case_set_as_a_whole_is_sensitive_to_session_state():
    """把会话拆掉重跑，必须有一批用例开始失败——否则这套用例根本没在测多轮。

    少数用例在无会话下同样成立（政策轮不写槽位、缺槽位不猜），那是预期的，
    所以这里断言的是**整批**的敏感度，而不是每条都敏感。
    """
    sensitive = 0
    for case in CASES:
        if case["category"] != "multiturn":
            continue
        agent = V3Agent()
        matched = []
        for turn in case["turns"]:
            response = agent.handle(turn["message"], customer_id=case["customer_id"])
            matched.append(_turn_matches(turn, response, agent.last_slot))
        if not all(matched):
            sensitive += 1
    assert sensitive >= 10, f"只有 {sensitive} 条用例真正依赖会话状态"
