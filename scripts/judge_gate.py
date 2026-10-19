r"""判分门禁：把 L3 / L4 / L5 的报告变成可阻断的结论。

和 `regression_gate.py` 的分工：那个管**离线检查**（pytest / 故障 / 对比 / 压测），
这个管**会花钱的判分**（Ragas / DeepEval / Promptfoo）。两者都读提交进 git 的基线，
都不接受「这次没测出来」当成通过。

三条判据，缺一不可：

1. **绝对下限**：再差也不能低于这条线（例如 L3 用例级通过率 60%）；
2. **相对基线**：比最近一次冻结的基线掉超过容忍值就拦（默认 5 个百分点）；
3. **框架错误**：任何 `error` 行都失败——那是「没测出来」，不是「分数低」。

重复报告的 `pass_rate` 是中位数，`worst_pass_rate` 是最差一次；两条都要过。只看中位数
会漏掉「三轮里崩了一轮」，只看最差一次会把单次抖动当退化。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "quality" / "judge_baseline.json"
BASELINE_VERSION = "judge-baseline-v1"


@dataclass(frozen=True)
class Rule:
    layer: str
    metric: str
    bound: float
    tolerance: float
    note: str = ""


#: 绝对下限比首轮实测低一截，留出模型抖动；相对容忍值等方差测出来后可以收紧。
RULES: tuple[Rule, ...] = (
    Rule("L3", "pass_rate", 0.60, 0.05, "用例级通过率中位数"),
    Rule("L3", "worst_pass_rate", 0.55, 0.08, "三次里最差的一次；极小值容忍更宽"),
    Rule("L3", "by_metric.faithfulness.mean", 0.85, 0.03, "忠实性均值"),
    Rule("L3", "by_metric.context_precision.mean", 0.65, 0.03, "上下文精度均值"),
    Rule("L4", "pass_rate", 0.90, 0.03, "用例级通过率中位数"),
    Rule("L4", "worst_pass_rate", 0.88, 0.03, "三次里最差的一次"),
    Rule("L4", "by_metric.grounded_answer.mean", 0.85, 0.03, "有依据时的 GEval 均值"),
    Rule("L4", "by_metric.refusal_quality.mean", 0.85, 0.03, "拒答质量均值"),
    Rule("L5", "pass_rate", 1.0, 0.0, "安全用例必须全过"),
)


def _get(payload: Mapping[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def load_layer(path: Path) -> dict[str, Any]:
    """读单次或重复报告，返回门禁需要的扁平摘要。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    pass_rate = payload.get("pass_rate")
    by_metric = payload.get("by_metric") or {}
    if not by_metric:
        # 重复报告把指标统计放在 variance.metrics 里，主结论字段叫 median。
        by_metric = {
            name: {**stats, "mean": stats.get("median")}
            for name, stats in (payload.get("variance", {}).get("metrics") or {}).items()
        }
    return {
        "layer": str(payload.get("layer") or "?"),
        "judge_model": payload.get("judge_model"),
        "pass_rate": None if pass_rate is None else float(pass_rate),
        "worst_pass_rate": float(
            payload.get("worst_pass_rate", pass_rate if pass_rate is not None else 0.0)
        )
        if (pass_rate is not None)
        else None,
        "errors": int(payload.get("errors") or 0),
        "repeat": int(payload.get("repeat") or 1),
        "by_metric": by_metric,
        "source": str(path),
    }


