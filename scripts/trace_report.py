"""把一批 trace 变成「这一轮到底坏在哪一层」的报告。

用法::

    D:\\Python\\python.exe scripts/trace_report.py --traces reports/traces.jsonl --cases reports/traces.cases.jsonl
    D:\\Python\\python.exe scripts/trace_report.py --traces reports/traces.jsonl \\
        --out reports/trace-report.md --json reports/trace-report.json --fail-on-error

它回答的问题只有一个：**失败案例该去找哪一块代码**。所以输出不是「通过率」，
而是「按环节聚类 + 每条的定位理由」——通过率是 compare_versions.py / pytest 的活。

定位规则（优先级从上到下，写成一张表而不是一坨 if，是为了能被逐条测试）：

1. 模型 span 失败            → ``reasoning`` / ``model-error``（限流、超时、5xx）
2. 检索 span 失败            → ``retrieval``（检索工具坏了）
3. 请求级校验不过            → ``request``（空消息、缺 customer_id）；但「意图认出来了、
   槽位没抽到」虽然也是 VALIDATION_ERROR，归 ``state`` —— 要去查的是槽位解析与会话继承，
   报成请求校验会把人指到错误的代码上
4. 工具 span 抛故障          → ``tool``
5. 错误码是 ``NO_KNOWLEDGE`` → ``retrieval``（知识库里没有依据）
6. 检索命中 0（grounded 假） → ``retrieval``
7. 走了兜底规划              → ``reasoning`` / ``prompt-format``（模型答了但不能用）
8. 槽位 / 继承相关字段不符   → ``state``
9. 工具返回失败（业务失败）  → ``tool``
10. 意图不符                 → ``reasoning`` / ``intent``
11. 其余                     → ``unclassified``

``unclassified`` 不是「没问题」，是「规则没覆盖」——它必须被当成待办，而不是噪声。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentevalops.tracing import SpanKind, SpanStatus, Trace, read_traces

#: 期望字段 → trace 根 span 上的属性名。**只在这里定义一次**，判分与报告都读它。
FIELD_TO_ATTR = {
    "expected_intent": "intent",
    "expected_state": "state",
    "expected_tool": "tool_name",
    "expected_error_code": "error_code",
    "expected_plan_source": "plan_source",
    "expected_order_id": "slot_order_id",
    "expected_slot_source": "slot_source",
}

#: 环节中文名。报告给人看，代码给人读，别混在一起。
STAGE_LABEL = {
    "retrieval": "检索层",
    "reasoning": "推理层",
    "tool": "工具层",
    "state": "状态管理层",
    "request": "请求校验层",
    "unclassified": "未定位",
}


@dataclass(frozen=True)
class Mismatch:
    """一个对不上的字段。带上期望与实际，报告里不用再去翻原始 trace。"""

    field: str
    attribute: str
    expected: Any
    observed: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "attribute": self.attribute,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass
class Verdict:
    case_id: str
    trace_id: str
    passed: bool
    stage: str | None
    detail: str | None
    mismatches: list[Mismatch] = field(default_factory=list)
    observed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "trace_id": self.trace_id,
            "passed": self.passed,
            "stage": self.stage,
            "detail": self.detail,
            "mismatches": [item.to_dict() for item in self.mismatches],
            "observed": self.observed,
        }


def split_case_id(case_id: str) -> tuple[str, int]:
    """``mt_ref_01#t1`` → ``("mt_ref_01", 1)``。多轮用例一轮一条 trace。"""
    head, _, tail = case_id.rpartition("#t")
    if head and tail.isdigit():
        return head, int(tail)
    return case_id, 0


def expectations_for(case: dict[str, Any] | None, turn_index: int) -> dict[str, Any]:
    """多轮用例的期望值在 ``turns[i]`` 里，单轮用例的在顶层。"""
    if not case:
        return {}
    turns = case.get("turns")
    if turns:
        if turn_index >= len(turns):
            return {}
        return dict(turns[turn_index])
    return case


def _root_attrs(trace: Trace) -> dict[str, Any]:
    root = trace.root
    return {} if root is None else dict(root.attributes)


