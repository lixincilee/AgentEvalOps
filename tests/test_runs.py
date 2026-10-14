"""Run aggregation, slicing, and persistence."""

from __future__ import annotations

import json

import pytest

from agentevalops.runs import CaseResult, Run, RunMetrics, new_run_id
from agentevalops.versioning import build_manifest


def _result(
    case_id: str,
    passed: bool,
    *,
    layer: str = "L1",
    category: str = "normal",
    cost: float = 0.0,
    tokens: int = 0,
    failures: tuple[str, ...] = (),
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        passed=passed,
        layer=layer,
        category=category,
        failures=failures,
        cost_usd=cost,
        total_tokens=tokens,
    )


def test_case_result_round_trips_through_dict():
    original = CaseResult(
        case_id="c1",
        passed=False,
        layer="L5",
        category="safety",
        failures=("工具未被正确调用",),
        latency_ms=12.5,
        cost_usd=0.001,
        total_tokens=42,
        trace_id="tr-1",
    )
    assert CaseResult.from_dict(original.to_dict()) == original


def test_metrics_on_an_empty_run_are_all_zero():
    metrics = RunMetrics.from_results([])
    assert (metrics.total, metrics.passed, metrics.failed) == (0, 0, 0)
    assert metrics.pass_rate == 0.0
    assert metrics.cost_usd == 0.0
    assert metrics.cost_per_success_usd is None


def test_metrics_pass_rate_and_cost_per_success():
    metrics = RunMetrics.from_results(
        [
            _result("a", True, cost=0.002, tokens=100),
            _result("b", True, cost=0.004, tokens=200),
            _result("c", False, cost=0.001, tokens=50),
        ]
    )
    assert metrics.total == 3
    assert metrics.passed == 2
    assert metrics.failed == 1
    assert metrics.pass_rate == pytest.approx(2 / 3)
    assert metrics.cost_usd == pytest.approx(0.007)
    assert metrics.total_tokens == 350
    assert metrics.cost_per_success_usd == pytest.approx(0.0035)


def test_cost_per_success_is_none_when_nothing_passed():
    metrics = RunMetrics.from_results([_result("a", False, cost=0.01)])
    assert metrics.cost_per_success_usd is None


def test_metrics_slice_by_layer_and_category():
    metrics = RunMetrics.from_results(
        [
            _result("a", True, layer="L1", category="normal"),
            _result("b", False, layer="L1", category="safety"),
            _result("c", True, layer="L1", category="safety"),
            _result("d", False, layer="L5", category="safety"),
        ]
    )
    assert metrics.by_layer["L1"] == pytest.approx(2 / 3)
    assert metrics.by_layer["L5"] == 0.0
    assert metrics.by_category["normal"] == 1.0
    assert metrics.by_category["safety"] == pytest.approx(1 / 3)


def test_filter_and_failure_helpers():
    run = Run(run_id="r1")
    run.add(_result("a", True, layer="L1"))
    run.add(_result("b", False, layer="L5", category="safety"))
    assert [result.case_id for result in run.failures()] == ["b"]
    assert [result.case_id for result in run.filter(layer="L1")] == ["a"]
    assert [result.case_id for result in run.filter(passed=False)] == ["b"]
    assert run.filter(category="nope") == []


def test_finalize_stamps_the_run_and_returns_metrics():
    run = Run(run_id="r1")
    run.add(_result("a", True))
    metrics = run.finalize()
    assert run.finished_at is not None
    assert metrics.passed == 1
    assert run.metrics.passed == 1


def test_run_saves_and_loads_round_trip(tmp_path):
    manifest = build_manifest(
        model_id="deepseek-chat", prompt_text="p", cases=[{"case_id": "a"}]
    )
    run = Run.start(manifest, run_id="fixed-run")
    run.add(_result("a", True, cost=0.001, tokens=10))
    run.add(_result("b", False, failures=("意图不匹配",)))
    run.finalize()

    path = run.save(tmp_path / "runs" / "run.json")
    restored = Run.load(path)

    assert path.is_file()
    assert restored.run_id == "fixed-run"
    assert restored.to_dict() == run.to_dict()
    assert restored.manifest is not None
    assert restored.manifest.manifest_id == manifest.manifest_id
    assert restored.metrics.passed == 1


def test_run_json_is_utf8_without_ascii_escapes(tmp_path):
    run = Run.start(run_id="r-utf8")
    run.add(_result("订单问题", False, failures=("未识别意图",)))
    text = run.save(tmp_path / "run.json").read_text(encoding="utf-8")
    assert "未识别意图" in text
    assert "\\u" not in text


def test_new_run_id_includes_the_manifest_prefix():
    manifest = build_manifest(model_id="m", prompt_text="p")
    stamp = "20260101T000000Z"
    assert new_run_id(manifest, moment=stamp) == f"{stamp}-{manifest.manifest_id[:8]}"
    assert new_run_id(None, moment=stamp).endswith("nomanifest")


def test_run_without_manifest_serialises_as_null():
    payload = Run.start(run_id="r-none").to_dict()
    assert payload["manifest"] is None
    assert json.loads(json.dumps(payload))["manifest"] is None