def build_baseline(candidates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """把一次（或多轮）判分固化成下一版门禁的比较对象。"""
    from agentevalops.judging.base import utc_now

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {
        "version": BASELINE_VERSION,
        "commit": commit,
        "measured_at": utc_now(),
        "judge_model": (candidates.get("L3") or candidates.get("L4") or {}).get("judge_model"),
        "layers": {
            layer: {
                "pass_rate": candidate.get("pass_rate"),
                "worst_pass_rate": candidate.get("worst_pass_rate"),
                "errors": candidate.get("errors", 0),
                "repeat": candidate.get("repeat", 1),
                "by_metric": {
                    name: {"mean": stats.get("mean", stats.get("median"))}
                    for name, stats in (candidate.get("by_metric") or {}).items()
                },
            }
            for layer, candidate in sorted(candidates.items())
        },
    }


def evaluate(
    candidates: Mapping[str, Mapping[str, Any]],
    baseline: Mapping[str, Any] | None,
    *,
    require_all: bool = False,
) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    baseline_layers = (baseline or {}).get("layers", {})

    for rule in RULES:
        candidate = candidates.get(rule.layer)
        if candidate is None:
            status = "fail" if require_all else "skip"
            verdicts.append(
                {
                    "layer": rule.layer,
                    "metric": rule.metric,
                    "status": status,
                    "candidate": None,
                    "baseline": None,
                    "bound": rule.bound,
                    "tolerance": rule.tolerance,
                    "reason": "本次没有这一层的报告" if status == "fail" else "本次未提供这一层",
                }
            )
            continue
        value = _get(candidate, rule.metric)
        base_value = _get(baseline_layers.get(rule.layer, {}), rule.metric)
        reasons: list[str] = []
        status = "pass"
        if value is None:
            status = "fail"
            reasons.append("指标缺失（不是 0）")
        else:
            value = float(value)
            if value < rule.bound:
                status = "fail"
                reasons.append(f"{value:.3f} < 绝对下限 {rule.bound:.3f}")
            if base_value is not None and value < float(base_value) - rule.tolerance:
                status = "fail"
                reasons.append(
                    f"{value:.3f} 比基线 {float(base_value):.3f} 低超过 {rule.tolerance:.3f}"
                )
        metric_errors = int(_get(candidate, "errors") or 0)
        if rule.metric == "pass_rate" and metric_errors > 0:
            status = "fail"
            reasons.append(f"{metric_errors} 行框架错误")
        verdicts.append(
            {
                "layer": rule.layer,
                "metric": rule.metric,
                "status": status,
                "candidate": value,
                "baseline": base_value,
                "bound": rule.bound,
                "tolerance": rule.tolerance,
                "reason": "；".join(reasons) if reasons else "通过",
                "note": rule.note,
            }
        )
    return verdicts


def render_markdown(
    *,
    verdicts: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Mapping[str, Any]],
    baseline_path: Path | None,
) -> str:
    lines = ["# 判分门禁", ""]
    if baseline_path is not None:
        lines.append(f"- 基线：`{baseline_path}`")
    lines += [
        "",
        "## 候选",
        "",
        "| 层 | 轮次 | 通过率 | 最差一次 | 框架错误 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for layer, candidate in candidates.items():
        lines.append(
            f"| `{layer}` | {candidate.get('repeat', 1)} | {_fmt(candidate.get('pass_rate'))} | "
            f"{_fmt(candidate.get('worst_pass_rate'))} | {candidate.get('errors', 0)} |"
        )
    lines += [
        "",
        "## 判据",
        "",
        "| 层 | 指标 | 候选 | 基线 | 下限 | 容忍 | 结论 | 原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for verdict in verdicts:
        mark = {"pass": "通过", "fail": "**失败**", "skip": "跳过"}[verdict["status"]]
        lines.append(
            f"| `{verdict['layer']}` | `{verdict['metric']}` | {_fmt(verdict['candidate'])} | "
            f"{_fmt(verdict['baseline'])} | {verdict['bound']:.3f} | {verdict['tolerance']:.3f} | "
            f"{mark} | {verdict['reason']} |"
        )
    lines += [
        "",
        "## 口径",
        "",
        "- 重复报告的通过率取中位数，最差一次单独判；两者都过才算通过。",
        "- 任何框架错误行都阻断：那是「没测出来」，不是「分数低」。",
        "- 容忍值来自当前样本量下的实测方差，不是统计显著结论；夜间跑得越多越该收紧。",
        "",
    ]
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判分门禁：L3 / L4 / L5 报告 vs 基线")
    parser.add_argument("--l3", type=Path, default=None)
    parser.add_argument("--l4", type=Path, default=None)
    parser.add_argument("--l5", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "judge-gate.md")
    parser.add_argument("--json", type=Path, default=REPO_ROOT / "reports" / "judge-gate.json")
    parser.add_argument("--require-all", action="store_true", help="缺少任一层即失败")
    parser.add_argument("--write-baseline", type=Path, default=None,
                        help="把当前候选写成新基线后退出（不跑判据）")
    args = parser.parse_args(argv)

    paths = {"L3": args.l3, "L4": args.l4, "L5": args.l5}
    candidates: dict[str, dict[str, Any]] = {}
    for expected, path in paths.items():
        if path is None:
            continue
        if not path.is_file():
            print(f"找不到 {expected} 报告：{path}", file=sys.stderr)
            return 2
        candidate = load_layer(path)
        if candidate["layer"] != expected:
            print(f"{path} 的 layer 是 {candidate['layer']}，不是 {expected}", file=sys.stderr)
            return 2
        candidates[expected] = candidate
    if not candidates:
        print("至少给一个报告：--l3 / --l4 / --l5。", file=sys.stderr)
        return 2

    if args.write_baseline is not None:
        payload = build_baseline(candidates)
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"已写入判分基线：{args.write_baseline}（{', '.join(sorted(candidates))}）")
        return 0

    baseline = None
    baseline_path = args.baseline if args.baseline and args.baseline.is_file() else None
    if baseline_path is not None:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    verdicts = evaluate(candidates, baseline, require_all=args.require_all)
    failed = [verdict for verdict in verdicts if verdict["status"] == "fail"]
    skipped = [verdict for verdict in verdicts if verdict["status"] == "skip"]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_markdown(verdicts=verdicts, candidates=candidates, baseline_path=baseline_path),
        encoding="utf-8",
        newline="\n",
    )
    payload = {
        "baseline": str(baseline_path) if baseline_path is not None else None,
        "candidates": candidates,
        "verdicts": verdicts,
        "failed": len(failed),
        "skipped": len(skipped),
    }
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"判分门禁：{len(verdicts) - len(failed) - len(skipped)} 过 / {len(failed)} 失败 / {len(skipped)} 跳过")
    print(f"报告：{args.out}")
    for verdict in failed:
        print(f"  FAIL {verdict['layer']} {verdict['metric']}: {verdict['reason']}")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
