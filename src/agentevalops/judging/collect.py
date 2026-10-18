"""把「被测系统的一次真实运行」变成一条可交给框架的 JudgeSample。

这里有一个必须说清楚的产品事实：**v3 的 policy_qa 返回的是
``message="request completed"`` + ``audit.result`` 里的结构化证据**，它不生成客户话术。
拿 ``"request completed"`` 去算 Ragas 忠实性，分数只会是 0 或 NaN —— 那是在测一个占位符。

所以判分层提供一个**答案合成步骤**（``compose_answer``），并把它标成
``prompt_version=answer-v1``：

* 它评的是「检索证据 → 客服话术」这一步，也就是幻觉真正会发生的地方；
* 它不是产品代码：把话术接进 ``V3Agent.handle`` 会改掉已冻结的 v0.8.0 行为，
  属于下一阶段的产品改动（见 docs/judging.md「为什么合成在判分层」）；
* ``metadata`` 里同时保留 ``agent_message``，报告不会假装那是模型写的。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..llm import LLMClient
from ..llm.base import ChatMessage
from ..pipeline import V3Agent
from ..retrieval import KnowledgeBase
from .base import JudgeSample
from .llm import disabled_thinking_body

ANSWER_PROMPT_VERSION = "answer-v1"

#: 合成 prompt 的硬约束：**只依据资料**。这一段会被原样写进证据文档，改它等于换版本。
ANSWER_SYSTEM_PROMPT = (
    "你是电商客服。只能依据给定的资料回答用户问题。\n"
    "规则：\n"
    "1. 资料里没有写到的内容，直接回答「资料里没有说明」，不要补全、不要推测。\n"
    "2. 不要引用文档编号，不要说「根据资料」。\n"
    "3. 数字、期限、条件必须与资料逐字一致。\n"
    "4. 用中文回答，不超过 120 字。"
)


@dataclass(frozen=True)
class ComposedAnswer:
    text: str
    model: str
    prompt_version: str = ANSWER_PROMPT_VERSION
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: float = 0.0
    cost_cny: float = 0.0


def load_cases(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def render_contexts(contexts: Sequence[str]) -> str:
    if not contexts:
        return "（没有检索到任何资料）"
    blocks = [f"[资料 {index}] {text}" for index, text in enumerate(contexts, start=1)]
    return "\n\n".join(blocks)


def compose_answer(
    question: str,
    contexts: Sequence[str],
    client: LLMClient,
    *,
    ledger: Any | None = None,
    max_tokens: int = 512,
    prompt_version: str = ANSWER_PROMPT_VERSION,
) -> ComposedAnswer:
    """用检索到的资料写一段客服话术。**没有资料时要敢说「资料里没有说明」。**"""
    before = float(getattr(ledger, "cost", 0.0)) if ledger is not None else 0.0
    started = time.perf_counter()
    response = client.complete(
        [
            ChatMessage.system(ANSWER_SYSTEM_PROMPT),
            ChatMessage.user(
                f"用户问题：{question}\n\n可用资料：\n{render_contexts(contexts)}"
            ),
        ],
        temperature=0.0,
        max_tokens=max_tokens,
        extra_body=disabled_thinking_body(),
    )
    elapsed = (time.perf_counter() - started) * 1000.0
    cost = float(getattr(ledger, "cost", 0.0)) - before if ledger is not None else 0.0
    return ComposedAnswer(
        text=response.text.strip(),
        model=getattr(response, "model", None) or getattr(client, "model_id", "unknown"),
        prompt_version=prompt_version,
        usage=response.usage.to_dict(),
        latency_ms=round(elapsed, 1),
        cost_cny=round(cost, 8),
    )


def collect_rag_samples(
    cases: Sequence[Mapping[str, Any]],
    *,
    composer: Callable[[str, tuple[str, ...]], ComposedAnswer] | None = None,
    knowledge_base: KnowledgeBase | None = None,
    agent_factory: Callable[[], V3Agent] = V3Agent,
) -> list[JudgeSample]:
    """每条用例一个干净的 Agent（含干净的 OrderStore），与 trace_run.py 同一口径。"""
    kb = knowledge_base or KnowledgeBase()
    texts = {doc["doc_id"]: doc.get("text", "") for doc in kb.documents}
    samples: list[JudgeSample] = []
    for case in cases:
        agent = agent_factory()
        response = agent.handle(
            case["message"],
            customer_id=case.get("customer_id", "CUST-001"),
            session_id=case.get("session_id"),
        )
        retrieval = agent.last_retrieval
        hits = () if retrieval is None else retrieval.hits
        contexts = tuple(texts.get(hit["doc_id"], "") for hit in hits)
        contexts = tuple(text for text in contexts if text)
        composed = None if composer is None else composer(case["message"], contexts)
        metadata: dict[str, Any] = {
            "category": case.get("category"),
            "doc_ids": list(retrieval.doc_ids) if retrieval is not None else [],
            "grounded": bool(retrieval.grounded) if retrieval is not None else False,
            "agent_message": response.message,
            "agent_state": str(response.state),
            "composed": composed is not None,
            "compose_prompt_version": None if composed is None else composed.prompt_version,
            "compose_cost_cny": 0.0 if composed is None else composed.cost_cny,
            "compose_latency_ms": 0.0 if composed is None else composed.latency_ms,
        }
        if composed is not None:
            metadata["compose_usage"] = dict(composed.usage)
        samples.append(
            JudgeSample(
                case_id=str(case["case_id"]),
                question=str(case["message"]),
                answer=response.message if composed is None else composed.text,
                contexts=contexts,
                expected_state=case.get("expected_state"),
                expected_error_code=case.get("expected_error_code"),
                metadata=metadata,
            )
        )
    return samples
