"""A deliberately discriminating case set: requests v1 rules get wrong.

The stage-1 case set is v1-shaped -- the rule baseline scores 100% on it, so it cannot
tell v1 and v2 apart. These cases are the counter-example: plain, reasonable requests
whose phrasing misses v1's keyword table. They are the regression net for v2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from agentevalops.agent import EcommerceSupportAgent
from agentevalops.planner import RulePlanner

GAPS_PATH = Path(REPO_ROOT) / "eval_cases" / "v1_gaps_cases.jsonl"
CASES = [
    json.loads(line)
    for line in GAPS_PATH.read_text(encoding="utf-8").splitlines()
    if line.strip()
]
CASE_IDS = [case["case_id"] for case in CASES]


def test_the_gap_set_exists_and_is_not_empty():
    assert GAPS_PATH.is_file()
    assert CASES


def test_gap_case_ids_are_unique():
    assert len(CASE_IDS) == len(set(CASE_IDS))


def test_every_gap_case_declares_the_full_schema():
    required = {
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
    for case in CASES:
        assert not required - set(case), case["case_id"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_v1_rules_fail_every_gap_case_by_design(case):
    """If v1 ever starts passing one of these, the set stopped discriminating."""
    plan = RulePlanner().plan(case["message"])
    assert plan.intent.value != case["expected_intent"], case["notes"]


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_v1_agent_returns_no_action_for_every_gap_case(case, agent):
    response = agent.handle(case["message"], customer_id=case["customer_id"])
    assert response.state.value == "no_action", case["notes"]
