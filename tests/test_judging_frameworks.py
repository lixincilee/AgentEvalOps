r"""框架接线测试：只在装了 ragas / deepeval 的判分 venv 里跑。

主环境（``D:\Python``）没有这两个包，所以整个文件会被 skip —— 这是有意的：
1220 条确定性测试不该为了两个评测框架把 openai 升到 3.x。

跑法：

    .venv-judge\\Scripts\\python.exe -m pytest tests/test_judging_frameworks.py -q
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "1")
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

ragas = pytest.importorskip("ragas")
deepeval = pytest.importorskip("deepeval")

from pydantic import BaseModel  # noqa: E402

from agentevalops.judging.base import JudgeSample, JudgeScore  # noqa: E402
from agentevalops.judging.collect import ComposedAnswer  # noqa: E402
from agentevalops.llm import Usage  # noqa: E402


class StubClient:
    model_id = "deepseek-flash"

    def __init__(self, text: str = '{"score": 1}') -> None:
        self.text = text
        self.calls = 0

    def complete(self, messages, *, temperature=None, max_tokens=None, extra_body=None):
        self.calls += 1
        self.last_extra_body = extra_body

        class _Response:
            text = self.text
            model = "deepseek-flash"
            usage = Usage(prompt_tokens=5, completion_tokens=3)

        return _Response()


class Verdict(BaseModel):
    verdict: str
    reason: str


def test_deepeval_model_returns_plain_text_without_schema():
    from agentevalops.judging.deepeval_layer import build_model

    model = build_model(StubClient("hello"), name="deepseek-flash")
    assert model.generate("prompt") == "hello"
    assert model.get_model_name() == "deepseek-flash"
    assert model.load_model() is not None


def test_deepeval_model_parses_a_pydantic_schema():
    from agentevalops.judging.deepeval_layer import build_model

    model = build_model(StubClient('{"verdict": "yes", "reason": "grounded"}'))
    result = model.generate("prompt", schema=Verdict)
    assert isinstance(result, Verdict)
    assert result.verdict == "yes"


def test_deepeval_metric_objects_construct_with_our_model():
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    from agentevalops.judging.deepeval_layer import CRITERIA, build_model

    model = build_model(StubClient())
    metric = GEval(
        name="grounded_answer",
        evaluation_steps=list(CRITERIA["grounded_answer"]["steps"]),
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        model=model,
        threshold=0.7,
        async_mode=False,
    )
    assert metric.name == "grounded_answer"


def test_ragas_llm_factory_accepts_an_openai_compatible_async_client():
    import openai

    from ragas.llms import llm_factory

    client = openai.AsyncOpenAI(api_key="test-key", base_url="http://127.0.0.1:9/v1")
    llm = llm_factory("deepseek-flash", client=client, adapter="instructor")
    assert hasattr(llm, "generate")
    assert hasattr(llm, "agenerate")


def test_ragas_metric_objects_construct_with_that_llm():
    import openai
    from ragas.llms import llm_factory

    from agentevalops.judging.ragas_layer import metric_object

    client = openai.AsyncOpenAI(api_key="test-key", base_url="http://127.0.0.1:9/v1")
    llm = llm_factory("deepseek-flash", client=client, adapter="instructor")
    for name in ("faithfulness", "context_precision"):
        metric = metric_object(name, llm)
        assert metric.name
