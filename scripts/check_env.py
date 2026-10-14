"""Diagnose the .env setup without ever printing the secret.

Run this first whenever a call reports "未配置 DEEPSEEK_API_KEY".

    python scripts/check_env.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentevalops.config import load_settings, parse_dotenv  # noqa: E402

KEY_NAME = "DEEPSEEK_API_KEY"


def main() -> int:
    env_file = REPO_ROOT / ".env"
    problems: list[str] = []

    print("=" * 60)
    print("AgentEvalOps 环境诊断（不会打印密钥内容）")
    print("=" * 60)

    if not env_file.is_file():
        print(f"[!] 找不到 {env_file}")
        problems.append(r"先执行: Copy-Item .env.example .env")
        entries: dict[str, str] = {}
    else:
        raw = env_file.read_bytes()
        print(f"[ok] 找到 {env_file}（{len(raw)} 字节）")
        if raw[:3] == b"\xef\xbb\xbf":
            print("[i]  文件带 UTF-8 BOM（读取时会自动处理，无需担心）")
        if b"\r\n" in raw:
            print("[i]  行尾是 CRLF（Windows 风格，可以正常解析）")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            print(f"[!]  无法按 UTF-8 解码: {exc}")
            problems.append("请用 UTF-8 编码重新保存 .env（不要存成 UTF-16）")
            text = ""

        entries = parse_dotenv(text)
        assignments = [
            line
            for line in text.splitlines()
            if "=" in line and line.strip() and not line.strip().startswith("#")
        ]
        print(f"[i]  非注释赋值行数: {len(assignments)}")
        if entries:
            print(f"[i]  解析到的键: {', '.join(sorted(entries))}")

    value = entries.get(KEY_NAME)
    print("-" * 60)
    if value is None:
        print(f"[!] 没有 {KEY_NAME} 这一行")
        problems.append(f"在 .env 中加一行: {KEY_NAME}=sk-你的真实Key")
    elif value == "":
        print(f"[!] {KEY_NAME} 存在但**值为空** —— 这正是当前的问题")
        problems.append(f"把等号右边补上真实 Key: {KEY_NAME}=sk-...")
    else:
        print(
            f"[ok] {KEY_NAME} 已填写：长度 {len(value)}，"
            f"以 sk- 开头: {value.startswith('sk-')}，含空格: {' ' in value}"
        )
        if " " in value:
            problems.append("Key 中混入了空格，请重新粘贴")

    typos = [key for key in entries if key != KEY_NAME and "DEEPSEEK" in key.upper()]
    if typos:
        print(f"[!] 还发现疑似拼错的键: {typos}（正确的名字是 {KEY_NAME}）")

    settings = load_settings(env_file=env_file)
    print("-" * 60)
    print(f"model      : {settings.model}")
    print(f"base_url   : {settings.base_url}")
    print(f"temperature: {settings.temperature}")
    print(f"budget     : {settings.budget} {settings.currency}")
    print(f"可真实调用  : {settings.llm_configured}")
    print("-" * 60)

    if problems:
        print("需要你修复的点：")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("配置没问题，接着跑: python scripts/smoke_llm.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
