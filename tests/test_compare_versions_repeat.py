"""`--repeat` 存在的唯一理由：单次运行的通过率是抽样的。

这些测试钉三件事：

1. ``--repeat 1`` 与老行为**一模一样**（一个多余字段都不加）；
2. ``N>1`` 时中位数落在**门禁真正读的那个字段**上（``metrics.pass_rate``），极差另存；
3. 次数没跑满时报告**不撒谎**：跑了几次、为什么停、哪部分账没记全，全部写在报告里。

全部离线：注入脚本化客户端或替换 ``build_client``，一个真实请求都不发。
"""

from __future__ import annotations

import json
import sys

import pytest
from conftest import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import compare_versions as harness  # noqa: E402

from agentevalops.config import Settings  # noqa: E402
from agentevalops.llm import (  # noqa: E402
    LLMResponse,
    MeteredLLMClient,
    ScriptedLLMClient,
    Usage,
    approx_tokens,
)

MATRIX_PATH = REPO_ROOT / "eval_cases" / "case_matrix_v2.jsonl"
MOMENT = "2026-01-01T00:00:00+00:00"
#: 前 12 条都是正常的 order_status 请求，这条回答能让它们全部判过。
GOOD = json.dumps({"intent": "order_status", "order_id": "ORD-1001"})
#: 合法但不是这 6 条期望的意图：判分四项里 intent 先不中，于是整条不通过。
BAD = json.dumps({"intent": "human_handoff"})
SETTINGS = Settings(api_key="test-key", model="deepseek-flash")


class ToggleClient:
    """前 ``good_runs`` 轮给 ``good``，之后一律给 ``bad``。

    注入的客户端是同一个实例，所以「我是第几次被调用」可以反推出「现在是第几轮」——
    用它制造跨轮次的结论翻转，成本为零。
    """

    def __init__(self, good: str, bad: str, *, calls_per_run: int, good_runs: int = 1) -> None:
        self._good, self._bad = good, bad
        self._calls_per_run = max(1, int(calls_per_run))
        self._good_runs = good_runs
        self.calls = 0

    @property
    def model_id(self) -> str:
        return "toggle-fake"

    @property
    def call_count(self) -> int:
        return self.calls

    def complete(self, messages, *, temperature=None, max_tokens=None):
        run_index = self.calls // self._calls_per_run
        self.calls += 1
        text = self._good if run_index < self._good_runs else self._bad
        return LLMResponse(
            text=text,
            model="toggle-fake",
            requested_model="toggle-fake",
            usage=Usage(
                prompt_tokens=sum(approx_tokens(m.content) for m in messages),
                completion_tokens=approx_tokens(text),
            ),
            latency_ms=0.0,
        )


def _column(pass_rate: float) -> dict:
    """一个够用的列载荷：``_pairwise_deltas`` 只读这四个字段。"""
    return {
        "metrics": {"pass_rate": pass_rate, "cost": 0.0, "total_tokens": 0},
        "case_level": {"pass_rate": pass_rate},
    }


@pytest.fixture(scope="module")
def matrix_cases() -> list[dict]:
    return harness.load_cases([MATRIX_PATH])


@pytest.fixture(scope="module")
def small_cases(matrix_cases) -> list[dict]:
    """6 条正常请求，全部期望 order_status —— 判定集合小到可以逐条断言。"""
    return matrix_cases[:6]


@pytest.fixture(scope="module")
def offline_repeat(small_cases) -> dict:
    return harness.run_repeated_comparison(
        small_cases, repeat=3, offline=True, case_set_version=MATRIX_PATH.name
    )


@pytest.fixture(scope="module")
def scripted_baseline(small_cases) -> dict:
    """先跑一次「每轮全对」的脚本臂，拿到每轮的调用次数，用来对齐轮次边界。"""
    clients = {"v2": ScriptedLLMClient(default=GOOD), "v3": ScriptedLLMClient(default=GOOD)}
    report = harness.run_comparison(
        small_cases,
        offline=False,
        confirm=True,
        settings=SETTINGS,
        clients=clients,
        moment=MOMENT,
    )
    calls_per_run = {key: clients[key].call_count for key in ("v2", "v3")}
    return {"report": report, "calls_per_run": calls_per_run}


