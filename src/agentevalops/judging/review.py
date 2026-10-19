"""人工抽检：把判分报告变成一张人能复核、能回填的表。

为什么必须有这一步：Ragas / GEval 的分数是模型给的，模型判错时报告会**自信地错**。
抽检不是再跑一个模型，而是让一个人看原文回答，回答两个问题：

1. 机器判通过的用例，人是否也认为通过（防假通过）；
2. 机器判失败的用例，人是否也认为失败（防假失败）。

一致性用 Cohen's kappa 而不是简单的「一致率」：一致率会把「两边都判通过」的多数类
算成一致性，kappa 扣掉了随机一致。样本量小的时候 kappa 的区间很宽，报告里必须写
「这不是统计显著结论」。

跨层一致性（L3 vs L4 对同一条用例的通过与否）是**另一个东西**：两套 Judge 都错时
它们也可能一致。它只能提示「哪条用例值得人先看」，不能替代人工标注。
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

NL = chr(10)


@dataclass(frozen=True)
class CaseView:
    """一条用例在一层里的可复核视图。"""

    layer: str
    case_id: str
    passed: bool
    errored: bool
    scores: tuple[dict[str, Any], ...] = ()
    question: str = ""
    answer: str = ""
    contexts: tuple[str, ...] = ()

    @property
    def reason(self) -> str:
        parts = []
        for score in self.scores:
            value = "n/a" if score.get("score") is None else f"{float(score['score']):.3f}"
            parts.append(f"{score.get('metric')}={value}")
        return "；".join(parts)


def load_layer(path: Path) -> list[CaseView]:
    """读单次报告或重复报告的 JSON，摊成 case 级视图。

    重复报告的 `scores` 取第一轮（抽检看的是原文，不是轮次）；通过与否仍按该轮
    所有指标判定。
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    layer = str(payload.get("layer") or "?")
    rows = list(payload.get("scores") or payload.get("rows") or [])
    samples = {
        str(sample.get("case_id")): sample
        for sample in payload.get("samples") or []
        if isinstance(sample, Mapping)
    }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if isinstance(row, Mapping):
            grouped.setdefault(str(row.get("case_id")), []).append(dict(row))

    views: list[CaseView] = []
    for case_id, case_rows in grouped.items():
        sample = samples.get(case_id, {})
        errored = any(row.get("error") for row in case_rows)
        passed = bool(case_rows) and all(bool(row.get("passed")) for row in case_rows) and not errored
        views.append(
            CaseView(
                layer=layer,
                case_id=case_id,
                passed=passed,
                errored=errored,
                scores=tuple(case_rows),
                question=str(sample.get("question") or ""),
                answer=str(sample.get("answer") or ""),
                contexts=tuple(str(text) for text in sample.get("contexts") or ()),
            )
        )
    return views


