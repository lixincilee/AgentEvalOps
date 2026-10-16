"""回归门禁的判据测试：全是离线纯逻辑，不起子进程、不碰网络。

守的是三件事：门禁**该拦的拦得住**、**不该拦的别拦**（否则会训练人忽略它）、
以及缺失指标不会被悄悄当成 0。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str) -> Any:
    """``scripts/`` 不是包，所以按文件路径加载模块。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("regression_gate", "regression_gate.py")


BASE_METRICS: dict[str, Any] = {
    "pytest.passed": 1025,
    "pytest.failed": 0,
    "pytest.xfailed": 10,
    "fault.pass_rate": 1.0,
    "fault.cases": 25,
    "matrix.v3_rules.pass_rate": 0.9671,
    "rag_multiturn.v3_rules.pass_rate": 1.0,
    "perf.http_error_rate": 0.0,
    "perf.task_success_rate": 1.0,
    "perf.p95_ms": 480.0,
    "perf.cost_per_success_cny": 0.00076,
}


def write_release(directory: Path, tag: str, metrics: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "release.json").write_text(
        json.dumps(
            {
                "kind": "release_index",
                "tag": tag,
                "commit": "abcdef0",
                "dirty": False,
                "metrics": metrics,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return directory


def metric_rule(metric: str) -> Any:
    return next(rule for rule in gate.RULES if rule.metric == metric)


def verdict_for(metric: str, candidate: float | None, baseline: float | None) -> Any:
    return gate.evaluate(metric_rule(metric), candidate, baseline)


def test_every_enforced_rule_has_a_criterion() -> None:
    """强制规则不能既没有绝对界、也没有相对容忍——那样它永远判不出东西。"""
    for rule in gate.RULES:
        if rule.enforce:
            assert rule.bound is not None or rule.tolerance is not None, rule.metric


def test_rule_metric_names_are_unique() -> None:
    names = [rule.metric for rule in gate.RULES]
    assert len(names) == len(set(names))


def test_gate_passes_when_candidate_equals_baseline(tmp_path: Path) -> None:
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", dict(BASE_METRICS)))
    baseline = gate.load_release(write_release(tmp_path / "b", "v0.6.0", dict(BASE_METRICS)))
    summary = gate.run_gate(candidate, [baseline])
    assert summary["passed"] is True
    assert summary["failure_count"] == 0


def test_absolute_floor_blocks_without_any_baseline(tmp_path: Path) -> None:
    """没有基线也要能拦：绝对下限防的是「基线烂，于是烂变成了标准」。"""
    metrics = dict(BASE_METRICS, **{"matrix.v3_rules.pass_rate": 0.42})
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", metrics))
    summary = gate.run_gate(candidate, [])
    assert summary["passed"] is False
    assert any("绝对下限" in item for item in summary["failures"])


def test_floor_is_reported_before_baseline_drift() -> None:
    """两条判据都踩线时，先说绝对下限——相对漂移在指标本身不可接受时会分散注意力。"""
    verdict = verdict_for("matrix.v3_rules.pass_rate", 0.10, 0.99)
    assert verdict.status == "fail"
    assert "绝对下限" in verdict.reason


def test_rate_tolerance_is_absolute_percentage_points() -> None:
    """通过率的容忍是「个百分点」：0.01 就是 1 个点，和基线高低无关。"""
    assert verdict_for("matrix.v3_rules.pass_rate", 0.9600, 0.9671).status == "pass"
    assert verdict_for("matrix.v3_rules.pass_rate", 0.9500, 0.9671).status == "fail"


def test_noisy_metric_tolerance_is_relative() -> None:
    """延迟的容忍是「百分比」：+45% 还行，+87% 就该提醒。"""
    assert verdict_for("perf.p95_ms", 700.0, 480.0).status == "pass"
    assert verdict_for("perf.p95_ms", 900.0, 480.0).status == "warn"


def test_advisory_rule_warns_but_never_blocks(tmp_path: Path) -> None:
    metrics = dict(BASE_METRICS, **{"perf.p95_ms": 9000.0})
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", metrics))
    baseline = gate.load_release(write_release(tmp_path / "b", "v0.6.0", dict(BASE_METRICS)))
    summary = gate.run_gate(candidate, [baseline])
    assert summary["passed"] is True
    assert summary["warning_count"] >= 1
    assert any("perf.p95_ms" in item for item in summary["warnings"])


def test_improvement_is_not_a_regression(tmp_path: Path) -> None:
    metrics = dict(BASE_METRICS, **{"rag_multiturn.v3_rules.pass_rate": 1.0,
                                    "perf.p95_ms": 100.0})
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", metrics))
    baseline = gate.load_release(
        write_release(tmp_path / "b", "v0.6.0",
                      dict(BASE_METRICS, **{"rag_multiturn.v3_rules.pass_rate": 0.5}))
    )
    summary = gate.run_gate(candidate, [baseline])
    assert summary["passed"] is True


def test_missing_enforced_metric_blocks(tmp_path: Path) -> None:
    metrics = {k: v for k, v in BASE_METRICS.items() if k != "fault.pass_rate"}
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", metrics))
    summary = gate.run_gate(candidate, [])
    assert summary["passed"] is False
    assert any("没有这个指标" in item for item in summary["failures"])


def test_null_metric_reads_as_missing_not_zero(tmp_path: Path) -> None:
    """``null`` 是「这次没测出来」。当成 0 会凭空造出一个失败。"""
    metrics = dict(BASE_METRICS, **{"perf.p95_ms": None})
    release = gate.load_release(write_release(tmp_path / "c", "candidate", metrics))
    assert release.metric("perf.p95_ms") is None
    assert "perf.p95_ms" not in release.metrics


def test_zero_baseline_skips_relative_check() -> None:
    """基线是 0 时相对判据没有定义，不能除零，也不该硬判失败。"""
    verdict = verdict_for("perf.cost_per_success_cny", 5.0, 0.0)
    assert verdict.status == "pass"
    assert "相对判据不适用" in verdict.reason


def test_last_baseline_is_the_enforcement_one(tmp_path: Path) -> None:
    """按时间顺序传多个基线时，最后一个执法；但每一个都要出现在对比表里。"""
    old = gate.load_release(write_release(tmp_path / "old", "v0.5.1", dict(BASE_METRICS)))
    new = gate.load_release(write_release(tmp_path / "new", "v0.6.0", dict(BASE_METRICS)))
    candidate = gate.load_release(
        write_release(tmp_path / "c", "candidate",
                      dict(BASE_METRICS, **{"matrix.v3_rules.pass_rate": 0.93}))
    )
    summary = gate.run_gate(candidate, [old, new])
    assert summary["enforcement_baseline"] == "v0.6.0"
    assert [entry["tag"] for entry in summary["metrics"]["matrix.v3_rules.pass_rate"]["series"]] == [
        "v0.5.1",
        "v0.6.0",
    ]


def test_load_release_rejects_a_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-release"
    plain.mkdir()
    with pytest.raises(gate.GateUsageError):
        gate.load_release(plain)


def test_load_release_rejects_the_wrong_kind(tmp_path: Path) -> None:
    directory = tmp_path / "wrong"
    directory.mkdir()
    (directory / "release.json").write_text(
        json.dumps({"kind": "version_comparison"}), encoding="utf-8"
    )
    with pytest.raises(gate.GateUsageError):
        gate.load_release(directory)


def test_load_release_rejects_broken_json(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    directory.mkdir()
    (directory / "release.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(gate.GateUsageError):
        gate.load_release(directory)


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """0 通过 / 1 退化 / 2 用法问题——CI 靠这三个数字决定红绿。"""
    baseline = write_release(tmp_path / "b", "v0.6.0", dict(BASE_METRICS))
    same = write_release(tmp_path / "same", "candidate", dict(BASE_METRICS))
    assert gate.main(["--candidate", str(same), "--baseline", str(baseline),
                      "--out", str(tmp_path / "r1.json")]) == 0

    drift = write_release(tmp_path / "drift", "candidate",
                          dict(BASE_METRICS, **{"fault.pass_rate": 0.5}))
    assert gate.main(["--candidate", str(drift), "--baseline", str(baseline),
                      "--out", str(tmp_path / "r2.json")]) == 1

    assert gate.main(["--candidate", str(tmp_path / "nowhere")]) == 2
    capsys.readouterr()


def test_report_names_the_candidate_and_baseline(tmp_path: Path) -> None:
    candidate = gate.load_release(write_release(tmp_path / "c", "candidate", dict(BASE_METRICS)))
    baseline = gate.load_release(write_release(tmp_path / "b", "v0.6.0", dict(BASE_METRICS)))
    summary = gate.run_gate(candidate, [baseline])
    assert summary["kind"] == "regression_gate"
    assert summary["candidate"]["tag"] == "candidate"
    assert summary["baselines"][0]["tag"] == "v0.6.0"


def _release_with_checks(directory: Path, tag: str, metrics: dict[str, Any],
                         checks: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "release.json").write_text(
        json.dumps({"kind": "release_index", "tag": tag, "commit": "abcdef0",
                    "dirty": False, "metrics": metrics, "checks": checks},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return directory


def test_skipped_check_downgrades_block_to_warning(tmp_path: Path) -> None:
    """「本次没跑这项检查」不该阻断版本——否则 --skip-load 的候选永远红。"""
    metrics = {k: v for k, v in BASE_METRICS.items() if not k.startswith("perf.")}
    candidate = gate.load_release(
        _release_with_checks(tmp_path / "c", "candidate", metrics,
                             [{"name": "load_test", "status": "skipped"}])
    )
    summary = gate.run_gate(candidate, [])
    assert summary["passed"] is True
    assert summary["skipped_checks"] == ["load_test"]
    assert any("load_test" in item for item in summary["warnings"])
    assert not any("load_test" in item for item in summary["failures"])


def test_missing_metric_still_blocks_when_the_check_did_run(tmp_path: Path) -> None:
    """检查跑了却没产出这个数 → 照样阻断：那是报告坏了，不是没测。"""
    metrics = {k: v for k, v in BASE_METRICS.items() if not k.startswith("perf.")}
    candidate = gate.load_release(
        _release_with_checks(tmp_path / "c", "candidate", metrics,
                             [{"name": "load_test", "status": "passed", "metrics": {}}])
    )
    summary = gate.run_gate(candidate, [])
    assert summary["passed"] is False
    assert any("没有这个指标" in item for item in summary["failures"])
