"""Tests for the research-grounded methods layer.

Covers: conformal syndrome prediction sets (finite-sample coverage),
EIG-driven active questioning (BED-style), claim-level groundedness labeling,
and semantic self-consistency (semantic-entropy-lite). Method provenance in
docs/research_grounding.md.
"""

from __future__ import annotations

from backend.engine.conformal import (
    _finite_sample_qhat,
    _nonconformity,
    calibrate,
    conformal_prediction_set,
    leave_one_out_coverage,
    load_calibration_cases,
)
from backend.skills.active_questioning import expected_information_gain
from backend.skills.groundedness_skill import check_groundedness
from backend.skills.pipeline import run_case_pipeline
from backend.skills.tao_consultation_skill import _cluster_by_meaning, _conclusion_signature


# -- conformal prediction ----------------------------------------------------------------

def test_nonconformity_zero_when_label_leads():
    cands = [{"name": "肝肾不足证", "score": 7}, {"name": "气血痹阻证", "score": 4}]
    assert _nonconformity(cands, "肝肾不足证") == 0.0
    assert 0 < _nonconformity(cands, "气血痹阻证") < 1
    assert _nonconformity(cands, "湿热痹阻证") == 1.0
    assert _nonconformity([], "肝肾不足证") == 1.0


def test_finite_sample_quantile_is_conservative_for_tiny_n():
    # n too small for the requested alpha → trivial threshold (full set), never sharper.
    assert _finite_sample_qhat([0.0, 0.1], alpha=0.1) == 1.0
    # exact quantile when n is large enough
    scores = [0.0] * 18 + [0.5, 0.9]
    assert _finite_sample_qhat(scores, alpha=0.1) == 0.5


def test_calibration_uses_golden_cases_and_membership_rule():
    cases = load_calibration_cases()
    assert len(cases) >= 10  # all labeled non-gap golden cases
    cal = calibrate()
    assert cal["calibration_n"] == len(cases)
    cands = [{"name": "肝肾不足证", "score": 7}, {"name": "气血痹阻证", "score": 4}]
    result = conformal_prediction_set(cands, calibration=cal)
    assert "肝肾不足证" in result["prediction_set"]
    assert result["target_coverage"] == 0.9
    assert "校准集" in result["coverage_note"]


def test_wide_qhat_widens_the_prediction_set():
    cands = [{"name": "A", "score": 5}, {"name": "B", "score": 3}, {"name": "C", "score": 1}]
    tight = conformal_prediction_set(cands, calibration={"alpha": 0.1, "target_coverage": 0.9, "q_hat": 0.0, "calibration_n": 20, "trivial": False})
    wide = conformal_prediction_set(cands, calibration={"alpha": 0.1, "target_coverage": 0.9, "q_hat": 0.9, "calibration_n": 20, "trivial": False})
    assert tight["set_size"] < wide["set_size"] == 3


def test_loo_coverage_meets_target():
    loo = leave_one_out_coverage()
    assert loo["n"] >= 10
    assert loo["coverage"] >= loo["target_coverage"]


def test_pipeline_report_renders_conformal_set():
    result = run_case_pipeline("患者女，68岁，腰痛反复5年，畏寒，下肢麻木，舌暗苔白腻，既往骨质疏松。")
    conformal = result["uncertainty"]["conformal"]
    assert conformal and conformal["prediction_set"]
    assert "共形候选集" in result["markdown_report"]


# -- EIG active questioning ---------------------------------------------------------------

def _competing_patterns():
    return [
        {"pattern": "肾阳不足证", "prob": 0.5, "score": 5},
        {"pattern": "肝肾不足证", "prob": 0.5, "score": 5},
    ]


def test_eig_ranks_discriminative_slot_first():
    slot_tags = {
        "waist_knee_soreness": {"lumbar_knee_soreness"},   # feeds 肝肾不足 only
        "stool": set(),                                     # feeds nothing
    }
    ranked = expected_information_gain(_competing_patterns(), slot_tags, ["stool", "waist_knee_soreness"])
    assert ranked[0]["slot"] == "waist_knee_soreness"
    assert ranked[0]["eig_bits"] > 0
    assert ranked[-1]["eig_bits"] == 0.0


def test_eig_zero_when_single_candidate():
    ranked = expected_information_gain(
        [{"pattern": "肝肾不足证", "prob": 1.0, "score": 7}],
        {"waist_knee_soreness": {"lumbar_knee_soreness"}},
        ["waist_knee_soreness"],
    )
    assert ranked[0]["eig_bits"] == 0.0


