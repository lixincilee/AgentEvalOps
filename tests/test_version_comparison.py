"""三方对比器必须说清三件事：谁跑了、谁没跑、谁在哪条上不一样。

全部离线：--offline 只跑规则列；把脚本化客户端注入进去之后，连 --llm 的接线也能零成本验证。
真实调用一个字都不发——一发就是钱。
"""

from __future__ import annotations

import json
import sys

import pytest
from conftest import REPO_ROOT, read_jsonl

# scripts/ 不是可导入的包，但对比器就是普通源码，测试直接把它当模块用。
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import compare_versions as harness  # noqa: E402

from agentevalops.config import Settings  # noqa: E402
from agentevalops.llm import ScriptedLLMClient  # noqa: E402
from agentevalops.spec import SPEC_VERSION  # noqa: E402

MATRIX_PATH = REPO_ROOT / "eval_cases" / "case_matrix_v2.jsonl"
RAG_PATH = REPO_ROOT / "eval_cases" / "rag_cases.jsonl"
MULTITURN_PATH = REPO_ROOT / "eval_cases" / "multiturn_cases.jsonl"

#: 钉死时钟与 run_id，才能断言「同一批用例跑两次拿到同一份报告」。
MOMENT = "2026-01-01T00:00:00+00:00"

#: SPEC-JUDGE-1 的四项判分字段。
JUDGED = ("intent", "state", "tool_name", "error_code")
RULES_COLUMNS = ("v1", "v3-rules")


@pytest.fixture(scope="module")
def matrix_cases() -> list[dict]:
    return harness.load_cases([MATRIX_PATH])


@pytest.fixture(scope="module")
def small_cases(matrix_cases) -> list[dict]:
    """前 12 条都是正常请求：跑得快，判分口径与其他行完全一样。"""
    return matrix_cases[:12]


@pytest.fixture(scope="module")
def offline_report(small_cases) -> dict:
    return harness.run_comparison(
        small_cases, offline=True, moment=MOMENT, case_set_version=MATRIX_PATH.name
    )


def test_the_report_carries_every_column_and_the_anchors(offline_report, small_cases):
    report = offline_report
    assert report["kind"] == "version_comparison"
    assert report["spec_version"] == SPEC_VERSION
    assert report["mode"] == "offline"
    assert tuple(report["columns"]) == harness.COLUMNS
    assert set(report["versions"]) == set(harness.COLUMNS)
    assert report["prompts"] == {"v2": "v2", "v3": "v3"}
    assert report["case_set_hash"] == harness.hash_cases(small_cases)

    manifest = report["manifest"]
    for field in (
        "model_id",
        "prompt_hash",
        "case_set_hash",
        "code_version",
        "prompt_version",
        "retrieval_config_hash",
    ):
        assert manifest[field], field
    # 真跑过的列才配进清单；没跑过的列不许留一个「假装跑过」的名字。
    assert manifest["extra"]["column.v1"] == "rules-v1"
    assert manifest["extra"]["column.v3-rules"] == "rules-v3"
    assert "column.v2" not in manifest["extra"]
    assert "column.v3" not in manifest["extra"]


def test_the_deltas_cover_every_pair(offline_report):
    expected = [
        f"{first}:{second}"
        for index, first in enumerate(harness.COLUMNS)
        for second in harness.COLUMNS[index + 1 :]
    ]
    assert list(offline_report["deltas"]) == expected
    delta = offline_report["deltas"]["v1:v3-rules"]
    assert set(delta) == {
        "left",
        "right",
        "pass_rate",
        "case_pass_rate",
        "cost",
        "total_tokens",
    }
    assert (delta["left"], delta["right"]) == ("v1", "v3-rules")


def test_every_case_gets_a_verdict_from_both_rules_columns(offline_report, small_cases):
    report = offline_report
    assert len(report["rows"]) == len(small_cases)
    assert [row["case_id"] for row in report["rows"]] == [
        case["case_id"] for case in small_cases
    ]
    expected_intents = {case["case_id"]: case["expected_intent"] for case in small_cases}
    for row in report["rows"]:
        assert row["expected"]["intent"] == expected_intents[row["case_id"]]
        for key in RULES_COLUMNS:
            cell = row[key]
            assert cell is not None, (row["case_id"], key)
            assert all(field in cell for field in JUDGED)
            assert isinstance(cell["passed"], bool)
            assert cell["planner"] == ("rules-v1" if key == "v1" else "rules-v3")
    for key in RULES_COLUMNS:
        assert report["versions"][key]["metrics"]["total"] == len(small_cases)


