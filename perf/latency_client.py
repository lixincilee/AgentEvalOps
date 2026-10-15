"""建模用的「假模型」：只花睡眠时间，一个字节的网络请求都不发。

它存在的理由是让并发压测能在**零成本**下跑完整条链路：规划、接地、审计、成本记账都真的
执行，只把「供应商那一跳」换成 ``time.sleep`` 与一组估计出来的 token 数。
真实调用走 ``agentevalops.llm.build_client``，那一条要花钱。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence

from agentevalops.llm.base import ChatMessage, LLMResponse, Usage

#: 与 planner 的抽取规则同形。``\b`` 边界是必须的：没有它 "ORD-10011" 会被解析成 ORD-1001。
ORDER_ID_PATTERN = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)

DEFAULT_LATENCY_MS = 60.0
DEFAULT_PROMPT_TOKENS = 260
DEFAULT_COMPLETION_TOKENS = 30
#: 借 deepseek-flash 的 id，好让建模 token 能乘上真实价目算出成本。
#: 它表示「按这个模型的价目折算」，不表示这次调用真的发生在该模型上。
DEFAULT_MODEL_ID = "deepseek-flash"


def classify_intent(message: str) -> str:
    """关键词规则：只求产出形状合法、可预测的规划结果，不追求判准。

    顺序是有意的：「退款政策是怎么规定的」既含「退款」又含「政策」，而它问的是政策，
    所以政策必须先判——否则检索链路在压测里永远压不到。
    """
    text = message.casefold()
    if "政策" in text:
        return "policy_qa"
    if "退款" in text:
        return "refund"
    if "人工" in text:
        return "human_handoff"
    if ORDER_ID_PATTERN.search(message) or "订单" in text:
        return "order_status"
    return "unknown"


class LatencyLLMClient:
    """压测用的 mock ``LLMClient``：延迟与 token 都是**建模值**，不是供应商的真实值。

    具体来说：

    * ``latency_ms`` 只是 ``time.sleep`` 的时长，用来模拟一次网络往返；
    * ``prompt_tokens`` / ``completion_tokens`` 是固定估计值，用来让成本记账有个可比较
      的量级。

    两者都不来自任何一次真实调用。因此由它跑出来的 RPS / P95 / tokens / 成本只能读作
    「该配置下**服务端**的行为」，不能读作「供应商的产能或报价」。要对齐真实时延与真实
    账单，只有 ``scripts/load_test.py --real`` 一条路（真花钱）。
    """

    def __init__(
        self,
        *,
        latency_ms: float = DEFAULT_LATENCY_MS,
        prompt_tokens: int = DEFAULT_PROMPT_TOKENS,
        completion_tokens: int = DEFAULT_COMPLETION_TOKENS,
        model: str = DEFAULT_MODEL_ID,
    ) -> None:
        self.latency_ms = latency_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self._model = model
        #: 调用次数。服务端会自己数一遍 ``model_calls``，两边对不上就是记账漏了。
        self.call_count = 0

    @property
    def model_id(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        time.sleep(self.latency_ms / 1000)
        self.call_count += 1
        last_user = next(
            (
                message.content
                for message in reversed(list(messages))
                if message.role == "user"
            ),
            "",
        )
        found = ORDER_ID_PATTERN.search(last_user)
        payload = {
            "intent": classify_intent(last_user),
            "order_id": found.group(0).upper() if found else None,
        }
        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            model=self._model,
            requested_model=self._model,
            usage=Usage(
                prompt_tokens=self.prompt_tokens,
                completion_tokens=self.completion_tokens,
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
