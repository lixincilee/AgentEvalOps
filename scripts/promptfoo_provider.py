"""Promptfoo 的 exec provider：一条用例 = 一次真实的 ``V3Agent.handle``。

promptfoo 会把 ``{prompt, vars, ...}`` 作为**最后一个 argv** 传进来。0.121.3 有两个
实测坑：``prompt`` 是对象（``raw`` 还是未渲染的 ``{{message}}``），真正的消息在
``vars.message``；exec provider 也**不会拆** ``{"output": ...}`` 信封。所以这里从 vars
取消息、直接打印原始 JSON 对象作为 output，断言里用 ``JSON.parse(output)`` 取状态。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(json.dumps({"error": "缺少 promptfoo 的 payload 参数"}, ensure_ascii=False))
        return 2
    if os.environ.get("AGENTEVALOPS_PROVIDER_DEBUG"):
        Path(os.environ["AGENTEVALOPS_PROVIDER_DEBUG"]).write_text(
            args[-1] if args else "<no-argv>", encoding="utf-8"
        )
    try:
        payload = json.loads(args[-1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"payload 不是 JSON: {exc}"}, ensure_ascii=False))
        return 2

    var_map = payload.get("vars") if isinstance(payload.get("vars"), dict) else {}
    prompt_field = payload.get("prompt")
    if isinstance(prompt_field, dict):
        # 0.121.3 传的是 {"raw": "{{message}}", "label": ..., "config": ...}，
        # raw 还没渲染。真正的值在 vars 里。
        prompt = prompt_field.get("raw") or prompt_field.get("label") or ""
    else:
        prompt = prompt_field or ""
    prompt = var_map.get("message") or prompt

    from agentevalops.pipeline import V3Agent

    agent = V3Agent()
    response = agent.handle(
        str(prompt),
        customer_id=str(var_map.get("customer_id") or "CUST-001"),
        session_id=var_map.get("session_id"),
    )
    error = response.audit.error or {}
    output = {
        "message": response.message,
        "state": str(response.state),
        "error_code": error.get("code"),
        "intent": str(response.intent),
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