def select_review_cases(
    views: Sequence[CaseView],
    n: int = 20,
    *,
    seed: int = 7,
    include_all_failures: bool = True,
) -> list[CaseView]:
    """失败优先、其余随机。随机源固定，报告可复现；不随 Python 版本变。"""
    unique: dict[tuple[str, str], CaseView] = {}
    for view in views:
        unique.setdefault((view.layer, view.case_id), view)
    ordered = [unique[key] for key in sorted(unique)]
    failures = [view for view in ordered if not view.passed or view.errored]
    passes = [view for view in ordered if view.passed and not view.errored]

    rng = random.Random(seed)
    rng.shuffle(failures)
    rng.shuffle(passes)

    # 全抽失败样本只能测「假失败」，测不到「假通过」。n >= 4 时至少留 1/4 给通过样本。
    pass_quota = 0 if n < 4 else max(1, n // 4)
    pass_quota = min(pass_quota, len(passes))
    failure_quota = max(0, n - pass_quota)
    selected = failures[:failure_quota] + passes[:pass_quota]
    if len(selected) < n:
        selected += passes[pass_quota:][: n - len(selected)]
    return selected


def render_sheet(
    items: Sequence[CaseView],
    *,
    generated_at: str,
    agreement: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        "# 判分抽检表",
        "",
        f"- 生成时间：{generated_at}",
        f"- 抽检数量：{len(items)} 条（失败 / 错误优先；至少 1/4 是通过样本，否则测不出假通过）",
        "- 怎么填：逐条读「问题 / 回答 / 资料」，判断回答是否只依据资料、该拒答时是否拒答。",
        "- 回填：在 labels JSONL 里把 `verdict` 改成 `pass` 或 `fail`，再填 `note` / `reviewer`。",
        "- 没有人工回填之前，**不存在**人工一致率；模型不能自评冒充人工。",
        "",
    ]
    if agreement is not None:
        lines += _agreement_lines(agreement) + [""]

    for index, item in enumerate(items, start=1):
        mark = "通过" if item.passed else ("错误" if item.errored else "失败")
        lines += [
            f"## {index}. `{item.layer}` / `{item.case_id}`",
            "",
            f"- 机器结论：**{mark}**（{item.reason}）",
            f"- 问题：{item.question or '（报告里没有样本原文；请用 --include-samples 重跑）'}",
            f"- 回答：{item.answer or '（报告里没有样本原文）'}",
        ]
        if item.scores:
            lines += ["", "| 指标 | 分数 | 阈值 | 通过 | 理由 |", "| --- | --- | --- | --- | --- |"]
            for score in item.scores:
                value = "n/a" if score.get("score") is None else f"{float(score['score']):.3f}"
                reason = str(score.get("reason") or score.get("error") or "").replace("|", "/").replace(NL, " ")[:300]
                lines.append(
                    f"| `{score.get('metric')}` | {value} | {score.get('threshold')} | "
                    f"{'是' if score.get('passed') else '否'} | {reason} |"
                )
        if item.contexts:
            lines += ["", "<details><summary>检索资料（点击展开）</summary>", ""]
            for number, context in enumerate(item.contexts, start=1):
                lines.append(f"[资料 {number}] {context}")
                lines.append("")
            lines += ["</details>"]
        lines += [
            "",
            "- 人工结论：[ ] 通过　[ ] 不通过　备注：____________________",
            "",
        ]
    return NL.join(lines) + NL


def label_template(items: Sequence[CaseView]) -> str:
    lines = []
    for item in items:
        lines.append(
            json.dumps(
                {
                    "layer": item.layer,
                    "case_id": item.case_id,
                    "verdict": None,
                    "note": "",
                    "reviewer": "",
                    "reviewed_at": "",
                },
                ensure_ascii=False,
            )
        )
    return NL.join(lines) + (NL if lines else "")


def load_labels(path: Path) -> dict[tuple[str, str], str]:
    """只收 `pass` / `fail`；`null` 与空文件表示「还没填」。"""
    labels: dict[tuple[str, str], str] = {}
    if not Path(path).is_file():
        return labels
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        verdict = record.get("verdict")
        if verdict is None:
            continue
        if verdict not in ("pass", "fail"):
            raise ValueError(f"{path}:{number} verdict 只能是 pass / fail / null，收到 {verdict!r}")
        labels[(str(record.get("layer")), str(record.get("case_id")))] = str(verdict)
    return labels


def cohen_kappa(machine: Sequence[bool], human: Sequence[bool]) -> dict[str, Any]:
    """二分类 Cohen's kappa。样本为空或边际完全一致时 kappa 无定义，返回 None。"""
    if len(machine) != len(human):
        raise ValueError("machine / human 长度不一致")
    n = len(machine)
    if n == 0:
        return {"n": 0, "agreement": None, "kappa": None}
    agree = sum(1 for a, b in zip(machine, human) if a == b)
    po = agree / n
    machine_pos = sum(machine) / n
    human_pos = sum(human) / n
    pe = machine_pos * human_pos + (1 - machine_pos) * (1 - human_pos)
    kappa = None if pe >= 1.0 else (po - pe) / (1 - pe)
    return {
        "n": n,
        "agreement": round(po, 6),
        "kappa": None if kappa is None else round(kappa, 6),
        "machine_pass_rate": round(machine_pos, 6),
        "human_pass_rate": round(human_pos, 6),
    }


def agreement_with_labels(
    items: Sequence[CaseView], labels: Mapping[tuple[str, str], str]
) -> dict[str, Any]:
    matched = [
        (item, labels[(item.layer, item.case_id)])
        for item in items
        if (item.layer, item.case_id) in labels
    ]
    machine = [item.passed for item, _ in matched]
    human = [verdict == "pass" for _, verdict in matched]
    stats = cohen_kappa(machine, human)
    false_pass = [item.case_id for item, verdict in matched if item.passed and verdict == "fail"]
    false_fail = [item.case_id for item, verdict in matched if not item.passed and verdict == "pass"]
    return {
        **stats,
        "selected": len(items),
        "labeled": len(matched),
        "coverage": round(len(matched) / len(items), 6) if items else None,
        "false_pass": false_pass,
        "false_fail": false_fail,
    }


def cross_layer_agreement(
    left: Sequence[CaseView], right: Sequence[CaseView]
) -> dict[str, Any]:
    """两层对同一批 case 的通过与否是否一致。**不是**正确率，只是分歧提示。"""
    left_map = {view.case_id: view for view in left}
    right_map = {view.case_id: view for view in right}
    common = sorted(set(left_map) & set(right_map))
    machine = [left_map[case].passed for case in common]
    other = [right_map[case].passed for case in common]
    stats = cohen_kappa(machine, other)
    return {
        **stats,
        "common_cases": len(common),
        "only_left": sorted(set(left_map) - set(right_map)),
        "only_right": sorted(set(right_map) - set(left_map)),
        "disagreements": [
            case for case in common if left_map[case].passed != right_map[case].passed
        ],
    }


def _agreement_lines(agreement: Mapping[str, Any]) -> list[str]:
    kappa = agreement.get("kappa")
    lines = [
        "## 人工一致性",
        "",
        f"- 已回填：{agreement.get('labeled', 0)} / {agreement.get('selected', 0)}"
        f"（覆盖率 {agreement.get('coverage')}）",
        f"- 一致率：{agreement.get('agreement')}；Cohen's kappa："
        f"{'n/a（样本为空或边际无变异）' if kappa is None else kappa}",
        f"- 假通过：{', '.join(agreement.get('false_pass') or []) or '无'}",
        f"- 假失败：{', '.join(agreement.get('false_fail') or []) or '无'}",
        "- 样本量小，kappa 的置信区间很宽：这是抽检，不是统计显著结论。",
    ]
    return lines
