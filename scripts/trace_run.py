"""跑一批用例，把每一轮真实发生的事落成 trace（默认离线、零成本；加 --llm --yes 才发模型请求）。

用法::

    D:\\Python\\python.exe scripts/trace_run.py --cases eval_cases/rag_cases.jsonl --limit 12
    D:\\Python\\python.exe scripts/trace_run.py --faults eval_cases/fault_cases.jsonl
    D:\\Python\\python.exe scripts/trace_run.py --multiturn eval_cases/multiturn_cases.jsonl
    D:\\Python\\python.exe scripts/trace_run.py --cases eval_cases/case_matrix_v2.jsonl --langfuse
    D:\\Python\\python.exe scripts/trace_run.py --cases eval_cases/case_matrix_v2.jsonl --limit 10 --llm --yes

产物是**两个**文件，这是刻意的：

* ``<out>``              —— trace：被测系统真实做了什么
* ``<out>.cases.jsonl``  —— 这次实际跑的用例行：期望值

期望值不写进 trace。trace 描述「发生了什么」，期望值是一把尺子；把尺子塞进被测对象的
记录里，读的人就再也分不清哪一段是证据、哪一段是断言。两个文件一起交给
``trace_report.py``，判分与定位都在那边做。

口径说明：

* **每个用例一个干净的 Agent**（含一份干净的 OrderStore）。退款会改订单状态，共用一个
  仓储会让后跑的用例读到前面用例改过的世界，trace 里就分不清「这条用例本来就这样」和
  「被上一条用例改成了这样」。
* 多轮用例的 ``case_id`` 形如 ``mt_ref_01#t0``（第 0 轮），一轮一条 trace。
* 故障用例里的 ``warmup`` 轮次会先在同一个会话里跑一遍（会话状态、订单状态都要被它改过），
  但**不记 trace**：预热是铺垫，不是被测的那一轮。把它也记上，报告里会多出一条没有期望值的
  「用例」，读的人就分不清哪一轮该被判分了。
* ``--langfuse`` 只是**多挂一个出口**：没装 SDK 或没配 key 时降级为 dry-run，把本来要
  发的载荷写到 ``<out>.langfuse-dryrun.jsonl``，本地文件照写不误。
* **默认零成本**：不加 ``--llm`` 就是规则臂（``RulePlannerV3``），一个模型请求都不发。
  加 ``--llm --yes`` 换成 ``GroundedLLMPlanner``，这时 ``llm.plan`` span 里才有真实的
  prompt / response / usage；没有 ``--yes`` 会停在发请求之前并返回 2，``--budget`` 是
  硬熔断（触到就停，退出码 1）。
* **默认覆盖输出文件**（``--append`` 才追加）。追加会把两次运行混进同一个文件，报告于是
  把同一批用例算两遍——而「跑了两遍」和「跑出两条不同结果」在文件里长得一模一样，
  这种账不该由读者去猜。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentevalops.config import Settings, load_env_into_environ, load_settings
from agentevalops.faults import build_faulted_agent
from agentevalops.llm import (
    BudgetExceededError,
    MeteredLLMClient,
    UsageLedger,
    build_client,
    build_pricing,
    resolve_price,
)
from agentevalops.pipeline import V3Agent
from agentevalops.planner import GroundedLLMPlanner
from agentevalops.tracing import (
    JsonlEmitter,
    LangfuseEmitter,
    MultiEmitter,
    Tracer,
)

try:  # 规格版本号只是个便于阅读的标注，取不到就不写，不要因此让脚本起不来
    from agentevalops.spec import SPEC_VERSION
except Exception:  # pragma: no cover
    SPEC_VERSION = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


#: 仓库根：``--llm`` 要从这里读 .env，锚定版本号也要在这里跑 git。
REPO_ROOT = Path(__file__).resolve().parents[1]


def _describe() -> dict[str, Any]:
    """这份 trace 属于哪份代码。拿不到 git 就如实写 nogit，不猜一个好看的值。"""
    root = REPO_ROOT
    describe = "nogit"
    try:
        done = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if done.returncode == 0 and done.stdout.strip():
            describe = done.stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 取决于环境
        pass
    return {"describe": describe, "spec_version": SPEC_VERSION}


def _load(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def _turn(
    agent: V3Agent,
    tracer: Tracer,
    message: str,
    customer_id: str,
    session_id: str | None,
):
    """带 tracer 跑一轮。

    tracer 是**注入**给 agent 的（``agent.tracer``），不是靠上下文变量：脚本读代码时
    不需要猜「现在到底有没有一个 tracer 被激活」。
    """
    agent.tracer = tracer
    return agent.handle(message, customer_id=customer_id, session_id=session_id)


class _SetupError(Exception):
    """用法或环境问题 → 退出码 2（与 trace_report.py 同一约定）。"""


def _build_planner(
    args: argparse.Namespace,
    *,
    settings: Settings | None,
    clients: Mapping[str, Any] | None,
) -> tuple[GroundedLLMPlanner | None, UsageLedger | None]:
    """按 ``--llm`` 造真实模型的规划器；不加 ``--llm`` 就返回 ``(None, None)``。

    会花钱的两条规矩和另外两个脚本一致：**没有 ``--yes`` 就停在发请求之前**（退出码 2），
    缺 key 同样零成本退出。``--budget`` 是硬熔断：触到就停，已经写出去的 trace 留在文件里，
    退出码变成 1——半份数据不是通过。
    """
    if not args.llm:
        return None, None
    if not args.yes:
        raise _SetupError(
            "真实调用会花钱：确认后加 --yes；不加 --llm 则是零成本的规则臂。已停在发请求之前。"
        )
    resolved = settings if settings is not None else load_settings(env_file=REPO_ROOT / ".env")
    if not resolved.llm_configured:
        raise _SetupError(
            "未配置 DEEPSEEK_API_KEY：请写入项目根目录 .env，或去掉 --llm 走零成本的规则臂。"
        )
    budget = args.budget if args.budget is not None else resolved.budget
    pricing = build_pricing(resolved.model_pricing())
    if budget is not None and resolve_price(resolved.model, pricing) is None:
        # 未计价的调用会让 UsageLedger 抛 UnknownModelPriceError，而它属于 LLMError，
        # 会被规划器当成「模型抽风」降级成 llm-fallback——整个 run 静默地不再发请求，
        # 分数还很好看。所以要在发请求之前拦住（与 compare_versions.py 同一道闸）。
        raise _SetupError(
            f"无法启用预算熔断：模型 {resolved.model!r} 没有价目，"
            "未计价的花费会让上限形同虚设。"
        )
    ledger = UsageLedger(limit=budget, pricing=pricing, currency=resolved.currency)
    injected = (clients or {}).get("v3")
    # 注入的客户端也要过同一个账本，否则 --budget 在测试里和真机上是两套行为。
    client = (
        MeteredLLMClient(injected, ledger)
        if injected is not None
        else build_client(resolved, ledger=ledger)
    )
    return GroundedLLMPlanner(client), ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把一批用例跑成 trace（默认离线、零成本、不发模型请求）"
    )
    parser.add_argument("--cases", type=Path, action="append", default=[],
                        help="message 型用例集，可重复")
    parser.add_argument("--faults", type=Path, action="append", default=[],
                        help="故障注入用例集，可重复")
    parser.add_argument("--multiturn", type=Path, action="append", default=[],
                        help="多轮用例集，可重复")
    parser.add_argument("--limit", type=int, default=0, help="最多跑多少条用例（0 = 不限）")
    parser.add_argument("--out", type=Path, default=None,
                        help="trace 输出路径（默认 reports/traces-<UTC>.jsonl）")
    parser.add_argument("--run-id", default=None, help="本次运行的标识，写进每条 trace")
    parser.add_argument("--langfuse", action="store_true",
                        help="同时尝试发往 Langfuse（没配 key 会降级成 dry-run）")
    parser.add_argument("--dump-langfuse", type=Path, default=None,
                        help="dry-run 时把「本来要发什么」落到这里")
    parser.add_argument("--llm", action="store_true",
                        help="换成真实模型规划器（会花钱，必须配 --yes）")
    parser.add_argument("--yes", action="store_true",
                        help="确认真实调用；不加就停在发请求之前并返回 2")
    parser.add_argument("--budget", type=float, default=None,
                        help="花费熔断上限（元）；触到就停，退出码 1")
    parser.add_argument("--append", action="store_true",
                        help="追加到已有 trace 文件（默认覆盖：一次运行就是一条新的测量）")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    clients: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
) -> int:
    """``clients`` / ``settings`` 是给测试留的注入口（同 compare_versions.py 的写法）：
    真实调用不发，但「规划器接线对不对」必须能在进程里被断言。
    """
    args = build_parser().parse_args(argv)
    if args.langfuse:
        # Langfuse SDK 只认进程环境变量；让 .env 里的 LANGFUSE_* 与 DEEPSEEK_API_KEY 同一处配置。
        load_env_into_environ(
            REPO_ROOT / ".env",
            keys=("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"),
        )
    sources = (*args.cases, *args.faults, *args.multiturn)
    if not sources:
        print("至少要给一个用例集：--cases / --faults / --multiturn", file=sys.stderr)
        return 2
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        print(f"用例文件不存在：{', '.join(missing)}", file=sys.stderr)
        return 2
    try:
        planner, ledger = _build_planner(args, settings=settings, clients=clients)
    except _SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = args.run_id or f"trace-{_utc_stamp()}"
    out = args.out or Path("reports") / f"traces-{_utc_stamp()}.jsonl"
    version = _describe()

    if not args.append:
        # 截断而不是删除：这个路径是用户明确指定要写的产物，覆盖是可预期的行为。
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"")

    jsonl = JsonlEmitter(out)
    langfuse: LangfuseEmitter | None = None
    if args.langfuse:
        langfuse = LangfuseEmitter(
            dump_path=args.dump_langfuse or out.with_name(f"{out.stem}.langfuse-dryrun.jsonl")
        )
    emitter: Any = MultiEmitter(jsonl, langfuse) if langfuse else jsonl

    work: list[tuple[str, dict[str, Any]]] = []
    for path in args.cases:
        work.extend(("case", record) for record in _load(path))
    for path in args.faults:
        work.extend(("fault", record) for record in _load(path))
    for path in args.multiturn:
        work.extend(("multiturn", record) for record in _load(path))
    if args.limit:
        work = work[: args.limit]

    cases_path = out.with_name(f"{out.stem}.cases.jsonl")
    cases_path.parent.mkdir(parents=True, exist_ok=True)
    def new_agent() -> V3Agent:
        """每个用例一个干净的 Agent（含一份干净的 OrderStore）。"""
        return V3Agent(planner=planner) if planner is not None else V3Agent()

    turns = 0
    failures = 0
    budget_hit: BudgetExceededError | None = None
    with cases_path.open("w", encoding="utf-8", newline="\n") as cases_handle:
        try:
            for kind, record in work:
                case_id = str(record.get("case_id") or f"case-{turns}")
                cases_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                if kind == "multiturn":
                    agent = new_agent()
                    for index, turn in enumerate(record.get("turns", ())):
                        tracer = Tracer(
                            emitter,
                            run_id=run_id,
                            case_id=f"{case_id}#t{index}",
                            version=version,
                        )
                        response = _turn(
                            agent,
                            tracer,
                            turn["message"],
                            record["customer_id"],
                            record.get("session_id"),
                        )
                        turns += 1
                        failures += 1 if response.audit.error else 0
                    continue

                agent = (
                    build_faulted_agent(record["fault"], planner=planner)
                    if kind == "fault"
                    else new_agent()
                )
                session_id = record.get("session_id")
                # 预热轮：同一个 agent、同一个会话，但关掉 tracer（见模块文档）。
                agent.tracer = None
                for warmup in record.get("warmup", ()):
                    agent.handle(
                        warmup["message"],
                        customer_id=warmup.get("customer_id", record["customer_id"]),
                        session_id=session_id,
                    )
                tracer = Tracer(emitter, run_id=run_id, case_id=case_id, version=version)
                response = _turn(
                    agent, tracer, record["message"], record["customer_id"], session_id
                )
                turns += 1
                failures += 1 if response.audit.error else 0
        except BudgetExceededError as exc:
            # 熔断：已经写出去的 trace 留着（半份数据也是证据），但退出码不能是 0。
            budget_hit = exc

    if budget_hit is not None:
        print(f"预算熔断：{budget_hit}", file=sys.stderr)

    if not args.quiet:
        print(f"用例 {len(work)} 条 → trace {turns} 条，其中带错误码的 {failures} 条")
        print(f"trace     : {out}")
        print(f"期望值随行: {cases_path}")
        if ledger is not None:
            print(f"模型      : 调用 {ledger.calls} 次，花费 {ledger.currency} {ledger.cost:.4f}")
        if langfuse is not None:
            print(f"Langfuse  : {langfuse.status}（dry-run 载荷：{langfuse.dump_path}）")
        print(
            "下一步    : D:\\Python\\python.exe scripts/trace_report.py "
            f"--traces {out} --cases {cases_path}"
        )
    if budget_hit is not None:
        return 1
    return 0 if turns else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
