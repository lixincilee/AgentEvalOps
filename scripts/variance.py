"""把「同一个测量做了 N 次」汇总成「噪声有多大」。

存在的理由只有一个：**只跑一次的数字不能用来比较版本**。

实测过两次这个问题：

* 同一份压测配置（`--arm rules --users 20 --spawn-rate 5 --run-time 15s`）在本机跑三次，
  RPS 是 125.4 / 179.2 / 210.1，P95 是 220 / 130 / 100 ms，代码一个字没改。
* 同一份用例、同一个 `v2`，两次真实运行的行级通过率是 99.1% / 100.0%。

所以门禁的容忍度不该拍脑袋定，而该由这个模块量出来：先跑 N 次，看到极差，再决定
「差多少算真的退化」。这也是 `--repeat N` 的全部意义。

判据只有一条，写得保守一点：**两组中位数之差必须大于两组各自的极差，才认为这个差是可
解释的**。达不到就老老实实说「不可解释」，而不是把噪声当成结论——把噪声当结论，
比没有结论更糟，因为它看起来像个结论。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Aggregate:
    """一组重复测量的分布。``spread`` 用极差而不是标准差：N 小的时候标准差没什么意义，
    极差至少是「我亲眼见过的最坏情况」。"""

    count: int
    median: float
    minimum: float
    maximum: float
    mean: float
    spread: float
    #: 极差相对中位数的比例。中位数是 0 时没有定义（返回 None），不是无穷大。
    spread_pct: float | None
    stdev: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "median": self.median,
            "min": self.minimum,
            "max": self.maximum,
            "mean": self.mean,
            "spread": self.spread,
            "spread_pct": self.spread_pct,
            "stdev": self.stdev,
        }


def aggregate(values: Sequence[float]) -> Aggregate | None:
    """汇总一组数。空集合返回 ``None``——「没有测量」不是「测量结果是 0」。"""
    numbers = [float(value) for value in values]
    if not numbers:
        return None
    median = statistics.median(numbers)
    spread = max(numbers) - min(numbers)
    return Aggregate(
        count=len(numbers),
        median=median,
        minimum=min(numbers),
        maximum=max(numbers),
        mean=statistics.fmean(numbers),
        spread=spread,
        # 极差相对中位数的比例。中位数为 0 时这个比值没有意义（0→0 的抖动不是 0% 而是无定义），
        # 所以给 None 而不是 0.0：0.0 会被下游当成「稳如磐石」。
        spread_pct=None if median == 0 else spread / abs(median),
        stdev=statistics.stdev(numbers) if len(numbers) > 1 else 0.0,
    )


def summarize_runs(
    runs: Sequence[Mapping[str, Any]], metrics: Iterable[str]
) -> dict[str, dict[str, Any]]:
    """把每次运行的指标表汇总成 ``{指标: {values, median, ...}}``。

    某个指标在部分运行里缺失（``None``）时**跳过那几次**，而不是补 0：
    补 0 会把「没测到」变成一个极低的极值，直接把极差撑大。
    """
    summary: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        values = [
            float(run[metric])
            for run in runs
            if run.get(metric) is not None
        ]
        stats = aggregate(values)
        payload: dict[str, Any] = {"values": values}
        if stats is not None:
            payload.update(stats.to_dict())
        else:
            payload.update({"count": 0})
        summary[metric] = payload
    return summary


def difference_is_interpretable(
    delta: float, spread_a: float, spread_b: float
) -> bool:
    """两组中位数之差，是否比噪声更大。

    ``spread_a`` / ``spread_b`` 是两组各自的极差。判据：``|delta| > max(两边的极差)``。

    故意取得保守：噪声估计本身就只有 N 个样本，宁可信其大。达不到就往「不可解释」报——
    这一步的作用是**阻止把噪声写成结论**，所以它的默认答案必须是「不显著」。
    """
    return abs(delta) > max(spread_a, spread_b)


def spread_of(summary: Mapping[str, Any], metric: str) -> float:
    """从 :func:`summarize_runs` 的结果里取某个指标的极差；没有就返回 0。"""
    entry = summary.get(metric) or {}
    value = entry.get("spread")
    return 0.0 if value is None else float(value)


__all__ = [
    "Aggregate",
    "aggregate",
    "difference_is_interpretable",
    "spread_of",
    "summarize_runs",
]