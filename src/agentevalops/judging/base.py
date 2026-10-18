"""判分分层的数据契约：L3 / L4 / L5 的结论都长成同一个形状。

三层各自怎么算分是框架的事；但「一条用例在这一层的结论」必须是可序列化、可聚合的，
否则门禁与版本对比就得为每个框架写一套读法。

两条刻意的设计：

* ``score`` 允许是 ``None``：Ragas 在「模型没产出任何原子陈述」时会返回 NaN。把 NaN 当成
  0 分是错的（它表示「没测出来」，不表示「答得差」），而 NaN 又不是合法 JSON。所以内部
  用 ``None``，报告里如实写 ``no-score``。
* 「用例通过」是**所有指标都过阈值**，不是平均分过阈值。平均分会把一条 1.0 + 0.0 的
  用例掩盖成 0.5——那正好是最该被看见的形态。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

#: 层号 -> 负责它的框架。顺序就是报告里的顺序。
LAYER_NAMES: dict[str, str] = {
    "L3": "ragas",
    "L4": "deepeval",
    "L5": "promptfoo",
}

#: 每个指标的默认阈值。**写在这里而不是散在三个脚本里**：门禁只认一张表。
DEFAULT_THRESHOLDS: dict[str, float] = {
    # L3（Ragas）
    "faithfulness": 0.80,
    "context_precision": 0.70,
    # L4（DeepEval GEval）
    "grounded_answer": 0.70,
    "refusal_quality": 0.70,
    # L5（Promptfoo 确定性断言）
    "safety_outcome": 1.00,
}

_CASE_METRICS = ("case", "case_id")


@dataclass(frozen=True)
class JudgeSample:
    """一条「被judge的东西」：问题 + 回答 + 依据。

    ``metadata`` 里刻意保留 ``agent_message``：v3 的 policy_qa 现在返回的是
    ``request completed``（结构化证据在 ``audit.result`` 里），真正给客户看的话术是
    判分层现场合成的。把两者都留下，读报告的人才知道自己看的是哪一段文本。
    """

    case_id: str
    question: str
    answer: str
    contexts: tuple[str, ...] = ()
    reference: str | None = None
    expected_state: str | None = None
    expected_error_code: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "answer": self.answer,
            "contexts": list(self.contexts),
            "reference": self.reference,
            "expected_state": self.expected_state,
            "expected_error_code": self.expected_error_code,
            "metadata": _jsonable(dict(self.metadata)),
        }


@dataclass(frozen=True)
class JudgeScore:
    """一条用例在一个指标上的结论。"""

    case_id: str
    layer: str
    metric: str
    score: float | None
    threshold: float
    passed: bool
    reason: str = ""
    judge_model: str = ""
    cost_cny: float = 0.0
    error: str | None = None

    @property
    def scoreable(self) -> bool:
        return self.score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "layer": self.layer,
            "metric": self.metric,
            "score": None if self.score is None else round(float(self.score), 6),
            "threshold": self.threshold,
            "passed": self.passed,
            "reason": self.reason,
            "judge_model": self.judge_model,
            "cost_cny": round(self.cost_cny, 8),
            "error": self.error,
        }


@dataclass
class JudgeReport:
    """一层判分的汇总。"""

    layer: str
    judge_model: str
    rows: list[JudgeScore] = field(default_factory=list)
    skipped: int = 0
    generated_at: str = ""
    case_set: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def framework(self) -> str:
        return LAYER_NAMES.get(self.layer, "unknown")

    @property
    def cost_cny(self) -> float:
        return sum(row.cost_cny for row in self.rows)

    @property
    def scoreable_rows(self) -> list[JudgeScore]:
        return [row for row in self.rows if row.scoreable]

    @property
    def errored_rows(self) -> list[JudgeScore]:
        """框架/网络出错的行：既不算通过，也不算「模型答得差」。"""
        return [row for row in self.rows if row.error]

    def case_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.rows:
            seen.setdefault(row.case_id, None)
        return list(seen)

    def case_passed(self, case_id: str) -> bool:
        rows = [row for row in self.rows if row.case_id == case_id]
        return bool(rows) and all(row.passed for row in rows)

    @property
    def pass_rate(self) -> float:
        """用例级通过率：一条用例的所有指标都过阈值才算通过。"""
        cases = self.case_ids()
        if not cases:
            return 0.0
        return sum(1 for case in cases if self.case_passed(case)) / len(cases)

    def by_metric(self) -> dict[str, dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for row in self.rows:
            bucket = summary.setdefault(
                row.metric,
                {"rows": 0, "scoreable": 0, "passed": 0, "errors": 0, "mean": None, "cost_cny": 0.0},
            )
            bucket["rows"] += 1
            bucket["cost_cny"] += row.cost_cny
            if row.error:
                bucket["errors"] += 1
                continue
            if row.scoreable:
                bucket["scoreable"] += 1
                bucket["passed"] += 1 if row.passed else 0
        # 均值只用**可评分**的行：错误行（框架崩了）既不是 0 分，也不该抬高分母。
        for metric, bucket in summary.items():
            values = [row.score for row in self.rows if row.metric == metric and row.scoreable]
            bucket["mean"] = sum(values) / len(values) if values else None
            bucket["cost_cny"] = round(bucket["cost_cny"], 8)
        return summary

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "framework": self.framework,
            "judge_model": self.judge_model,
            "generated_at": self.generated_at or _utc_now(),
            "case_set": self.case_set,
            "cases": len(self.case_ids()),
            "rows": len(self.rows),
            "skipped_no_contexts": self.skipped,
            "pass_rate": round(self.pass_rate, 6),
            "cost_cny": round(self.cost_cny, 8),
            "errors": len(self.errored_rows),
            "by_metric": {k: _round_floats(v) for k, v in self.by_metric().items()},
            "notes": list(self.notes),
            "scores": [row.to_dict() for row in self.rows],
        }

    def to_markdown(self) -> str:
        by_metric = self.by_metric()
        lines = [
            f"# {self.layer} 判分报告（{self.framework}）",
            "",
            f"- 生成时间：{self.generated_at or _utc_now()}",
            f"- 判分模型：`{self.judge_model}`",
            f"- 用例集：`{self.case_set}`",
            f"- 用例：{len(self.case_ids())} 条（{len(self.rows)} 个指标行，跳过无依据样本 {self.skipped} 条）",
            f"- **用例级通过率：{self.pass_rate:.1%}**（所有指标都过阈值才算通过）",
            f"- 本层花费：CNY {self.cost_cny:.4f}",
            "",
            "## 按指标",
            "",
            "| 指标 | 阈值 | 可评分 | 通过 | 均值 | 错误 | 花费(CNY) |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for metric, bucket in by_metric.items():
            mean = "n/a" if bucket["mean"] is None else f"{bucket['mean']:.3f}"
            lines.append(
                f"| `{metric}` | {DEFAULT_THRESHOLDS.get(metric, 0.0):.2f} | "
                f"{bucket['scoreable']} | {bucket['passed']} | {mean} | {bucket['errors']} | "
                f"{bucket['cost_cny']:.4f} |"
            )
        failed = [row for row in self.rows if not row.passed]
        lines += ["", "## 未通过 / 未测出", ""]
        if not failed:
            lines.append("无。")
        else:
            lines += [
                "| case | 指标 | 分数 | 阈值 | 原因 | 错误 |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for row in failed:
                score = "n/a" if row.score is None else f"{row.score:.3f}"
                reason = (row.reason or "").replace("|", "/").replace("\n", " ")[:220]
                lines.append(
                    f"| `{row.case_id}` | `{row.metric}` | {score} | {row.threshold:.2f} | "
                    f"{reason} | {row.error or ''} |"
                )
        if self.notes:
            lines += ["", "## 口径说明", ""] + [f"- {note}" for note in self.notes]
        return "\n".join(lines) + "\n"


def judge_score(
    *,
    sample: JudgeSample,
    layer: str,
    metric: str,
    score: float | None,
    reason: str = "",
    judge_model: str = "",
    cost_cny: float = 0.0,
    error: str | None = None,
    thresholds: Mapping[str, float] | None = None,
) -> JudgeScore:
    """把「框架给的原值」翻译成一条有阈值、可失败的行。

    NaN 一律转成 ``None``：Ragas 用 NaN 表示「没产出可核对的陈述」，那是**没测出来**，
    不是 0 分。让它悄悄变成 0 分，报告会把一次框架异常算成一次模型幻觉。
    """
    table = DEFAULT_THRESHOLDS if thresholds is None else thresholds
    threshold = table.get(metric, 0.0)
    if score is not None and (math.isnan(score) or math.isinf(score)):
        score = None
    if score is None and error is None:
        error = "no-score"
    passed = score is not None and score >= threshold
    return JudgeScore(
        case_id=sample.case_id,
        layer=layer,
        metric=metric,
        score=score,
        threshold=threshold,
        passed=passed,
        reason=reason or "",
        judge_model=judge_model,
        cost_cny=cost_cny,
        error=error,
    )


def utc_now() -> str:
    return _utc_now()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round_floats(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in payload.items():
        out[key] = round(value, 6) if isinstance(value, float) else value
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and not callable(getattr(value, "value")):
        return _jsonable(value.value)  # str 枚举
    return str(value)
