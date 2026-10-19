r"""生成人工抽检表，并在回填后计算一致性。

    D:\Python\python.exe scripts\review_sheet.py --n 20 --write-template

产物：

* ``reports/review-sheet.md``：每条用例的问题 / 回答 / 资料 / 机器分数 + 空白人工结论；
* ``reports/review-labels.jsonl``：回填模板（**已存在时绝不覆盖**，否则会吃掉人工标注）；
* ``reports/review-agreement.json``：一致率、Cohen's kappa、假通过 / 假失败、跨层一致性。

退出码：0 生成成功 / 2 用法或输入文件问题。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

from agentevalops.judging.base import utc_now  # noqa: E402
from agentevalops.judging.review import (  # noqa: E402
    agreement_with_labels,
    cross_layer_agreement,
    label_template,
    load_labels,
    load_layer,
    render_sheet,
    select_review_cases,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="从判分报告生成人工抽检表")
    parser.add_argument("--l3", type=Path, default=REPO_ROOT / "reports" / "l3-ragas.json")
    parser.add_argument("--l4", type=Path, default=REPO_ROOT / "reports" / "l4-deepeval.json")
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "reports" / "review-sheet.md")
    parser.add_argument("--labels", type=Path, default=REPO_ROOT / "reports" / "review-labels.jsonl")
    parser.add_argument("--agreement", type=Path, default=REPO_ROOT / "reports" / "review-agreement.json")
    parser.add_argument("--write-template", action="store_true", help="labels 不存在时写一份空模板")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n <= 0:
        print("--n 必须大于 0。", file=sys.stderr)
        return 2

    views = []
    missing = []
    for path in (args.l3, args.l4):
        if path is None:
            continue
        if not path.is_file():
            missing.append(str(path))
            continue
        views.extend(load_layer(path))
    if missing:
        print("找不到报告：" + "，".join(missing), file=sys.stderr)
        print("先跑 judge_l3_ragas.py / judge_l4_deepeval.py。", file=sys.stderr)
        return 2
    if not views:
        print("报告里没有可抽检的用例。", file=sys.stderr)
        return 2

    items = select_review_cases(views, n=args.n, seed=args.seed)
    try:
        labels = load_labels(args.labels)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    agreement = agreement_with_labels(items, labels)
    cross = None
    if args.l3.is_file() and args.l4.is_file():
        cross = cross_layer_agreement(load_layer(args.l3), load_layer(args.l4))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        render_sheet(items, generated_at=utc_now(), agreement=agreement),
        encoding="utf-8",
    )
    payload = {"generated_at": utc_now(), "selected": len(items), "human": agreement, "cross_layer": cross}
    args.agreement.parent.mkdir(parents=True, exist_ok=True)
    args.agreement.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    if args.write_template and not args.labels.exists():
        args.labels.parent.mkdir(parents=True, exist_ok=True)
        args.labels.write_text(label_template(items), encoding="utf-8")

    print(f"抽检 {len(items)} 条 → {args.out}")
    print(f"已回填 {agreement['labeled']}/{agreement['selected']}（覆盖率 {agreement['coverage']}）")
    if cross is not None:
        print(
            f"跨层一致性（L3 vs L4）：{cross['common_cases']} 条共同用例，"
            f"kappa={cross['kappa']}，分歧 {len(cross['disagreements'])} 条"
        )
    if agreement["labeled"] == 0:
        print("提示：还没有人工回填，因此不存在人工一致率；模板在 " + str(args.labels))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