def test_offline_mode_marks_the_model_columns_unavailable_instead_of_dropping_them(
    offline_report,
):
    """不可用要写成 None + 原因。用规则臂冒充模型列，等于伪造一个「模型和基线一样」的结论。"""
    report = offline_report
    for key in ("v2", "v3"):
        assert report["versions"][key] is None
        assert "--llm" in report["unavailable"][key]
        assert all(row[key] is None for row in report["rows"])
    model_pairs = [
        f"{first}:{second}"
        for index, first in enumerate(harness.COLUMNS)
        for second in harness.COLUMNS[index + 1 :]
        if first in ("v2", "v3") or second in ("v2", "v3")
    ]
    assert model_pairs
    assert all(report["deltas"][pair] is None for pair in model_pairs)
    assert report["deltas"]["v1:v3-rules"] is not None


def test_offline_never_claims_a_model_call_or_a_cost(offline_report):
    report = offline_report
    assert all(report["invariants"].values()), report["invariants"]
    for key in RULES_COLUMNS:
        payload = report["versions"][key]
        assert payload["model_calls"] == 0
        assert payload["metrics"]["cost"] == 0.0
        assert payload["metrics"]["total_tokens"] == 0
        assert payload["source_breakdown"] == {"rules": len(report["rows"])}
    assert all(
        row[key]["model_calls"] == 0 for row in report["rows"] for key in RULES_COLUMNS
    )


def test_a_real_run_without_confirmation_refuses_before_building_a_client(
    monkeypatch, small_cases
):
    def explode(*args, **kwargs):  # pragma: no cover - 被调用就是失败
        raise AssertionError("没确认就不该构造模型客户端，更不该发请求")

    monkeypatch.setattr(harness, "build_client", explode)
    with pytest.raises(harness.RunSetupError, match="--yes"):
        harness.run_comparison(
            small_cases,
            offline=False,
            confirm=False,
            settings=Settings(api_key="test-key"),
        )


def test_a_confirmed_real_run_without_credentials_fails_before_spending(small_cases):
    with pytest.raises(harness.RunSetupError, match="DEEPSEEK_API_KEY"):
        harness.run_comparison(
            small_cases, offline=False, confirm=True, settings=Settings()
        )


def test_an_injected_scripted_client_runs_the_model_columns(small_cases):
    """零成本地把 --llm 的接线走通：注入假客户端，真调用一次都不发。"""
    answer = json.dumps({"intent": "order_status", "order_id": "ORD-1001"})
    clients = {"v2": ScriptedLLMClient(default=answer), "v3": ScriptedLLMClient(default=answer)}
    report = harness.run_comparison(
        small_cases,
        offline=False,
        confirm=True,
        settings=Settings(api_key="test-key", model="deepseek-flash"),
        clients=clients,
        moment=MOMENT,
    )
    assert report["mode"] == "real"
    assert report["unavailable"] == {}
    assert all(report["versions"][key] is not None for key in harness.COLUMNS)
    assert report["deltas"]["v1:v2"] is not None
    assert report["versions"]["v2"]["planner"] == "llm-v2"
    assert report["versions"]["v3"]["planner"] == "llm-v3"
    assert report["versions"]["v1"]["metrics"]["cost"] == 0.0
    assert all(
        row["v2"]["plan_source"] in {"llm", "llm-fallback", "rules"} for row in report["rows"]
    )


def test_two_offline_runs_produce_byte_identical_reports(small_cases):
    kwargs = dict(offline=True, moment=MOMENT, case_set_version=MATRIX_PATH.name)
    first = harness.run_comparison(small_cases, **kwargs)
    second = harness.run_comparison(small_cases, **kwargs)
    assert first["run_id"] == second["run_id"]
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_an_empty_case_set_is_a_setup_error():
    with pytest.raises(harness.RunSetupError):
        harness.run_comparison([], offline=True)


