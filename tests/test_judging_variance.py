"""重复判分的方差口径：中位数、最差一次、墙头草用例。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agentevalops.judging.base import JudgeReport, JudgeSample, judge_score
from agentevalops.judging.variance import RepeatReport, failed_cases, flaky_cases
from agentevalops.llm import BudgetExceededError


def make_report(
    verdicts: dict[str, bool],
    *,
    layer: str = "L3",
    metrics: tuple[str, ...] = ("faithfulness", "context_precision"),
    error_cases: tuple[str, ...] = (),
) -> JudgeReport:
    rows = []
    for case_id, passed in verdicts.items():
        for metric in metrics:
            if case_id in error_cases:
                rows.append(
                    judge_score(
                        sample=JudgeSample(case_id, "q", "a", ("c",)),
                        layer=layer,
                        metric=metric,
                        score=None,
                        error="IncompleteOutputException: boom",
                    )
                )
            else:
                rows.append(
                    judge_score(
                        sample=JudgeSample(case_id, "q", "a", ("c",)),
                        layer=layer,
                        metric=metric,
                        score=0.95 if passed else 0.10,
                    )
                )
    return JudgeReport(layer=layer, judge_model="m", rows=rows, case_set="cases.jsonl")


def test_median_is_the_headline_and_worst_run_is_reported():
    reports = [
        make_report({"a": True, "b": True}),
        make_report({"a": True, "b": False}),
        make_report({"a": True, "b": True}),
    ]
    combined = RepeatReport(layer="L3", judge_model="m", case_set="cases.jsonl", reports=reports)
    assert combined.pass_rates == [1.0, 0.5, 1.0]
    assert combined.pass_rate == 1.0
    assert combined.worst_pass_rate == 0.5
    assert combined.best_pass_rate == 1.0


def test_a_case_that_flips_between_runs_is_listed_as_flaky():
    reports = [make_report({"a": True}), make_report({"a": False})]
    rows = flaky_cases(reports)
    assert [row["case_id"] for row in rows] == ["a"]
    assert rows[0]["passed_runs"] == 1
    assert rows[0]["verdicts"] == ["pass", "fail"]
    assert set(rows[0]["flipped_metrics"]) == {"faithfulness", "context_precision"}


def test_failed_cases_are_listed_with_their_metric_scores():
    rows = failed_cases([make_report({"a": True, "b": False})])
    assert [row["case_id"] for row in rows] == ["b"]
    assert {item["metric"] for item in rows[0]["metrics"]} == {"faithfulness", "context_precision"}
    assert all(not item["passed"] for item in rows[0]["metrics"])


def test_metric_variance_reports_median_min_max_and_range():
    reports = [
        make_report({"a": True}),
        make_report({"a": False}),
        make_report({"a": True}),
    ]
    stats = RepeatReport(
        layer="L3", judge_model="m", case_set="cases.jsonl", reports=reports
    ).metric_variance()["faithfulness"]
    assert stats["median"] == 0.95
    assert stats["min"] == 0.10
    assert stats["max"] == 0.95
    assert stats["range"] == pytest.approx(0.85)


def test_errors_are_counted_but_do_not_enter_the_mean():
    reports = [
        make_report({"a": True, "b": True}, error_cases=("b",)),
        make_report({"a": True}),
    ]
    combined = RepeatReport(layer="L3", judge_model="m", case_set="cases.jsonl", reports=reports)
    payload = combined.to_dict()
    assert payload["errors"] == 2  # 两条指标行
    assert payload["variance"]["metrics"]["faithfulness"]["median"] == pytest.approx(0.95)
    assert len(payload["runs"][0]["by_metric"]) == 2


def test_payload_is_json_serialisable_and_carries_the_repeat_count():
    reports = [make_report({"a": True}), make_report({"a": True})]
    payload = RepeatReport(
        layer="L4", judge_model="m", case_set="rag.jsonl", reports=reports
    ).to_dict()
    assert payload["repeat"] == 2
    assert payload["case_set"] == "rag.jsonl"
    json.dumps(payload, ensure_ascii=False, allow_nan=False)


def test_markdown_names_the_flaky_section_and_both_headline_numbers():
    reports = [make_report({"a": True, "b": True}), make_report({"a": True, "b": False})]
    markdown = RepeatReport(
        layer="L4", judge_model="m", case_set="rag.jsonl", reports=reports
    ).to_markdown()
    assert "中位数" in markdown
    assert "最差一次" in markdown
    assert "墙头草用例" in markdown
    assert "`b`" in markdown
    # 表头 2 列（轮次 / 通过率）+ 指标列 + 2 列（错误 / 花费），分隔行必须同宽。
    separator = next(line for line in markdown.splitlines() if line.startswith("| --- |"))
    assert separator.count("---") == 2 + 2 + 2


def test_budget_error_is_not_swallowed_while_recording_usage():
    """预算熔断被吞掉，就会变成「已经超支还在继续打分」。"""
    from agentevalops.judging.llm import record_openai_usage

    class Ledger:
        def record(self, model, usage):
            raise BudgetExceededError("budget exceeded")

    response = SimpleNamespace(
        model="deepseek-flash",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    )
    with pytest.raises(BudgetExceededError):
        record_openai_usage(Ledger(), response)


def test_a_regular_bookkeeping_failure_is_still_swallowed():
    from agentevalops.judging.llm import record_openai_usage

    class Ledger:
        def record(self, model, usage):
            raise ValueError("unknown price")

    response = SimpleNamespace(
        model="deepseek-flash",
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    )
    record_openai_usage(Ledger(), response)  # 不抛：分数是主产物，成本是附属信息
