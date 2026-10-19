"""L3/L4 的退出码语义：测量失败 ≠ 流水线失败。

`judge-real` 里 L3 通过率 64.5% 时脚本返回 1，job 在跑到 `judge_gate.py` 之前就停了。
门禁读的是报告与基线，L3/L4 只负责「把数测出来」。所以 CI 用 `--allow-failures`：

* 指标没达标 → 报告照写，退出码 0；
* 框架报错（errored_rows）→ 仍然退出码 1，不能把测量事故伪装成正常；
* 不带该参数时 → 保持本地直觉：有失败就返回 1。
"""

from __future__ import annotations

import sys

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import judge_l3_ragas as l3  # noqa: E402
import judge_l4_deepeval as l4  # noqa: E402

from agentevalops.judging.base import JudgeReport, JudgeSample, judge_score  # noqa: E402

SAMPLE = JudgeSample(case_id="c1", question="q", answer="a", contexts=("ctx",))


def failing_report(layer: str) -> JudgeReport:
    row = judge_score(sample=SAMPLE, layer=layer, metric="faithfulness", score=0.10)
    return JudgeReport(layer=layer, judge_model="m", rows=[row])


def errored_report(layer: str) -> JudgeReport:
    row = judge_score(
        sample=SAMPLE,
        layer=layer,
        metric="faithfulness",
        score=None,
        error="TimeoutError: boom",
    )
    return JudgeReport(layer=layer, judge_model="m", rows=[row])


@pytest.mark.parametrize("module,layer", [(l3, "L3"), (l4, "L4")])
def test_failure_is_a_failure_without_the_flag(module, layer):
    assert module._exit_ok([failing_report(layer)], allow_failures=False) is False


@pytest.mark.parametrize("module,layer", [(l3, "L3"), (l4, "L4")])
def test_allow_failures_lets_the_gate_decide(module, layer):
    assert module._exit_ok([failing_report(layer)], allow_failures=True) is True


@pytest.mark.parametrize("module,layer", [(l3, "L3"), (l4, "L4")])
def test_framework_errors_still_fail_with_the_flag(module, layer):
    """框架报错不是质量结论：报告里出现错误行时，测量本身就不完整。"""
    assert module._exit_ok([errored_report(layer)], allow_failures=True) is False


@pytest.mark.parametrize("module", [l3, l4])
def test_parser_accepts_the_flag(module):
    args = module.build_parser().parse_args(["--allow-failures"])
    assert args.allow_failures is True

