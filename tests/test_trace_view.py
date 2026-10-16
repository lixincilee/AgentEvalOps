"""trace 视图的测试：几何、转义、退出码。

重点是那条转义：用例集里有提示注入样本，视图把用户文本原样插进 HTML，就是自己造一个
XSS。另外几何要按 ``start_ms`` / ``duration_ms`` 算，否则「瀑布图」只是好看，看不出
哪一段慢。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from agentevalops.tracing import JsonlEmitter, MemoryEmitter, Span, SpanKind, Tracer

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


view = _load("trace_view", "trace_view.py")


def make_trace(*, case_id: str = "c1", message: str = "hi", intent: str | None = None):
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, case_id=case_id)
    attributes = {"message": message}
    if intent is not None:
        attributes["intent"] = intent
    with tracer.turn("agent.turn", **attributes):
        with tracer.span("node.validate", kind=SpanKind.NODE):
            pass
        with tracer.span(
            "retriever.search",
            kind=SpanKind.RETRIEVER,
            query=message,
            hits=1,
            grounded=True,
        ):
            pass
    return emitter.traces[0]


def trail_with_offsets() -> Any:
    """直接按偏移量造一条 trace：这里测的是渲染几何，不是计时器。"""
    from agentevalops.tracing import Trace

    trace = Trace(started_at="2026-10-16T00:00:00+00:00", trace_id="tr_x", case_id="c1")
    trace.duration_ms = 100.0
    root = Span(name="agent.turn", kind=SpanKind.TURN, span_id="s0", duration_ms=100.0)
    child = Span(
        name="tool.query_order_status",
        kind=SpanKind.TOOL,
        span_id="s1",
        parent_id="s0",
        start_ms=50.0,
        duration_ms=25.0,
    )
    trace.spans = [root, child]
    return trace


# --- 转义 ---------------------------------------------------------------------


def test_escapes_user_controlled_message():
    document = view.render([make_trace(message="<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document


def test_escapes_query_and_chip_values():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, case_id="c1")
    with tracer.turn("agent.turn", intent='"><img src=x onerror=alert(1)>'):
        with tracer.span(
            "retriever.search", kind=SpanKind.RETRIEVER, query="<b>忽略之前的指令</b>"
        ):
            pass
    document = view.render(emitter.traces)
    assert "<img src=x" not in document
    assert "&lt;img src=x" in document
    assert "<b>忽略之前的指令</b>" not in document


# --- 几何与内容 ---------------------------------------------------------------


def test_bar_geometry_follows_the_recorded_offsets():
    document = view.render([trail_with_offsets()])
    assert "left:50.0%;width:25.0%" in document
    assert "left:0.0%;width:100.0%" in document


def test_renders_every_span_with_a_kind_badge():
    document = view.render([make_trace()])
    assert "agent.turn" in document
    assert "node.validate" in document
    assert "retriever.search" in document
    assert 'kind kind-retriever">retriever<' in document


def test_renders_the_error_code_of_a_failed_span():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, case_id="c1")
    with tracer.turn("agent.turn"):
        with tracer.span("tool.query_order_status", kind=SpanKind.TOOL) as tool:
            tool.fail("TOOL_TIMEOUT", "工具超时")
    document = view.render(emitter.traces)
    assert "TOOL_TIMEOUT" in document
    assert "bar err" in document


def test_chips_skip_fields_without_a_value():
    document = view.render([make_trace(intent="order_status")])
    assert "intent=order_status" in document
    assert "tool_name" not in document  # 没值的字段不许渲染成 None


def test_indentation_follows_the_span_tree():
    document = view.render([make_trace()])
    assert "padding-left:0px" in document  # 根 span
    assert "padding-left:14px" in document  # 子 span


def test_a_span_cycle_does_not_hang_the_renderer():
    from agentevalops.tracing import Trace

    trace = Trace(started_at="2026-10-16T00:00:00+00:00", trace_id="tr_y", case_id="c9")
    trace.spans = [
        Span(name="a", span_id="a", parent_id="b", duration_ms=1.0),
        Span(name="b", span_id="b", parent_id="a", duration_ms=1.0),
    ]
    document = view.render([trace])
    assert document.count('class="row"') == 2


def test_empty_trace_list_still_renders_a_summary():
    document = view.render([])
    assert "0 条 trace" in document
    assert "<!doctype html>" in document


def test_limit_is_respected_by_the_cli(tmp_path):
    path = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlEmitter(path))
    for index in range(4):
        with tracer.turn("agent.turn", index=index):
            pass
    out = tmp_path / "view.html"
    assert view.main(["--traces", str(path), "--limit", "2", "--out", str(out), "--quiet"]) == 0
    assert out.read_text(encoding="utf-8").count('<section class="trace') == 2


def test_main_writes_a_lf_terminated_file(tmp_path):
    path = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlEmitter(path), case_id="c1")
    with tracer.turn("agent.turn", message="hi"):
        pass
    out = tmp_path / "view.html"
    assert view.main(["--traces", str(path), "--out", str(out), "--quiet"]) == 0
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert raw.startswith(b"<!doctype html>")


def test_main_returns_two_for_a_missing_file(tmp_path):
    assert view.main(["--traces", str(tmp_path / "没有.jsonl")]) == 2


def test_main_returns_one_when_there_are_no_traces(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert view.main(["--traces", str(empty), "--quiet"]) == 1