# --- 纯函数：汇总口径 --------------------------------------------------------------


def test_column_metric_reads_case_level_from_its_own_slot():
    """行级和用例级是两个字段，混读一次就会把用例级通过率写进行级那一栏。"""
    report = {"versions": {"v1": {"metrics": {"pass_rate": 0.5}, "case_level": {"pass_rate": 0.25}}}}
    assert harness._column_metric(report, "v1", "pass_rate") == 0.5
    assert harness._column_metric(report, "v1", "case_pass_rate") == 0.25


def test_column_metric_treats_an_unavailable_column_as_missing_not_zero():
    """离线时模型列不可用：它是「没测到」，不是 0 分。补 0 会把极差凭空撑大。"""
    assert harness._column_metric({"versions": {"v2": None}}, "v2", "pass_rate") is None
    assert harness._column_metric({"versions": {}}, "v2", "cost") is None


def test_flaky_cases_only_lists_the_ones_that_flipped():
    """全过和全不过都不是墙头草；只有翻过的才算。"""
    per_run = [
        {"v1": {"a": True, "b": True, "c": False}},
        {"v1": {"a": True, "b": False, "c": False}},
        {"v1": {"a": True, "b": True, "c": False}},
    ]
    flaky = harness._flaky_cases(per_run)
    assert flaky == {"v1": [{"case_id": "b", "passed_runs": 2, "total_runs": 3}]}


def test_flaky_cases_needs_more_than_one_run():
    assert harness._flaky_cases([{"v1": {"a": True}}]) == {}


def test_flaky_cases_are_ordered_by_how_often_they_passed():
    per_run = [
        {"v1": {"a": True, "b": True}},
        {"v1": {"a": False, "b": True}},
        {"v1": {"a": False, "b": False}},
    ]
    assert [item["case_id"] for item in harness._flaky_cases(per_run)["v1"]] == ["a", "b"]


def test_representative_run_picks_the_one_closest_to_the_median():
    """顶层明细只能来自某一次真实运行，所以要挑离中位数最近的那一次。"""
    runs = [
        {"columns": {"v1": {"pass_rate": 0.5}}},
        {"columns": {"v1": {"pass_rate": 0.9}}},
        {"columns": {"v1": {"pass_rate": 0.6}}},
    ]
    assert harness._representative_run(runs, {"v1.pass_rate": {"median": 0.6}}) == 2


def test_aggregate_invariants_reject_a_median_outside_the_observed_range():
    versions = {"v1": {"metrics": {}}, "v2": None, "v3": None, "v3-rules": None}
    variance = {"v1.pass_rate": {"median": 0.99, "min": 0.1, "max": 0.2}}
    assert harness._aggregate_invariants(versions, variance) == {
        "median_within_observed_range": False
    }


def test_aggregate_invariants_accept_a_median_inside_the_observed_range():
    versions = {"v1": {"metrics": {}}, "v2": None, "v3": None, "v3-rules": None}
    variance = {"v1.pass_rate": {"median": 0.15, "min": 0.1, "max": 0.2}}
    assert harness._aggregate_invariants(versions, variance)["median_within_observed_range"]


def test_interpretable_deltas_compare_the_delta_against_the_noise():
    """差值 10 个百分点、两边极差最大 5 个百分点 -> 解释得了。"""
    versions = {"v1": _column(0.50), "v2": _column(0.60), "v3": None, "v3-rules": None}
    variance = {
        "v1.pass_rate": {"spread": 0.05},
        "v2.pass_rate": {"spread": 0.04},
        "v1.case_pass_rate": {"spread": 0.05},
        "v2.case_pass_rate": {"spread": 0.04},
    }
    entry = harness._interpretable_deltas(versions, variance)["v1:v2"]
    assert entry["pass_rate_delta"] == pytest.approx(0.10)
    assert entry["pass_rate_noise"] == pytest.approx(0.05)
    assert entry["pass_rate_interpretable"] is True


def test_interpretable_deltas_call_a_small_difference_unexplainable():
    """差值 2 个百分点、噪声 5 个百分点 -> 达不到，就把「解释不了」说出来。"""
    versions = {"v1": _column(0.50), "v2": _column(0.52), "v3": None, "v3-rules": None}
    variance = {"v1.pass_rate": {"spread": 0.05}, "v2.pass_rate": {"spread": 0.01}}
    entry = harness._interpretable_deltas(versions, variance)["v1:v2"]
    assert entry["pass_rate_interpretable"] is False


