"""故障用例的展示报告：跑一遍 25 条，判分，并把**故障计数**写进 JSON。

Usage:
    python scripts/run_faults.py
    python scripts/run_faults.py --out reports/faults.json

为什么要有这个脚本，而不只是 pytest：

* pytest 的输出是一次性的，而「依赖坏掉时系统会怎样」这件事需要一份**可归档的**报告
  （后面 CI 要按 tag 冻结它）；
* 报告必须记录**故障真的发生了**：`injected_by_mode` 与 `rate_limited_calls` 就是干这个的。
  没有这两个数字，「25 条全通过」既可能说明系统健壮，也可能说明注入根本没生效。

判分口径与主矩阵一致（SPEC-JUDGE-1 的四项），另外叠加 `expected_plan_source` 与
`expected_slot_source`。故障分数**不并入**功能通过率：两者不是一个口径。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentevalops.faults import FaultSpec, build_faulted_agent  # noqa: E402
from agentevalops.spec import SPEC_VERSION  # noqa: E402
from agentevalops.versioning import git_code_version, hash_cases  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = REPO_ROOT / "eval_cases" / "fault_cases.jsonl"
REPORTS_DIR = REPO_ROOT / "reports"

#: SPEC-JUDGE-1 的四项判定字段。
JUDGED = ("intent", "state", "tool", "error")


def load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"用例文件不存在: {path}")
    cases = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        raise SystemExit(f"用例文件是空的: {path}")
    for case in cases:
        if "fault" not in case:
            raise SystemExit(f"{path.name} 里 {case.get('case_id')} 没有 fault 声明")
        FaultSpec.parse(case["fault"])  # 声明写错就当场炸，不许悄悄不注入
    return cases


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    agent = build_faulted_agent(case["fault"])
    session_id = case.get("session_id")
    for turn in case.get("warmup", []):
        agent.handle(
            turn["message"],
            customer_id=turn.get("customer_id", case["customer_id"]),
            session_id=session_id,
        )

    response = agent.handle(
        case["message"], customer_id=case["customer_id"], session_id=session_id
    )
    observed = {
        "intent": str(response.intent),
        "state": str(response.state),
        "tool": response.audit.tool_name,
        "error": (response.audit.error or {}).get("code"),
        "plan_source": agent.last_plan.source,
        "slot_source": agent.last_slot.source,
    }
    expected = {
        "intent": case["expected_intent"],
        "state": case["expected_state"],
        "tool": case["expected_tool"],
        "error": case["expected_error_code"],
    }
    if case["expected_plan_source"] is not None:
        expected["plan_source"] = case["expected_plan_source"]
    if "expected_slot_source" in case:
        expected["slot_source"] = case["expected_slot_source"]

    mismatched = {key: [expected[key], observed[key]] for key in expected if expected[key] != observed[key]}
    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "fault": case["fault"],
        "expected": expected,
        "observed": observed,
        "judged": list(JUDGED),
        "passed": not mismatched,
        "mismatched": mismatched,
        "rate_limited_calls": getattr(getattr(agent, "fault_client", None), "rate_limited", 0),
    }


def build_report(cases: list[dict[str, Any]], rows: list[dict[str, Any]], *, path: Path) -> dict[str, Any]:
    by_category: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_category.setdefault(row["category"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if row["passed"] else 0

    #: 「故障真的发生了」的证据：按 mode 统计注入次数，以及限流被计数的总次数。
    injected = Counter(f"{row['fault']['target']}:{row['fault']['mode']}" for row in rows)
    return {
        "kind": "fault_run",
        "spec_version": SPEC_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_version": git_code_version(),
        "case_set_version": path.name,
        "case_set_hash": hash_cases(cases),
        "cases": len(rows),
        "passed": sum(1 for row in rows if row["passed"]),
        "pass_rate": sum(1 for row in rows if row["passed"]) / len(rows),
        "by_category": by_category,
        "injected_by_mode": dict(sorted(injected.items())),
        "rate_limited_calls": sum(row["rate_limited_calls"] for row in rows),
        "failure_codes": dict(
            sorted(Counter(row["observed"]["error"] for row in rows if row["observed"]["error"]).items())
        ),
        "rows": rows,
    }


def print_report(report: dict[str, Any]) -> None:
    print(
        f"故障用例: {report['cases']} 条（规格 {report['spec_version']}，"
        f"代码 {report['code_version']}）"
    )
    print("| 类别 | 通过 | 总数 |")
    print("| --- | --- | --- |")
    for category, bucket in sorted(report["by_category"].items()):
        print(f"| {category} | {bucket['passed']} | {bucket['total']} |")
    print(f"合计通过率: {report['pass_rate']:.1%}")
    print(f"注入次数（按 target:mode）: {report['injected_by_mode']}")
    print(f"限流被计数的调用次数: {report['rate_limited_calls']}")
    print(f"故障错误码分布: {report['failure_codes']}")
    failed = [row for row in report["rows"] if not row["passed"]]
    for row in failed:
        print(f"  FAIL {row['case_id']}: {row['mismatched']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="故障用例报告（零成本、零网络）")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--out", type=Path, default=None, help="报告路径（默认写到 reports/ 下带时间戳）")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    rows = [run_case(case) for case in cases]
    report = build_report(cases, rows, path=args.cases)

    out = args.out or REPORTS_DIR / (
        f"fault-run-{report['generated_at'].replace(':', '').replace('-', '')}-"
        f"spec{SPEC_VERSION}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    print_report(report)
    print(f"报告已写入: {out}")
    print(f"用例集指纹: {report['case_set_hash']}")
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