def compare(trace: Trace, expected: dict[str, Any]) -> list[Mismatch]:
    """逐字段比对期望与实际。只比对**用例里写了**的字段。

    ``expected_error_code: null`` 与「没写这个字段」是两件事：前者是「不该有错误码」，
    后者是「这条用例不管错误码」。
    """
    attrs = _root_attrs(trace)
    out: list[Mismatch] = []
    for key, attribute in FIELD_TO_ATTR.items():
        if key not in expected:
            continue
        want = expected[key]
        got = attrs.get(attribute)
        if want is None and got is None:
            continue
        if want != got:
            out.append(Mismatch(key, attribute, want, got))
    return out


def locate(trace: Trace, mismatches: list[Mismatch]) -> tuple[str, str]:
    """定位失败环节。返回 ``(环节, 细节)``。"""
    attrs = _root_attrs(trace)
    llm = trace.by_name("llm.plan")
    retriever = trace.by_name("retriever.search")
    tools = trace.of_kind(SpanKind.TOOL)

    if llm is not None and llm.status == SpanStatus.ERROR:
        return "reasoning", f"model-error:{llm.error_code}"
    if retriever is not None and retriever.status == SpanStatus.ERROR:
        return "retrieval", f"retriever-error:{retriever.error_code}"
    if attrs.get("state") == "validation_failed":
        # 校验失败有两种，长得一样但不能混为一谈：请求本身不合法（空消息 / 缺 customer_id），
        # 和「意图认出来了、槽位却没抽到」。后者要去查的是 planner 的槽位解析与会话继承。
        # tool_name 有没有值就是判据：dispatch 在缺 order_id 时会把「本来要调的工具」记下来。
        if attrs.get("tool_name"):
            return "state", "missing-slot"
        return "request", "request-validation"

    faulted = [span for span in tools if span.status == SpanStatus.ERROR]
    if faulted:
        return "tool", f"tool-error:{faulted[0].error_code}"

    if attrs.get("error_code") == "NO_KNOWLEDGE":
        return "retrieval", "no-grounding"
    if retriever is not None and not retriever.attributes.get("grounded", True):
        return "retrieval", "no-hit"

    if str(attrs.get("plan_source") or "").endswith("fallback"):
        return "reasoning", "prompt-format"

    fields = {item.field for item in mismatches}
    if fields & {"expected_order_id", "expected_slot_source"}:
        return "state", "slot-inheritance"

    failed = [span for span in tools if span.attributes.get("success") is False]
    if failed:
        return "tool", f"tool-failed:{failed[0].attributes.get('error_code')}"

    if "expected_intent" in fields:
        return "reasoning", "intent"
    if "expected_tool" in fields:
        return "tool", "tool-selection"
    if "expected_state" in fields:
        return "state", "final-state"
    return "unclassified", ""


def judge(traces: list[Trace], cases: dict[str, dict[str, Any]]) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for trace in traces:
        case_id = trace.case_id or "<无 case_id>"
        base_id, turn_index = split_case_id(case_id)
        case = cases.get(base_id) or cases.get(case_id)
        expected = expectations_for(case, turn_index)
        mismatches = compare(trace, expected)
        if not expected:
            # 没给期望值：只做描述统计，不用「没报错」冒充「通过」。
            stage, detail = (None, None)
            if trace.status == SpanStatus.ERROR:
                stage, detail = locate(trace, mismatches)
            verdicts.append(
                Verdict(case_id, trace.trace_id, True, stage, detail, mismatches,
                        _root_attrs(trace))
            )
            continue
        passed = not mismatches
        stage, detail = (None, None) if passed else locate(trace, mismatches)
        verdicts.append(
            Verdict(case_id, trace.trace_id, passed, stage, detail, mismatches,
                    _root_attrs(trace))
        )
    return verdicts


