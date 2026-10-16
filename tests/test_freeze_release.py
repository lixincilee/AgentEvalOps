"""冻结脚本的纯函数测试：解析、拍平、渲染。不起子进程、不跑 pytest。

真跑一遍 ``freeze_release.py`` 要一分钟起（压测占大头），那是 CI 的活，不是单元测试的活。
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


freeze = _load("freeze_release", "freeze_release.py")


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1025 passed, 10 xfailed in 16.28s", {"passed": 1025, "xfailed": 10}),
        ("1 failed, 3 passed in 1.02s", {"failed": 1, "passed": 3}),
        ("2 errors", {"error": 2}),
        ("1 error, 4 passed", {"error": 1, "passed": 4}),
        ("7 skipped, 2 deselected in 0.30s", {"skipped": 7, "deselected": 2}),
        ("no tests ran in 0.01s", {}),
        ("", {}),
    ],
)
def test_parse_pytest_counts(output: str, expected: dict[str, int]) -> None:
    assert freeze.parse_pytest_counts(output) == expected


def test_parse_pytest_counts_sums_duplicate_words() -> None:
    """pytest 有时分两处报同一个词，计数必须累加而不是覆盖。"""
    assert freeze.parse_pytest_counts("1 failed, 1 failed, 5 passed")["failed"] == 2


def test_parse_pytest_counts_normalises_plurals() -> None:
    """``errors`` 必须落到 ``error`` 这个键上，且不能再留一个复数键：两个键会让
    「读 failed + error」的下游漏掉一个。"""
    counts = freeze.parse_pytest_counts("3 errors")
    assert counts == {"error": 3}
    assert "errors" not in counts


def test_collect_metrics_flattens_and_sorts() -> None:
    checks = [
        {"metrics": {"pytest.failed": 0, "pytest.passed": 1025}},
        {"metrics": {"fault.pass_rate": 1.0, "fault.cases": None}},
    ]
    assert freeze.collect_metrics(checks) == {
        "fault.cases": None,
        "fault.pass_rate": 1.0,
        "pytest.failed": 0.0,
        "pytest.passed": 1025.0,
    }


def test_collect_metrics_keeps_null_distinct_from_zero() -> None:
    """``None``（没测出来）与 ``0.0``（测出来是零）必须是两个不同的值。"""
    metrics = freeze.collect_metrics([{"metrics": {"a": None, "b": 0}}])
    assert metrics["a"] is None
    assert metrics["b"] == 0.0


def test_column_pass_rate_returns_none_for_unavailable_column() -> None:
    """离线模式下模型列在报告里是 ``None``，不是缺字段——别把它当 0 分。"""
    report = {"versions": {"v1": {"metrics": {"pass_rate": 0.9}}, "v3": None}}
    assert freeze._column_pass_rate(report, "v1") == 0.9
    assert freeze._column_pass_rate(report, "v3") is None
    assert freeze._column_pass_rate(report, "nope") is None


def test_render_markdown_states_the_version_and_dirtiness() -> None:
    index = {
        "tag": "v0.6.0",
        "commit": "1fd5135",
        "describe": "v0.6.0",
        "spec_version": "1.3",
        "frozen_at": "2026-10-16T00:00:00+00:00",
        "dirty": True,
        "checks": [
            {"name": "pytest", "status": "passed", "seconds": 16.3, "detail": "1025 passed"},
            {"name": "faults", "status": "failed", "seconds": 9.1, "detail": "24/25"},
        ],
        "metrics": {"fault.pass_rate": 0.96, "perf.p95_ms": None},
    }
    text = freeze.render_markdown(index)
    assert "v0.6.0" in text
    assert "1fd5135" in text
    assert "脏" in text
    assert "**失败**" in text
    assert "| `perf.p95_ms` | n/a |" in text


def test_case_paths_point_at_real_files() -> None:
    """冻结跑的用例集必须真的存在，否则一到 CI 才发现是断链。"""
    for relative in (freeze.CASE_MATRIX, freeze.RAG_CASES, freeze.MULTITURN_CASES):
        assert (REPO_ROOT / relative).is_file(), relative


def test_spec_version_is_readable() -> None:
    """规格版本是给人看的锚点，读不到就是 None——但仓库里现在应该读得到。"""
    assert freeze.spec_version()


def test_is_dirty_returns_a_bool() -> None:
    assert isinstance(freeze.is_dirty(), bool)