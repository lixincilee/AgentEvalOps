"""Tracing 层的契约测试：离线纯逻辑，不发网络请求、不读 reports/ 下的运行产物。

守四件事：

1. 没有 tracer 时一切都是空操作，被测行为逐字节不变（否则「加了可观测性」就变成了
   换了一个被测系统，M3 之前所有回归数字都要重跑）；
2. span 树 / 父子关系 / 时长 / 失败标记都对得上；
3. 落盘口径（一行一 trace、LF、UTF-8）与脱敏（密钥 / 邮箱 / 手机号）；
4. Langfuse 出口：载荷映射对，发不出去时降级而不是丢数据。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from agentevalops.faults import FaultSpec, FaultyTool
from agentevalops.llm.base import BudgetExceededError, LLMResponse, LLMUnavailableError, Usage
from agentevalops.pipeline import V3Agent
from agentevalops.planner import GroundedLLMPlanner
from agentevalops.tracing import (
    NULL_SPAN,
    SCHEMA,
    JsonlEmitter,
    LangfuseEmitter,
    MemoryEmitter,
    MultiEmitter,
    SpanKind,
    SpanStatus,
    Tracer,
    clip,
    current_tracer,
    langfuse_payload,
    read_traces,
    redact_text,
    redact_value,
    span,
    tracing,
)

# --- 空操作与上下文 -----------------------------------------------------------


def test_span_without_tracer_is_a_noop():
    with span("noop", kind=SpanKind.TOOL, arguments={"order_id": "ORD-1001"}) as captured:
        captured.set(success=True)
        captured.fail("X", "boom")
    assert captured is NULL_SPAN


def test_span_outside_a_turn_is_counted_as_orphan():
    tracer = Tracer(MemoryEmitter())
    with tracer.span("loose") as orphan:
        orphan.set(note="没有 turn")
    assert tracer.orphan_spans == 1
    assert tracer.traces == []


def test_tracing_context_is_scoped():
    tracer = Tracer(MemoryEmitter())
    assert current_tracer() is None
    with tracing(tracer):
        assert current_tracer() is tracer
        with tracing(None):
            assert current_tracer() is None
        assert current_tracer() is tracer
    assert current_tracer() is None


# --- span 树 ------------------------------------------------------------------


def test_turn_records_parent_child_links():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, run_id="r1", case_id="c1")
    with tracer.turn("agent.turn", message="hi") as root:
        with tracer.span("node.validate", kind=SpanKind.NODE):
            with tracer.span("tool.query_order_status", kind=SpanKind.TOOL):
                pass
    trace = emitter.traces[0]
    assert [item.name for item in trace.spans] == [
        "agent.turn",
        "node.validate",
        "tool.query_order_status",
    ]
    node, tool = trace.spans[1], trace.spans[2]
    assert trace.root is root
    assert node.parent_id == root.span_id
    assert tool.parent_id == node.span_id
    assert (trace.run_id, trace.case_id) == ("r1", "c1")
    assert root.attributes["message"] == "hi"


def test_span_failure_marks_the_trace_as_error():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn"):
        with tracer.span("tool.apply_refund", kind=SpanKind.TOOL) as failed:
            failed.fail("TOOL_TIMEOUT", "超时了")
    trace = emitter.traces[0]
    assert trace.status == SpanStatus.ERROR
    assert [item.error_code for item in trace.errors()] == ["TOOL_TIMEOUT"]
    assert trace.by_name("tool.apply_refund").error_message == "超时了"


def test_exception_marks_span_and_still_emits_the_trace():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with pytest.raises(ValueError):
        with tracer.turn("agent.turn"):
            with tracer.span("node.plan"):
                raise ValueError("bad plan")
    trace = emitter.traces[0]
    assert trace.by_name("node.plan").error_code == "ValueError"
    assert trace.status == SpanStatus.ERROR


def test_root_duration_covers_children():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn") as root:
        with tracer.span("node.plan"):
            pass
    trace = emitter.traces[0]
    child = trace.by_name("node.plan")
    assert trace.duration_ms == root.duration_ms
    assert root.duration_ms >= child.duration_ms
    assert root.duration_ms > 0


def test_kind_and_name_lookups():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn"):
        with tracer.span("node.validate", kind=SpanKind.NODE):
            pass
        with tracer.span("retriever.search", kind=SpanKind.RETRIEVER):
            pass
    trace = emitter.traces[0]
    assert [item.name for item in trace.of_kind(SpanKind.RETRIEVER)] == ["retriever.search"]
    assert trace.by_name("没这个 span") is None
    assert trace.by_name("node.validate").kind == SpanKind.NODE


# --- 脱敏与截断 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("key=sk-abcdef123456", "sk-abcdef123456"),
        ("Authorization: Bearer abcdefghijklmn", "abcdefghijklmn"),
        ("联系我 zhangsan@example.com", "zhangsan@example.com"),
        ("手机号 13812345678", "13812345678"),
    ],
)
def test_redact_text_masks_secrets(raw, secret):
    masked = redact_text(raw)
    assert secret not in masked
    assert "***" in masked


@pytest.mark.parametrize("text", ["订单 ORD-1001 现在什么状态", "退款政策里无理由退款的期限"])
def test_redact_text_keeps_ordinary_text(text):
    assert redact_text(text) == text


def test_redact_value_masks_sensitive_keys_recursively():
    payload = {"api_key": "sk-live-1", "nested": {"token": "t", "order_id": "ORD-1001"}}
    masked = redact_value(payload)
    assert masked["api_key"] == "***"
    assert masked["nested"]["token"] == "***"
    assert masked["nested"]["order_id"] == "ORD-1001"


def test_token_counts_are_not_mistaken_for_secrets():
    masked = redact_value(
        {"prompt_tokens": 10, "total_tokens": 15, "max_tokens": 128, "token": "abc"}
    )
    assert masked["prompt_tokens"] == 10
    assert masked["total_tokens"] == 15
    assert masked["max_tokens"] == 128
    assert masked["token"] == "***"


def test_redact_value_truncates_long_text_and_lists():
    masked = redact_value({"big": "x" * 1000, "many": list(range(50))}, limit=10, max_items=3)
    assert masked["big"].startswith("x" * 10)
    assert "+990 chars" in masked["big"]
    assert masked["many"][:3] == [0, 1, 2]
    assert masked["many"][-1] == "…(+47 items)"


def test_clip_marks_what_was_cut():
    assert clip("abc", 10) == "abc"
    assert clip("abcdef", 3) == "abc…(+3 chars)"


def test_usage_counters_survive_redaction_in_a_real_turn():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn"):
        with tracer.span("llm.plan", kind=SpanKind.LLM) as llm:
            llm.set(usage={"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24})
    assert emitter.traces[0].by_name("llm.plan").attributes["usage"]["total_tokens"] == 24


def test_span_attributes_are_redacted_before_storage():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn", api_key="sk-secret-123456", message="13812345678"):
        pass
    attributes = emitter.traces[0].root.attributes
    assert attributes["api_key"] == "***"
    assert attributes["message"] == "***"


# --- 落盘 ---------------------------------------------------------------------


def test_jsonl_emitter_writes_one_lf_terminated_line_per_trace(tmp_path):
    path = tmp_path / "traces.jsonl"
    tracer = Tracer(JsonlEmitter(path))
    for index in range(3):
        with tracer.turn("agent.turn", index=index):
            with tracer.span("node.plan", kind=SpanKind.NODE):
                pass
    raw = path.read_bytes()
    assert b"\r\n" not in raw  # 仓库 .gitattributes 钉了 eol=lf
    assert raw.count(b"\n") == 3
    assert raw.endswith(b"\n")
    assert len(read_traces(path)) == 3


def test_jsonl_roundtrip_preserves_the_span_tree(tmp_path):
    path = tmp_path / "t.jsonl"
    emitter = JsonlEmitter(path)
    tracer = Tracer(emitter, case_id="c1")
    with tracer.turn("agent.turn", message="hi"):
        with tracer.span("retriever.search", kind=SpanKind.RETRIEVER) as search:
            search.set(query="退货期限", hits=2, grounded=True)
    trace = read_traces(path)[0]
    assert trace.case_id == "c1"
    assert trace.root.attributes["message"] == "hi"
    search = trace.by_name("retriever.search")
    assert (search.kind, search.attributes["hits"]) == (SpanKind.RETRIEVER, 2)
    assert search.parent_id == trace.root.span_id


def test_read_traces_skips_broken_lines_and_missing_files(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        '{"trace_id": "a", "spans": []}\n不是 JSON\n{"trace_id": "b", "spans": []}\n',
        encoding="utf-8",
    )
    assert [item.trace_id for item in read_traces(path)] == ["a", "b"]
    assert read_traces(tmp_path / "不存在.jsonl") == []


def test_schema_version_is_recorded(tmp_path):
    path = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlEmitter(path))
    with tracer.turn("agent.turn"):
        pass
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == SCHEMA
    assert payload["spans"][0]["name"] == "agent.turn"


# --- 出口 ---------------------------------------------------------------------


def test_multi_emitter_survives_a_broken_emitter():
    class Boom:
        def emit(self, trace):
            raise RuntimeError("这个出口坏了")

    memory = MemoryEmitter()
    tracer = Tracer(MultiEmitter(Boom(), memory))
    with tracer.turn("agent.turn"):
        pass
    assert len(memory.traces) == 1


def test_langfuse_payload_maps_input_output_by_kind():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, case_id="c9")
    with tracer.turn("agent.turn", intent="policy_qa"):
        with tracer.span(
            "retriever.search",
            kind=SpanKind.RETRIEVER,
            query="无理由退款的期限",
            top_k=3,
            hits=2,
            doc_ids=["DOC-REFUND-001"],
            grounded=True,
        ):
            pass
        with tracer.span(
            "tool.apply_refund", kind=SpanKind.TOOL, arguments={"order_id": "ORD-1002"}
        ) as refund:
            refund.set(success=True, result={"refunded": True})
        with tracer.span("llm.plan", kind=SpanKind.LLM, model="deepseek-flash") as llm:
            llm.set(
                response='{"intent": "refund"}',
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
    items = langfuse_payload(emitter.traces[0])
    by_name = {item["name"]: item for item in items}
    assert by_name["retriever.search"]["input"]["query"] == "无理由退款的期限"
    assert by_name["retriever.search"]["output"]["doc_ids"] == ["DOC-REFUND-001"]
    assert by_name["tool.apply_refund"]["input"] == {"order_id": "ORD-1002"}
    assert by_name["tool.apply_refund"]["output"]["success"] is True
    assert by_name["llm.plan"]["usage_details"] == {"input": 10, "output": 5, "total": 15}
    assert by_name["llm.plan"]["model"] == "deepseek-flash"
    assert by_name["retriever.search"]["parent_name"] == "agent.turn"
    assert by_name["retriever.search"]["parent_span_id"] == by_name["agent.turn"]["span_id"]
    assert by_name["agent.turn"]["parent_name"] is None
    assert by_name["agent.turn"]["parent_span_id"] is None
    assert all(item["metadata"]["case_id"] == "c9" for item in items)


def test_langfuse_payload_marks_errors():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracer.turn("agent.turn"):
        with tracer.span("tool.query_order_status", kind=SpanKind.TOOL) as failed:
            failed.fail("TOOL_TIMEOUT", "工具超时")
    item = langfuse_payload(emitter.traces[0])[1]
    assert item["level"] == "ERROR"
    assert item["status_message"] == "工具超时"
    assert item["metadata"]["error_code"] == "TOOL_TIMEOUT"


def test_langfuse_emitter_dry_run_dumps_the_would_be_payload(tmp_path):
    dump = tmp_path / "dry.jsonl"
    emitter = LangfuseEmitter(dump_path=dump, public_key="", secret_key="")
    tracer = Tracer(MultiEmitter(MemoryEmitter(), emitter))
    with tracer.turn("agent.turn"):
        pass
    assert emitter.status.startswith("dry-run:")
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["reason"] in {"no-sdk", "no-keys"}
    assert payload["would_send"][0]["name"] == "agent.turn"


def test_langfuse_emitter_reads_keys_from_environment(monkeypatch):
    created: dict[str, str] = {}

    class FakeSpan:
        id = "span-1"

        def end(self):
            pass

    class FakeLangfuse:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start_observation(self, **kwargs):
            return FakeSpan()

        def flush(self):
            pass

    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=FakeLangfuse))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example")

    emitter = LangfuseEmitter()
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn"):
        pass

    assert emitter.status == "live"
    assert created == {
        "public_key": "pk-test",
        "secret_key": "sk-test",
        "host": "https://langfuse.example",
    }


def test_langfuse_base_url_wins_over_deprecated_host(monkeypatch):
    """v5 的正式变量是 LANGFUSE_BASE_URL；同时存在时不能让旧的 HOST 抢占区域。"""
    created: dict[str, str] = {}

    class FakeSpan:
        id = "span-1"

        def end(self):
            pass

    class FakeLangfuse:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def start_observation(self, **kwargs):
            return FakeSpan()

        def flush(self):
            pass

    monkeypatch.setitem(sys.modules, "langfuse", types.SimpleNamespace(Langfuse=FakeLangfuse))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_HOST", "https://old.example")

    emitter = LangfuseEmitter()
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn"):
        pass

    assert emitter.status == "live"
    assert created["host"] == "https://jp.cloud.langfuse.com"


def test_langfuse_emitter_v5_preserves_parent_context():
    calls: list[tuple[str, str | None, dict]] = []
    created = {"n": 0}

    class FakeSpan:
        def __init__(self, name):
            created["n"] += 1
            self.id = f"span-{created['n']}"

        def end(self):
            calls.append(("end", self.id, {}))

    class FakeClient:
        def start_observation(self, **kwargs):
            span = FakeSpan(kwargs["name"])
            calls.append(("start", span.id, kwargs))
            return span

        def flush(self):
            calls.append(("flush", None, {}))

    emitter = LangfuseEmitter(client=FakeClient())
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn"):
        with tracer.span("tool.query_order_status", kind=SpanKind.TOOL):
            pass

    starts = [call for call in calls if call[0] == "start"]
    assert [call[2]["name"] for call in starts] == ["agent.turn", "tool.query_order_status"]
    root, child = starts
    assert "parent_span_id" not in root[2]["trace_context"]
    assert child[2]["trace_context"]["parent_span_id"] == root[1]
    assert len(root[2]["trace_context"]["trace_id"]) == 32
    assert root[2]["as_type"] == "chain"
    assert child[2]["as_type"] == "tool"


def test_langfuse_emitter_live_path_uses_the_client():
    calls: list[tuple[str, str | None, dict]] = []

    class FakeSpan:
        def __init__(self, name):
            self.name = name

        def update(self, **kwargs):
            calls.append(("update", self.name, kwargs))

        def end(self, **kwargs):
            calls.append(("end", self.name, kwargs))

    class FakeClient:
        def start_span(self, **kwargs):
            calls.append(("start", kwargs["name"], kwargs))
            return FakeSpan(kwargs["name"])

        def flush(self):
            calls.append(("flush", None, {}))

    emitter = LangfuseEmitter(client=FakeClient())
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn", message="hi"):
        pass
    assert (emitter.status, emitter.sent) == ("live", 1)
    assert {name for action, name, _ in calls if action == "end"} == {"agent.turn"}
    assert any(action == "flush" for action, _, _ in calls)


def test_langfuse_emitter_swallows_client_errors_and_dumps(tmp_path):
    class BoomClient:
        def start_span(self, **kwargs):
            raise RuntimeError("服务不可达")

    dump = tmp_path / "d.jsonl"
    emitter = LangfuseEmitter(client=BoomClient(), dump_path=dump)
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn"):
        pass
    assert emitter.status == "error:RuntimeError"
    assert dump.exists()


def test_langfuse_emitter_can_be_disabled(tmp_path):
    emitter = LangfuseEmitter(enabled=False)
    tracer = Tracer(MultiEmitter(emitter))
    with tracer.turn("agent.turn"):
        pass
    assert (emitter.status, emitter.sent) == ("disabled", 0)


# --- 插桩：真实管道 -----------------------------------------------------------


def test_v3_turn_records_nodes_and_the_tool_span():
    emitter = MemoryEmitter()
    V3Agent(tracer=Tracer(emitter)).handle("订单 ORD-1001 现在什么状态？", customer_id="CUST-001")
    trace = emitter.traces[0]
    names = [item.name for item in trace.spans]
    assert names[0] == "agent.turn"
    assert {"node.validate", "node.plan", "node.dispatch", "node.respond"} <= set(names)
    assert "tool.query_order_status" in names
    root = trace.by_name("agent.turn")
    assert root.attributes["slot_order_id"] == "ORD-1001"
    assert root.attributes["nodes"] == ["validate", "plan", "dispatch", "respond"]


def test_instrumentation_does_not_change_behaviour():
    message = "订单 ORD-1001 现在什么状态？"
    plain = V3Agent().handle(message, customer_id="CUST-001")
    traced = V3Agent(tracer=Tracer(MemoryEmitter())).handle(message, customer_id="CUST-001")
    assert (plain.intent, plain.state, plain.audit) == (traced.intent, traced.state, traced.audit)


def test_policy_qa_records_retriever_span_with_hits():
    emitter = MemoryEmitter()
    V3Agent(tracer=Tracer(emitter)).handle(
        "退款政策里无理由退款的期限是怎么规定的", customer_id="CUST-001"
    )
    search = emitter.traces[0].by_name("retriever.search")
    assert search is not None
    assert search.attributes["hits"] >= 1
    assert search.attributes["grounded"] is True
    assert search.attributes["doc_ids"]


def test_policy_qa_without_grounding_records_zero_hits():
    emitter = MemoryEmitter()
    V3Agent(tracer=Tracer(emitter)).handle("退货政策是怎样的？", customer_id="CUST-001")
    search = emitter.traces[0].by_name("retriever.search")
    assert (search.attributes["hits"], search.attributes["grounded"]) == (0, False)


def test_faulted_tool_records_an_error_span():
    emitter = MemoryEmitter()
    agent = V3Agent(tracer=Tracer(emitter))
    spec = FaultSpec.parse(
        {"target": "tool", "mode": "timeout", "tool_name": "query_order_status"}
    )
    agent.agent.order_status_tool = FaultyTool(agent.agent.order_status_tool, spec)
    response = agent.handle("订单 ORD-1001 到哪了", customer_id="CUST-001")
    tool_span = emitter.traces[0].by_name("tool.query_order_status")
    assert (tool_span.status, tool_span.error_code) == (SpanStatus.ERROR, "TOOL_TIMEOUT")
    assert str(response.state) == "tool_failed"


def test_faulted_retrieval_records_the_fault_on_the_search_span():
    emitter = MemoryEmitter()
    agent = V3Agent(tracer=Tracer(emitter))
    spec = FaultSpec.parse({"target": "retrieval", "mode": "unavailable"})
    agent.knowledge_tool = FaultyTool(agent.knowledge_tool, spec)
    agent.handle("退货政策是怎样的？", customer_id="CUST-001")
    search = emitter.traces[0].by_name("retriever.search")
    assert (search.status, search.error_code) == (SpanStatus.ERROR, "TOOL_UNAVAILABLE")


def test_multiturn_records_state_update_and_inherited_slot():
    emitter = MemoryEmitter()
    agent = V3Agent(tracer=Tracer(emitter))
    agent.handle("订单 ORD-1001 现在到哪了", customer_id="CUST-001", session_id="S1")
    agent.handle("这个订单现在什么状态了", customer_id="CUST-001", session_id="S1")
    assert len(emitter.traces) == 2
    second = emitter.traces[1]
    assert second.by_name("state.update").attributes["source"] == "inherited"
    assert second.by_name("agent.turn").attributes["slot_source"] == "inherited"


def test_handle_inside_an_active_turn_does_not_open_a_second_trace():
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracing(tracer), tracer.turn("batch"):
        V3Agent().handle("订单 ORD-1001 现在什么状态？", customer_id="CUST-001")
    assert len(emitter.traces) == 1
    names = [item.name for item in emitter.traces[0].spans]
    assert "batch" in names and "agent.turn" in names


# --- 插桩：模型层 -------------------------------------------------------------


class _StubClient:
    model_id = "stub-model"

    def __init__(self, *, text=None, error=None):
        self._text = text
        self._error = error

    def complete(self, messages, *, temperature=None, max_tokens=None):
        if self._error is not None:
            raise self._error
        return LLMResponse(
            text=self._text,
            model="stub-model",
            usage=Usage(20, 4),
            latency_ms=33.0,
        )


def test_llm_planner_records_prompt_usage_and_plan_source():
    planner = GroundedLLMPlanner(
        _StubClient(text='{"intent": "order_status", "order_id": "ORD-1001"}')
    )
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracing(tracer), tracer.turn("agent.turn"):
        plan = planner.plan("帮我看下 ORD-1001")
    llm = emitter.traces[0].by_name("llm.plan")
    assert llm.attributes["model"] == "stub-model"
    assert llm.attributes["usage"]["total_tokens"] == 24
    assert llm.attributes["latency_ms"] == 33.0
    assert llm.attributes["plan_source"] == plan.source
    assert llm.attributes["user_message"] == "帮我看下 ORD-1001"
    assert llm.attributes["system_prompt"]
    assert str(plan.intent) == "order_status"


def test_llm_planner_records_a_model_error_and_falls_back():
    planner = GroundedLLMPlanner(_StubClient(error=LLMUnavailableError("503")))
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracing(tracer), tracer.turn("agent.turn"):
        plan = planner.plan("订单 ORD-1001 状态")
    trace = emitter.traces[0]
    llm = trace.by_name("llm.plan")
    assert llm.status == SpanStatus.ERROR
    assert llm.error_code == "LLMUnavailableError"
    assert trace.status == SpanStatus.ERROR
    assert str(plan.intent) == "order_status"  # 兜底规划器仍然判得出意图


def test_llm_planner_records_unusable_output_as_a_parse_failure():
    planner = GroundedLLMPlanner(_StubClient(text="我觉得这个订单在路上了"))
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with tracing(tracer), tracer.turn("agent.turn"):
        planner.plan("订单 ORD-1001 状态")
    llm = emitter.traces[0].by_name("llm.plan")
    assert llm.status == SpanStatus.OK  # 模型调通了
    assert llm.attributes["parse"] == "not-json"


def test_budget_error_is_recorded_and_reraised():
    planner = GroundedLLMPlanner(_StubClient(error=BudgetExceededError("超预算")))
    emitter = MemoryEmitter()
    tracer = Tracer(emitter)
    with pytest.raises(BudgetExceededError):
        with tracing(tracer), tracer.turn("agent.turn"):
            planner.plan("订单 ORD-1001 状态")
    llm = emitter.traces[0].by_name("llm.plan")
    assert (llm.status, llm.error_code) == (SpanStatus.ERROR, "BUDGET_EXCEEDED")
