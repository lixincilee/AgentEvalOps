"""L3：Ragas 的 RAG 指标。

只选**不需要 embedding** 的两个指标 —— 这是一个刻意的取舍：

* ``faithfulness``：把回答拆成原子陈述，逐条问「上下文支持它吗」。抓「多编了一个数字」。
* ``context_precision``（without reference）：逐块问「这段检索内容对回答有用吗」。
  抓「检索回来一堆无关文档」。

Ragas 的 ``context_recall`` / ``answer_relevancy`` 要 embedding 模型，而 DeepSeek 不提供
embedding 接口。用哈希向量凑一个 embedding 是**假指标**：它测的是字符串重合，不是语义。
所以这两个指标不启用，报告里如实写「未启用」——不是忘了。

API 形状（ragas 0.4.3，2026-10 本机探测）：

* ``llm_factory(model, client=<AsyncOpenAI>, adapter="instructor", max_tokens=4096)``；
* ``Faithfulness(llm=...).score(user_input=..., response=..., retrieved_contexts=[...])``；
* 返回 ``MetricResult(value, reason)``，value 可能是 NaN（表示没产出可核对的陈述）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .base import DEFAULT_THRESHOLDS, JudgeSample, JudgeScore, judge_score
from ..llm import BudgetExceededError
from .llm import JudgeUnavailableError

LAYER = "L3"
DEFAULT_METRICS = ("faithfulness", "context_precision")

#: 需要 embedding 的指标，明确不启用（理由见模块文档）。
DISABLED_METRICS = ("context_recall", "answer_relevancy", "answer_correctness")


#: Ragas 的默认值是 1024，DeepSeek 的**思考模式**会先花掉几百个 token 写 reasoning，
#: 结构化 JSON 因此被截断（实测 IncompleteOutputException）。4096 是实测能稳定出分的值。
DEFAULT_JUDGE_MAX_TOKENS = 4096


def build_llm(model: str, client: Any, *, max_tokens: int = DEFAULT_JUDGE_MAX_TOKENS) -> Any:
    try:
        from ragas.llms import llm_factory
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise JudgeUnavailableError(
            "L3 需要 ragas。请用判分 venv：.venv-judge\\Scripts\\pip install -e \".[judge,dev,llm]\""
        ) from exc
    return llm_factory(
        model,
        client=client,
        adapter="instructor",
        max_tokens=max_tokens,
    )


def metric_object(name: str, llm: Any) -> Any:
    """按名字造指标对象。**每次评测只造一次**：构造里没有网络调用，但复用更省。"""
    try:
        from ragas.metrics.collections import (
            ContextPrecisionWithoutReference,
            Faithfulness,
        )
    except ImportError as exc:  # pragma: no cover
        raise JudgeUnavailableError("L3 需要 ragas>=0.4（ragas.metrics.collections）") from exc

    table: dict[str, Any] = {
        "faithfulness": Faithfulness,
        "context_precision": ContextPrecisionWithoutReference,
    }
    if name not in table:
        raise JudgeUnavailableError(
            f"未知的 L3 指标 {name!r}；已启用：{', '.join(sorted(table))}；"
            f"明确不启用（需要 embedding）：{', '.join(DISABLED_METRICS)}"
        )
    return table[name](llm=llm)


def evaluate_samples(
    samples: Sequence[JudgeSample],
    *,
    llm: Any,
    metrics: Sequence[str] = DEFAULT_METRICS,
    thresholds: Mapping[str, float] | None = None,
    ledger: Any | None = None,
    judge_model: str = "",
) -> list[JudgeScore]:
    """逐条 × 逐指标打分。**单条失败不中断整批**：错误记在那一行上，报告能看出是框架崩了。"""
    objects = {name: metric_object(name, llm) for name in metrics}
    rows: list[JudgeScore] = []
    for sample in samples:
        if not sample.contexts:
            continue  # 没有依据就没有忠实性可言；调用方负责报告 skipped 数
        for name, metric in objects.items():
            before = float(getattr(ledger, "cost", 0.0)) if ledger is not None else 0.0
            cost = 0.0
            try:
                result = metric.score(
                    user_input=sample.question,
                    response=sample.answer,
                    retrieved_contexts=list(sample.contexts),
                )
                raw = getattr(result, "value", result)
                reason = getattr(result, "reason", "") or ""
                score = _as_float(raw)
                rows.append(
                    judge_score(
                        sample=sample,
                        layer=LAYER,
                        metric=name,
                        score=score,
                        reason=reason,
                        judge_model=judge_model,
                        thresholds=thresholds,
                    )
                )
                rows[-1] = _with_cost(rows[-1], _delta(ledger, before))
            except BudgetExceededError:
                raise
            except Exception as exc:  # noqa: BLE001 - 框架/网络错误要落行，不能吞
                rows.append(
                    judge_score(
                        sample=sample,
                        layer=LAYER,
                        metric=name,
                        score=None,
                        error=f"{type(exc).__name__}: {exc}",
                        judge_model=judge_model,
                        cost_cny=_delta(ledger, before),
                        thresholds=thresholds,
                    )
                )
    return rows


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _delta(ledger: Any | None, before: float) -> float:
    if ledger is None:
        return 0.0
    return max(0.0, float(getattr(ledger, "cost", 0.0)) - before)


def _with_cost(row: JudgeScore, cost: float) -> JudgeScore:
    from dataclasses import replace

    return replace(row, cost_cny=round(cost, 8))
