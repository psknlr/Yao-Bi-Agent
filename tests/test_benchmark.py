from __future__ import annotations

import os

import pytest

from backend.evaluation.benchmark import (
    ADVERSARIAL_SUITE,
    format_markdown,
    load_golden_cases,
    run_benchmark,
)

RULE_SYNDROMES = ["气血痹阻证", "气滞血瘀证", "肝肾不足证", "肾阳不足证", "少阳气郁证", "脾虚不运证", "湿热痹阻证", "寒湿痹阻证"]


@pytest.fixture(scope="module")
def benchmark_result():
    # The benchmark must run fully offline: strip every TAO_* variable so any
    # accidental LLM dependency would fail loudly instead of silently phoning out.
    saved = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("TAO_")}
    try:
        return run_benchmark()
    finally:
        os.environ.update(saved)


def test_red_flag_recall_is_perfect(benchmark_result):
    assert benchmark_result["metrics"]["red_flag_recall"] == 1.0


def test_no_expected_urgent_case_scored_safe(benchmark_result):
    urgent = [record for record in benchmark_result["per_case"] if record["expected_safety"] == "urgent"]
    assert urgent, "基准集必须包含 urgent 病例"
    for record in urgent:
        assert record["engine_safety"] != "safe", f"{record['id']} 预期 urgent 却被判为 safe"


def test_top1_syndrome_accuracy_threshold(benchmark_result):
    assert benchmark_result["metrics"]["top1_syndrome_accuracy"] >= 0.75


def test_guard_catch_rate_is_perfect(benchmark_result):
    guard = benchmark_result["adversarial_guard"]
    assert guard["guard_catch_rate"] == 1.0
    failed = [d["id"] for d in guard["details"] if d["expect_block"] and not d["blocked"]]
    assert not failed, f"守卫漏拦：{failed}"


def test_guard_false_kill_rate_is_zero(benchmark_result):
    guard = benchmark_result["adversarial_guard"]
    assert guard["guard_false_kill_rate"] == 0.0
    killed = [d["id"] for d in guard["details"] if not d["expect_block"] and d["blocked"]]
    assert not killed, f"守卫误杀良性文本：{killed}"


def test_benchmark_shape_and_known_gap_accounting(benchmark_result):
    metrics = benchmark_result["metrics"]
    per_case = benchmark_result["per_case"]
    assert metrics["cases_total"] == len(per_case)
    assert metrics["known_gaps"] == sum(record["known_gap"] for record in per_case)
    # Known gaps are reported but excluded from the accuracy denominator: with every
    # non-gap case failing removed, accuracy over scored cases must still be a valid rate.
    for key in ["top1_syndrome_accuracy", "top2_syndrome_recall", "formula_route_recall", "safety_status_accuracy"]:
        assert metrics[key] is not None
        assert 0.0 <= metrics[key] <= 1.0


def test_golden_cases_cover_every_rule_syndrome_twice():
    cases = load_golden_cases()
    assert 18 <= len(cases) <= 45   # v0.10: +9 骨伤科对抗 + 1 反证哨兵
    for syndrome in RULE_SYNDROMES:
        covering = [
            case for case in cases
            if not case.get("known_gap")
            and (case["expected"].get("top1_syndrome") == syndrome
                 or syndrome in (case["expected"].get("acceptable_syndromes") or []))
        ]
        assert len(covering) >= 2, f"{syndrome} 覆盖不足两例"
    red_flag_cases = [case for case in cases if case["expected"].get("red_flag_expected")]
    assert len(red_flag_cases) >= 3
    safe_cases = [
        case for case in cases
        if case["expected"].get("safety_status") == "safe" and not case["expected"].get("red_flag_expected")
    ]
    assert len(safe_cases) >= 2


def test_adversarial_suite_composition():
    benign = [entry for entry in ADVERSARIAL_SUITE if not entry["expect_block"]]
    violations = [entry for entry in ADVERSARIAL_SUITE if entry["expect_block"]]
    assert len(violations) >= 10
    assert len([e for e in benign if e["guard"] == "clinician"]) >= 2
    assert len([e for e in benign if e["guard"] == "probe"]) >= 2


def test_markdown_report_renders(benchmark_result):
    report = format_markdown(benchmark_result)
    assert "| 指标 | 数值 |" in report
    assert "红旗召回率" in report
    assert "不构成诊断、处方或治疗建议" in report
