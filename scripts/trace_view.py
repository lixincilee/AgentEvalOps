"""把 trace 渲染成一个自包含的 HTML 瀑布图（离线可看、可入库）。

为什么不直接用现成的观测服务：本机没有 Docker，Langfuse 服务端起不来。但「可展示的
trace」这件事不该等基础设施——一份 JSONL + 一个单文件 HTML，双击就能看，还能跟着
``releases/`` 一起入库。有了 Langfuse 之后它依然有用：不需要网络、不需要账号。

**这里的 HTML 必须转义。** 用例集里有提示注入样本，``message`` / ``query`` /
``error_message`` 全是用户可控文本；原样插进页面，就等于这个视图本身是一个 XSS。
"""

from __future__ import annotations

import argparse
import html
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from agentevalops.tracing import Span, SpanKind, SpanStatus, Trace, read_traces

#: 根 span 上会被当成「标签」显示的字段。没值的字段不显示——一屏 ``None`` 没有信息量。
CHIP_FIELDS = ("intent", "state", "tool_name", "error_code", "plan_source", "slot_source")

#: 每个 kind 上「值得看」的属性。不渲染它们，视图就只剩 span 名字——那等于把 trace
#: 里最该看的东西（用户说了什么、检索到什么、工具收到什么、模型答了什么）藏起来。
#: 这些都是用户可控文本，渲染时必须转义。
DETAIL_FIELDS = {
    SpanKind.TURN: ("message", "slot_order_id"),
    SpanKind.NODE: (),
    SpanKind.LLM: ("model", "plan_source", "parse", "response"),
    SpanKind.RETRIEVER: ("query", "hits", "doc_ids"),
    SpanKind.TOOL: ("arguments", "success", "error_code"),
    SpanKind.STATE: ("session_id", "order_id", "source"),
}

KIND_LABEL = {
    SpanKind.TURN: "turn",
    SpanKind.NODE: "node",
    SpanKind.LLM: "llm",
    SpanKind.RETRIEVER: "retriever",
    SpanKind.TOOL: "tool",
    SpanKind.STATE: "state",
}

_STYLE = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0;
       padding: 24px; background: #f6f7f9; color: #1f2328; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.summary { color: #57606a; margin: 0 0 20px; font-size: 13px; }
section.trace { background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
                padding: 14px 16px; margin-bottom: 14px; }
