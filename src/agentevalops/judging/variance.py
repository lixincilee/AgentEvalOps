"""重复判分的方差：同一批用例跑 N 次，结论到底稳不稳。

单次判分有两个噪声源：合成回答（`answer-v1`）与 Judge 模型本身。只跑一次就把
68.4% 当成真值，和压测只跑一次就把 P95 当 SLA 是同一类错误。

这里的口径与 `load_test.py --repeat N` / `compare_versions.py --repeat N` 一致：

* **中位数**是主结论（`pass_rate`），**最差一次**单独报出来（`worst_pass_rate`）。
  门禁只看中位数会漏掉「三次里有一次崩了」；只看最差一次又把单次抖动当退化。
  两个都看，才是完整的。
* 用例级「墙头草」单独列：同一用例在部分轮次通过、部分轮次不通过。它比均值更能
  指出哪条用例的判分不稳定。
* 框架错误（`error`）不参与均值，但会计入 `errors`；有任何错误行时门禁直接失败，
  因为那不是「分数低」，是「没测出来」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Sequence

from .base import JudgeReport, utc_now


def _stats(values: Sequence[float]) -> dict[str, float | None]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"median": None, "min": None, "max": None, "range": None}
    return {
        "median": round(median(clean), 6),
        "min": round(min(clean), 6),
        "max": round(max(clean), 6),
        "range": round(max(clean) - min(clean), 6),
    }


def _run_summary(report: JudgeReport) -> dict[str, Any]:
    """一次运行的摘要。不重复存 `scores`：三轮 48 条会把报告撑成三份。"""
    return {
        "generated_at": report.generated_at,
        "cases": len(report.case_ids()),
        "rows": len(report.rows),
        "pass_rate": round(report.pass_rate, 6),
        "errors": len(report.errored_rows),
        "cost_cny": round(report.cost_cny, 8),
        "by_metric": {
            name: {
                "scoreable": bucket["scoreable"],
                "passed": bucket["passed"],
                "mean": None if bucket["mean"] is None else round(bucket["mean"], 6),
                "errors": bucket["errors"],
            }
            for name, bucket in report.by_metric().items()
        },
    }


def flaky_cases(reports: Sequence[JudgeReport]) -> list[dict[str, Any]]:
    """同一用例在不同轮次结论不同：按用例列出各轮结果与翻转的指标。"""
    if not reports:
        return []
    order: dict[str, None] = {}
    for report in reports:
        for case_id in report.case_ids():
            order.setdefault(case_id, None)

    rows: list[dict[str, Any]] = []
    for case_id in order:
        verdicts = [report.case_passed(case_id) for report in reports]
        if all(verdicts) or not any(verdicts):
            continue
        metrics: dict[str, list[bool]] = {}
        for report in reports:
            by_metric: dict[str, bool] = {}
            for row in report.rows:
                if row.case_id == case_id:
                    by_metric[row.metric] = by_metric.get(row.metric, True) and row.passed
            for name, passed in by_metric.items():
                metrics.setdefault(name, []).append(passed)
        flipped = [name for name, values in metrics.items() if len(set(values)) > 1]
        rows.append(
            {
                "case_id": case_id,
                "passed_runs": sum(1 for value in verdicts if value),
                "runs": len(reports),
                "verdicts": ["pass" if value else "fail" for value in verdicts],
                "flipped_metrics": flipped,
            }
        )
    return rows


def failed_cases(reports: Sequence[JudgeReport]) -> list[dict[str, Any]]:
    """第一轮里没通过的用例（逐指标）。方差报告不能只给数字，人得知道是哪条。"""
    if not reports:
        return []
    first = reports[0]
    rows: list[dict[str, Any]] = []
    for case_id in first.case_ids():
        case_rows = [row for row in first.rows if row.case_id == case_id]
        if case_rows and all(row.passed for row in case_rows):
            continue
        rows.append(
            {
                "case_id": case_id,
                "metrics": [
                    {
                        "metric": row.metric,
                        "score": None if row.score is None else round(row.score, 6),
                        "threshold": row.threshold,
                        "passed": row.passed,
                        "error": row.error,
                        "reason": (row.reason or "")[:300],
                    }
                    for row in case_rows
                ],
            }
        )
    return rows


@dataclass
class RepeatReport:
    """N 次判分的合并报告。`pass_rate` 是中位数，`worst_pass_rate` 是最差一次。"""

    layer: str
    judge_model: str
    case_set: str
    reports: list[JudgeReport]
    generated_at: str = ""
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.reports:
            raise ValueError("RepeatReport 至少需要一次运行")
        layers = {report.layer for report in self.reports}
        if len(layers) != 1:
            raise ValueError(f"同一份重复报告里混了多个层：{sorted(layers)}")

    @property
    def framework(self) -> str:
        from .base import LAYER_NAMES

        return LAYER_NAMES.get(self.layer, "unknown")

    @property
    def repeat(self) -> int:
        return len(self.reports)

    @property
    def pass_rates(self) -> list[float]:
        return [round(report.pass_rate, 6) for report in self.reports]

    @property
    def pass_rate(self) -> float:
        return float(_stats(self.pass_rates)["median"] or 0.0)

    @property
    def worst_pass_rate(self) -> float:
        return min(self.pass_rates)

    @property
    def best_pass_rate(self) -> float:
        return max(self.pass_rates)

    @property
    def cost_cny(self) -> float:
        return round(sum(report.cost_cny for report in self.reports), 8)

    @property
    def errors(self) -> int:
        return sum(len(report.errored_rows) for report in self.reports)

    @property
    def samples(self) -> list[dict[str, Any]]:
        """第一轮的样本：抽检表只需要一份可读的问题 / 回答 / 资料。"""
        return [sample.to_dict() for sample in self.reports[0].samples]

    def metric_variance(self) -> dict[str, dict[str, Any]]:
        names: dict[str, None] = {}
        for report in self.reports:
            for name in report.by_metric():
                names.setdefault(name, None)
        out: dict[str, dict[str, Any]] = {}
        for name in names:
            values = [report.by_metric().get(name, {}).get("mean") for report in self.reports]
            passed = [report.by_metric().get(name, {}).get("passed", 0) for report in self.reports]
            out[name] = {
                **_stats([value for value in values if value is not None]),
                "means": [None if value is None else round(value, 6) for value in values],
                "passed": passed,
                "errors": sum(
                    report.by_metric().get(name, {}).get("errors", 0) for report in self.reports
                ),
            }
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "framework": self.framework,
            "judge_model": self.judge_model,
            "generated_at": self.generated_at or utc_now(),
            "case_set": self.case_set,
            "repeat": self.repeat,
            "cases": len(self.reports[0].case_ids()),
            "pass_rate": round(self.pass_rate, 6),
            "worst_pass_rate": round(self.worst_pass_rate, 6),
            "best_pass_rate": round(self.best_pass_rate, 6),
            "cost_cny": self.cost_cny,
            "errors": self.errors,
            "runs": [_run_summary(report) for report in self.reports],
            "variance": {
                "pass_rate": _stats(self.pass_rates),
                "metrics": self.metric_variance(),
            },
            "flaky_cases": flaky_cases(self.reports),
            "failed_cases": failed_cases(self.reports),
            # 第一轮的逐行分数：抽检工具需要知道「机器判了通过还是失败」。
            "scores": [row.to_dict() for row in self.reports[0].rows],
            "samples": self.samples,
            "notes": list(self.notes),
        }

    def to_markdown(self) -> str:
        lines = [
            f"# {self.layer} 判分报告（{self.framework}，{self.repeat} 次重复）",
            "",
            f"- 生成时间：{self.generated_at or utc_now()}",
            f"- 判分模型：`{self.judge_model}`",
            f"- 用例集：`{self.case_set}`",
            f"- 用例：{len(self.reports[0].case_ids())} 条 × {self.repeat} 轮",
            f"- **用例级通过率（中位数）：{self.pass_rate:.1%}**（各轮："
            + " / ".join(f"{value:.1%}" for value in self.pass_rates)
            + "）",
            f"- 最差一次：{self.worst_pass_rate:.1%}；最好一次：{self.best_pass_rate:.1%}",
            f"- 总花费：CNY {self.cost_cny:.4f}；框架错误：{self.errors} 行",
            "",
            "## 每次运行",
            "",
        ]
        metric_names = list(self.metric_variance())
        header = "| 轮次 | 通过率 | " + " | ".join(f"`{name}` 均值" for name in metric_names) + " | 错误 | 花费(CNY) |"
        separator = "| --- | --- | " + " ".join("--- |" for _ in metric_names) + " --- | --- |"
        lines += [header, separator]
        for index, report in enumerate(self.reports, start=1):
            by_metric = report.by_metric()
            cells = []
            for name in metric_names:
                mean = by_metric.get(name, {}).get("mean")
                cells.append("n/a" if mean is None else f"{mean:.3f}")
            lines.append(
                f"| {index} | {report.pass_rate:.1%} | "
                + " | ".join(cells)
                + f" | {len(report.errored_rows)} | {report.cost_cny:.4f} |"
            )

        lines += [
            "",
            "## 方差",
            "",
            "| 指标 | 中位数 | 最小 | 最大 | 极差 | 各轮均值 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for name, stats in self.metric_variance().items():
            means = " / ".join("n/a" if value is None else f"{value:.3f}" for value in stats["means"])
            lines.append(
                f"| `{name}` | {_fmt(stats['median'])} | {_fmt(stats['min'])} | "
                f"{_fmt(stats['max'])} | {_fmt(stats['range'])} | {means} |"
            )
        pass_stats = _stats(self.pass_rates)
        lines.append(
            f"| 用例级通过率 | {_fmt(pass_stats['median'])} | {_fmt(pass_stats['min'])} | "
            f"{_fmt(pass_stats['max'])} | {_fmt(pass_stats['range'])} | "
            + " / ".join(f"{value:.3f}" for value in self.pass_rates)
            + " |"
        )

        flaky = flaky_cases(self.reports)
        lines += ["", "## 墙头草用例", ""]
        if not flaky:
            lines.append("无：所有用例在各轮结论一致。")
        else:
            lines += [
                "| case | 通过轮次 | 各轮结论 | 翻转的指标 |",
                "| --- | --- | --- | --- |",
            ]
            for row in flaky:
                lines.append(
                    f"| `{row['case_id']}` | {row['passed_runs']}/{row['runs']} | "
                    f"{' / '.join(row['verdicts'])} | "
                    f"{', '.join(f'`{name}`' for name in row['flipped_metrics']) or 'case 级'} |"
                )
        failures = failed_cases(self.reports)
        lines += ["", "## 未通过用例（第一轮）", ""]
        if not failures:
            lines.append("无。")
        else:
            lines += [
                "| case | 指标 | 分数 | 阈值 | 错误 | 理由 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for failure in failures:
                for item in failure["metrics"]:
                    if item["passed"]:
                        continue
                    score = "n/a" if item["score"] is None else f"{item['score']:.3f}"
                    reason = (item["reason"] or "").replace("|", "/").replace("\n", " ")
                    lines.append(
                        f"| `{failure['case_id']}` | `{item['metric']}` | {score} | "
                        f"{item['threshold']:.2f} | {item['error'] or ''} | {reason} |"
                    )
        if self.notes:
            lines += ["", "## 口径说明", ""] + [f"- {note}" for note in self.notes]
        return "\n".join(lines) + "\n"


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"