def test_interpretable_deltas_mark_a_missing_column_as_not_available():
    versions = {"v1": _column(0.50), "v2": None, "v3": None, "v3-rules": _column(0.50)}
    deltas = harness._interpretable_deltas(versions, {})
    assert deltas["v1:v2"] is None
    assert deltas["v1:v3-rules"] is not None


# --- 重复运行：报告的形状 ----------------------------------------------------------


def test_repeat_runs_the_case_set_exactly_n_times(offline_repeat, small_cases):
    assert offline_repeat["repeat"] == 3
    assert offline_repeat["repeat_completed"] == 3
    assert offline_repeat["repeat_aborted"] is None
    assert [run["run"] for run in offline_repeat["runs"]] == [1, 2, 3]
    assert len(offline_repeat["rows"]) == len(small_cases)


def test_compact_runs_keep_the_numbers_but_not_the_rows(offline_repeat):
    """每次运行都要留数字，但整份 rows 只有一份（代表运行），否则报告没人看。"""
    run = offline_repeat["runs"][0]
    assert set(run) == {"run", "run_id", "generated_at", "columns"}
    assert set(run["columns"]) == set(harness.COLUMNS)
    assert run["columns"]["v1"]["pass_rate"] is not None
    assert run["columns"]["v2"]["pass_rate"] is None


def test_repeat_puts_the_median_where_the_gate_reads_it(offline_repeat):
    """冻结器和门禁读的都是 metrics.pass_rate：它必须是中位数，而且单次原值不能被抹掉。"""
    payload = offline_repeat["versions"]["v1"]
    stats = offline_repeat["variance"]["v1.pass_rate"]
    assert payload["metrics"]["pass_rate"] == stats["median"]
    assert payload["metrics"]["pass_rate_min"] == stats["min"]
    assert payload["metrics"]["pass_rate_max"] == stats["max"]
    assert payload["metrics"]["pass_rate_single_run"] == offline_repeat["runs"][
        offline_repeat["representative_run"] - 1
    ]["columns"]["v1"]["pass_rate"]
    assert payload["case_level"]["pass_rate"] == offline_repeat["variance"][
        "v1.case_pass_rate"
    ]["median"]


def test_repeat_records_every_run_in_runs_and_variance(offline_repeat):
    values = offline_repeat["variance"]["v1.pass_rate"]["values"]
    assert len(values) == 3
    assert values == [
        offline_repeat["runs"][index]["columns"]["v1"]["pass_rate"] for index in range(3)
    ]


def test_identical_offline_runs_have_zero_noise_and_no_flaky_cases(offline_repeat):
    """规则列是确定性的：三次跑出来必须一模一样，否则是环境在飘，不是版本在变。"""
    assert offline_repeat["variance"]["v1.pass_rate"]["spread"] == 0.0
    assert offline_repeat["variance"]["v3-rules.pass_rate"]["spread"] == 0.0
    assert offline_repeat["flaky_cases"] == {}
    assert all(offline_repeat["invariants"].values()), offline_repeat["invariants"]


def test_repeat_keeps_the_single_run_report_contract(offline_repeat):
    """汇总不能把老字段弄丢——冻结器和门禁都还按老形状读。"""
    assert offline_repeat["kind"] == "version_comparison"
    assert offline_repeat["columns"] == list(harness.COLUMNS)
    assert set(offline_repeat["deltas"]) == {
        f"{first}:{second}"
        for index, first in enumerate(harness.COLUMNS)
        for second in harness.COLUMNS[index + 1 :]
    }
    assert offline_repeat["manifest"]["manifest_id"]
    assert offline_repeat["summary"]["cases"] == 6
    assert offline_repeat["summary"]["repeat_completed"] == 3


def test_a_single_repeat_is_the_single_run_path(small_cases):
    report = harness.run_repeated_comparison(small_cases, repeat=1, offline=True)
    assert report["repeat"] == 1
    assert report["repeat_completed"] == 1
    assert report["flaky_cases"] == {}
    payload = report["versions"]["v1"]
    assert payload["metrics"]["pass_rate"] == payload["metrics"]["pass_rate_single_run"]