section.trace.error { border-left: 4px solid #cf222e; }
section.trace.ok { border-left: 4px solid #1a7f37; }
h2 { font-size: 14px; margin: 0 0 6px; font-weight: 600; }
h2 .dur { color: #57606a; font-weight: 400; margin-left: 8px; }
.badge { font-size: 11px; padding: 1px 6px; border-radius: 10px; background: #dafbe1;
         color: #1a7f37; margin-left: 6px; }
.badge.error { background: #ffebe9; color: #cf222e; }
.chips { margin: 0 0 10px; }
.chip { display: inline-block; font-size: 11px; background: #eef2f6; border-radius: 4px;
        padding: 1px 6px; margin: 0 6px 4px 0; color: #24292f; font-family: monospace; }
.waterfall { border-top: 1px solid #eaeef2; padding-top: 8px; }
.row { display: grid; grid-template-columns: 320px 1fr 150px; gap: 10px;
       align-items: center; height: 22px; font-size: 12px; }
.label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: monospace; }
.track { position: relative; height: 12px; background: #f2f4f7; border-radius: 3px; }
.bar { position: absolute; top: 0; height: 12px; border-radius: 3px; background: #54aeff; }
.bar.err { background: #cf222e; }
.meta { color: #57606a; font-family: monospace; text-align: right; font-size: 11px; }
.kind { font-size: 10px; padding: 0 4px; border-radius: 3px; margin-right: 6px;
        background: #eaeef2; color: #57606a; }
.kind-tool { background: #fff1e5; color: #bc4c00; }
.kind-retriever { background: #ddf4ff; color: #0969da; }
.kind-llm { background: #fbefff; color: #8250df; }
.kind-state { background: #dafbe1; color: #1a7f37; }
.kind-turn { background: #eaeef2; color: #24292f; font-weight: 600; }
.detail { font-family: monospace; font-size: 11px; color: #57606a; margin: -1px 0 4px 6px;
          padding: 2px 6px; background: #f6f8fa; border-radius: 3px; word-break: break-all; }
"""


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _depth(span: Span, by_id: dict[str, Span]) -> int:
    """缩进层级。防环（trace 是外部读进来的，不能假设它一定是一棵良构的树）。"""
    level = 0
    current = span
    seen: set[str] = set()
    while current.parent_id and level < 8:
        if current.span_id in seen:
            break
        seen.add(current.span_id)
        parent = by_id.get(current.parent_id)
        if parent is None:
            break
        current = parent
        level += 1
    return level


def _bar_style(span: Span, total_ms: float) -> str:
    total = total_ms if total_ms > 0 else 1.0
    left = max(0.0, min(100.0, span.start_ms / total * 100))
    width = max(0.6, min(100.0 - left, span.duration_ms / total * 100))
    return f"left:{left:.1f}%;width:{width:.1f}%"


def _render_detail(span: Span) -> str:
    """把 span 上值得看的属性摊成一行，全部转义后再插进 HTML。"""
    parts = []
    for name in DETAIL_FIELDS.get(span.kind, ()):
        value = span.attributes.get(name)
        if value in (None, ""):
            continue
        parts.append(f"{name}={value}")
    if span.error_message:
        parts.append(f"error={span.error_message}")
    if not parts:
        return ""
    return f'<div class="detail">{_escape(" · ".join(parts))}</div>'


def _render_span_rows(spans: Sequence[Span], total_ms: float) -> str:
    by_id = {span.span_id: span for span in spans}
    blocks = []
    for span in spans:
        kind = str(span.kind)
        label = KIND_LABEL.get(span.kind, kind)
        failed = span.status == SpanStatus.ERROR
        meta = f"{span.duration_ms:.1f} ms"
        if failed and span.error_code:
            meta = f"{span.error_code} · {meta}"
        blocks.append(
            '<div class="span">'
            '<div class="row">'
            f'<div class="label" style="padding-left:{_depth(span, by_id) * 14}px">'
            f'<span class="kind kind-{_escape(kind)}">{_escape(label)}</span>'
            f"{_escape(span.name)}</div>"
            f'<div class="track"><div class="bar{" err" if failed else ""}" '
            f'style="{_bar_style(span, total_ms)}"></div></div>'
            f'<div class="meta">{_escape(meta)}</div>'
            "</div>"
            f"{_render_detail(span)}"
            "</div>"
        )
    return "\n".join(blocks)


def _render_chips(trace: Trace) -> str:
    root = trace.root
    attributes = {} if root is None else root.attributes
    chips = []
    for field in CHIP_FIELDS:
        value = attributes.get(field)
        if value in (None, ""):
            continue
        chips.append(f'<span class="chip">{_escape(field)}={_escape(value)}</span>')
    if not chips:
        return ""
    return f'<p class="chips">{"".join(chips)}</p>'


def render(traces: Sequence[Trace], *, title: str = "AgentEvalOps trace 视图") -> str:
    """渲染成一份完整的、自包含的 HTML 字符串。"""
    failed = [trace for trace in traces if trace.status == SpanStatus.ERROR]
    kinds = Counter(str(span.kind) for trace in traces for span in trace.spans)
    kind_text = " / ".join(f"{name} {count}" for name, count in sorted(kinds.items()))

    sections = []
    for trace in traces:
        total = trace.duration_ms
        if trace.spans:
            total = max(total, max(span.start_ms + span.duration_ms for span in trace.spans))
        error_count = len(trace.errors())
        badge = (
            f'<span class="badge error">error × {error_count}</span>'
            if trace.status == SpanStatus.ERROR
            else '<span class="badge">ok</span>'
        )
        css = "error" if trace.status == SpanStatus.ERROR else "ok"
        sections.append(
            f'<section class="trace {css}">'
            f"<h2>{_escape(trace.case_id or trace.trace_id)}{badge}"
            f'<span class="dur">{trace.duration_ms:.1f} ms · {len(trace.spans)} spans</span></h2>'
            f"{_render_chips(trace)}"
            f'<div class="waterfall">{_render_span_rows(trace.spans, total)}</div>'
            "</section>"
        )

    generated = datetime.now(timezone.utc).isoformat()
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{_escape(title)}</title>\n<style>{_STYLE}</style>\n</head>\n<body>\n"
        f"<h1>{_escape(title)}</h1>\n"
        f'<p class="summary">{len(traces)} 条 trace，失败 {len(failed)} 条'
        f"{' · ' + _escape(kind_text) if kind_text else ''}"
        f" · 生成于 {_escape(generated)}</p>\n"
        f"{''.join(sections)}\n</body>\n</html>\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把 trace 渲染成自包含的 HTML 瀑布图")
    parser.add_argument("--traces", type=Path, action="append", required=True,
                        help="trace JSONL（可重复）")
    parser.add_argument("--out", type=Path, default=Path("reports") / "trace-view.html",
                        help="输出 HTML 路径")
    parser.add_argument("--limit", type=int, default=0, help="最多渲染多少条（0 = 不限）")
    parser.add_argument("--title", default="AgentEvalOps trace 视图")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    missing = [str(path) for path in args.traces if not path.exists()]
    if missing:
        print(f"文件不存在：{', '.join(missing)}", file=sys.stderr)
        return 2

    traces = [trace for path in args.traces for trace in read_traces(path)]
    if args.limit:
        traces = traces[: args.limit]
    if not traces:
        print("没有读到任何 trace。", file=sys.stderr)
        return 1

    document = render(traces, title=args.title)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(document.encode("utf-8"))
    if not args.quiet:
        failed = sum(1 for trace in traces if trace.status == SpanStatus.ERROR)
        print(f"trace {len(traces)} 条，失败 {failed} 条 → {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())