"""Data-driven evaluation: replay every JSONL scenario through the agent."""

from __future__ import annotations

import pytest
from conftest import EVAL_CASES_PATH, load_support_cases

from agentevalops.models import AgentState, ErrorCode, Intent

CASES = load_support_cases()
CASE_IDS = [case["case_id"] for case in CASES]
REQUIRED_FIELDS = {
    "case_id",
    "category",
    "message",
    "customer_id",
    "expected_intent",
    "expected_state",
    "expected_tool",
    "expected_error_code",
    "expected_data",
    "expected_arguments",
}
REQUIRED_CATEGORIES = {"normal", "boundary", "error", "safety"}


def test_eval_cases_file_exists_and_is_not_empty():
    assert EVAL_CASES_PATH.is_file()
    assert CASES


def test_every_case_declares_the_full_schema():
    for case in CASES:
        missing = REQUIRED_FIELDS - set(case)
        assert not missing, f"{case.get('case_id')} 缺少字段: {sorted(missing)}"


def test_case_ids_are_unique():
    assert len(CASE_IDS) == len(set(CASE_IDS))


def test_cases_cover_every_required_category():
    categories = {case["category"] for case in CASES}
    assert REQUIRED_CATEGORIES <= categories


def test_cases_cover_every_intent():
    assert {case["expected_intent"] for case in CASES} == {intent.value for intent in Intent}


def test_cases_cover_every_agent_state_the_agent_can_reach():
    covered = {case["expected_state"] for case in CASES}
    assert covered <= {state.value for state in AgentState}
    assert covered == {"tool_completed", "validation_failed", "tool_failed", "no_action"}


def test_declared_error_codes_are_all_documented():
    declared = {case["expected_error_code"] for case in CASES} - {None}
    assert declared <= {code.value for code in ErrorCode}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_scenario_matches_agent_behaviour(case, agent):
    response = agent.handle(case["message"], customer_id=case["customer_id"])
    hint = f"{case['case_id']}: {case['notes']}"

    assert response.intent.value == case["expected_intent"], hint
    assert response.state.value == case["expected_state"], hint
    assert response.audit.tool_name == case["expected_tool"], hint

    if case["expected_error_code"] is None:
        assert response.audit.error is None, hint
    else:
        assert response.audit.error is not None, hint
        assert response.audit.error["code"] == case["expected_error_code"], hint

    if case["expected_state"] == AgentState.TOOL_COMPLETED.value:
        assert isinstance(response.audit.result, dict), hint
    else:
        assert response.audit.result is None, hint

    for key, expected_value in case["expected_data"].items():
        assert response.audit.result is not None, hint
        assert response.audit.result[key] == expected_value, f"{hint} / {key}"

    for key, expected_value in case["expected_arguments"].items():
        assert response.audit.arguments[key] == expected_value, f"{hint} / {key}"
