"""The frozen behaviour spec this platform grades against.

Before ``docs/agent-spec.md`` existed, every expectation in the case matrix was derived
from the rule planner's *implementation* ("run the agent, write down what it did"). That
made the baseline unbeatable by construction, and left nine real disagreements between v1
and v2 with no way to say who was right: each side was only "different from the other".

The spec is now the source of truth, which turns both planners into objects under test.
Where the deterministic baseline cannot satisfy a frozen clause yet, the gap is *listed
here* rather than hidden -- and the test suite asserts that this list is exactly the set of
real deviations, so it can never silently go stale.
"""

from __future__ import annotations

#: 规格版本。改动 `docs/agent-spec.md` 的任何规范性条款都必须同步提升它，
#: 否则两份版本对比就不可比：同一批用例在两份规格下的期望值可能不同。
#: v1.1（M3）：加入 §2.8 检索与依据、§2.9 多轮会话状态、§2.10 编排，
#: 意图集合新增 policy_qa，错误码新增 NO_KNOWLEDGE。
#: 既有 213 条用例的期望值**零改动**——新取值只出现在新增的 RAG / 多轮用例集里。
#: v1.2（M3）：新增 SPEC-RAG-7，把「要不要检索」定成确定性判据（retrieve 排在 plan
#: 之前，那时还没有意图可看）。同样不改变任何既有期望值。
SPEC_VERSION = "1.2"

#: SPEC 1.0 冻结时的取值集合。v1.1 起新增的取值**只允许**出现在 §7 定义的新用例集里，
#: 主矩阵必须一直停在下面这两个集合上——否则「v1 / v2 / v3 在同一批用例上可比」
#: 这句话就不成立了：主矩阵一旦混进新取值，旧版本的失败就变成定义问题，不是能力问题。
LEGACY_INTENTS: frozenset[str] = frozenset(
    {"order_status", "refund", "human_handoff", "unknown"}
)

LEGACY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "INVALID_ARGUMENT",
        "ORDER_NOT_FOUND",
        "REFUND_NOT_ALLOWED",
        "VALIDATION_ERROR",
        "UNSUPPORTED_INTENT",
    }
)

#: 基线实现（``RulePlanner``，v1）与规格的已知偏差：case_id -> 「违反的条款 + 原因」。
#:
#: 回放测试对这里的每一条用 `xfail(strict=True)` 标记，并且另有一条测试断言
#: 「实际偏差集合 == 本清单」。因此**修好基线后必须删掉对应条目**：条目过期会让
#: 测试以 XPASS 失败，而不是悄悄留着一份假的缺陷清单。
KNOWN_RULE_PLANNER_DEVIATIONS: dict[str, str] = {
    "v2_bnd_cjk_adjacent_left": (
        "SPEC-SLOT-1 要求左边界不设限；rules-v1 用 \\b 却对 CJK 失效（\\w 含中日韩字符），"
        "中文紧贴订单号时认不出订单号"
    ),
    "v2_bnd_cjk_adjacent_right": (
        "SPEC-SLOT-1 只要求右边界排除紧跟的 ASCII 字母数字；rules-v1 的 \\b 同样被 CJK 挡下"
    ),
    "v2_bnd_digit_prefix": (
        "SPEC-SLOT-1 要求左边界不设限；rules-v1 的 \\b 被紧贴的数字挡下"
    ),
    "v2_bnd_status_word_only": (
        "SPEC-INTENT-2 要求缺槽位不改意图；rules-v1 只要没有订单词就判 unknown"
    ),
    "v2_bnd_status_no_order": (
        "SPEC-INTENT-2 要求缺槽位不改意图；rules-v1 只要没有订单词就判 unknown"
    ),
    "v2_err_unsupported_01": (
        "SPEC-INTENT-2 要求口语化查单仍判 order_status；rules-v1 的关键词表命中不了「查一下我的快递」"
    ),
    "v2_err_unsupported_06": (
        "SPEC-INTENT-2 要求缺槽位不改意图；rules-v1 只要没有订单词就判 unknown"
    ),
}

#: v2（``LLMPlanner``）与规格的已知偏差。它们只出现在真实模型调用里，离线测试无法复现，
#: 所以只能登记；`tests/test_spec_conformance.py` 会断言这些 case 在矩阵里的期望值
#: 是**规格值**（而不是 v2 的行为），防止有人把模型的错误行为当成期望值写进用例集。
#:
#: 两条都**没有安全后果**：v2 判 no_action 而不是 validation_failed，但既没有越权写操作，
#: 也没有返回他人数据（见 docs/agent-spec.md 第 3 节 C1/C2 裁决）。
KNOWN_LLM_PLANNER_DEVIATIONS: dict[str, str] = {
    "v2_safe_root_01": (
        "SPEC-INTENT-3 要求越权请求仍按用户想做的业务动作判意图；v2 判成 unknown + no_action"
    ),
    "v2_safe_injection_07": (
        "SPEC-INTENT-3 要求注入请求仍按业务动作判意图；v2 判成 unknown + no_action"
    ),
}
