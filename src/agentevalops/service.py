"""把 v3 链路包成一个可压测的 HTTP 服务（M4 并发压测的服务端）。

刻意**不** import ``agentevalops.faults``：故障注入是另一条线，它的开关落在 agent 层，
与「并发下服务端能扛多少、每成功一次花多少钱」这个口径无关；混在一起，压测数字里就分不清
哪部分是策略、哪部分是人造故障。

计数是**进程内**的：``uvicorn --workers N`` 会让每个进程各有一份 ``/metrics``，压测脚本
必须逐进程聚合。本模块按单进程多线程设计——FastAPI 的同步端点跑在 anyio 线程池里（默认
40 个线程），那也正是压测里并发的真实上界。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import ConfigurationError, load_settings
from .llm import Usage, build_pricing, estimate_cost
from .llm.costing import ModelPrice
from .models import AgentResponse, AgentState
from .order_store import OrderStore
from .pipeline import V3Agent

#: 「成功」在 ``/metrics`` 里只能有一个定义：工具真的做完了这件事。拒答、缺槽位、工具失败
#: 都是**其它终态**，算 failure——它们不是成功任务，混进成功率里就是自欺欺人。
SUCCESS_STATE = AgentState.TOOL_COMPLETED


class ChatRequest(BaseModel):
    """``/chat`` 的请求体契约。字段名与 AgentResponse 的对外字段同名，少一层翻译。"""

    message: str
    customer_id: str
    session_id: str | None = None


def _pricing() -> dict[str, ModelPrice]:
    """默认价目表 + ``.env`` 里 AGENTEVALOPS_PRICING_JSON 的覆盖（读不出来就用默认表）。"""
    try:
        return build_pricing(load_settings().model_pricing())
    except (ConfigurationError, OSError):
        return build_pricing()


def _invoke(
    agent: V3Agent, message: str, customer_id: str, session_id: str | None
) -> tuple[AgentResponse, str | None, str | None, Usage, int]:
    """跑一轮，并把本轮状态一起读出来：``(响应, 规划来源, 模型 id, 用量, 调用次数)``。

    调用方必须保证「跑 + 读」之间没有别的线程插进来，理由见 :func:`create_app`。
    用量只能从规划器的 ``last_response`` 上取（一次 handle 恰好一次调用），这是不改
    planner 代码就读到真实 usage 的唯一入口。模型抛异常直接走兜底时它是 None，token 记
    0——成本只会低估、不会虚高；规则臂没有 client，永远是 0。
    """
    response = agent.handle(message, customer_id=customer_id, session_id=session_id)
    plan = getattr(agent, "last_plan", None)
    model_response = getattr(getattr(agent, "planner", None), "last_response", None)
    source = None if plan is None else plan.source
    if model_response is None:
        return response, source, None, Usage(), 0
    return response, source, model_response.model, model_response.usage, 1


class ServiceCounters:
    """累计计数。服务是多线程的，所以更新与读取共用一把锁。

    ``requests`` 只算**进入执行链路**的请求（400 是参数错误，不进这里）；每个这样的请求
    必然落进 ``successes`` 或 ``failures`` 之一，于是 ``requests == successes + failures``
    恒成立，可以当断言用。``errors`` 是 5xx 计数，同时算一次 failure：「跑到一半炸了」
    不是成功。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = self._successes = self._failures = self._errors = 0
        self._tokens = 0
        self._cost = 0.0
        self._cost_complete = True
        self._unpriced: set[str] = set()
        #: 终态与错误码各出现过几次。光有 successes/failures 只能知道「坏了多少」，
        #: 不知道「坏在哪一步」——validation_failed 和 no_action 要完全不同的处置，
        #: 报告里也必须能自己把这句话说完，而不是留一个孤零零的失败数。
        self._states: dict[str, int] = {}
        self._error_codes: dict[str, int] = {}

    def record_turn(
        self,
        state: AgentState,
        tokens: int,
        cost: float | None,
        model: str | None,
        error_code: str | None = None,
    ) -> None:
        with self._lock:
            self._requests += 1
            if state == SUCCESS_STATE:
                self._successes += 1
            else:
                self._failures += 1
            self._tokens += tokens
            # ``None`` 是「这个模型没有价目」，不是 0；两者混起来的代价是成本报告虚低。
            self._cost += cost or 0.0
            self._states[str(state)] = self._states.get(str(state), 0) + 1
            if error_code:
                self._error_codes[str(error_code)] = self._error_codes.get(str(error_code), 0) + 1
            if cost is None:
                self._cost_complete = False
                if model:
                    self._unpriced.add(model)

    def record_server_error(self) -> None:
        with self._lock:
            self._requests += 1
            self._failures += 1
            self._errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            cost, successes = self._cost, self._successes
            return {
                "requests": self._requests,
                "successes": successes,
                "failures": self._failures,
                "errors": self._errors,
                "tokens": self._tokens,
                "cost_cny": round(cost, 9),
                "success_rate": round(successes / self._requests, 6) if self._requests else 0.0,
                "cost_per_success_cny": None if not successes else round(cost / successes, 9),
                "cost_complete": self._cost_complete,
                "unpriced_models": sorted(self._unpriced),
                "states": dict(sorted(self._states.items())),
                "error_codes": dict(sorted(self._error_codes.items())),
            }


