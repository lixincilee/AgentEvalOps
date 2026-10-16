"""``scripts/variance.py`` 的测试：纯算术，无依赖、无 I/O。

这个模块的作用是**阻止把噪声写成结论**，所以它的默认答案必须是「不显著」。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


variance = _load("variance", "variance.py")


def test_aggregate_of_nothing_is_none() -> None:
    """「没有测量」不是「测量结果是 0」。"""
    assert variance.aggregate([]) is None


def test_single_measurement_has_no_spread() -> None:
    result = variance.aggregate([42.0])
    assert result is not None
    assert result.count == 1
    assert result.median == 42.0
    assert result.spread == 0.0
    assert result.stdev == 0.0


@pytest.mark.parametrize(
    ("values", "median"),
    [([1.0, 2.0, 3.0], 2.0), ([1.0, 2.0, 3.0, 100.0], 2.5), ([5.0], 5.0)],
)
def test_median(values: list[float], median: float) -> None:
    result = variance.aggregate(values)
    assert result is not None
    assert result.median == median


def test_spread_is_the_range() -> None:
    result = variance.aggregate([10.0, 30.0, 20.0])
    assert result is not None
    assert result.spread == 20.0
    assert result.spread_pct == pytest.approx(1.0)
    assert result.minimum == 10.0
    assert result.maximum == 30.0


def test_median_zero_makes_relative_spread_undefined_not_zero() -> None:
    """0 附近的抖动不是「0% 的波动」而是无定义。给 0.0 会被下游读成「稳如磐石」。"""
    result = variance.aggregate([0.0, 0.0, 5.0])   # 中位数是 0，极差是 5
    assert result is not None
    assert result.median == 0.0
    assert result.spread == 5.0
    assert result.spread_pct is None


def test_summarize_runs_collects_each_metric() -> None:
    runs = [{"p95_ms": 10.0, "rps": 1.0}, {"p95_ms": 30.0, "rps": 3.0}]
    summary = variance.summarize_runs(runs, ("p95_ms", "rps"))
    assert summary["p95_ms"]["values"] == [10.0, 30.0]
    assert summary["p95_ms"]["median"] == 20.0
    assert summary["rps"]["max"] == 3.0


def test_summarize_runs_skips_missing_values_instead_of_zero_padding() -> None:
    """补 0 会把「没测到」变成一个极低的极值，直接把极差撑大。"""
    runs = [{"p95_ms": 100.0}, {"p95_ms": None}, {"p95_ms": 120.0}]
    summary = variance.summarize_runs(runs, ("p95_ms",))
    assert summary["p95_ms"]["values"] == [100.0, 120.0]
    assert summary["p95_ms"]["count"] == 2
    assert summary["p95_ms"]["min"] == 100.0


def test_summarize_runs_reports_zero_count_for_a_metric_never_measured() -> None:
    summary = variance.summarize_runs([{"a": 1.0}], ("missing",))
    assert summary["missing"]["count"] == 0
    assert summary["missing"]["values"] == []


def test_difference_is_interpretable_only_when_it_beats_the_noise() -> None:
    assert variance.difference_is_interpretable(0.10, 0.02, 0.03) is True
    assert variance.difference_is_interpretable(0.05, 0.02, 0.03) is True
    assert variance.difference_is_interpretable(0.03, 0.02, 0.03) is False
    assert variance.difference_is_interpretable(0.01, 0.02, 0.03) is False


def test_zero_difference_is_never_interpretable() -> None:
    """确定性链路的差就是 0，无论噪声多小都不该被报成「显著」。"""
    assert variance.difference_is_interpretable(0.0, 0.0, 0.0) is False


def test_difference_uses_the_larger_of_the_two_spreads() -> None:
    """两边噪声不一样时，取大的那个：否则会在噪声大的那一侧误报。"""
    assert variance.difference_is_interpretable(0.04, 0.01, 0.10) is False


def test_spread_of_missing_metric_is_zero() -> None:
    assert variance.spread_of({"p95_ms": {"spread": 12.0}}, "p95_ms") == 12.0
    assert variance.spread_of({}, "p95_ms") == 0.0
    assert variance.spread_of({"p95_ms": {"values": []}}, "p95_ms") == 0.0


def test_to_dict_is_json_safe() -> None:
    import json

    result = variance.aggregate([1.0, 2.0, 3.0])
    assert result is not None
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["count"] == 3
    assert payload["median"] == 2.0