"""Locust 压测脚本：只当 HTTP 客户端用，不 import 仓库里的任何模块。

闭环压测：``wait_time = between(0, 0)``，每个虚拟用户发完一条立刻发下一条，于是 ``-u``
就是「同时在飞的请求数」上界——真正的上界在服务端（anyio 线程池，默认 40 个线程）。

两个口径必须分开，混起来会得出反的结论：

* **http 错误**：只看传输层——非 200、响应不是 JSON、连接异常/超时。它才是「系统坏了」。
* **任务是否成功**：看这一轮的终态是不是**这条消息该有的结果**（见 ``EXPECTATIONS``）。
  拒答（``no_action`` + ``NO_KNOWLEDGE``）是正确结果；重复退款在本仓库也是成功
  （``RefundTool`` 对同一 (order, customer) 幂等，见 SPEC 的实现注释）。把这些算成错误，
  等于说「系统越守规矩，错误率越高」。

退出前打两行机器可读的结果，``scripts/load_test.py`` 直接解析它们拼报告。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import requests
from locust import HttpUser, between, events, task

DEFAULT_HOST = "http://127.0.0.1:8000"
CUSTOMER_ID = "CUST-001"

#: 每条消息的期望终态：允许出现的 ``(state, error_code)`` 组合，``error_code`` 为 None
#: 表示这一轮不该有错误码。放在文件顶部而不是散在断言里，是为了让「什么算对」一眼看全、
#: 也能被复审——压测的判据本身就是一份需要被 review 的声明。
#: 退款那条额外允许 REFUND_NOT_ALLOWED：本仓库的 RefundTool 幂等（重复退款仍返回成功），
#: 所以那是给「万一换了实现」留的容错，不是常见路径。
EXPECTATIONS: dict[str, tuple[tuple[str, str | None], ...]] = {
    "查单 ORD-1001": (("tool_completed", None),),
    "退款 ORD-1004": (("tool_completed", None), ("tool_failed", "REFUND_NOT_ALLOWED")),
    "转人工": (("tool_completed", None),),
    "政策问答": (("tool_completed", None),),
    "拒答未知主题": (("no_action", "NO_KNOWLEDGE"),),
    "多轮 第1轮·查单": (("tool_completed", None),),
    "多轮 第2轮·指代退款": (("tool_completed", None),),
}

#: 任务级结果。gevent 是协作式调度，greenlet 不会在 ``+=`` 中间被切走，所以不必加锁。
TASK_OUTCOMES: dict[str, Any] = {"total": 0, "unexpected": 0, "by_outcome": {}}
UNEXPECTED_SAMPLES: list[str] = []
#: 服务端在响应里自报的 ``latency_ms``（端点内计时，不含网络与排队）。它与 locust 的 P50
#: 之差就是「排队 + 客户端开销」那一块——压测报告要能把这两块拆开，否则没法解释 P95。
SERVER_LATENCY: dict[str, float] = {"sum": 0.0, "count": 0, "max": 0.0}


def _error_code(body: dict[str, Any]) -> str | None:
    error = (body.get("audit") or {}).get("error") or {}
    code = error.get("code")
    return None if code is None else str(code)


def _record_task(
    name: str, state: str, code: str | None, server_ms: float | None = None
) -> None:
    """记一条任务级结果；不符合 ``EXPECTATIONS`` 的算 unexpected——但**不算 http 错误**。"""
    if server_ms is not None:
        SERVER_LATENCY["sum"] += server_ms
        SERVER_LATENCY["count"] += 1
        SERVER_LATENCY["max"] = max(SERVER_LATENCY["max"], server_ms)
    TASK_OUTCOMES["total"] += 1
    outcome = state if code is None else f"{state}/{code}"
    TASK_OUTCOMES["by_outcome"][outcome] = TASK_OUTCOMES["by_outcome"].get(outcome, 0) + 1
    if (state, code) in EXPECTATIONS[name]:
        return
    TASK_OUTCOMES["unexpected"] += 1
    if len(UNEXPECTED_SAMPLES) < 5:
        UNEXPECTED_SAMPLES.append(f"{name}: {outcome}")


class AgentUser(HttpUser):
    """一个虚拟用户就是一位顾客：起步先领一个只属于自己的 session_id。

    会话 id 必须每个用户各不相同。多轮任务要压的是「同一 session 连续两轮的槽位继承」，
    所有用户共用一个 id 的话，压出来的就不是会话状态、而是相互覆盖的乱序。
    """

    wait_time = between(0, 0)
    host = DEFAULT_HOST

    def on_start(self) -> None:
        self.session_id = f"locust-{uuid.uuid4().hex[:12]}"

    def chat(self, name: str, message: str, *, session_id: str | None = None) -> None:
        """发一轮：传输层失败记 ``response.failure``，终态是否符合期望记进任务级统计。"""
        payload = {"message": message, "customer_id": CUSTOMER_ID, "session_id": session_id}
        with self.client.post("/chat", json=payload, name=name, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}: {response.text[:160]}")
                _record_task(name, "http_error", f"HTTP_{response.status_code}")
                return
            try:
                body = response.json()
            except ValueError:
                response.failure(f"响应不是 JSON: {response.text[:160]}")
                _record_task(name, "http_error", "NOT_JSON")
                return
            reported = body.get("latency_ms")
            _record_task(
                name,
                str(body.get("state")),
                _error_code(body),
                float(reported) if isinstance(reported, (int, float)) else None,
            )

    @task(6)
    def order_status(self) -> None:
        self.chat("查单 ORD-1001", "我的订单 ORD-1001 到哪了")

    @task(3)
    def refund(self) -> None:
        self.chat("退款 ORD-1004", "订单 ORD-1004 我要退款")

    @task(2)
    def human_handoff(self) -> None:
        self.chat("转人工", "转人工")

    @task(4)
    def policy_qa(self) -> None:
        # 政策问答会真的走检索链路（needs_retrieval 命中），压的是检索那一段。
        self.chat("政策问答", "退款政策是怎么规定的")

    @task(2)
    def refusal(self) -> None:
        # 拒答不是故障：知识库里没有就该拒答。期望值写在 EXPECTATIONS 里，不在这里判。
        self.chat("拒答未知主题", "宠物寄存政策是什么")

    @task(2)
    def multi_turn(self) -> None:
        """两轮同一 session_id：第一轮给出订单号，第二轮只说「这个」。"""
        self.chat("多轮 第1轮·查单", "我的订单 ORD-1004 到哪了", session_id=self.session_id)
        self.chat("多轮 第2轮·指代退款", "这个我要退款", session_id=self.session_id)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    """收尾把两边的账都打出来，各一行机器可读的 JSON。

    ``[service-metrics]`` 只取服务端知道的东西（token、成本、终态与错误码分布），
    ``[task-metrics]`` 是这里的任务级判据。分两行是因为它们来自两个进程，
    合成一行就得在某一侧假装知道对方的事。
    """
    total = TASK_OUTCOMES["total"]
    expected = total - TASK_OUTCOMES["unexpected"]
    print(
        "[task-metrics] "
        + json.dumps(
            {
                "total": total,
                "expected": expected,
                "unexpected": TASK_OUTCOMES["unexpected"],
                "task_success_rate": None if not total else round(expected / total, 6),
                "by_outcome": dict(TASK_OUTCOMES["by_outcome"]),
                "unexpected_samples": list(UNEXPECTED_SAMPLES),
                "server_latency_ms_avg": None
                if not SERVER_LATENCY["count"]
                else round(SERVER_LATENCY["sum"] / SERVER_LATENCY["count"], 3),
                "server_latency_ms_max": round(SERVER_LATENCY["max"], 3),
            },
            ensure_ascii=False,
        )
    )
    host = (environment.host or DEFAULT_HOST).rstrip("/")
    try:
        metrics = requests.get(f"{host}/metrics", timeout=10).json()
    except Exception as exc:  # 服务端可能已经不在了；客户端侧的统计仍然有效
        print(f"[service-metrics] 读取 /metrics 失败: {type(exc).__name__}: {exc}")
        _print_failures(environment)
        return
    successes = metrics.get("successes") or 0
    cost = metrics.get("cost_cny") or 0.0
    # 原样透传服务端的 payload（键名一个都不改），再补一个压测侧算的 cost_per_success。
    # 名字改过一道，两边就对不上了：报告里 terminal_states 会变成空的，成本口径也会被误判。
    payload = dict(metrics)
    payload["cost_per_success"] = None if not successes else cost / successes
    print(
        "[service-metrics] "
        + json.dumps(payload, ensure_ascii=False)
    )
    _print_failures(environment)


def _print_failures(environment) -> None:
    """把 http 失败原文打出来。

    locust 只在**没给** ``--json-file`` 时打印错误表（main.py 里那是 if/elif 分支），
    而压测脚本恰恰给了 ``--json-file``——错误率是个数字，失败原因才是一条线索，
    所以这里直接从 stats 里捞出来自己打。
    """
    details = [
        {"name": entry.name, "error": str(entry.error), "occurrences": entry.occurrences}
        for entry in getattr(getattr(environment, "stats", None), "errors", {}).values()
    ]
    for detail in sorted(details, key=lambda item: item["occurrences"], reverse=True)[:5]:
        print(f"[failure] {detail['occurrences']}x {detail['name']}: {detail['error'][:200]}")
