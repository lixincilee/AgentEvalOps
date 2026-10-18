"""L4：DeepEval 的 LLM-as-a-Judge（GEval）。

两条判据，按「有没有检索依据」二选一：

* ``grounded_answer``（有依据）—— 回答里的每个数字、期限、条件都必须在资料里有出处。
* ``refusal_quality``（无依据）—— 该说「资料里没有说明」时有没有编造。

为什么不用 DeepEval 的 ``AnswerRelevancyMetric`` / ``FaithfulnessMetric``：前者默认要
OpenAI embedding，后者要 ``retrieval_context`` 且内部另有判据。守住「不引入假 embedding」
这条线，只用 GEval（纯 LLM 打分）。

评分步骤写死（``evaluation_steps``）而不是让 GEval 自己生成：省一次模型调用，也让
「同一份回答为什么是 0.7」在版本之间可比。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .base import JudgeSample, JudgeScore, judge_score
from .llm import JudgeUnavailableError, disabled_thinking_body

LAYER = "L4"

#: 判据表。``params`` 用字符串而不是 DeepEval 的枚举，import 时才映射（主环境不装 deepeval）。
CRITERIA: dict[str, dict[str, Any]] = {
    "grounded_answer": {
        "steps": [
            "逐句检查 actual_output 中的事实性内容（数字、期限、条件、金额、渠道）。",
            "对每一项，判断 retrieval_context 里是否有明确依据。",
            "只要有一项在资料里找不到依据，视为编造，最多给 0.3 分。",
            "如果回答主动补充了资料之外的建议或承诺，额外扣分。",
        ],
        "params": ("input", "actual_output", "retrieval_context"),
    },
    "refusal_quality": {
        "steps": [
            "判断用户问题在 retrieval_context 里是否有依据。",
            "如果没有依据，回答必须明确说明「资料里没有说明」或等价表述。",
            "如果回答给出了资料之外的答案、期限或承诺，视为编造，最多给 0.2 分。",
            "如果回答只是含糊其辞但没有编造，给 0.5 分。",
        ],
        "params": ("input", "actual_output"),
    },
}

#: 有依据走 grounded，没依据走 refusal。判据选择本身也是口径的一部分。
def criterion_for(sample: JudgeSample) -> str:
    return "grounded_answer" if sample.contexts else "refusal_quality"


def build_model(client: Any, *, name: str = "", max_tokens: int = 1024) -> Any:
    """把本项目的 LLMClient 包成 DeepEval 认的模型。"""
    try:
        from deepeval.models import DeepEvalBaseLLM
    except ImportError as exc:  # pragma: no cover
        raise JudgeUnavailableError(
            "L4 需要 deepeval。请用判分 venv：.venv-judge\\Scripts\\pip install -e \".[judge,dev,llm]\""
        ) from exc

    from ..llm.base import ChatMessage

    class DeepSeekJudge(DeepEvalBaseLLM):  # type: ignore[misc, valid-type]
        """DeepEval 只要求四个方法：load_model / get_model_name / generate / a_generate。"""

        def __init__(self) -> None:
            self._client = client
            self._name = name or getattr(client, "model_id", "deepseek")
            self._max_tokens = max_tokens
            self.last_cost_cny = 0.0
            self.last_usage: dict[str, int] = {}
            super().__init__(self._name)

        def load_model(self) -> Any:  # noqa: D401 - DeepEval 的接口名
            return self._client

        def get_model_name(self) -> str:
            return self._name

        def generate(self, prompt: str, schema: Any | None = None) -> Any:
            return self._invoke(prompt, schema)

        async def a_generate(self, prompt: str, schema: Any | None = None) -> Any:
            return self._invoke(prompt, schema)

        def _invoke(self, prompt: str, schema: Any | None = None) -> Any:
            messages = [ChatMessage.user(prompt)]
            if schema is not None:
                messages.append(
                    ChatMessage.user(
                        "只输出一个 JSON 对象，不要解释、不要 markdown 代码块。"
                    )
                )
            response = self._client.complete(
                messages,
                temperature=0.0,
                max_tokens=self._max_tokens,
                extra_body=disabled_thinking_body(),
            )
            self.last_usage = response.usage.to_dict()
            text = response.text
            if schema is None:
                return text
            return _parse_schema(schema, text)

    return DeepSeekJudge()


def _parse_schema(schema: Any, text: str) -> Any:
    """DeepEval 传进来的 schema 通常是 pydantic 模型类；解析失败要抛，不能返回字符串。"""
    import json

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    if hasattr(schema, "model_validate_json"):
        return schema.model_validate_json(cleaned)
    if hasattr(schema, "parse_raw"):  # pydantic v1 形状，留个口子
        return schema.parse_raw(cleaned)
    return json.loads(cleaned)


def evaluate_samples(
    samples: Sequence[JudgeSample],
    *,
    model: Any,
    thresholds: Mapping[str, float] | None = None,
    ledger: Any | None = None,
    judge_model: str = "",
) -> list[JudgeScore]:
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, SingleTurnParams

    metrics: dict[str, Any] = {}
    for name, spec in CRITERIA.items():
        metrics[name] = GEval(
            name=name,
            evaluation_steps=list(spec["steps"]),
            evaluation_params=[getattr(SingleTurnParams, param.upper()) for param in spec["params"]],
            model=model,
            threshold=float((thresholds or {}).get(name, 0.7)),
            async_mode=False,
        )

    rows: list[JudgeScore] = []
    for sample in samples:
        name = criterion_for(sample)
        metric = metrics[name]
        before = float(getattr(ledger, "cost", 0.0)) if ledger is not None else 0.0
        case = LLMTestCase(
            input=sample.question,
            actual_output=sample.answer,
            retrieval_context=list(sample.contexts) if sample.contexts else None,
        )
        try:
            metric.measure(case, _show_indicator=False)
            rows.append(
                judge_score(
                    sample=sample,
                    layer=LAYER,
                    metric=name,
                    score=float(metric.score),
                    reason=str(getattr(metric, "reason", "") or ""),
                    judge_model=judge_model,
                    cost_cny=_delta(ledger, before),
                    thresholds=thresholds,
                )
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                judge_score(
                    sample=sample,
                    layer=LAYER,
                    metric=name,
                    score=None,
                    error=f"{type(exc).__name__}: {exc}",
                    judge_model=judge_model,
                    cost_cny=_delta(ledger, before),
                    thresholds=thresholds,
                )
            )
    return rows


def _delta(ledger: Any | None, before: float) -> float:
    if ledger is None:
        return 0.0
    return max(0.0, float(getattr(ledger, "cost", 0.0)) - before)
