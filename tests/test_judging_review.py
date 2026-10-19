"""人工抽检工具：抽样、表格、kappa 与跨层一致性。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentevalops.judging.review import (
    CaseView,
    agreement_with_labels,
    cohen_kappa,
    cross_layer_agreement,
    label_template,
    load_labels,
    load_layer,
    render_sheet,
    select_review_cases,
)


def view(case_id: str, passed: bool, *, layer: str = "L3", errored: bool = False) -> CaseView:
    return CaseView(
        layer=layer,
        case_id=case_id,
        passed=passed,
        errored=errored,
        scores=(
            {
                "metric": "faithfulness",
                "score": 0.9 if passed else 0.2,
                "threshold": 0.8,
                "passed": passed,
                "reason": "ok" if passed else "not grounded",
            },
        ),
        question=f"问题 {case_id}",
        answer=f"回答 {case_id}",
        contexts=(f"资料 {case_id}",),
    )


def write_report(path: Path, layer: str, cases: list[tuple[str, bool]]) -> None:
    payload = {
        "layer": layer,
        "judge_model": "deepseek-flash",
        "pass_rate": 0.5,
        "scores": [
            {
                "case_id": case_id,
                "metric": "faithfulness" if layer == "L3" else "grounded_answer",
                "score": 0.9 if passed else 0.2,
                "threshold": 0.8,
                "passed": passed,
                "reason": "",
                "error": None,
            }
            for case_id, passed in cases
        ],
        "samples": [
            {"case_id": case_id, "question": f"问题 {case_id}", "answer": f"回答 {case_id}",
             "contexts": [f"资料 {case_id}"]}
            for case_id, _ in cases
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_layer_reads_samples_and_case_level_verdicts(tmp_path):
    path = tmp_path / "report.json"
    write_report(path, "L3", [("a", True), ("b", False)])
    views = {item.case_id: item for item in load_layer(path)}
    assert views["a"].passed is True
    assert views["b"].passed is False
    assert views["b"].answer == "回答 b"
    assert views["b"].contexts == ("资料 b",)


def test_load_layer_accepts_a_repeat_payload(tmp_path):
    path = tmp_path / "repeat.json"
    path.write_text(json.dumps({
        "layer": "L4",
        "repeat": 3,
        "pass_rate": 0.98,
        "scores": [
            {"case_id": "a", "metric": "grounded_answer", "score": 0.9, "threshold": 0.7,
             "passed": True, "reason": "", "error": None},
        ],
        "samples": [{"case_id": "a", "question": "q", "answer": "a", "contexts": ["c"]}],
    }), encoding="utf-8")
    views = load_layer(path)
    assert len(views) == 1
    assert views[0].layer == "L4"
    assert views[0].passed is True
    assert views[0].answer == "a"


def test_selection_puts_failures_first_and_is_reproducible():
    views = [view(f"c{i}", i % 3 != 0) for i in range(12)]
    first = select_review_cases(views, n=6, seed=11)
    second = select_review_cases(list(reversed(views)), n=6, seed=11)
    assert [item.case_id for item in first] == [item.case_id for item in second]
    assert all(not item.passed for item in first[:4])
    assert len(first) == 6


def test_selection_keeps_a_pass_quota_so_false_passes_are_measurable():
    views = [view(f"bad{i}", False) for i in range(13)] + [view("good", True)]
    items = select_review_cases(views, n=5)
    assert len(items) == 5
    assert sum(1 for item in items if not item.passed) == 4
    assert sum(1 for item in items if item.passed) == 1


def test_sheet_contains_the_original_question_answer_and_contexts():
    sheet = render_sheet([view("a", False)], generated_at="2026-10-19T00:00:00+00:00")
    assert "`L3` / `a`" in sheet
    assert "问题 a" in sheet
    assert "回答 a" in sheet
    assert "资料 a" in sheet
    assert "machine" not in sheet.lower()


def test_label_template_round_trips_and_null_means_unfilled(tmp_path):
    items = [view("a", True), view("b", False)]
    path = tmp_path / "labels.jsonl"
    path.write_text(label_template(items), encoding="utf-8")
    assert load_labels(path) == {}
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps({"layer": "L3", "case_id": "a", "verdict": "fail"})
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert load_labels(path) == {("L3", "a"): "fail"}


def test_invalid_verdict_is_rejected(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text('{"layer": "L3", "case_id": "a", "verdict": "maybe"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_labels(path)


def test_cohen_kappa_matches_a_hand_computed_example():
    # 4 条：一致 3 条，po=0.75；两边通过率都是 0.5，pe=0.5，kappa=0.5。
    stats = cohen_kappa([True, True, False, False], [True, False, False, False])
    assert stats["agreement"] == pytest.approx(0.75)
    assert stats["kappa"] == pytest.approx(0.5)


def test_kappa_is_none_when_both_sides_are_constant():
    assert cohen_kappa([True, True], [True, True])["kappa"] is None


def test_agreement_separates_false_pass_from_false_fail():
    items = [view("a", True), view("b", False), view("c", True)]
    labels = {("L3", "a"): "fail", ("L3", "b"): "pass", ("L3", "c"): "pass"}
    stats = agreement_with_labels(items, labels)
    assert stats["labeled"] == 3
    assert stats["false_pass"] == ["a"]
    assert stats["false_fail"] == ["b"]


def test_cross_layer_agreement_lists_disagreements():
    left = [view("a", True), view("b", False), view("c", True)]
    right = [view("a", True, layer="L4"), view("b", True, layer="L4")]
    stats = cross_layer_agreement(left, right)
    assert stats["common_cases"] == 2
    assert stats["disagreements"] == ["b"]
    assert stats["only_left"] == ["c"]


def test_cli_writes_sheet_template_and_agreement(tmp_path):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import review_sheet

    l3 = tmp_path / "l3.json"
    l4 = tmp_path / "l4.json"
    write_report(l3, "L3", [("a", True), ("b", False)])
    write_report(l4, "L4", [("a", True), ("b", True)])
    out = tmp_path / "sheet.md"
    labels = tmp_path / "labels.jsonl"
    agreement = tmp_path / "agreement.json"
    code = review_sheet.main(
        [
            "--l3", str(l3), "--l4", str(l4), "--n", "2",
            "--out", str(out), "--labels", str(labels), "--agreement", str(agreement),
            "--write-template",
        ]
    )
    assert code == 0
    assert "判分抽检表" in out.read_text(encoding="utf-8")
    assert labels.is_file()
    payload = json.loads(agreement.read_text(encoding="utf-8"))
    assert payload["human"]["labeled"] == 0
    assert payload["cross_layer"]["common_cases"] == 2
