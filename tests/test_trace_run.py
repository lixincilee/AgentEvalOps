"""trace_run.py 的测试：默认零成本、真实模型的接线、预算熔断与产物口径。

这一批测的**不是**「模型答得好不好」（那是判分层的事），而是**接线**：

* 不加 ``--llm`` 就是一个模型请求都不发的规则臂（零成本、零方差）；
* 加 ``--llm`` 时 ``llm.plan`` span 里必须真的有 prompt / response / usage —— 这是
  「用 trace 定位 Prompt 问题」这句话的技术前提，缺了它 trace 只能告诉你「模型答错了」；
* 没有 ``--yes`` 或没有 key 时**停在发请求之前**（退出码 2，不是 1）；
* ``--budget`` 触到就停（退出码 1），而**没有价目的模型必须在发请求之前被拒**——
  否则 ``UnknownModelPriceError`` 会被规划器当成「模型抽风」降级成 llm-fallback，
  整个 run 静默地不再发请求，通过率还很好看。

真实网络一律不发：注入 ``ScriptedLLMClient``，它的 token 数与延迟都是定死的。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentevalops.config import Settings
from agentevalops.faults import build_faulted_agent
from agentevalops.llm import LLMUnavailableError, ScriptedLLMClient
from agentevalops.planner import GroundedLLMPlanner

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(module_name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


trace_run = _load("trace_run", "trace_run.py")

#: 能让「查 ORD-1001 状态」判过的模型回答。
GOOD = json.dumps({"intent": "order_status", "order_id": "ORD-1001"})
#: 用 deepseek-flash 这个**有价目**的模型名：否则预算熔断根本起不来（那正是被测的闸门）。
SETTINGS = Settings(api_key="test-key", model="deepseek-flash")

CASES: list[dict[str, Any]] = [
    {
        "case_id": "c1",
        "customer_id": "CUST-001",
        "message": "帮我查一下订单 ORD-1001 现在到哪了",
        "expected_intent": "order_status",
        "expected_state": "tool_completed",
    },
    {
        "case_id": "c2",
        "customer_id": "CUST-001",
        "message": "订单 ORD-1004 的物流到哪了",
        "expected_intent": "order_status",
        "expected_state": "tool_completed",
    },
]

FAULT_CASE: dict[str, Any] = {
    "case_id": "flt_demo_01",
    "customer_id": "CUST-001",
    "message": "我的订单 ORD-1001 到哪了",
    "fault": {"target": "tool", "mode": "timeout", "tool_name": "query_order_status"},
    "warmup": [{"message": "帮我查订单 ORD-1001 的状态", "customer_id": "CUST-001"}],
}

MULTITURN_CASE: dict[str, Any] = {
    "case_id": "mt_demo_01",
    "customer_id": "CUST-001",
    "session_id": "MT-DEMO-1",
    "turns": [
        {"message": "帮我查一下订单 ORD-1001 现在到哪了"},
        {"message": "这个订单现在什么状态了"},
    ],
}


class SpyClient:
    """只数调用次数：被调用 = 「真的发了请求」这件事发生了。"""

    model_id = "spy"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.calls.append(list(messages))
        raise AssertionError("这个测试不该真的调用模型")


class RateLimitedStub:
    """每次都像撞上限流。用来钉住「一轮 error trace 不等于一条失败用例」。"""

    model_id = "deepseek-flash"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, temperature=None, max_tokens=None):
        self.calls += 1
        raise LLMUnavailableError("HTTP 429 Too Many Requests（测试注入）")


def write_cases(tmp_path: Path, records: list[dict[str, Any]], name: str = "cases.jsonl") -> Path:
    path = tmp_path / name
    path.write_bytes(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode("utf-8")
    )
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def spans_of(trace: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [span for span in trace["spans"] if span["kind"] == kind]


def root_of(trace: dict[str, Any]) -> dict[str, Any]:
    return next(span for span in trace["spans"] if span["kind"] == "turn")


def run(tmp_path: Path, *extra: str, **kwargs: Any) -> tuple[int, Path]:
    out = tmp_path / "traces.jsonl"
    argv = ["--out", str(out), *extra]
    code = trace_run.main(argv, **kwargs)
    return code, out


# --- 默认：规则臂，零请求 ------------------------------------------------------------


def test_default_arm_never_calls_the_model(tmp_path):
    """不加 --llm 就是规则臂：一个模型请求都不发，trace 里也没有 llm span。"""
    cases = write_cases(tmp_path, CASES)
    spy = SpyClient()

    code, out = run(
        tmp_path, "--cases", str(cases), "--quiet", clients={"v3": spy}, settings=SETTINGS
    )

    assert code == 0
    assert spy.calls == []
    traces = read_jsonl(out)
    assert len(traces) == len(CASES)
    assert all(spans_of(trace, "llm") == [] for trace in traces)


def test_every_trace_is_anchored_to_the_run_and_the_case(tmp_path):
    """run_id / case_id / 版本锚点都要在每一行上，否则 trace 找不到它属于哪次运行。"""
    cases = write_cases(tmp_path, CASES)

    code, out = run(
        tmp_path, "--cases", str(cases), "--quiet", "--run-id", "run-under-test", settings=SETTINGS
    )

    assert code == 0
    traces = read_jsonl(out)
    assert [trace["case_id"] for trace in traces] == [case["case_id"] for case in CASES]
    assert {trace["run_id"] for trace in traces} == {"run-under-test"}
    for trace in traces:
        assert trace["version"]["describe"]
        assert trace["schema"] == "agentevalops.trace/v1"


# --- 花费闸门：都在发请求之前 --------------------------------------------------------


def test_llm_without_yes_stops_before_the_first_request(tmp_path, capsys):
    cases = write_cases(tmp_path, CASES)
    spy = SpyClient()

    code, out = run(tmp_path, "--cases", str(cases), "--llm", clients={"v3": spy}, settings=SETTINGS)

    assert code == 2
    assert spy.calls == []
    assert not out.exists()
    assert "--yes" in capsys.readouterr().err


def test_llm_without_a_key_exits_2(tmp_path, capsys):
    cases = write_cases(tmp_path, CASES)
    spy = SpyClient()

    code, out = run(
        tmp_path, "--cases", str(cases), "--llm", "--yes", clients={"v3": spy}, settings=Settings()
    )

    assert code == 2
    assert spy.calls == []
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_budget_on_an_unpriced_model_is_refused_before_any_request(tmp_path, capsys):
    """没有价目的模型 + 预算 = 静默降级的陷阱，必须在发请求之前拦住。"""
    cases = write_cases(tmp_path, CASES)
    spy = SpyClient()
    unpriced = Settings(api_key="test-key", model="some-unpriced-model")

    code, _ = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        "--budget",
        "1.0",
        clients={"v3": spy},
        settings=unpriced,
    )

    assert code == 2
    assert spy.calls == []
    assert "预算熔断" in capsys.readouterr().err


def test_budget_trips_and_the_exit_code_is_one(tmp_path, capsys):
    """熔断不是「用法问题」：退出码 1，而且已经写出去的 trace 留在文件里。"""
    cases = write_cases(tmp_path, CASES)
    client = ScriptedLLMClient(default=GOOD, model="deepseek-flash")

    code, out = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        "--budget",
        "0.0",
        clients={"v3": client},
        settings=SETTINGS,
    )

    assert code == 1
    assert client.call_count == 1  # 第一次调用就超了，第二次不该再发
    assert out.exists()
    assert "预算熔断" in capsys.readouterr().err


# --- 真实模型的接线 -----------------------------------------------------------------


def test_llm_run_records_the_real_prompt_response_and_usage(tmp_path):
    """prompt / 回答 / token 都要在 trace 里：缺了就只能知道「答错了」，不知道「问错了」。"""
    cases = write_cases(tmp_path, CASES)
    client = ScriptedLLMClient(default=GOOD, model="deepseek-flash")

    code, out = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        "--quiet",
        clients={"v3": client},
        settings=SETTINGS,
    )

    assert code == 0
    traces = read_jsonl(out)
    llm_spans = [span for trace in traces for span in spans_of(trace, "llm")]
    assert len(llm_spans) == len(CASES)

    attributes = llm_spans[0]["attributes"]
    assert attributes["response"] == GOOD  # 模型原话，不做加工
    assert CASES[0]["message"] in attributes["user_message"]
    assert attributes["system_prompt"].strip()
    assert attributes["usage"]["prompt_tokens"] > 0
    assert attributes["usage"]["completion_tokens"] > 0
    assert "***" not in json.dumps(attributes)  # 计数指标不该被脱敏掉
    assert attributes["parse"] == "ok"
    assert attributes["plan_source"] == "llm"  # 这次的决定真的是模型做的
    assert llm_spans[0]["status"] == "ok"


def ancestors_of(trace, span):
    """沿 parent_id 往上走，返回从自己到根的整条链。"""
    by_id = {item["span_id"]: item for item in trace["spans"]}
    chain = [span]
    while chain[-1]["parent_id"]:
        chain.append(by_id[chain[-1]["parent_id"]])
    return chain


def test_llm_span_hangs_under_the_turn_span(tmp_path):
    """模型 span 必须能沿 parent_id 走回那一轮的根 span，否则跨轮定位只能靠时间戳猜。"""
    cases = write_cases(tmp_path, CASES)
    client = ScriptedLLMClient(default=GOOD, model="deepseek-flash")

    code, out = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        "--quiet",
        clients={"v3": client},
        settings=SETTINGS,
    )

    assert code == 0
    for trace in read_jsonl(out):
        for span in spans_of(trace, "llm"):
            chain = ancestors_of(trace, span)
            assert chain[-1] is root_of(trace)  # 走到根
            assert chain[1]["name"] == "node.plan"  # 模型调用发生在 plan 节点里


def test_a_rate_limited_turn_is_an_error_trace_but_a_passing_case(tmp_path):
    """一条重要口径：故障轮的 trace 是 error，而用例是**通过**的（降级答对了）。

    把 error trace 数当成失败用例数，故障注入一跑就会得出「19 条失败」的错误结论。
    """
    cases = write_cases(tmp_path, CASES[:1])
    client = RateLimitedStub()

    code, out = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        "--quiet",
        clients={"v3": client},
        settings=SETTINGS,
    )

    # trace_run 的退出码只回答「有没有跑出 trace」，不回答「质量好不好」——
    # 判质量是 trace_report.py 的事（它才有 --fail-on-error）。这一轮照样写出来了。
    assert code == 0
    trace = read_jsonl(out)[0]
    assert trace["status"] == "error"
    llm_span = spans_of(trace, "llm")[0]
    assert llm_span["status"] == "error"
    assert llm_span["error_code"] == "LLMUnavailableError"
    assert root_of(trace)["attributes"]["plan_source"] == "llm-fallback"
    assert root_of(trace)["attributes"]["state"] == "tool_completed"


def test_the_cost_line_reports_calls_and_spend(tmp_path, capsys):
    """花了多少钱必须打在屏幕上：没有这一行，真实运行的成本只能靠猜。"""
    cases = write_cases(tmp_path, CASES)
    client = ScriptedLLMClient(default=GOOD, model="deepseek-flash")

    code, _ = run(
        tmp_path,
        "--cases",
        str(cases),
        "--llm",
        "--yes",
        clients={"v3": client},
        settings=SETTINGS,
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert f"调用 {len(CASES)} 次" in printed
    assert "CNY" in printed


# --- 故障轮也要走真实模型 -----------------------------------------------------------


def test_a_fault_case_runs_on_the_injected_model_planner():
    """--llm 的故障轮也要走真实模型，否则同一份报告里混着两套规划器。"""
    client = ScriptedLLMClient(default=GOOD, model="deepseek-flash")
    planner = GroundedLLMPlanner(client)
    agent = build_faulted_agent(FAULT_CASE["fault"], planner=planner)

    assert agent.planner is planner
    response = agent.handle(FAULT_CASE["message"], customer_id="CUST-001")

    assert client.call_count == 1  # 规划确实经过模型，故障只发生在工具层
    assert response.audit.error is not None
    assert response.audit.error["code"] == "TOOL_TIMEOUT"


def test_a_model_fault_ignores_the_injected_planner():
    """模型类故障自带 client：注入的 planner 必须被忽略，否则等于把被测对象换掉了。"""
    injected = GroundedLLMPlanner(ScriptedLLMClient(default=GOOD, model="deepseek-flash"))
    agent = build_faulted_agent({"target": "model", "mode": "rate_limit"}, planner=injected)

    assert agent.planner is not injected
    response = agent.handle("帮我查一下订单 ORD-1001 的状态", customer_id="CUST-001")

    assert agent.fault_client.rate_limited == 1
    assert response.audit.error is None  # 限流不许让请求失败


# --- 产物口径 -----------------------------------------------------------------------


def test_warmup_turns_are_not_traced(tmp_path):
    """预热是铺垫，不是被测的那一轮：记上就会多出一条没有期望值的「用例」。"""
    path = write_cases(tmp_path, [FAULT_CASE], name="faults.jsonl")

    code, out = run(tmp_path, "--faults", str(path), "--quiet", settings=SETTINGS)

    assert code == 0
    traces = read_jsonl(out)
    assert len(traces) == 1
    assert root_of(traces[0])["attributes"]["error_code"] == "TOOL_TIMEOUT"


def test_multiturn_case_writes_one_trace_per_turn(tmp_path):
    """多轮用例一轮一条 trace：合起来就无法回答「是第几轮开始跑偏的」。"""
    path = write_cases(tmp_path, [MULTITURN_CASE], name="multiturn.jsonl")

    code, out = run(tmp_path, "--multiturn", str(path), "--quiet", settings=SETTINGS)

    assert code == 0
    traces = read_jsonl(out)
    assert [trace["case_id"] for trace in traces] == ["mt_demo_01#t0", "mt_demo_01#t1"]


def test_rerunning_the_same_out_overwrites_instead_of_appending(tmp_path):
    """同一个输出文件跑两次，第二次覆盖：否则「跑了两遍」和「跑出两条不同结果」长得一样。"""
    cases = write_cases(tmp_path, CASES)

    first, out = run(tmp_path, "--cases", str(cases), "--quiet", settings=SETTINGS)
    second, _ = run(tmp_path, "--cases", str(cases), "--quiet", settings=SETTINGS)

    assert (first, second) == (0, 0)
    assert len(read_jsonl(out)) == len(CASES)


def test_the_trace_never_carries_the_expectations(tmp_path):
    """证据与断言分开：期望值只能出现在 <out>.cases.jsonl 里。"""
    cases = write_cases(tmp_path, CASES)

    code, out = run(tmp_path, "--cases", str(cases), "--quiet", settings=SETTINGS)

    assert code == 0
    assert "expected_" not in out.read_text(encoding="utf-8")
    sidecar = read_jsonl(out.with_name(f"{out.stem}.cases.jsonl"))
    assert [record["case_id"] for record in sidecar] == [case["case_id"] for case in CASES]
    assert all("expected_intent" in record for record in sidecar)
