"""判分层的模型客户端与花费计量。

L3（Ragas）与 L4（DeepEval）都必须真的调模型，所以都要回答两个问题：**用哪个 client**、
**花了多少钱**。这里的做法是：

* Ragas 要一个 OpenAI 兼容的 async client —— 用 ``openai.AsyncOpenAI`` 指向 DeepSeek 的
  base_url，并在**实例上**包一层 ``chat.completions.create``，把响应里的 ``usage`` 记进
  ``UsageLedger``。包在实例上而不是改类：同一个进程里其它 client 不该被顺带改掉。
* DeepEval 的自定义模型直接拿本项目的 ``LLMClient``（``DeepSeekClient``），同样过账本。

``openai`` 只在真正构造 Ragas client 时才 import —— 主测试环境不装它也能跑完 1220 条测试。
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..llm import MeteredLLMClient, Usage, UsageLedger, build_client, build_pricing


class JudgeUnavailableError(RuntimeError):
    """判分层缺依赖 / 缺凭据。调用方应该把它翻译成退出码 2，而不是当成低分。"""


#: DeepSeek 默认开思考模式。判分/合成都是短结构化输出，reasoning 会把 token 预算
#: 花光：实测 1024 的预算只够写 422 个 reasoning token，JSON 直接被截断
#: （IncompleteOutputException），合成回答还会返回空字符串。判分层统一关掉。
def disabled_thinking_body() -> dict[str, Any]:
    return {"thinking": {"type": "disabled"}}


def build_ledger(settings: Settings, budget: float | None = None) -> UsageLedger:
    """与其它会花钱的脚本同一个账本：没有价目的模型 + 预算 = 发请求前就该被拒。"""
    return UsageLedger(
        limit=budget if budget is not None else settings.budget,
        pricing=build_pricing(settings.model_pricing()),
        currency=settings.currency,
    )


def require_api_key(settings: Settings) -> str:
    try:
        return settings.require_api_key()
    except Exception as exc:  # MissingCredentialError
        raise JudgeUnavailableError(str(exc)) from exc


def build_metered_client(settings: Settings, ledger: UsageLedger) -> Any:
    """本项目的 httpx 客户端 + 账本（DeepEval 与答案合成用这个）。"""
    require_api_key(settings)
    return MeteredLLMClient(build_client(settings, ledger=ledger), ledger)


def build_async_openai(settings: Settings, ledger: UsageLedger) -> Any:
    """给 Ragas 用的 OpenAI 兼容 async client，每次调用都记 usage。

    ``usage`` 从**原始响应**里读：instructor 会在我们的包装层外面再包一层，把结构化结果
    返回给 Ragas，但底层那次 HTTP 仍然经过这里，所以 token 数不会丢。
    """
    require_api_key(settings)
    try:
        import openai  # noqa: F401  （可选依赖，只有 L3 需要）
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise JudgeUnavailableError(
            "L3 需要 openai SDK。请在判分 venv 里执行："
            '.venv-judge\\Scripts\\pip install -e ".[judge,dev,llm]"'
        ) from exc

    import openai

    client = openai.AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.request_timeout_s,
        max_retries=settings.max_retries,
    )
    original = client.chat.completions.create

    async def metered_create(*args: Any, **kwargs: Any) -> Any:
        body = dict(kwargs.get("extra_body") or {})
        body.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_body"] = body
        response = await original(*args, **kwargs)
        record_openai_usage(ledger, response)
        return response

    client.chat.completions.create = metered_create  # type: ignore[method-assign]
    return client


def record_openai_usage(ledger: UsageLedger, response: Any) -> None:
    """把一次 OpenAI 兼容响应的 usage 记进账本；字段缺失就跳过，不猜。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    model = getattr(response, "model", None) or "unknown"
    try:
        ledger.record(
            model,
            Usage(
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ),
        )
    except Exception:
        # 记账失败不该让判分崩：分数是主产物，成本是附属信息（报告里会标 incomplete）。
        pass
