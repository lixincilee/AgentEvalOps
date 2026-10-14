"""One-call smoke test: verifies the API key, the endpoint, and cost accounting.

Usage:
    python scripts/smoke_llm.py            # 需要已配置 DEEPSEEK_API_KEY
    python scripts/smoke_llm.py --offline  # 无 Key 也能验证链路（用假客户端）
    python scripts/smoke_llm.py --save reports/smoke.json   # 同时落盘一份可展示的证据
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentevalops.config import load_settings  # noqa: E402
from agentevalops.llm import (  # noqa: E402
    ChatMessage,
    UsageLedger,
    build_client,
    build_pricing,
)
from agentevalops.versioning import build_manifest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_PROMPT = "你是一个简洁的助手。"
PROMPT = "用一句话解释什么是 AI 应用的评测工程。"


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 连通性烟囱测试")
    parser.add_argument("--offline", action="store_true", help="使用离线假客户端")
    parser.add_argument(
        "--save", type=Path, default=None, help="把本次运行的证据写入该 JSON 文件"
    )
    args = parser.parse_args()

    # Resolve .env against the repository root so the script works from any cwd.
    settings = load_settings(env_file=REPO_ROOT / ".env")
    if args.offline:
        print("[offline 模式] 使用 ScriptedLLMClient，不产生任何费用")
        client = build_client(settings, offline=True, scripts=["（离线假答案）"])
    else:
        if not settings.llm_configured:
            print("未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env（见 README）。")
            return 2
        print(f"model={settings.model}  base_url={settings.base_url}")
        ledger = UsageLedger(
            limit=settings.budget,
            pricing=build_pricing(settings.model_pricing()),
            currency=settings.currency,
        )
        client = build_client(settings, ledger=ledger)

    response = client.complete(
        [ChatMessage.system(SYSTEM_PROMPT), ChatMessage.user(PROMPT)],
        max_tokens=64,
    )
    print("回答:", response.text)
    print("请求模型:", response.requested_model, "| 实际返回:", response.model)
    print("用量:", response.usage.to_dict())
    print("延迟: %.1f ms" % response.latency_ms)
    ledger = getattr(client, "ledger", None)
    if ledger is not None:
        print("账本:", ledger.to_dict())
        if not ledger.cost_is_complete:
            print(
                "注意: 以下模型没有价目，成本只能算下限：",
                ", ".join(sorted(ledger.unpriced_models)),
                "（请在 .env 用 AGENTEVALOPS_PRICING_JSON 声明单价）",
            )

    if args.save is not None:
        evidence = {
            "kind": "llm_smoke",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "offline": args.offline,
            "endpoint": settings.base_url,
            "request": {
                "system": SYSTEM_PROMPT,
                "user": PROMPT,
                "max_tokens": 64,
                "temperature": settings.temperature,
            },
            "response": response.to_dict(),
            "ledger": None if ledger is None else ledger.to_dict(),
            "manifest": build_manifest(
                model_id=settings.model,
                prompt_text=SYSTEM_PROMPT + "\n" + PROMPT,
                prompt_version="smoke-v1",
                case_set_version="smoke-v1",
                temperature=settings.temperature,
                repo_dir=REPO_ROOT,
            ).to_dict(),
        }
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("证据已写入:", args.save)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
