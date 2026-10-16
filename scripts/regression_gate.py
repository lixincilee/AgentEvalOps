r"""回归门禁：把候选发布和基线放进同一张表，按阈值判是否退化。

用法::

    D:\Python\python.exe scripts/regression_gate.py --candidate .release-candidate --baseline releases/v0.6.0
    D:\Python\python.exe scripts/regression_gate.py --candidate runs/cur --baseline releases/v0.5.1 releases/v0.6.0

退出码：``0`` 通过；``1`` 检测到退化；``2`` 用法或文件问题。

两条独立的判据，缺一不可：

* **绝对下限**（``bound``）——不管基线多低，指标本身就不该跌破某条线。它防的是
  「基线烂，于是烂变成了标准」。
* **相对退化**（``tolerance``）——和基线比，允许掉多少。它防的是「从 100% 掉到 91%
  但没跌破绝对下限」。

带噪声的指标（延迟、成本）标 ``enforce=False``：只报不拦。见证据文档 §11.2——
同一份配置单次运行的 RPS 差 1.7 倍、P95 差 2.2 倍，还没有压出结论就拿来拦版本，
拦的是噪声。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = REPO_ROOT / "reports"
INDEX_NAME = "release.json"


class GateUsageError(RuntimeError):
    """用法不对或文件缺失：这是环境问题，不是质量结论，所以退出码是 2 而不是 1。"""


@dataclass(frozen=True)
class Rule:
    """一条门禁判据。

    ``direction="higher"`` 表示越大越好（通过率、成功率），``"lower"`` 表示越小越好
    （错误率、延迟、成本）。``bound`` 是绝对界，``tolerance`` 是允许相对基线退化多少。
    """

    metric: str
    direction: str
    bound: float | None
    tolerance: float | None
    enforce: bool = True
    note: str = ""
    #: ``tolerance`` 的量纲。``absolute`` = 指标原单位（0.01 就是 1 个百分点），
    #: ``relative`` = 相对基线的比例（0.50 就是允许差 50%）。带噪声的指标必须用相对：
    #: 它们的绝对差本来就随机器状态浮动，写死一个毫秒数只会变成随机门禁。
    tolerance_kind: str = "absolute"

    def __post_init__(self) -> None:
        if self.direction not in ("higher", "lower"):
            raise ValueError(f"direction 只能是 higher / lower，收到 {self.direction!r}")
        if self.tolerance_kind not in ("absolute", "relative"):
            raise ValueError(
                f"tolerance_kind 只能是 absolute / relative，收到 {self.tolerance_kind!r}"
            )
        if self.bound is None and self.tolerance is None and self.enforce:
            raise ValueError(f"{self.metric}: 强制规则至少要有一条判据（bound 或 tolerance）")


#: 门禁规则表。**只有这一处**，因为它就是「什么算退化」的定义。
#: 阈值写死在这里而不是当命令行参数传——能被现场放宽的门禁等于没有门禁。
RULES: tuple[Rule, ...] = (
    Rule("pytest.failed", "lower", bound=0.0, tolerance=0.0,
         note="任何一条测试失败都阻断"),
    Rule("fault.pass_rate", "higher", bound=1.0, tolerance=0.0,
         note="故障注入必须全过：它测的就是错误路径，掉一条就是错误路径坏了"),
    Rule("matrix.v3_rules.pass_rate", "higher", bound=0.90, tolerance=0.01,
         note="主矩阵（213 行）规则臂通过率"),
    Rule("rag_multiturn.v3_rules.pass_rate", "higher", bound=0.90, tolerance=0.01,
         note="检索 + 多轮（120 行）规则臂通过率"),
    Rule("perf.http_error_rate", "lower", bound=0.01, tolerance=0.005,
         note="传输层错误率（与 scripts/load_test.py 的门禁同源）"),
    Rule("perf.task_success_rate", "higher", bound=0.99, tolerance=0.005,
         note="任务成功率：终态是不是这条消息该有的结果"),
    # 下面两条只报不拦：单次运行的延迟与成本还压不出结论（证据文档 §11.2）。
    Rule("perf.p95_ms", "lower", bound=None, tolerance=0.50, enforce=False,
         tolerance_kind="relative",
         note="延迟噪声大（实测单次运行差 2.2 倍），先只报；等 --repeat N"),
    Rule("perf.cost_per_success_cny", "lower", bound=None, tolerance=0.50, enforce=False,
         tolerance_kind="relative",
         note="模型臂的建模成本，同上"),
)


@dataclass
class Release:
    """一次冻结发布：目录 + 机器可读的指标索引。"""

    path: Path
    tag: str
    commit: str | None
    frozen_at: str | None
    dirty: bool
    metrics: dict[str, float]
    raw: dict[str, Any]

    def metric(self, name: str) -> float | None:
        value = self.metrics.get(name)
        return None if value is None else float(value)


@dataclass
class Verdict:
    rule: Rule
    candidate: float | None
    baseline: float | None
    status: str  # pass / fail / warn
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.rule.metric,
            "direction": self.rule.direction,
            "status": self.status,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "bound": self.rule.bound,
            "tolerance": self.rule.tolerance,
            "tolerance_kind": self.rule.tolerance_kind,
            "enforce": self.rule.enforce,
            "reason": self.reason,
            "note": self.rule.note,
        }


def load_release(path: Path) -> Release:
    """读一个冻结发布目录。缺 ``release.json`` 就是用法错误，不是质量结论。"""
    path = Path(path)
    index = path / INDEX_NAME
    if not index.is_file():
        raise GateUsageError(
            f"{path} 下没有 {INDEX_NAME}：这不是一次冻结发布的目录"
            f"（用 scripts/freeze_release.py 生成）。"
        )
    try:
        raw = json.loads(index.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GateUsageError(f"{index} 不是合法 JSON：{exc}") from exc
    if raw.get("kind") != "release_index":
        raise GateUsageError(f"{index} 的 kind 是 {raw.get('kind')!r}，不是 release_index。")
    metrics: dict[str, float] = {}
    for name, value in (raw.get("metrics") or {}).items():
        # null 表示「这次没测出来」，不是 0——把它当 0 会凭空造出一个失败。
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        metrics[str(name)] = float(value)
    return Release(
        path=path,
        tag=str(raw.get("tag") or path.name),
        commit=raw.get("commit"),
        frozen_at=raw.get("frozen_at"),
        dirty=bool(raw.get("dirty")),
        metrics=metrics,
        raw=raw,
    )


def evaluate(rule: Rule, candidate: float | None, baseline: float | None) -> Verdict:
    """判一条规则。顺序是有意的：**先绝对下限，再相对退化**。

    反过来会先给出「比基线掉了多少」，那个数字在指标本身就不可接受时会分散注意力。
    """
    if candidate is None:
        status = "fail" if rule.enforce else "warn"
        return Verdict(rule, None, baseline, status, "候选里没有这个指标")

    if rule.bound is not None:
        if rule.direction == "higher" and candidate < rule.bound:
            return Verdict(rule, candidate, baseline, "fail",
                           f"{_fmt(rule.metric, candidate)} < 绝对下限 "
                           f"{_fmt(rule.metric, rule.bound)}")
        if rule.direction == "lower" and candidate > rule.bound:
            return Verdict(rule, candidate, baseline, "fail",
                           f"{_fmt(rule.metric, candidate)} > 绝对上限 "
                           f"{_fmt(rule.metric, rule.bound)}")

    if baseline is not None and rule.tolerance is not None:
        worse, shown, limit = _degradation(rule, candidate, baseline)
        if worse is None:
            return Verdict(rule, candidate, baseline, "pass", "基线是 0，相对判据不适用")
        if worse > rule.tolerance:
            status = "fail" if rule.enforce else "warn"
            return Verdict(rule, candidate, baseline, status,
                           f"比基线退化 {shown} > 容忍 {limit}")

    if baseline is None and rule.tolerance is not None:
        return Verdict(rule, candidate, baseline, "pass", "基线没有这个指标，只判绝对下限")

    return Verdict(rule, candidate, baseline, "pass", "通过")


def _degradation(
    rule: Rule, candidate: float, baseline: float
) -> tuple[float | None, str, str]:
    """算出「比基线差了多少」，连同给人看的两个字符串。

    ``direction="higher"``（越大越好）时退化量是 ``基线 - 候选``；``"lower"``（越小越好）
    时是 ``候选 - 基线``。负值表示反而更好了，调用方按「没退化」处理。
    """
    raw = (baseline - candidate) if rule.direction == "higher" else (candidate - baseline)
    if rule.tolerance_kind == "relative":
        if baseline == 0:
            return None, "n/a", "n/a"
        ratio = raw / abs(baseline)
        return ratio, f"{ratio * 100:.1f}%", f"{rule.tolerance * 100:.1f}%"
    return raw, _fmt(rule.metric, raw), _fmt(rule.metric, rule.tolerance)


def _fmt(metric: str, value: float | None) -> str:
    if value is None:
        return "n/a"
    if metric.endswith("_rate") or "pass_rate" in metric:
        return f"{value * 100:.3f}%"
    if metric.endswith("_ms"):
        return f"{value:.0f}ms"
    if metric.endswith("_cny"):
        return f"CNY {value:.8f}"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}"


def run_gate(candidate: Release, baselines: Sequence[Release]) -> dict[str, Any]:
    """跑完整张规则表。``baselines`` 的**最后一个**是执法基线（调用方按时间顺序传）。"""
    baseline = baselines[-1] if baselines else None
    verdicts = [
        evaluate(rule, candidate.metric(rule.metric),
                 None if baseline is None else baseline.metric(rule.metric))
        for rule in RULES
    ]
    failures = [v for v in verdicts if v.status == "fail"]
    warnings = [v for v in verdicts if v.status == "warn"]
    return {
        "kind": "regression_gate",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate": {
            "path": str(candidate.path), "tag": candidate.tag,
            "commit": candidate.commit, "dirty": candidate.dirty,
        },
        "baselines": [
            {"path": str(b.path), "tag": b.tag, "commit": b.commit, "dirty": b.dirty}
            for b in baselines
        ],
        "enforcement_baseline": None if baseline is None else baseline.tag,
        "metrics": {
            name: {
                "candidate": candidate.metric(name),
                "baseline": None if baseline is None else baseline.metric(name),
                "series": [{"tag": b.tag, "value": b.metric(name)} for b in baselines],
            }
            for name in sorted({r.metric for r in RULES} | set(candidate.metrics))
        },
        "verdicts": [v.to_dict() for v in verdicts],
        "passed": not failures,
        "failure_count": len(failures),
        "warning_count": len(warnings),
        "failures": [f"{v.rule.metric}: {v.reason}" for v in failures],
        "warnings": [f"{v.rule.metric}: {v.reason}" for v in warnings],
    }


def print_report(summary: dict[str, Any], candidate: Release, baselines: Sequence[Release]) -> None:
    """打一张人看的表。列是「基线们 + 候选」，行是指标。"""
    tags = [b.tag for b in baselines] + [candidate.tag]
    width = max([len("指标")] + [len(r.metric) for r in RULES] + [len(t) for t in tags])
    line = "=" * (width + 17 * len(tags) + 12)
    print("\n" + line)
    print(f"回归门禁  候选 {candidate.tag}（{candidate.commit or '?'}）"
          f"  执法基线 {summary['enforcement_baseline'] or '无'}")
    print(line)
    header = "指标".ljust(width) + "".join(t.rjust(17) for t in tags) + "  判定"
    print(header)
    print("-" * len(header))
    verdict_by_metric = {v["metric"]: v for v in summary["verdicts"]}
    for name, entry in summary["metrics"].items():
        cells = [_fmt(name, s["value"]) for s in entry["series"]]
        cells.append(_fmt(name, entry["candidate"]))
        verdict = verdict_by_metric.get(name)
        mark = {None: "—", "pass": "通过", "warn": "提醒", "fail": "阻断"}[
            None if verdict is None else verdict["status"]
        ]
        print(name.ljust(width) + "".join(c.rjust(17) for c in cells) + "  " + mark)
    print("-" * len(header))
    for failure in summary["failures"]:
        print("阻断:", failure)
    for warning in summary["warnings"]:
        print("提醒:", warning)
    print(line)
    print("门禁通过。" if summary["passed"] else
          f"门禁未通过：{summary['failure_count']} 条阻断、{summary['warning_count']} 条提醒。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回归门禁：候选发布 vs 基线发布")
    parser.add_argument("--candidate", type=Path, required=True, help="候选发布目录")
    parser.add_argument("--baseline", type=Path, action="append", default=[],
                        help="基线发布目录，可给多个（按时间顺序，最后一个执法）")
    parser.add_argument("--out", type=Path, default=None,
                        help="报告路径；默认 reports/regression-<UTC>-<tag>.json")
    args = parser.parse_args(argv)

    try:
        candidate = load_release(args.candidate)
        baselines = [load_release(path) for path in args.baseline]
    except GateUsageError as exc:
        print(exc)
        return 2

    summary = run_gate(candidate, baselines)
    print_report(summary, candidate, baselines)
    out = args.out or REPORTS_DIR / (
        f"regression-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{candidate.tag}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告已写入:", out)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())