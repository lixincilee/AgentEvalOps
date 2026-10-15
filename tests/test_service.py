"""``agentevalops.service`` 的契约测试：离线、零成本、不起服务器。

这里用 TestClient 而不是真 uvicorn，因为要断言的是**契约**：状态码、计数自洽、成本口径。
并发能力是另一回事，归 ``scripts/load_test.py`` 管——那条路要花时间和端口，不该进单元测试。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentevalops.llm.fake import ScriptedLLMClient
from agentevalops.order_store import OrderStore
from agentevalops.pipeline import V3Agent
from agentevalops.planner import GroundedLLMPlanner
from agentevalops.service import create_app
from agentevalops.session import SessionStore

ORDER_QUESTION = "我的订单 ORD-1001 到哪了"
REFUND_QUESTION = "这个我要退款"
REFUSED_QUESTION = "宠物寄存政策是什么"
ORDER_STATUS_PLAN = '{"intent": "order_status", "order_id": "ORD-1001"}'


def chat(client: TestClient, message: str, *, session_id: str | None = None, customer_id: str = "CUST-001"):
    return client.post(
        "/chat",
        json={"message": message, "customer_id": customer_id, "session_id": session_id},
    )


def scripted_client(model: str) -> GroundedLLMPlanner:
    """脚本化模型臂：固定回一个合法 JSON，不发网络请求。"""
    return GroundedLLMPlanner(ScriptedLLMClient(default=ORDER_STATUS_PLAN, model=model))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_order_status_turn_succeeds_on_the_rules_arm(client: TestClient) -> None:
    body = chat(client, ORDER_QUESTION).json()
    assert body["state"] == "tool_completed"
    assert body["intent"] == "order_status"
    assert body["audit"]["tool_name"] == "query_order_status"
    assert body["plan_source"] == "rules"
    # 规则臂一次模型调用都没有，所以 token 与成本必须都是 0，而不是「没记账」。
    assert body["model_calls"] == 0 and body["total_tokens"] == 0
    assert body["cost_cny"] == 0.0


def test_refusal_is_no_action_and_never_a_success(client: TestClient) -> None:
    response = chat(client, REFUSED_QUESTION)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "no_action"
    assert body["audit"]["error"]["code"] == "NO_KNOWLEDGE"
    metrics = client.get("/metrics").json()
    assert metrics["successes"] == 0 and metrics["failures"] == 1
    assert metrics["success_rate"] == 0.0


@pytest.mark.parametrize(
    "payload",
    [
        {"customer_id": "CUST-001"},
        {"message": ORDER_QUESTION},
        {"message": 42, "customer_id": "CUST-001"},
        {"message": ORDER_QUESTION, "customer_id": None},
    ],
)
def test_bad_body_is_400_with_a_reason(client: TestClient, payload: dict) -> None:
    response = client.post("/chat", json=payload)
    # 422 是 FastAPI 的内部约定，漏出去会把「客户端写错参数」伪装成「服务端故障」。
    assert response.status_code == 400
    body = response.json()
    assert body["errors"] and isinstance(body["errors"][0], str)
    # 参数错误不进计数：它没进执行链路，也就不该污染成功率。
    assert client.get("/metrics").json()["requests"] == 0


def test_metrics_counters_are_self_consistent(client: TestClient) -> None:
    for message in (ORDER_QUESTION, ORDER_QUESTION, REFUSED_QUESTION, "订单 ORD-1004 我要退款"):
        assert chat(client, message).status_code == 200
    metrics = client.get("/metrics").json()
    assert metrics["requests"] == 4
    assert metrics["requests"] == metrics["successes"] + metrics["failures"]
    assert metrics["successes"] == 3 and metrics["failures"] == 1
    assert metrics["errors"] == 0
    assert metrics["success_rate"] == pytest.approx(0.75)


def test_unpriced_model_reports_incomplete_cost() -> None:
    """没有价目的模型不许静默按 0 计入：token 会涨，但成本口径必须标成不完整。"""
    app = create_app(agent=V3Agent(planner=scripted_client("scripted-fake")))
    client = TestClient(app)
    body = chat(client, ORDER_QUESTION).json()
    assert body["plan_source"] == "llm" and body["model_calls"] == 1
    assert body["total_tokens"] > 0
    assert body["cost_cny"] == 0.0
    metrics = client.get("/metrics").json()
    assert metrics["tokens"] > 0
    assert metrics["cost_complete"] is False
    assert metrics["unpriced_models"] == ["scripted-fake"]


def test_priced_model_accumulates_real_cost() -> None:
    """价目表里有的模型：成本必须按真实价目算出来，且口径完整。"""
    app = create_app(agent=V3Agent(planner=scripted_client("deepseek-flash")))
    client = TestClient(app)
    body = chat(client, ORDER_QUESTION).json()
    assert body["total_tokens"] > 0 and body["cost_cny"] > 0
    metrics = client.get("/metrics").json()
    assert metrics["cost_complete"] is True and metrics["unpriced_models"] == []
    assert metrics["cost_cny"] == pytest.approx(body["cost_cny"])
    assert metrics["cost_per_success_cny"] == pytest.approx(body["cost_cny"])


def test_explicit_flat_rate_prices_an_unpriced_model() -> None:
    """显式费率是给没有价目的模型用的口径：此时成本可算，且不再标成不完整。"""
    app = create_app(
        agent=V3Agent(planner=scripted_client("scripted-fake")), cost_per_1k_tokens=1.5
    )
    client = TestClient(app)
    body = chat(client, ORDER_QUESTION).json()
    assert body["cost_cny"] == pytest.approx(body["total_tokens"] / 1000 * 1.5)
    assert client.get("/metrics").json()["cost_complete"] is True


def test_second_turn_of_a_session_inherits_its_order_id(client: TestClient) -> None:
    """多轮：第二轮只说「这个」，槽位继承上一轮；这里只断言对外可见的那部分。"""
    first = chat(client, "订单 ORD-1004 到哪了", session_id="s-mt-1").json()
    second = chat(client, REFUND_QUESTION, session_id="s-mt-1").json()
    assert first["state"] == "tool_completed" and first["plan_source"] == "rules"
    assert second["state"] == "tool_completed" and second["plan_source"] == "rules"
    assert second["intent"] == "refund"
    assert second["audit"]["arguments"]["order_id"] == "ORD-1004"
    metrics = client.get("/metrics").json()
    assert metrics["requests"] == 2 and metrics["successes"] == 2


def test_sessions_are_isolated_between_customers(client: TestClient) -> None:
    """同一个 session_id 换个 customer_id 不能继承上一个客户的订单号（SPEC-MT-6）。"""
    chat(client, "订单 ORD-1004 到哪了", session_id="s-shared")
    other = chat(client, REFUND_QUESTION, session_id="s-shared", customer_id="CUST-002").json()
    assert other["state"] == "validation_failed"
    assert client.get("/metrics").json()["failures"] == 1


def test_metrics_expose_terminal_states_and_error_codes(client: TestClient) -> None:
    """报告必须能自己解释「失败」：终态分布与错误码分布得由 /metrics 说清楚。

    只有一个 failures 数字的报告，读的人分不清「工具坏了」和「这一轮本来就该拒答」。
    """
    chat(client, ORDER_QUESTION)
    chat(client, REFUSED_QUESTION)
    chat(client, REFUND_QUESTION)  # 没有 session_id，缺槽位
    metrics = client.get("/metrics").json()
    assert metrics["states"] == {
        "no_action": 1,
        "tool_completed": 1,
        "validation_failed": 1,
    }
    assert metrics["error_codes"] == {"NO_KNOWLEDGE": 1, "VALIDATION_ERROR": 1}


def test_repeated_refund_of_one_order_stays_a_success(client: TestClient) -> None:
    """同一条 ORD-1004 反复退款：首次提交，之后返回同一个 REF 号（``duplicate=True``）。

    ``RefundTool`` 按 (order_id, customer_id) 记住请求并且**仍然返回成功**，所以压测里
    重复退款不会把成功率拖下来。这条断言就是那个结论的证据。
    """
    bodies = [chat(client, "订单 ORD-1004 我要退款").json() for _ in range(3)]
    assert {body["state"] for body in bodies} == {"tool_completed"}
    assert [body["audit"]["result"]["duplicate"] for body in bodies] == [False, True, True]
    assert client.get("/metrics").json()["successes"] == 3


class _SharedSessions(SessionStore):
    """空 store 是**假值**：``V3Agent.__init__`` 里那句 ``sessions or SessionStore()``
    会把它换成一个新的，于是「每线程一个 agent、共享一份会话」的写法会静默失效。"""

    def __bool__(self) -> bool:
        return True


def test_multi_turn_survives_per_thread_agents_with_a_shared_store() -> None:
    """工厂模式（压测用的那种）+ 共享 SessionStore：第二轮仍然继承得到槽位。

    这是 load_test.py 那条链路的回归点：不同的请求可能落在不同线程上，会话要是没共享，
    第二轮的「这个」就变成缺槽位（validation_failed），在压测报告里会显示成一个很高的失败率。
    """
    sessions = _SharedSessions()
    client = TestClient(create_app(agent=lambda: V3Agent(OrderStore(), sessions=sessions)))
    first = chat(client, "订单 ORD-1004 到哪了", session_id="s-factory").json()
    second = chat(client, REFUND_QUESTION, session_id="s-factory").json()
    assert first["state"] == "tool_completed"
    assert second["state"] == "tool_completed"
    assert second["intent"] == "refund"
    assert second["audit"]["arguments"]["order_id"] == "ORD-1004"
