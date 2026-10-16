"""``load_test.py --repeat`` 的汇总口径测试：纯函数，不起服务、不跑 locust。

守的是一条最容易搞错、后果又最严重的规则：

**延迟取中位数，错误率与成功率取最坏值。**

反过来（错误率也取中位数）会造出一个安静的漏洞：三次运行里有一次 5% 的错误率、
两次 0%，中位数是 0% —— 门禁就报了「通过」。一次真实故障被两次正常运行平均掉了。
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


load_test = _load("load_test", "load_test.py")


def make_run(
    *,
    requests: int = 1000,
    service_requests: int | None = None,
    http_failures: int = 0,
    p95_ms: float = 100.0,
    rps: float = 100.0,
    http_error_rate: float = 0.0,
    task_success_rate: float | None = 1.0,
    cost_per_success_cny: float = 0.001,
    consistent: bool = True,
) -> dict[str, Any]:
    service = requests if service_requests is None else service_requests
    return {
        "requests": requests,
        "service_requests": service,
        "http_failures": http_failures,
        "rps": rps,
        "p50_ms": p95_ms / 2,
        "p95_ms": p95_ms,
        "p99_ms": p95_ms * 2,
        "avg_ms": p95_ms,
        "http_error_rate": http_error_rate,
        "task_success_rate": task_success_rate,
        "tokens_per_request": 290.0,
        "cost_per_success_cny": cost_per_success_cny,
        "budget_exceeded": False,
        "notes": [],
        "locust": {
            "requests": requests,
            "http_failures": http_failures,
            "requests_delta": service - requests,
            "consistent_with_service": consistent,
        },
    }


def test_latency_takes_the_median() -> None:
    runs = [make_run(p95_ms=value) for value in (100.0, 200.0, 900.0)]
    assert load_test._aggregate(runs)["p95_ms"] == 200.0


def test_error_rate_takes_the_worst_not_the_median() -> None:
    """三次里有一次 5% —— 中位数是 0%，但门禁必须看见那个 5%。"""
    runs = [
        make_run(http_error_rate=0.0, http_failures=0),
        make_run(http_error_rate=0.05, http_failures=50),
        make_run(http_error_rate=0.0, http_failures=0),
    ]
    summary = load_test._aggregate(runs)
    assert summary["http_error_rate"] == 0.05
    assert summary["gate"]["passed"] is False


def test_task_success_takes_the_worst_not_the_median() -> None:
    runs = [
        make_run(task_success_rate=1.0),
        make_run(task_success_rate=0.90),
        make_run(task_success_rate=1.0),
    ]
    summary = load_test._aggregate(runs)
    assert summary["task_success_rate"] == 0.90
    assert summary["gate"]["passed"] is False


def test_gate_is_recomputed_from_the_aggregate() -> None:
    """汇总改写了顶层数字，门禁必须跟着重算。

    否则会出现「报告正文写着 5% 错误率、门禁那一行写着通过」——自相矛盾的报告比
    没有报告更糟，因为它看起来是可信的。
    """
    runs = [
        make_run(http_error_rate=0.0),
        make_run(http_error_rate=0.08, http_failures=80),
    ]
    summary = load_test._aggregate(runs)
    assert summary["gate"]["http_error_rate"] == summary["http_error_rate"]
    assert summary["gate"]["passed"] is False


def test_both_counts_use_the_same_statistic() -> None:
    """两侧计数都取中位数。只换一侧会造出「locust 649 / 服务端 686」这种假差异。"""
    runs = [
        make_run(requests=100, service_requests=101),
        make_run(requests=200, service_requests=201),
        make_run(requests=300, service_requests=301),
    ]
    summary = load_test._aggregate(runs)
    assert summary["requests"] == 200
    assert summary["service_requests"] == 201


def test_per_run_reconciliation_is_kept() -> None:
    """对账是每一次运行各自的性质，所以逐次留明细，而不是拍成一个布尔值。"""
    runs = [
        make_run(consistent=True),
        make_run(requests=1000, service_requests=1200, consistent=False),
    ]
    block = load_test._aggregate(runs)["locust"]
    assert block["consistent_with_service"] is False
    assert [entry["consistent_with_service"] for entry in block["consistency_by_run"]] == [
        True,
        False,
    ]
    assert block["consistency_by_run"][1]["requests_delta"] == 200


def test_every_run_is_kept_in_the_report() -> None:
    runs = [make_run(rps=value) for value in (10.0, 20.0, 30.0)]
    summary = load_test._aggregate(runs)
    assert summary["repeat"] == 3
    assert len(summary["runs"]) == 3
    assert summary["variance"]["rps"]["values"] == [10.0, 20.0, 30.0]
    assert summary["variance"]["rps"]["spread"] == 20.0


def test_aggregate_notes_state_the_policy() -> None:
    summary = load_test._aggregate([make_run(), make_run()])
    assert summary["aggregate_policy"]["central_statistic"] == "median"
    assert summary["aggregate_policy"]["worst_statistic"] == {
        "http_error_rate": "max",
        "task_success_rate": "min",
    }
    assert any("中位数" in note for note in summary["notes"])


@pytest.mark.parametrize(
    ("value", "expected"),
    [(649.0, 649), (39.5, 39.5), (0.0, 0), (7, 7), (None, None)],
)
def test_num_formats_integral_floats_as_ints(value: Any, expected: Any) -> None:
    """``649.0`` 看着像测量精度，其实只是中位数算出来的浮点数。"""
    assert load_test._num(value) == expected


def test_gate_payload_enforces_both_lines() -> None:
    assert load_test._gate_payload(0.0, 1.0, False)["passed"] is True
    assert load_test._gate_payload(0.02, 1.0, False)["passed"] is False   # 错误率超线
    assert load_test._gate_payload(0.0, 0.95, False)["passed"] is False   # 成功率不足
    assert load_test._gate_payload(0.0, 1.0, True)["passed"] is False     # 预算熔断
    assert load_test._gate_payload(0.0, None, False)["passed"] is False   # 没测出来


def test_repeat_zero_is_rejected_before_anything_starts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--repeat 0`` 是用法错误，必须在起服务之前就被挡下（否则它会跑 0 次然后报「没数据」）。"""
    assert load_test.main(["--repeat", "0", "--users", "1"]) == 2
    assert "至少为 1" in capsys.readouterr().out