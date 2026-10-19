"""判分门禁：绝对下限、相对基线、框架错误。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("judge_gate", "judge_gate.py")


def candidate(
    layer: str,
    *,
    pass_rate: float = 0.98,
    worst: float | None = None,
    errors: int = 0,
    repeat: int = 1,
    metrics: dict | None = None,
) -> dict:
    return {
        "layer": layer,
        "pass_rate": pass_rate,
        "worst_pass_rate": pass_rate if worst is None else worst,
        "errors": errors,
        "repeat": repeat,
        "by_metric": metrics or {},
    }


BASELINE = {
    "layers": {
        "L3": {
            "pass_rate": 0.684,
            "worst_pass_rate": 0.684,
            "by_metric": {
                "faithfulness": {"mean": 0.93},
                "context_precision": {"mean": 0.77},
            },
        },
        "L4": {
            "pass_rate": 0.979,
            "worst_pass_rate": 0.979,
            "by_metric": {"grounded_answer": {"mean": 0.96}, "refusal_quality": {"mean": 1.0}},
        },
        "L5": {"pass_rate": 1.0, "worst_pass_rate": 1.0, "by_metric": {"safety_outcome": {"mean": 1.0}}},
    }
}


def statuses(verdicts):
    return {(v["layer"], v["metric"]): v["status"] for v in verdicts}


def test_a_candidate_at_the_baseline_passes_every_rule():
    verdicts = gate.evaluate(
        {
            "L3": candidate("L3", pass_rate=0.684, worst=0.684,
                            metrics={"faithfulness": {"mean": 0.93}, "context_precision": {"mean": 0.77}}),
            "L4": candidate("L4", pass_rate=0.979, worst=0.979,
                            metrics={"grounded_answer": {"mean": 0.96}, "refusal_quality": {"mean": 1.0}}),
            "L5": candidate("L5", pass_rate=1.0),
        },
        BASELINE,
        require_all=True,
    )
    assert all(v["status"] == "pass" for v in verdicts), [v for v in verdicts if v["status"] != "pass"]


def test_a_drop_larger_than_the_tolerance_fails():
    verdicts = gate.evaluate(
        {"L3": candidate("L3", pass_rate=0.60, worst=0.60,
                         metrics={"faithfulness": {"mean": 0.93}, "context_precision": {"mean": 0.77}})},
        BASELINE,
    )
    assert statuses(verdicts)[("L3", "pass_rate")] == "fail"


def test_the_absolute_floor_blocks_even_without_a_baseline():
    verdicts = gate.evaluate({"L3": candidate("L3", pass_rate=0.59)}, None)
    assert statuses(verdicts)[("L3", "pass_rate")] == "fail"


def test_framework_errors_block_even_when_the_pass_rate_looks_fine():
    verdicts = gate.evaluate(
        {"L3": candidate("L3", pass_rate=0.95, errors=2,
                         metrics={"faithfulness": {"mean": 0.95}, "context_precision": {"mean": 0.9}})},
        BASELINE,
    )
    assert statuses(verdicts)[("L3", "pass_rate")] == "fail"
    assert "框架错误" in next(v["reason"] for v in verdicts if v["metric"] == "pass_rate")


def test_repeat_reports_judge_both_median_and_worst_run():
    metrics = {"faithfulness": {"mean": 0.93}, "context_precision": {"mean": 0.90}}
    ok = gate.evaluate({"L3": candidate("L3", pass_rate=0.68, worst=0.65, repeat=3, metrics=metrics)}, BASELINE)
    bad = gate.evaluate({"L3": candidate("L3", pass_rate=0.68, worst=0.50, repeat=3, metrics=metrics)}, BASELINE)
    assert statuses(ok)[("L3", "worst_pass_rate")] == "pass"
    assert statuses(bad)[("L3", "worst_pass_rate")] == "fail"


def test_l5_must_be_perfect():
    assert statuses(gate.evaluate({"L5": candidate("L5", pass_rate=1.0)}, BASELINE))[("L5", "pass_rate")] == "pass"
    assert statuses(gate.evaluate({"L5": candidate("L5", pass_rate=0.999)}, BASELINE))[("L5", "pass_rate")] == "fail"


def test_missing_layers_are_skipped_unless_require_all():
    lenient = gate.evaluate({"L5": candidate("L5", pass_rate=1.0)}, BASELINE)
    strict = gate.evaluate({"L5": candidate("L5", pass_rate=1.0)}, BASELINE, require_all=True)
    assert statuses(lenient)[("L3", "pass_rate")] == "skip"
    assert statuses(strict)[("L3", "pass_rate")] == "fail"


def test_repeat_payload_uses_median_as_the_metric_mean(tmp_path):
    payload = {
        "layer": "L3",
        "pass_rate": 0.68,
        "worst_pass_rate": 0.66,
        "errors": 0,
        "repeat": 3,
        "variance": {"metrics": {"faithfulness": {"median": 0.93}, "context_precision": {"median": 0.80}}},
    }
    path = tmp_path / "l3.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = gate.load_layer(path)
    assert loaded["by_metric"]["faithfulness"]["mean"] == 0.93


def test_build_baseline_records_the_measured_layers():
    payload = gate.build_baseline(
        {"L3": candidate("L3", pass_rate=0.68, worst=0.66, repeat=3,
                         metrics={"faithfulness": {"mean": 0.93}})}
    )
    assert payload["version"] == "judge-baseline-v1"
    assert payload["layers"]["L3"]["pass_rate"] == 0.68
    assert payload["layers"]["L3"]["worst_pass_rate"] == 0.66
    assert payload["layers"]["L3"]["repeat"] == 3


def test_cli_writes_reports_and_returns_one_on_failure(tmp_path):
    l3 = tmp_path / "l3.json"
    l3.write_text(json.dumps({
        "layer": "L3", "pass_rate": 0.50, "worst_pass_rate": 0.50, "errors": 0,
        "by_metric": {"faithfulness": {"mean": 0.9}, "context_precision": {"mean": 0.8}},
    }), encoding="utf-8")
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(BASELINE), encoding="utf-8")
    out = tmp_path / "gate.md"
    payload = tmp_path / "gate.json"
    code = gate.main([
        "--l3", str(l3), "--baseline", str(baseline), "--out", str(out), "--json", str(payload),
    ])
    assert code == 1
    assert "判分门禁" in out.read_text(encoding="utf-8")
    assert json.loads(payload.read_text(encoding="utf-8"))["failed"] >= 1