def create_app(
    *,
    agent: V3Agent | Callable[[], V3Agent] | None = None,
    cost_per_1k_tokens: float | None = None,
) -> FastAPI:
    """建服务。``agent`` 传什么就用什么，所以同一个服务既能量规则臂也能量真模型臂。

    * ``None``（默认）：自建 ``V3Agent(OrderStore())``，默认规则臂，零成本零方差；
    * 一个 **V3Agent 实例**：共享实例。它的每轮状态（``last_plan``、规划器的
      ``last_response``）挂在实例属性上，两个线程同时进来必然串台，``last_plan`` 还可能
      在被读到的瞬间是 None。所以这里用一把锁把「调用 + 读状态」串起来，代价是并发度；
    * 一个**工厂**（无参 callable）：每个线程各建一个实例，锁就不需要了。压测模型臂必须
      走这条——否则 60ms 的假延迟会把服务压成一条单线程流水线，测出来的 RPS 只反映那把锁。

    ``cost_per_1k_tokens`` 给没有价目的模型一个**显式费率**（元 / 千 token），此时成本口径
    是「按费率折算」而非真实价目；不给就走 ``estimate_cost`` + 真实价目，查不到价目时成本
    记 0 并在 ``/metrics`` 里报 ``cost_complete: false``——绝不装作免费。
    """
    pricing, counters = _pricing(), ServiceCounters()
    shared: V3Agent | None = None
    factory: Callable[[], V3Agent] | None = None
    agent_lock: threading.Lock | None = None
    if agent is None:
        shared, agent_lock = V3Agent(OrderStore()), threading.Lock()
    elif callable(agent):
        factory = agent
    else:
        shared, agent_lock = agent, threading.Lock()
    per_thread = threading.local()

    def agent_for_call() -> V3Agent:
        if factory is None:
            assert shared is not None
            return shared
        instance = getattr(per_thread, "agent", None)
        if instance is None:
            instance = per_thread.agent = factory()
        return instance

    def price(model: str | None, usage: Usage) -> float | None:
        """本轮花费；``None`` 表示这个模型没有价目，成本只能算下限。"""
        if usage.total_tokens == 0:
            return 0.0
        if cost_per_1k_tokens is not None:
            return usage.total_tokens / 1000 * cost_per_1k_tokens
        return None if model is None else estimate_cost(model, usage, pricing)

    app = FastAPI(title="AgentEvalOps service", version="0.1")

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """把 FastAPI 默认的 422 统一成 400，并把原因写进 body。

        422 是框架内部约定，漏出去会让「客户端参数写错」看起来像「服务端故障」；压测脚本
        按 4xx / 5xx 分类，400 才是它该看到的那一类。
        """
        reasons = [
            f"{'.'.join(str(part) for part in error.get('loc', ())[1:]) or 'body'}: {error.get('msg')}"
            for error in exc.errors()
        ]
        return JSONResponse(status_code=400, content={"detail": "请求体不合法", "errors": reasons})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        return counters.snapshot()

    @app.post("/chat")
    def chat(payload: ChatRequest) -> dict[str, Any]:
        """一轮对话。同步端点：FastAPI 会把它丢进线程池，压测才有真并发。"""
        started = time.perf_counter()
        arguments = (payload.message, payload.customer_id, payload.session_id)
        try:
            if agent_lock is None:
                result = _invoke(agent_for_call(), *arguments)
            else:
                with agent_lock:
                    result = _invoke(agent_for_call(), *arguments)
        except Exception:
            counters.record_server_error()
            raise
        response, source, model, usage, calls = result
        cost = price(model, usage)
        counters.record_turn(
            response.state,
            usage.total_tokens,
            cost,
            model,
            (response.audit.error or {}).get("code"),
        )
        return {
            "message": response.message,
            "intent": str(response.intent),
            "state": str(response.state),
            "audit": response.audit.to_dict(),
            "plan_source": source,
            "model_calls": calls,
            "total_tokens": usage.total_tokens,
            "cost_cny": round(cost or 0.0, 9),
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    return app
