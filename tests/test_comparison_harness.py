"""The comparison harness must not lie about what it actually ran.

The 165-case matrix contains six malformed requests (blank message, blank customer id).
They are rejected before planning, so they make no model call. A harness that books them
as model-driven decisions -- or keeps reading the previous case's plan -- reports fiction:
it claimed ``source=llm`` for cases that called nothing, and printed 159 model calls next
to a case count of 165 without ever saying why.

These tests pin the harness itself: the zero-cost offline self-check must reproduce v1
exactly, and every number in the report must reconcile with every other number.
"""

from __future__ import annotations

import sys

import pytest
from conftest import REPO_ROOT, read_jsonl

# scripts/ is not an importable package, but the harness is plain source we can import.
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import compare_planners as harness  # noqa: E402

from agentevalops.agent import pre_plan_rejection  # noqa: E402
from agentevalops.config import Settings  # noqa: E402

MATRIX_PATH = REPO_ROOT / "eval_cases" / "case_matrix_v2.jsonl"
CASES = read_jsonl(MATRIX_PATH)
NOT_PLANNED = [
    case for case in CASES if pre_plan_rejection(case["message"], case["customer_id"]) is not None
]


@pytest.fixture(scope="module")
def offline_report() -> dict:
    """Run the whole harness in-process once: it is deterministic and costs nothing."""
    return harness.run_comparison(
        CASES, offline=True, settings=Settings(), case_set_version=MATRIX_PATH.name
    )


def test_the_matrix_really_exercises_the_short_circuit_path():
    """6/159 是 2026-10-14 那份报告对不上时暴露出来的数字，钉住它防止悄悄改变。"""
    assert len(NOT_PLANNED) == 6
    assert {case["expected_state"] for case in NOT_PLANNED} == {"validation_failed"}


def test_offline_scripts_cover_exactly_the_planned_cases():
    """脚本按调用顺序回放，多给一条就会让后面每条答案整体错位一位。"""
    assert len(harness.offline_scripts(CASES)) == len(CASES) - len(NOT_PLANNED)


def test_the_offline_self_check_reproduces_v1_exactly(offline_report):
    """回放的是 v1 自己的决策，所以 v2 必须逐条一致；任何差异都是编排层的 bug。"""
    summary = offline_report["summary"]
    assert summary["changed_cases"] == []
    for name in harness.METRICS:
        assert summary["v2"][name] == summary["v1"][name], name


def test_every_reported_number_reconciles(offline_report):
    summary = offline_report["summary"]
    assert all(summary["invariants"].values()), summary["invariants"]
    assert summary["cases"] == len(CASES)
    assert summary["planned_cases"] == len(CASES) - len(NOT_PLANNED)
    assert summary["short_circuited_cases"] == len(NOT_PLANNED)
    assert summary["cases"] == summary["planned_cases"] + summary["short_circuited_cases"]
    assert summary["model_calls"] == summary["planned_cases"]
    assert summary["peak_calls"] + summary["off_peak_calls"] == summary["model_calls"]


def test_short_circuited_cases_are_traced_as_not_planned(offline_report):
    rows = {row["case_id"]: row for row in offline_report["cases"]}
    for case in NOT_PLANNED:
        row = rows[case["case_id"]]
        assert row["planned"] is False
        assert row["v2_source"] == harness.SOURCE_NOT_PLANNED
        assert row["v2_note"] is None
        assert row["v2_latency_ms"] is None


def test_planned_cases_still_carry_a_planner_source(offline_report):
    for row in offline_report["cases"]:
        if row["planned"]:
            assert row["v2_source"] in {"rules", "llm", "llm-fallback"}


def test_latency_average_only_uses_real_call_samples(offline_report):
    """被拒绝的用例没有调用，把它的 0 计进平均延迟会凭空拉低延迟。"""
    summary = offline_report["summary"]
    assert summary["latency_samples"] == summary["planned_cases"]


def test_an_empty_case_set_is_a_setup_error():
    with pytest.raises(harness.RunSetupError):
        harness.run_comparison([], offline=True, settings=Settings())


def test_a_real_run_without_credentials_fails_before_spending_anything():
    with pytest.raises(harness.RunSetupError, match="DEEPSEEK_API_KEY"):
        harness.run_comparison(CASES[:1], offline=False, settings=Settings())