def test_an_unrecognised_case_shape_names_the_file_and_the_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"case_id": "x", "category": "normal"}\n', encoding="utf-8")
    with pytest.raises(harness.RunSetupError, match="broken.jsonl:1"):
        harness.load_cases([path])


def test_offline_the_two_rules_columns_agree_on_the_main_matrix(matrix_cases):
    """主矩阵没有 policy_qa、也没有多轮：v3 的编排在单轮既有用例上不该改变任何结论。

    它一旦不成立，要么是编排层引入了行为漂移，要么是主矩阵混进了新意图——两种都得先看清。
    """
    report = harness.run_comparison(matrix_cases, offline=True, moment=MOMENT)
    assert report["summary"]["divergent_rows"] == []
    assert report["deltas"]["v1:v3-rules"]["pass_rate"] == 0.0


def test_multiturn_cases_become_one_row_per_turn_and_only_v3_sees_the_session():
    cases = harness.load_cases([MULTITURN_PATH])
    raw = read_jsonl(MULTITURN_PATH)
    assert len(cases) == sum(len(case["turns"]) for case in raw)
    assert {case["case_id"] for case in cases} == {
        f"{case['case_id']}#t{index}"
        for case in raw
        for index in range(len(case["turns"]))
    }

    report = harness.run_comparison(cases, offline=True, moment=MOMENT)
    assert report["summary"]["rows"] == len(cases)
    assert report["summary"]["cases"] == len(raw)
    assert report["summary"]["turn_rows"] == len(cases)

    for row in report["rows"]:
        # 会话是 v3 独有的观测项：基线两个版本连 slot 都没有。
        assert row["v1"]["slot_source"] is None
        if row["expected"]["slot_source"] is not None:
            assert row["v3-rules"]["slot_ok"] is True, row["case_id"]
    # 实测结论：会话状态让 v3 的规则臂明显赢过无状态基线（用例集若变，这条会提醒我们重看）。
    assert (
        report["versions"]["v3-rules"]["case_level"]["pass_rate"]
        > report["versions"]["v1"]["case_level"]["pass_rate"]
    )
    assert report["versions"]["v3-rules"]["slot_source_failures"] == 0


def test_rag_cases_fail_on_the_baseline_and_carry_evidence_on_v3():
    cases = harness.load_cases([RAG_PATH])
    report = harness.run_comparison(cases, offline=True, moment=MOMENT)
    # 规则基线没有检索链路：policy_qa 全部落空。这正是 v3 存在的理由。
    assert report["versions"]["v1"]["metrics"]["pass_rate"] == 0.0
    assert report["versions"]["v3-rules"]["metrics"]["pass_rate"] == 1.0
    assert report["versions"]["v3-rules"]["retrieval_failures"] == 0
    for row in report["rows"]:
        assert row["v1"]["retrieval"] is None
        cell = row["v3-rules"]
        if cell["retrieval_ok"] is not None:
            assert cell["retrieval"] is not None
            assert cell["retrieval"]["top_k"] == harness.DEFAULT_TOP_K


def test_the_cli_writes_a_timestamped_spec_versioned_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(harness, "REPORTS_DIR", tmp_path)
    exit_code = harness.main(["--offline", "--limit", "3", "--cases", str(MATRIX_PATH)])
    assert exit_code == 0

    written = list(tmp_path.glob("version-comparison-*-spec*.json"))
    assert len(written) == 1
    assert written[0].name.endswith(f"-spec{SPEC_VERSION}.json")
    report = json.loads(written[0].read_text(encoding="utf-8"))
    assert report["spec_version"] == SPEC_VERSION
    assert report["summary"]["rows"] == 3
    assert report["versions"]["v2"] is None

    printed = capsys.readouterr().out
    assert "用例: 3 条" in printed
    assert "报告已写入" in printed


def test_the_cli_refuses_to_mix_offline_and_llm(capsys):
    assert harness.main(["--offline", "--llm", "--yes"]) == 2
    assert "互斥" in capsys.readouterr().out


def test_the_cli_stops_before_paying_without_yes(capsys):
    assert harness.main(["--llm", "--limit", "2", "--cases", str(MATRIX_PATH)]) == 2
    printed = capsys.readouterr().out
    assert "未确认" in printed
    assert "花费上界" in printed