def test_interview_exposes_question_selection(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
    from backend.llm.dao_client import DaoClient

    engine = YaoBiInterviewEngine(dao_client=DaoClient(), use_llm=True)
    case = YaoBiCaseState(session_id="eig-api")
    engine.run_turn(case, "我68岁女，腰痛5年了，最近加重")
    engine.run_turn(case, "痛在腰部，酸痛为主，不放射，腿有点麻")
    out = engine.run_turn(case, "没有大小便问题，腿没无力，怕冷")
    assert "question_selection" in out
    if out["question_selection"]:
        assert {"slot", "eig_bits"} <= set(out["question_selection"][0])


def test_interview_slot_tags_match_rule_vocabulary():
    # Every SLOT_TAGS tag that claims to feed syndrome rules must exist in the
    # 02 rule trigger vocabulary (regression for the dead-tag mapping bug).
    from backend.agents.yaobi_interview import SLOT_TAG_MAP
    from backend.skills.uncertainty_skill import _syndrome_triggers

    rule_tags = set().union(*_syndrome_triggers().values())
    for slot in ("waist_knee_soreness", "cold_heat", "pain_nature"):
        assert SLOT_TAG_MAP[slot] & rule_tags, f"{slot} no longer feeds any syndrome rule"


# -- groundedness -------------------------------------------------------------------------

def test_groundedness_separates_rule_backed_from_model_knowledge():
    text = "证候倾向肝肾不足证，选独活寄生汤加减，药用杜仲、牛膝；亦可参右归丸之意。"
    evidence = {
        "syndrome_candidates": [{"name": "肝肾不足证"}],
        "formula_routes": [{"name": "独活寄生汤加减"}],
        "herb_modules": [{"herbs": ["杜仲", "牛膝"]}],
    }
    result = check_groundedness(text, evidence)
    assert "肝肾不足证" in result["grounded"]["syndrome"]
    assert "右归丸" in result["ungrounded"]["formula"]
    assert 0 < result["grounding_ratio"] < 1
    assert "需医师重点复核" in result["annotation"]
    assert result["blocking"] is False


def test_groundedness_full_when_everything_backed():
    result = check_groundedness("独活寄生汤加减，杜仲。", {
        "formula_routes": [{"name": "独活寄生汤加减"}],
        "herb_modules": [{"herbs": ["杜仲"]}],
    })
    assert result["grounding_ratio"] == 1.0
    assert "全部" in result["annotation"]


def test_groundedness_handles_no_entities():
    result = check_groundedness("请注意休息，避免久坐弯腰。", {})
    assert result["grounding_ratio"] is None
    assert result["checked_entities"] == 0


def test_consultation_returns_groundedness(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    import importlib

    import backend.server as server_module
    server = importlib.reload(server_module)
    turn = server.handle_chat({"question": "这个病人是什么证型、用什么方？", "tags": ["dark_tongue", "chronic_yabi"], "doctor_mode": True})["turn"]
    assert turn.get("groundedness") is not None
    assert turn["groundedness"]["grounding_ratio"] is None or 0 <= turn["groundedness"]["grounding_ratio"] <= 1


# -- semantic self-consistency ------------------------------------------------------------

def test_conclusion_signature_extracts_committed_entities():
    sig = _conclusion_signature("倾向肝肾不足证，选独活寄生汤加减。")
    assert "s:肝肾不足证" in sig
    assert any(item.startswith("f:独活寄生汤") for item in sig)


def test_cluster_by_meaning_groups_agreeing_answers():
    same = _conclusion_signature("肝肾不足证，独活寄生汤加减。")
    different = _conclusion_signature("肾阳不足证，金匮肾气丸。")
    sizes = _cluster_by_meaning([same, same, different])
    assert sorted(sizes) == [1, 2]


def test_self_consistency_off_by_default(monkeypatch):
    monkeypatch.delenv("TAO_SELF_CONSISTENCY", raising=False)
    monkeypatch.setenv("TAO_BACKEND", "mock")
    import importlib

    import backend.server as server_module
    server = importlib.reload(server_module)
    turn = server.handle_chat({"question": "这个病人是什么证型？", "tags": ["dark_tongue", "chronic_yabi"], "doctor_mode": True})["turn"]
    assert turn.get("semantic_consistency") is None


def test_self_consistency_stable_on_deterministic_mock(monkeypatch):
    monkeypatch.setenv("TAO_SELF_CONSISTENCY", "3")
    monkeypatch.setenv("TAO_BACKEND", "mock")
    import importlib

    import backend.server as server_module
    server = importlib.reload(server_module)
    turn = server.handle_chat({"question": "这个病人是什么证型？", "tags": ["dark_tongue", "chronic_yabi"], "doctor_mode": True})["turn"]
    sc = turn["semantic_consistency"]
    assert sc["n_samples"] == 3
    assert sc["verdict"] == "stable"
    assert sc["agreement"] == 1.0


# -- benchmark integration ----------------------------------------------------------------

def test_benchmark_reports_conformal_coverage():
    from backend.evaluation.benchmark import run_benchmark

    result = run_benchmark()
    conformal = result["conformal"]
    assert conformal["coverage"] >= conformal["target_coverage"]
    assert conformal["avg_set_size"] >= 1.0
