"""判分分层（L3 / L4 / L5）。

三层都是**可选**依赖：``import agentevalops.judging`` 不需要装 ragas / deepeval，
框架只在真正调用对应层时才 import（见各 ``*_layer.py`` 的模块文档）。

分层的口径（``src/agentevalops/runs.py`` 里早就写死了）：

    L1 规则   —— 字段级断言（pytest、trace_report）
    L2 检索   —— 命中率 / top-1（compare_versions）
    L3 Ragas  —— 忠实性、上下文精度（本包 ragas_layer）
    L4 DeepEval —— LLM-as-a-Judge（本包 deepeval_layer）
    L5 Promptfoo —— 提示注入 / 越权 / 泄露（promptfoo_layer）
    L6 性能   —— P95 / 吞吐 / 成本（Locust）

为什么 L3 与 L4 都判「忠实性」还不算重复：两者的判据不同。Ragas 拆成**原子陈述**逐条
核对上下文，擅长抓「多编了一个数字」；GEval 是按评分步骤给整体打分，擅长抓「答非所问」
和「该拒答却硬答」。两条分数不一致的用例，正是最值得人看的那批。
"""

from .base import (
    DEFAULT_THRESHOLDS,
    LAYER_NAMES,
    JudgeReport,
    JudgeSample,
    JudgeScore,
)

__all__ = [
    "DEFAULT_THRESHOLDS",
    "LAYER_NAMES",
    "JudgeReport",
    "JudgeSample",
    "JudgeScore",
]