# --- 重复运行：预算口径 ------------------------------------------------------------


@pytest.fixture
def metered_scripted(monkeypatch) -> list[float | None]:
    """让 ``build_client`` 返回**带账本**的脚本客户端（一次真调用都不发）。

    两个细节都不能省，否则预算这条线根本测不到：

    * 直接往 ``clients=`` 里塞客户端会**绕过账本**（成本和调用数都不记），所以只能在
      ``build_client`` 这一层换；
    * 账本按 ``response.model`` 查价，所以假客户端必须**自称一个真实存在的模型**
      （``scripted-fake`` 没有价目，记出来的成本恒为 0，熔断永远不会触发）。
    """
    limits: list[float | None] = []

    def fake(settings, *, offline=False, ledger=None, **kwargs):
        limits.append(None if ledger is None else ledger.limit)
        base = ScriptedLLMClient((GOOD,) * 200, model=settings.model)
        return MeteredLLMClient(base, ledger) if ledger is not None else base

    monkeypatch.setattr(harness, "build_client", fake)
    return limits


def test_all_runs_complete_within_a_generous_budget(small_cases, metered_scripted):
    single = harness.run_repeated_comparison(
        small_cases, repeat=1, offline=False, confirm=True, settings=SETTINGS
    )
    per_run = single["summary"]["cost_total"]
    assert per_run > 0, "脚本客户端也必须记进账本，否则预算熔断形同虚设"

    report = harness.run_repeated_comparison(
        small_cases, repeat=3, offline=False, confirm=True, settings=SETTINGS, budget=per_run * 20
    )
    assert report["repeat_completed"] == 3
    assert report["summary"]["cost_total"] == pytest.approx(per_run * 3, rel=0.01)
    assert report["summary"]["cost_total_complete"] is True


def test_budget_is_a_running_total_not_a_per_run_cap(small_cases, metered_scripted):
    """每次开跑前拿到的是**剩余**额度：第二轮的账本上限必须比第一轮小。"""
    budget = 1.0
    harness.run_repeated_comparison(
        small_cases, repeat=3, offline=False, confirm=True, settings=SETTINGS, budget=budget
    )
    per_run = harness.run_repeated_comparison(
        small_cases, repeat=1, offline=False, confirm=True, settings=SETTINGS
    )["summary"]["cost_total"]
    assert metered_scripted[0] == pytest.approx(budget)
    assert metered_scripted[1] == pytest.approx(budget)
    assert metered_scripted[2] == pytest.approx(budget - per_run, rel=0.01)


def test_the_repeat_stops_when_the_budget_runs_out(small_cases, metered_scripted):
    per_run = harness.run_repeated_comparison(
        small_cases, repeat=1, offline=False, confirm=True, settings=SETTINGS
    )["summary"]["cost_total"]
    report = harness.run_repeated_comparison(
        small_cases,
        repeat=3,
        offline=False,
        confirm=True,
        settings=SETTINGS,
        budget=per_run * 1.5,
    )
    assert report["repeat_completed"] == 1
    assert report["repeat_aborted"]["reason"] == "budget"
    # 被中断那一次的花费没进账：cost_total 是下限，报告必须自己说出来。
    assert report["summary"]["cost_total_complete"] is False
    assert any("下限" in note for note in report["notes"])


def test_a_budget_that_trips_the_first_run_is_a_setup_error(small_cases, metered_scripted):
    with pytest.raises(harness.RunSetupError) as caught:
        harness.run_repeated_comparison(
            small_cases, repeat=3, offline=False, confirm=True, settings=SETTINGS, budget=1e-9
        )
    assert "预算" in str(caught.value)


# --- 重复运行：中途出事了怎么办 ----------------------------------------------------


