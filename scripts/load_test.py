"""并发压测入口：起服务、跑 locust、汇总指标、给 CI 一个能用的退出码。

Usage:
    D:\\Python\\python.exe scripts/load_test.py --users 20 --spawn-rate 5 --run-time 20s
    D:\\Python\\python.exe scripts/load_test.py --users 50 --run-time 20s --port 8124
    D:\\Python\\python.exe scripts/load_test.py --arm rules --users 20 --run-time 10s
    D:\\Python\\python.exe scripts/load_test.py --real --yes --users 5 --run-time 30s --budget 0.5
    D:\\Python\\python.exe scripts/load_test.py --arm rules --users 20 --run-time 15s --repeat 5

两种模式只差一件事：模型臂是谁。

    mock（默认）  规则臂，或 LatencyLLMClient（建模 60ms 假延迟 + 固定 token），零成本
    real          真 DeepSeek 客户端（GroundedLLMPlanner + build_client）：会花钱，必须
                  同时给 --yes，且环境里要有 DEEPSEEK_API_KEY

退出码：0 通过；1 门禁没过（错误率 > 1%、成功率 < 99%，或预算熔断）；2 根本起不来。

``--repeat N`` 把同一份配置连跑 N 次（**每次都重启服务**，所以量到的是 CI 门禁真正会面对的
那种方差）。延迟 / RPS / 成本取中位数，``http_error_rate`` 取 max、``task_success_rate`` 取 min——
一次真实故障不该被另外两次的 0% 平均掉。逐次的原始数字留在 ``runs[]``，极差留在 ``variance[]``。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))
# scripts/ 不是包。当脚本直接跑时 sys.path[0] 就是它，但被 importlib 加载时不是，
# 所以显式加一次——否则 `from variance import ...` 只在一种运行方式下成立。
sys.path.insert(0, str(SCRIPTS_DIR))

from agentevalops.config import load_settings  # noqa: E402
from agentevalops.llm import UsageLedger, build_client, build_pricing  # noqa: E402
from agentevalops.order_store import OrderStore  # noqa: E402
from agentevalops.pipeline import V3Agent  # noqa: E402
from agentevalops.planner import GroundedLLMPlanner  # noqa: E402
from agentevalops.service import create_app  # noqa: E402
from agentevalops.session import SessionStore  # noqa: E402
from perf.latency_client import LatencyLLMClient  # noqa: E402
from variance import summarize_runs  # noqa: E402

LOCUSTFILE = REPO_ROOT / "perf" / "locustfile.py"
REPORTS_DIR = REPO_ROOT / "reports"
HOST = "127.0.0.1"
HEALTH_TIMEOUT_S = 30.0
HEALTH_POLL_S = 0.5
BUDGET_POLL_S = 1.0
#: 门禁两条线，语义不许混：``http_error_rate`` 是**传输层**失败（非 200、连接异常、超时），
#: ``task_success_rate`` 是「终态是不是这条消息该有的结果」。阈值写死在脚本里而不是当参数
#: 传——门禁能被现场放宽，就等于没有门禁。
HTTP_ERROR_RATE_LIMIT = 0.01
TASK_SUCCESS_RATE_FLOOR = 0.99
#: 收尾时 locust 会杀掉还在飞的 greenlet：那些请求服务端已经数进去了，locust 却不会记账。
#: 所以两边的请求数对不齐是正常的，只问「差得够不够少」。
#:
#: 判据必须是**相对**的：固定一个绝对值在低 RPS 下合适，一到高 RPS 就变成狼来了——
#: 2217 次请求里差 6 次是 0.27%，成本上完全不可见，却会打出一句「口径存疑」。
INFLIGHT_TOLERANCE = 5
INFLIGHT_TOLERANCE_RATIO = 0.005

#: 汇总进 ``variance`` 的指标（``--repeat`` 时每次运行都记一个）。
REPEAT_METRICS = (
    "requests", "rps", "p50_ms", "p95_ms", "p99_ms", "avg_ms",
    "http_error_rate", "task_success_rate", "tokens_per_request", "cost_per_success_cny",
)
#: 取**中位数**的指标：延迟、吞吐、成本。一次运行的数字本来就是抽样的，中位数抗离群。
CENTRAL_METRICS = (
    "requests", "rps", "p50_ms", "p95_ms", "p99_ms", "avg_ms",
    "tokens_per_request", "cost_per_success_cny",
)
#: 取**最坏值**的指标：问的不是「典型那次怎样」，而是「有没有哪一次出过事」。
#: 一次 5% 的错误率不该被另外两次的 0% 平均掉。
WORST_LOWER = ("http_error_rate",)        # 越大越坏 -> 取 max
WORST_HIGHER = ("task_success_rate",)     # 越小越坏 -> 取 min


class SharedSessionStore(SessionStore):
    """恒为真值的会话容器，专门喂给 ``V3Agent(sessions=...)``。

    ``V3Agent.__init__`` 写的是 ``sessions or SessionStore()``，而 ``SessionStore`` 实现了
    ``__len__``——**空的** store 是假值，会被悄悄换成一个新的。压测要的正是「所有线程共享
    一份会话」，不显式表态的话，每个线程各拿一个空 store，多轮的第二轮就会丢槽位。
    """

    def __bool__(self) -> bool:
        return True


class RunSetupError(RuntimeError):
    """本次压测根本起不来：参数不对、缺凭证，或者没确认就要真实调用。"""


def _git(*arguments: str) -> str | None:
    """跑一条 git 命令取版本；取不到就返回 None，报告里写 null 比猜一个诚实。"""
    try:
        done = subprocess.run(
            ["git", *arguments], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = done.stdout.strip()
    return output if done.returncode == 0 and output else None


def _http_json(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
    """取一个 JSON 端点；连不上或不是 JSON 都算 None（调用方按「还没起来」处理）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _tail(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _parse_marked_json(text: str, marker: str) -> dict[str, Any] | None:
    """从子进程日志里捞一行 ``[marker] {...}``。

    locust 与这个脚本是一对：locustfile 打什么标记，这里就解析什么标记，格式由我们自己定，
    不依赖 locust 内部的输出结构（那些字段名换过版本会变）。
    """
    for line in reversed(text.splitlines()):
        if line.startswith(marker + " "):
            try:
                payload = json.loads(line[len(marker) + 1 :])
            except ValueError:
                return None
            return payload if isinstance(payload, dict) else None
    return None


def _terminate(proc: subprocess.Popen | None) -> None:
    """先 terminate 再 kill，并等它真的退出。

    Windows 上 terminate() 走 TerminateProcess，一般够用；但 uvicorn 已经起过线程池，
    偶尔会拖住，所以多一步 kill——不然下一个用例会对着被占用的端口连不上。
    """
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_for_health(base_url: str, server: subprocess.Popen, log_path: Path) -> None:
    """等 /healthz 通；服务自己先死了就别干等，把它的日志尾巴抛出来。"""
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if _http_json(f"{base_url}/healthz") is not None:
            return
        if server.poll() is not None:
            raise RunSetupError(
                f"服务进程提前退出（exit code {server.returncode}），日志尾巴：\n"
                f"{_tail(log_path)}"
            )
        time.sleep(HEALTH_POLL_S)
    raise RunSetupError(
        f"{HEALTH_TIMEOUT_S:.0f}s 内 /healthz 没通。端口 {base_url} 可能被占用，"
        f"或上一次的 uvicorn 没退干净。服务日志尾巴：\n{_tail(log_path)}"
    )


def _build_app(arm: str, *, budget: float | None):
    """按模型臂建 app 对象。

    模型臂一律走**工厂**：共享实例会被 service 里那把锁串起来，60ms 的假延迟会把整场压测
    压成 16 RPS 的单线程流水线（真模型臂同理，只是延迟更长）。每线程一个 agent、共享一个
    SessionStore，会话状态才既并发又连续。
    """
    if arm == "rules":
        return create_app()
    sessions = SharedSessionStore()
    if arm == "modeled":
        return create_app(
            agent=lambda: V3Agent(
                OrderStore(), planner=GroundedLLMPlanner(LatencyLLMClient()), sessions=sessions
            )
        )
    settings = load_settings(env_file=REPO_ROOT / ".env")
    ledger = UsageLedger(
        limit=budget,
        pricing=build_pricing(settings.model_pricing()),
        currency=settings.currency,
    )
    client = build_client(settings, ledger=ledger)
    return create_app(
        agent=lambda: V3Agent(
            OrderStore(), planner=GroundedLLMPlanner(client), sessions=sessions
        )
    )


def _serve(arm: str, port: int, budget: float | None) -> int:
    """子进程入口：只把服务起起来。日志由父进程重定向到文件，回显在这里没有意义。"""
    import uvicorn  # 只在子进程里需要，父进程（纯客户端）不必背这个 import

    if arm == "rules":
        # 规则臂就是 create_app() 的默认配置，可以按工厂字符串启动，由 uvicorn 自己 import。
        uvicorn.run(
            "agentevalops.service:create_app", factory=True, host=HOST, port=port, log_level="warning"
        )
        return 0
    uvicorn.run(_build_app(arm, budget=budget), host=HOST, port=port, log_level="warning")
    return 0


def _run_locust(args, base_url: str, log_path: Path, json_stem: Path) -> tuple[bool, int]:
    """跑 locust 无头模式，返回 ``(是否被预算熔断打断, 退出码)``。

    ``--json-file`` 传的是**不带扩展名的前缀**：locust 自己会补上 ``.json``
    （``stats.save_stats_json`` 里写的是 f"{filename}.json"），传 full path 反而找不到文件。

    子进程强制 UTF-8：locust 会把 cwd 下的 ``pyproject.toml`` 当默认配置文件读
    （``DEFAULT_CONFIG_FILES`` 里就有它），而它是按系统编码打开的——本仓库那份带中文注释，
    在 GBK 默认的 Windows 上会直接炸在解析前。PYTHONUTF8 让它按 UTF-8 打开。
    """
    command = [
        sys.executable, "-m", "locust", "-f", str(LOCUSTFILE), "--headless",
        "-u", str(args.users), "-r", str(args.spawn_rate), "-t", args.run_time,
        "--host", base_url, "--only-summary", "--json-file", str(json_stem),
    ]
    exceeded = False
    # 输出重定向到文件而不是管道：管道在父进程不读的时候会被写满，双方一起卡死。
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        try:
            while proc.poll() is None:
                time.sleep(BUDGET_POLL_S)
                if args.budget is None:
                    continue
                snapshot = _http_json(f"{base_url}/metrics") or {}
                spent = snapshot.get("cost_cny") or 0.0
                if spent >= args.budget:
                    exceeded = True
                    print(f"预算熔断：已花费 {spent:.6f} 元 >= 上限 {args.budget:.6f} 元，停并发。")
                    break
        finally:
            _terminate(proc)
    return exceeded, proc.returncode


def _merge_response_times(entries: list[dict[str, Any]]) -> dict[int, int]:
    merged: dict[int, int] = {}
    for entry in entries:
        for key, count in (entry.get("response_times") or {}).items():
            merged[int(key)] = merged.get(int(key), 0) + int(count)
    return merged


def _percentile(response_times: dict[int, int], num_timed: int, quantile: float) -> int:
    """从「响应时间 -> 次数」直方图里取分位。算法与 locust 的一致，口径才对得上。

    自己算而不是 import locust.stats：import locust 会顺手 monkey-patch gevent，把父进程
    （要起子进程、要轮询）变成另一个东西，代价远大于这十行。
    """
    if not num_timed or not response_times:
        return 0
    allowed = int(num_timed * quantile)
    processed = 0
    for response_time in sorted(response_times, reverse=True):
        processed += response_times[response_time]
        if num_timed - processed <= allowed:
            return int(response_time)
    return 0


def inflight_tolerance(requests: int) -> int:
    """两边的请求数差多少还算「同一次运行」。取绝对值与 0.5% 里的较大者。"""
    return max(INFLIGHT_TOLERANCE, int(requests * INFLIGHT_TOLERANCE_RATIO))


def _summarize(
    entries: list[dict[str, Any]],
    service_metrics: dict[str, Any],
    service_metrics_final: dict[str, Any],
    tasks: dict[str, Any] | None,
    *,
    mode: str,
    arm: str,
    args,
    base_url: str,
    budget_exceeded: bool,
    started_at: datetime,
    wall_seconds: float,
    notes: list[str],
) -> dict[str, Any]:
    """把 locust 的客户端视角和服务端的 /metrics 合成一份报告。

    两边都得看：locust 知道分位数但不知道 token 和钱，``/metrics`` 知道钱但不知道时延。
    能对上的那一格是请求数——所以这里比对的是**locust 停表那一刻**的服务端快照，
    停表之后才落地的请求会单独列在 ``service_metrics_final`` 里，并在 ``notes`` 里说明。
    """
    requests = sum(entry.get("num_requests", 0) for entry in entries)
    failures = sum(entry.get("num_failures", 0) for entry in entries)
    num_none = sum(entry.get("num_none_requests", 0) for entry in entries)
    total_time = sum(entry.get("total_response_time", 0) for entry in entries)
    timed = requests - num_none
    buckets = _merge_response_times(entries)
    starts = [e["start_time"] for e in entries if e.get("start_time")]
    ends = [e["last_request_timestamp"] for e in entries if e.get("last_request_timestamp")]
    window_s = max(ends) - min(starts) if starts and ends else wall_seconds
    service_requests = service_metrics.get("requests", 0)
    # 比率的分母统一用服务端快照的请求数：分子（token、成本）也来自同一次快照，
    # 这样 tokens/请求 与 成本/请求 是自己内部自洽的，不受收尾那几个请求影响。
    denominator = service_requests or requests
    tokens = service_metrics.get("tokens", 0)
    cost = service_metrics.get("cost_cny", 0.0)
    http_error_rate = failures / requests if requests else 1.0
    task_total = (tasks or {}).get("total", 0)
    task_expected = (tasks or {}).get("expected", 0)
    task_success_rate = None if not task_total else task_expected / task_total
    rounded_task_rate = None if task_success_rate is None else round(task_success_rate, 6)
    return {
        "mode": mode,
        "arm": arm,
        # 建模值还是实测值：modeled 臂的 60ms 与 token 全是估计，报告第一眼就得说清楚。
        "provenance": {
            "latency": "modeled" if arm == "modeled" else "measured",
            "tokens": "modeled" if arm == "modeled" else "measured",
        },
        "users": args.users,
        "spawn_rate": args.spawn_rate,
        "run_time": args.run_time,
        "host": base_url,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "git": {"describe": _git("describe", "--tags"), "commit": _git("rev-parse", "--short", "HEAD")},
        "budget_cny": args.budget,
        "budget_exceeded": budget_exceeded,
        "requests": requests,
        "service_requests": service_requests,
        "rps": round(requests / window_s, 3) if window_s > 0 else 0.0,
        "http_failures": failures,
        "http_error_rate": round(http_error_rate, 6),
        "task_total": task_total,
        "task_expected": task_expected,
        "task_success_rate": rounded_task_rate,
        "tasks": tasks or {},
        "p50_ms": _percentile(buckets, timed, 0.5),
        "p95_ms": _percentile(buckets, timed, 0.95),
        "p99_ms": _percentile(buckets, timed, 0.99),
        "avg_ms": round(total_time / timed, 3) if timed else 0.0,
        "tokens": tokens,
        "tokens_per_request": round(tokens / denominator, 3) if denominator else 0.0,
        "cost_cny": cost,
        "cost_per_request_cny": round(cost / denominator, 9) if denominator else 0.0,
        # 单次成功任务成本：分母是**符合期望的任务数**。拒答也花了钱，但它不是成功任务。
        "cost_per_success_cny": None if not task_expected else round(cost / task_expected, 9),
        "cost_per_tool_completed_cny": service_metrics.get("cost_per_success_cny"),
        "cost_complete": service_metrics.get("cost_complete", False),
        "terminal_states": service_metrics.get("states", {}),
        "error_codes": service_metrics.get("error_codes", {}),
        # 服务端自己的口径：successes 只算 tool_completed。它和服务端的 failures 一样，
        # 是给「服务端怎么记账」用的，不是门禁判据（门禁看上面两条 rate）。
        "service_success_rate": service_metrics.get("success_rate", 0.0),
        # 服务端自报的端点内延迟：与 locust 的 P50 之差 = 排队 + 客户端开销。
        "server_latency_ms_avg": (tasks or {}).get("server_latency_ms_avg"),
        "locust": {
            "requests": requests,
            "http_failures": failures,
            "none_requests": num_none,
            "window_s": round(window_s, 3),
            "requests_delta": service_requests - requests,
            "consistent_with_service": abs(service_requests - requests)
            <= inflight_tolerance(requests),
        },
        "service_metrics": service_metrics,
        "service_metrics_final": service_metrics_final,
        "notes": notes,
        "gate": _gate_payload(http_error_rate, task_success_rate, budget_exceeded),
    }


def _gate_payload(
    http_error_rate: float, task_success_rate: float | None, budget_exceeded: bool
) -> dict[str, Any]:
    """门禁判据。抽成函数是为了 ``--repeat`` 汇总后能**重算**一遍——
    汇总改写了顶层的错误率与成功率（取最坏值），门禁必须跟着变，不能沿用某一次运行的结论。
    """
    http_ok = http_error_rate <= HTTP_ERROR_RATE_LIMIT
    task_ok = task_success_rate is not None and task_success_rate >= TASK_SUCCESS_RATE_FLOOR
    return {
        "passed": http_ok and task_ok and not budget_exceeded,
        "http_error_rate": round(http_error_rate, 6),
        "http_error_rate_limit": HTTP_ERROR_RATE_LIMIT,
        "http_error_rate_ok": http_ok,
        "task_success_rate": None if task_success_rate is None else round(task_success_rate, 6),
        "task_success_rate_floor": TASK_SUCCESS_RATE_FLOOR,
        "task_success_rate_ok": task_ok,
    }


def _num(value: Any) -> Any:
    """整数就按整数显示。``649.0`` 看着像测量精度，其实只是中位数算出来的浮点数。"""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _print_report(summary: dict[str, Any]) -> None:
    """打印一张人看的表。数值口径与 JSON 完全一致，不另算一遍。"""
    task_rate = summary["task_success_rate"]
    per_success = summary["cost_per_success_cny"]
    rows = [
        ("模式 / 模型臂", f"{summary['mode']} / {summary['arm']}"),
        ("指标来源", f"latency={summary['provenance']['latency']} / tokens={summary['provenance']['tokens']}"),
        ("虚拟用户 / 加压", f"{summary['users']} / {summary['spawn_rate']} 用户每秒（{summary['run_time']}）"),
        ("HTTP 请求 locust / 服务端",
         f"{_num(summary['requests'])} / {_num(summary['service_requests'])}"),
        ("RPS", f"{_num(round(summary['rps'], 1))}"),
        ("P50 / P95 / P99 (ms)",
         f"{_num(summary['p50_ms'])} / {_num(summary['p95_ms'])} / {_num(summary['p99_ms'])}"),
        ("平均响应 (ms)", f"{_num(round(summary['avg_ms'], 1))}"),
        (
            "服务端自报延迟均值 (ms)",
            "n/a"
            if summary["server_latency_ms_avg"] is None
            else f"{_num(round(summary['server_latency_ms_avg'], 1))}",
        ),
        ("http_error_rate", f"{summary['http_error_rate'] * 100:.3f}%  "
                     f"({_num(summary['http_failures'])}/{_num(summary['requests'])})"),
        ("task_success_rate", "n/a" if task_rate is None else f"{task_rate * 100:.3f}%  ({summary['task_expected']}/{summary['task_total']})"),
        ("终态分布", json.dumps(summary["terminal_states"], ensure_ascii=False)),
        ("错误码分布", json.dumps(summary["error_codes"], ensure_ascii=False)),
        ("不符合期望的样本", "；".join(summary["tasks"].get("unexpected_samples", [])) or "无"),
        ("tokens 总量 / 每请求",
         f"{_num(summary['tokens'])} / {_num(round(summary['tokens_per_request'], 1))}"),
        ("成本 CNY 总计 / 每请求", f"{summary['cost_cny']:.6f} / {summary['cost_per_request_cny']:.8f}"),
        ("单次成功任务成本 CNY", "n/a（没有符合期望的任务）" if per_success is None else f"{per_success:.8f}"),
        ("成本口径完整", str(summary["cost_complete"])),
        ("预算熔断", "是" if summary["budget_exceeded"] else "否"),
    ]
    if int(summary.get("repeat", 1)) > 1:
        variance = summary.get("variance") or {}
        rows.append(("重复次数", f"{summary['repeat']}（延迟/吞吐取中位数，错误率与成功率取最坏值）"))
        for metric, label in (("p95_ms", "P95 极差"), ("rps", "RPS 极差")):
            entry = variance.get(metric) or {}
            if entry.get("spread") is not None:
                shown = f"{entry['spread']:.2f}"
                if entry.get("spread_pct") is not None:
                    shown += f"（{entry['spread_pct'] * 100:.1f}%）"
                rows.append((label, shown))
    width = max(len(label) for label, _ in rows)
    print("\n" + "=" * (width + 34))
    print(f"压测汇总  {summary['host']}  ({summary['mode']} / {summary['arm']})")
    print("=" * (width + 34))
    for label, value in rows:
        print(f"{label.ljust(width)}  {value}")
    print("=" * (width + 34))


def _report_path(requested: str | None) -> Path:
    """--report 给目录就往里写标准文件名，给 .json 就照写，不给就用 reports/ 下的时间戳名。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not requested:
        return REPORTS_DIR / f"load-test-{stamp}.json"
    path = Path(requested)
    if path.is_dir() or requested.endswith(("/", "\\")):
        return path / f"load-test-{stamp}.json"
    return path


def _run_once(args, mode: str, arm: str, tmp: Path, run_index: int) -> dict[str, Any]:
    """起一次服务 → 等健康 → 跑一次 locust → 汇总 → 停服务，返回这一轮的报告。

    每次重复都**重启服务**，是刻意的：门禁将来面对的就是「某一次运行」的抖动，而进程冷
    启动、端口回收、机器负载漂移都是这个抖动的一部分。服务留着不重启，量出来的方差会
    偏小——那不是门禁要面对的方差。
    """
    port = args.port + run_index
    base_url = f"http://{HOST}:{port}"
    server_log = tmp / f"server-{run_index}.log"
    command = [sys.executable, str(Path(__file__).resolve()), "--serve-only",
               "--port", str(port), "--arm", arm]
    if args.budget is not None:
        command += ["--budget", str(args.budget)]
    with open(server_log, "w", encoding="utf-8") as log:
        server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, cwd=REPO_ROOT)
    try:
        _wait_for_health(base_url, server, server_log)
        print(f"服务就绪: {base_url}（mode={mode} arm={arm}，子进程 pid={server.pid}）")
        started_at, wall_start = datetime.now(timezone.utc), time.monotonic()
        locust_log = tmp / f"locust-stdout-{run_index}.txt"
        budget_exceeded, locust_code = _run_locust(
            args, base_url, locust_log, tmp / f"locust-stats-{run_index}"
        )
        wall_seconds = time.monotonic() - wall_start
        text = locust_log.read_text(encoding="utf-8", errors="replace") if locust_log.is_file() else ""
        tail = "\n".join(text.splitlines()[-25:])
        if tail:
            print("---- locust 输出（尾 25 行）----")
            print(tail)
        stats_file = tmp / f"locust-stats-{run_index}.json"
        if stats_file.is_file():
            entries = json.loads(stats_file.read_text(encoding="utf-8"))
        else:
            # 没有统计文件又没被预算打断，就是 locust 自己没跑起来：这是环境问题（退出码 2），
            # 不能当成「100% 失败」报出去，那会把配置错误说成服务质量问题。
            if not budget_exceeded and locust_code != 0:
                raise RunSetupError(
                    f"locust 没跑起来（exit code {locust_code}）。输出尾巴：\n{tail}"
                )
            print("注意: locust 没写出统计 JSON（预算熔断被打断时就会这样），本次只有服务端指标。")
            entries = []
        # 与 locust 对齐的那份快照取「停表那一刻」的：停表之后还会有在飞的请求落地，
        # 拿停表后的数字去比，就永远差那么几个（上一版报告里差 2 就是这么来的）。
        at_stop = _parse_marked_json(text, "[service-metrics]")
        tasks = _parse_marked_json(text, "[task-metrics]")
        final = _http_json(f"{base_url}/metrics")
        if at_stop is None and final is None:
            raise RunSetupError("跑完读不到 /metrics：服务可能已退出，本次结果不可用。")
        notes: list[str] = []
        if at_stop is None:
            notes.append("locust 没打出 [service-metrics]（多半是被预算熔断打断），改用停表后的快照。")
        if tasks is None:
            notes.append("locust 没打出 [task-metrics]，task_success_rate 记 null，门禁按不通过处理。")
        service_at_stop = at_stop or final or {}
        service_final = final or service_at_stop
        delta = service_final.get("requests", 0) - service_at_stop.get("requests", 0)
        if delta:
            notes.append(
                f"locust 停表之后还有 {delta} 次请求落到 /metrics：与 locust 对齐的计数用停表快照"
                f"（service_metrics），停表后的完整快照存在 service_metrics_final；token 与成本按前者计。"
            )
        return _summarize(
            entries, service_at_stop, service_final, tasks, mode=mode, arm=arm, args=args,
            base_url=base_url, budget_exceeded=budget_exceeded, started_at=started_at,
            wall_seconds=wall_seconds, notes=notes,
        )
    finally:
        _terminate(server)


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """把 N 次运行压成一份报告。两类指标两套口径，混起来会得出反的结论：

    * **中心趋势类**（延迟 / RPS / 成本）取**中位数**——一次运行的延迟本来就是抽样的，
      中位数比均值抗离群。
    * **通过失败类**（http 错误率 / 任务成功率）取**最坏值**——问的不是「典型那次怎样」，
      而是「有没有哪一次出过事」。一次 5% 的错误率不该被另外两次的 0% 平均掉。

    两套口径都写进 ``aggregate_policy``，免得读报告的人猜这个数字是哪一种。
    """
    summary = json.loads(json.dumps(runs[-1]))
    variance = summarize_runs(runs, REPEAT_METRICS)
    for metric in CENTRAL_METRICS:
        median = (variance.get(metric) or {}).get("median")
        if median is not None:
            summary[metric] = median
    for metric in WORST_LOWER:
        values = [run[metric] for run in runs if run.get(metric) is not None]
        if values:
            summary[metric] = max(values)
    for metric in WORST_HIGHER:
        values = [run[metric] for run in runs if run.get(metric) is not None]
        if values:
            summary[metric] = min(values)
    summary["repeat"] = len(runs)
    summary["runs"] = runs
    summary["variance"] = variance
    # 两侧计数都要取中位数。只换一侧会造出「locust 649 / 服务端 686」这种假差异——
    # 那不是对账失败，那是把中位数和某一次的数字摆在了同一行。
    for key in ("service_requests", "http_failures"):
        values = [run[key] for run in runs if run.get(key) is not None]
        if values:
            summary[key] = sorted(values)[len(values) // 2]
    # 对账是**每一次运行**各自的性质，所以逐次留下明细，而不是把 N 次拍成一个布尔值。
    summary["locust"] = {
        "requests": summary["requests"],
        "http_failures": summary["http_failures"],
        "requests_delta": None,
        "consistent_with_service": all(
            run["locust"]["consistent_with_service"] for run in runs
        ),
        "consistency_by_run": [
            {
                "run": index + 1,
                "requests": run["locust"]["requests"],
                "service_requests": run["service_requests"],
                "requests_delta": run["locust"]["requests_delta"],
                "consistent_with_service": run["locust"]["consistent_with_service"],
            }
            for index, run in enumerate(runs)
        ],
    }
    summary["aggregate_policy"] = {
        "central_statistic": "median",
        "central_metrics": list(CENTRAL_METRICS),
        "worst_statistic": {"http_error_rate": "max", "task_success_rate": "min"},
        "worst_metrics": list(WORST_LOWER + WORST_HIGHER),
    }
    # 汇总改写了顶层的错误率与成功率（取最坏值），门禁必须跟着**重算**：
    # 沿用某一次运行的结论，就会出现「报告说 5% 错误率、门禁说通过」这种自相矛盾。
    summary["gate"] = _gate_payload(
        summary["http_error_rate"], summary["task_success_rate"], summary["budget_exceeded"]
    )
    summary["notes"] = list(summary.get("notes") or []) + [
        f"这份报告汇总了 {len(runs)} 次运行：延迟 / RPS / 成本取中位数，错误率与成功率取最坏值。"
        f"每次的原始数字在 runs[]，噪声（极差、相对极差）在 variance[]。"
    ]
    return summary


def _run(args, mode: str, arm: str, report_path: Path, tmp: Path) -> int:
    """跑 ``--repeat`` 次 → 汇总 → 写报告 → 判门禁。"""
    repeat = max(1, int(args.repeat))
    runs: list[dict[str, Any]] = []
    for index in range(repeat):
        summary = _run_once(args, mode, arm, tmp, index)
        runs.append(summary)
        if repeat > 1:
            rate = summary["task_success_rate"]
            shown = "n/a" if rate is None else f"{rate * 100:.3f}%"
            print(
                f"  第 {index + 1}/{repeat} 次: RPS {summary['rps']}"
                f" / P95 {summary['p95_ms']}ms"
                f" / http 错误率 {summary['http_error_rate'] * 100:.3f}%"
                f" / 任务成功率 {shown}"
            )
    summary = runs[0] if repeat == 1 else _aggregate(runs)
    _print_report(summary)
    for note in summary.get("notes") or []:
        print("说明:", note)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("报告已写入:", report_path)
    locust_block = summary["locust"]
    if not locust_block.get("consistent_with_service", True):
        by_run = locust_block.get("consistency_by_run") or []
        if by_run:
            bad = [entry for entry in by_run if not entry["consistent_with_service"]]
            print(
                f"警告: {len(bad)}/{summary['repeat']} 次运行的 locust 请求数与停表时的 /metrics"
                f" 对不上，token / 成本的口径存疑："
            )
            for entry in bad:
                print(f"  第 {entry['run']} 次: 差 {entry['requests_delta']}")
        else:
            print(
                f"警告: locust 的请求数与停表时的 /metrics 对不上"
                f"（差 {locust_block['requests_delta']}），token / 成本的口径存疑。"
            )
    if summary["gate"]["passed"]:
        print("门禁通过（这是给 CI 门禁用的）：http_error_rate ≤ 1% 且 task_success_rate ≥ 99%。")
        return 0
    reasons = []
    if not summary["gate"]["http_error_rate_ok"]:
        reasons.append(f"http_error_rate {summary['http_error_rate'] * 100:.3f}% > 1%")
    if not summary["gate"]["task_success_rate_ok"]:
        rate = summary["task_success_rate"]
        shown = "n/a" if rate is None else f"{rate * 100:.3f}%"
        reasons.append(f"task_success_rate {shown} < 99%")
    if summary["budget_exceeded"]:
        reasons.append("预算熔断，本次数据不完整")
    print("门禁未通过（这是给 CI 门禁用的，退出码非 0）：" + "；".join(reasons))
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="并发压测：起服务 + locust + 汇总 + 门禁")
    parser.add_argument("--users", type=int, default=20, help="虚拟用户数")
    parser.add_argument("--spawn-rate", type=float, default=5.0, help="加压速率（用户/秒）")
    parser.add_argument("--run-time", default="30s", help="时长，locust 语法（30s / 2m）")
    parser.add_argument("--port", type=int, default=8123, help="服务端口，换一个就能避开占用")
    parser.add_argument("--report", default=None, help="汇总 JSON 路径；给目录则写入 load-test-<UTC>.json")
    parser.add_argument("--arm", choices=("modeled", "rules"), default=None, help="mock 模式的模型臂")
    parser.add_argument("--real", action="store_true", help="真实调用 DeepSeek：会花钱，需要 --yes")
    parser.add_argument("--yes", action="store_true", help="确认这次真实调用可以花钱")
    parser.add_argument("--budget", type=float, default=None, help="本次运行的花费上限（元）")
    parser.add_argument("--repeat", type=int, default=1,
                        help="重复运行次数（量方差；每次重启服务）")
    parser.add_argument("--serve-only", action="store_true", help="内部用：只把服务起起来")
    args = parser.parse_args(argv)

    if args.serve_only:
        return _serve(args.arm or "modeled", args.port, args.budget)
    if args.users <= 0 or args.spawn_rate <= 0:
        print("--users 与 --spawn-rate 必须是正数。")
        return 2
    if args.repeat < 1:
        print("--repeat 至少为 1。")
        return 2
    if args.real and args.arm is not None:
        print("--real 与 --arm 互斥：--real 就是真模型臂。")
        return 2
    mode = "real" if args.real else "mock"
    arm = "deepseek" if args.real else (args.arm or "modeled")

    if args.real:
        # 三道闸都在**发请求之前**：确认、凭证、预算提示。
        if not args.yes:
            print("未确认：真实调用需要显式加 --yes。已停在发请求之前。")
            return 2
        if not load_settings(env_file=REPO_ROOT / ".env").llm_configured:
            print(
                "未配置 DEEPSEEK_API_KEY：真实模式起不来。请写入项目根目录 .env，"
                "或去掉 --real 跑零成本的 mock 模式。"
            )
            return 2
        if args.budget is None:
            print("警告: 未设 --budget，本次真实调用的花费不设上限。")

    report_path = _report_path(args.report)
    print(
        f"压测开始: mode={mode} arm={arm} users={args.users} spawn_rate={args.spawn_rate} "
        f"run_time={args.run_time} port={args.port}"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="agentevalops-load-test-") as tmp:
            return _run(args, mode, arm, report_path, Path(tmp))
    except RunSetupError as exc:
        print(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
