"""失败环节定位的规则测试：每条规则一条测试。

规则表是「失败该去找哪块代码」的唯一依据，改它必须有人知道——所以这里不是「跑一遍
看看」，而是逐条钉住：命中什么证据 → 报哪个环节。

用的 trace 都由真的 :class:`Tracer` 造出来（不是手搓对象）：定位规则读的是落盘后的形状，
手搓对象会让测试通过而线上读不出来。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from agentevalops.tracing import JsonlEmitter, MemoryEmitter, SpanKind, Tracer

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str) -> Any:
    """``scripts/`` 不是包，所以按文件路径加载模块。"""
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


report = _load("trace_report", "trace_report.py")


def build_trace(
    *,
    case_id: str = "c1",
    turn_attrs: dict[str, Any] | None = None,
    retriever: dict[str, Any] | None = None,
    retriever_error: str | None = None,
    tool: dict[str, Any] | None = None,
    tool_error: str | None = None,
    tool_success: bool | None = None,
    llm: dict[str, Any] | None = None,
    llm_error: str | None = None,
):
    emitter = MemoryEmitter()
    tracer = Tracer(emitter, case_id=case_id)
    with tracer.turn("agent.turn", **(turn_attrs or {})):
        if retriever is not None or retriever_error is not None:
            with tracer.span(
                "retriever.search", kind=SpanKind.RETRIEVER, **(retriever or {})
            ) as search:
                if retriever_error:
                    search.fail(retriever_error, "检索坏了")
        if tool is not None or tool_error is not None or tool_success is not None:
            with tracer.span(
                "tool.query_order_status", kind=SpanKind.TOOL, **(tool or {})
            ) as tool_span:
                if tool_success is not None:
                    tool_span.set(success=tool_success)
                if tool_error:
                    tool_span.fail(tool_error, "工具坏了")
        if llm is not None or llm_error is not None:
            with tracer.span("llm.plan", kind=SpanKind.LLM, **(llm or {})) as llm_span:
                if llm_error:
                    llm_span.fail(llm_error, "模型坏了")
    return emitter.traces[0]


# --- 逐条规则 -----------------------------------------------------------------


def test_locates_a_model_error_first():
    trace = build_trace(llm_error="LLMUnavailableError", turn_attrs={"state": "tool_completed"})
    assert report.locate(trace, []) == ("reasoning", "model-error:LLMUnavailableError")


def test_locates_a_retriever_fault():
    trace = build_trace(retriever_error="TOOL_TIMEOUT")
    assert report.locate(trace, []) == ("retrieval", "retriever-error:TOOL_TIMEOUT")


def test_locates_request_validation():
    trace = build_trace(
        turn_attrs={"state": "validation_failed", "error_code": "VALIDATION_ERROR"}
    )
    assert report.locate(trace, []) == ("request", "request-validation")


def test_a_missing_slot_is_state_not_request_validation():
    trace = build_trace(
        turn_attrs={
            "state": "validation_failed",
            "error_code": "VALIDATION_ERROR",
            "tool_name": "query_order_status",
        }
    )
    assert report.locate(trace, []) == ("state", "missing-slot")


def test_locates_a_tool_fault():
    trace = build_trace(tool_error="TOOL_UNAVAILABLE")
    assert report.locate(trace, []) == ("tool", "tool-error:TOOL_UNAVAILABLE")


def test_locates_no_grounding():
    trace = build_trace(turn_attrs={"error_code": "NO_KNOWLEDGE", "state": "no_action"})
    assert report.locate(trace, []) == ("retrieval", "no-grounding")


def test_locates_a_zero_hit_retrieval():
    trace = build_trace(
        retriever={"hits": 0, "grounded": False}, turn_attrs={"state": "no_action"}
    )
    assert report.locate(trace, []) == ("retrieval", "no-hit")


def test_locates_prompt_format_when_the_model_answered_but_output_was_unusable():
    trace = build_trace(llm={"parse": "not-json"}, turn_attrs={"plan_source": "llm-fallback"})
    assert report.locate(trace, []) == ("reasoning", "prompt-format")


def test_locates_slot_inheritance_mismatch():
    trace = build_trace(turn_attrs={"slot_source": None, "session_id": "S1"})
    mismatch = report.Mismatch("expected_slot_source", "slot_source", "inherited", None)
    assert report.locate(trace, [mismatch]) == ("state", "slot-inheritance")


def test_locates_a_tool_business_failure():
    trace = build_trace(
        tool={"error_code": "ORDER_NOT_FOUND"},
        tool_success=False,
        turn_attrs={"state": "tool_failed"},
    )
    assert report.locate(trace, []) == ("tool", "tool-failed:ORDER_NOT_FOUND")


def test_locates_an_intent_mismatch():
    trace = build_trace(turn_attrs={"intent": "unknown"})
    mismatch = report.Mismatch("expected_intent", "intent", "order_status", "unknown")
    assert report.locate(trace, [mismatch]) == ("reasoning", "intent")


def test_locates_tool_selection_and_final_state_mismatches():
    trace = build_trace(turn_attrs={"state": "no_action"})
    assert report.locate(
        trace, [report.Mismatch("expected_tool", "tool_name", "apply_refund", None)]
    ) == ("tool", "tool-selection")
    assert report.locate(
        trace, [report.Mismatch("expected_state", "state", "tool_completed", "no_action")]
    ) == ("state", "final-state")


def test_unclassified_when_no_rule_matches():
    trace = build_trace(turn_attrs={"state": "tool_completed"})
    mismatch = report.Mismatch("expected_error_code", "error_code", None, "X")
    assert report.locate(trace, [mismatch]) == ("unclassified", "")


# --- 判分 ---------------------------------------------------------------------


def test_compare_only_checks_the_fields_the_case_declares():
    trace = build_trace(turn_attrs={"intent": "order_status", "state": "tool_completed"})
    assert report.compare(trace, {"expected_intent": "order_status"}) == []
    assert report.compare(trace, {}) == []


def test_compare_treats_null_and_missing_alike():
    trace = build_trace(turn_attrs={"error_code": None})
    assert report.compare(trace, {"expected_error_code": None}) == []


def test_compare_reports_a_mismatch_with_both_sides():
    trace = build_trace(turn_attrs={"intent": "unknown"})
    mismatches = report.compare(trace, {"expected_intent": "order_status"})
    assert [item.to_dict() for item in mismatches] == [
        {
            "field": "expected_intent",
            "attribute": "intent",
            "expected": "order_status",
            "observed": "unknown",
        }
    ]


def test_split_case_id_and_multiturn_expectations():
    assert report.split_case_id("mt_ref_01#t1") == ("mt_ref_01", 1)
    assert report.split_case_id("c1") == ("c1", 0)
    case = {
        "case_id": "mt",
        "turns": [
            {"message": "a", "expected_intent": "order_status"},
            {"message": "b", "expected_intent": "refund"},
        ],
    }
    assert report.expectations_for(case, 1)["expected_intent"] == "refund"
    assert report.expectations_for(case, 9) == {}
    assert report.expectations_for({"expected_intent": "refund"}, 0) == {
        "expected_intent": "refund"
    }
    assert report.expectations_for(None, 0) == {}


def test_judge_marks_pass_and_failure():
    traces = [
        build_trace(case_id="c1", turn_attrs={"intent": "order_status"}),
        build_trace(case_id="c2", turn_attrs={"intent": "unknown"}),
    ]
    cases = {
        "c1": {"expected_intent": "order_status"},
        "c2": {"expected_intent": "order_status"},
    }
    verdicts = report.judge(traces, cases)
    assert [item.passed for item in verdicts] == [True, False]
    assert (verdicts[1].stage, verdicts[1].detail) == ("reasoning", "intent")


def test_judge_without_expectations_does_not_claim_a_stage():
    trace = build_trace(case_id="c1", turn_attrs={"state": "tool_completed"})
    verdict = report.judge([trace], {})[0]
    assert verdict.passed is True  # 没有期望值就谈不上「不通过」
    assert verdict.stage is None  # 也不假装定位过


def test_judge_joins_multiturn_traces_to_the_right_turn():
    traces = [
        build_trace(case_id="mt#t0", turn_attrs={"intent": "order_status"}),
        build_trace(case_id="mt#t1", turn_attrs={"intent": "unknown"}),
    ]
    cases = {
        "mt": {
            "case_id": "mt",
            "turns": [
                {"message": "第一轮", "expected_intent": "order_status"},
                {"message": "第二轮", "expected_intent": "refund"},
            ],
        }
    }
    verdicts = report.judge(traces, cases)
    assert [item.passed for item in verdicts] == [True, False]


# --- 报告与 CLI ---------------------------------------------------------------


def test_markdown_contains_the_stage_cluster_and_the_mismatch():
    traces = [build_trace(case_id="c1", turn_attrs={"intent": "unknown"})]
    verdicts = report.judge(traces, {"c1": {"expected_intent": "order_status"}})
    markdown = report.render_markdown(
        verdicts, trace_files=[Path("t.jsonl")], case_files=[Path("c.jsonl")], skipped=0
    )
    assert "1/1 条不通过" in markdown
    assert "reasoning（推理层）" in markdown
    assert "expected_intent" in markdown
    assert "unclassified" in markdown  # 口径说明里必须交代这个环节的含义


def test_main_writes_markdown_and_json_and_keeps_lf(tmp_path):
    traces_path = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlEmitter(traces_path), case_id="c1")
    with tracer.turn("agent.turn", intent="unknown"):
        pass
    cases_path = tmp_path / "c.jsonl"
    cases_path.write_bytes(
        (json.dumps({"case_id": "c1", "expected_intent": "order_status"}) + "\n").encode("utf-8")
    )
    out = tmp_path / "report.md"
    payload_path = tmp_path / "report.json"
    code = report.main(
        [
            "--traces", str(traces_path),
            "--cases", str(cases_path),
            "--out", str(out),
            "--json", str(payload_path),
            "--quiet",
        ]
    )
    assert code == 0
    assert b"\r\n" not in out.read_bytes()
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    assert payload["failed"] == 1
    assert payload["stages"] == {"reasoning": 1}


def test_main_fail_on_error_returns_one(tmp_path):
    traces_path = tmp_path / "t.jsonl"
    tracer = Tracer(JsonlEmitter(traces_path), case_id="c1")
    with tracer.turn("agent.turn", intent="unknown"):
        pass
    cases_path = tmp_path / "c.jsonl"
    cases_path.write_bytes(
        (json.dumps({"case_id": "c1", "expected_intent": "order_status"}) + "\n").encode("utf-8")
    )
    assert report.main(
        ["--traces", str(traces_path), "--cases", str(cases_path), "--fail-on-error", "--quiet"]
    ) == 1
    assert report.main(["--traces", str(traces_path), "--cases", str(cases_path), "--quiet"]) == 0


def test_main_returns_two_when_a_file_is_missing(tmp_path):
    assert report.main(["--traces", str(tmp_path / "没有这个文件.jsonl")]) == 2


def test_main_returns_one_when_there_are_no_traces(tmp_path):
    empty = tmp_path / "empty.jsonl"
    empty.write_bytes(b"")
    assert report.main(["--traces", str(empty), "--quiet"]) == 1