def test_an_error_in_a_later_run_keeps_the_completed_runs(monkeypatch, small_cases, offline_repeat):
    real = harness.run_comparison
    calls = {"n": 0}

    def flaky(cases, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二次运行的环境坏了")
        return real(cases, **kwargs)

    monkeypatch.setattr(harness, "run_comparison", flaky)
    report = harness.run_repeated_comparison(small_cases, repeat=3, offline=True)
    assert calls["n"] == 2, "第二次炸了就该停，不该继续烧机时"
    assert report["repeat_completed"] == 1
    assert report["repeat_aborted"]["reason"] == "error"
    assert len(report["rows"]) == len(small_cases)


def test_an_error_in_the_first_run_is_not_swallowed(small_cases):
    clients = {"v2": _ExplodingClient(), "v3": _ExplodingClient()}
    with pytest.raises(RuntimeError):
        harness.run_repeated_comparison(
            small_cases, repeat=2, offline=False, confirm=True, settings=SETTINGS, clients=clients
        )


def test_flaky_answers_across_runs_are_reported_as_flaky_cases(small_cases, scripted_baseline):
    """第一轮全对、之后全错：中位数必须是「错」，而那 6 条用例必须被标成墙头草。"""
    calls_per_run = scripted_baseline["calls_per_run"]
    clients = {
        key: ToggleClient(GOOD, BAD, calls_per_run=calls_per_run[key], good_runs=1)
        for key in ("v2", "v3")
    }
    report = harness.run_repeated_comparison(
        small_cases, repeat=3, offline=False, confirm=True, settings=SETTINGS, clients=clients
    )
    assert report["variance"]["v2.pass_rate"]["values"] == [1.0, 0.0, 0.0]
    assert report["versions"]["v2"]["metrics"]["pass_rate"] == 0.0
    flaky = {item["case_id"]: item for item in report["flaky_cases"]["v2"]}
    assert set(flaky) == {case["case_id"] for case in small_cases}
    assert {item["passed_runs"] for item in flaky.values()} == {1}
    assert {item["total_runs"] for item in flaky.values()} == {3}


# --- 命令行 ------------------------------------------------------------------------


def _run_cli(tmp_path, *extra: str):
    out = tmp_path / "repeat.json"
    code = harness.main(
        [
            "--offline",
            "--cases",
            str(MATRIX_PATH),
            "--limit",
            "6",
            "--out",
            str(out),
            *extra,
        ]
    )
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_the_cli_writes_a_repeat_report(tmp_path, capsys):
    code, report = _run_cli(tmp_path, "--repeat", "2")
    assert code == 0
    assert report["repeat"] == 2
    assert report["repeat_completed"] == 2
    assert "重复运行方差" in capsys.readouterr().out


def test_a_single_repeat_is_the_old_behaviour(tmp_path):
    """``--repeat`` 缺省与 ``--repeat 1`` 都必须走原来的那条路，一个字段都不多。"""
    code, report = _run_cli(tmp_path)
    assert code == 0
    assert "repeat" not in report
    assert "pass_rate_single_run" not in report["versions"]["v1"]["metrics"]

    code, report = _run_cli(tmp_path, "--repeat", "1")
    assert code == 0
    assert "repeat" not in report


def test_the_cli_rejects_a_zero_repeat(capsys):
    assert harness.main(["--offline", "--repeat", "0"]) == 2
    assert "--repeat 必须" in capsys.readouterr().out


def test_the_cli_returns_one_when_the_repeat_could_not_finish(monkeypatch, tmp_path, capsys):
    """数据不完整不能长得像成功：退出码必须是 1，而不是 0。"""

    def fake(cases, *, repeat, **kwargs):
        report = harness.run_comparison(cases, offline=True, case_set_version="injected")
        report["repeat"] = repeat
        report["repeat_aborted"] = {
            "after_run": 1,
            "reason": "budget",
            "detail": "测试注入的熔断",
        }
        return report

    monkeypatch.setattr(harness, "run_repeated_comparison", fake)
    out = tmp_path / "short.json"
    code = harness.main(
        [
            "--offline",
            "--cases",
            str(MATRIX_PATH),
            "--limit",
            "6",
            "--repeat",
            "2",
            "--out",
            str(out),
        ]
    )
    assert code == 1
    assert "未通过（数据不完整）" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["repeat_aborted"]["reason"] == "budget"


class _ExplodingClient:
    """一被调用就抛：用来验证「第一次就崩」不会被当成「跑了零次，一切正常」。"""

    @property
    def model_id(self) -> str:
        return "exploding-fake"

    def complete(self, messages, *, temperature=None, max_tokens=None):
        raise RuntimeError("注入的故障：模型客户端炸了")