def render_markdown(
    verdicts: list[Verdict], *, trace_files: list[Path], case_files: list[Path], skipped: int
) -> str:
    failed = [item for item in verdicts if not item.passed]
    stages = Counter(item.stage for item in failed if item.stage)
    lines: list[str] = []
    lines.append("# 失败环节定位报告\n")
    lines.append(f"- 生成时间：{datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 输入 trace：{', '.join(str(path) for path in trace_files)}（{len(verdicts)} 条）")
    if skipped:
        lines.append(f"- 跳过的坏行：{skipped} 行（截断或非法 JSON）")
    lines.append(
        "- 判分依据："
        + (", ".join(str(path) for path in case_files) if case_files else "**未给期望值**（只做描述统计）")
    )
    lines.append(f"- 结果：**{len(failed)}/{len(verdicts)} 条不通过**\n")

    lines.append("## 按环节聚类\n")
    if stages:
        lines.append("| 环节 | 条数 | 典型细节 | 涉及用例 |")
        lines.append("| --- | --- | --- | --- |")
        for stage, count in stages.most_common():
            details = Counter(item.detail for item in failed if item.stage == stage)
            cases = ", ".join(item.case_id for item in failed if item.stage == stage)[:120]
            label = f"{stage}（{STAGE_LABEL.get(stage, '?')}）"
            lines.append(f"| {label} | {count} | {details.most_common(1)[0][0]} | {cases} |")
    else:
        lines.append("没有定位到任何失败环节。\n")

    lines.append("\n## 逐条\n")
    if failed:
        lines.append("| case | 环节 | 细节 | 对不上的字段（期望 → 实际） |")
        lines.append("| --- | --- | --- | --- |")
        for item in failed:
            diff = "; ".join(
                f"{m.field}={m.expected!r} → {m.observed!r}" for m in item.mismatches
            ) or "（根 span 状态为 error）"
            lines.append(
                f"| {item.case_id} | {item.stage or '-'} | {item.detail or '-'} | {diff[:200]} |"
            )
    else:
        lines.append("没有不通过的用例。\n")

    lines.append("\n## 口径\n")
    lines.append(
        "- 逐字段比对，只比对用例里**写了**的期望字段；`expected_error_code: null` 的意思是「不该有错误码」。"
    )
    lines.append("- 环节定位按固定优先级（见 `scripts/trace_report.py` 的模块文档），先硬事实（span 失败）")
    lines.append(
        "  再间接证据（无依据、兜底规划），最后才按对不上的字段名归类。`unclassified` 是要补规则，不是噪声。"
    )
    lines.append("- 这份报告不判「通过率」：那是 pytest 与 `compare_versions.py` 的职责，同一批 trace 不应该有两套分数。")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 trace 变成失败环节定位报告")
    parser.add_argument("--traces", type=Path, action="append", required=True,
                        help="trace JSONL（可重复）")
    parser.add_argument("--cases", type=Path, action="append", default=[],
                        help="期望值 JSONL（可重复；不给就只做描述统计）")
    parser.add_argument("--out", type=Path, default=None, help="markdown 报告输出路径")
    parser.add_argument("--json", type=Path, default=None, help="机器可读输出路径")
    parser.add_argument("--fail-on-error", action="store_true",
                        help="有不通过用例时返回 1（给 CI 用）")
    parser.add_argument("--quiet", action="store_true")
    return parser


def _load_cases(paths: list[Path]) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            case_id = record.get("case_id")
            if case_id:
                cases[str(case_id)] = record
    return cases


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    missing = [str(path) for path in (*args.traces, *args.cases) if not path.exists()]
    if missing:
        print(f"文件不存在：{', '.join(missing)}", file=sys.stderr)
        return 2

    traces: list[Trace] = []
    raw_lines = 0
    for path in args.traces:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        raw_lines += len(lines)
        traces.extend(read_traces(path))
    if not traces:
        print("没有读到任何 trace。", file=sys.stderr)
        return 1

    verdicts = judge(traces, _load_cases(args.cases))
    markdown = render_markdown(
        verdicts, trace_files=args.traces, case_files=args.cases, skipped=raw_lines - len(traces)
    )
    failed = [item for item in verdicts if not item.passed]
    stages = Counter(item.stage for item in failed if item.stage)

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(markdown.encode("utf-8"))
    if args.json is not None:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "traces": len(verdicts),
            "failed": len(failed),
            "stages": dict(stages),
            "verdicts": [item.to_dict() for item in verdicts],
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_bytes(
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        )

    if not args.quiet:
        print(f"trace {len(verdicts)} 条，不通过 {len(failed)} 条")
        if stages:
            print("按环节：" + "，".join(f"{stage} {count}" for stage, count in stages.most_common()))
        if args.out is not None:
            print(f"markdown: {args.out}")
        if args.json is not None:
            print(f"json    : {args.json}")

    if args.fail_on_error and failed:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())