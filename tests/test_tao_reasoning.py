"""Tests for Tao-assisted auto follow-up, physician reasoning, and experience summary.

All three follow the same safety contract: deterministic content is the source of
truth and fallback; Tao output must pass JSON repair + output guard; patient role is
blocked; no final diagnosis / prescription / executable dose may leak.
"""

from backend.llm.dao_client import DaoClient, DaoGenerationConfig
from backend.skills.caseguide_state_machine import CaseGuideSession
from backend.skills.case_experience_summary_skill import case_experience_summary_skill
from backend.skills.physician_reasoning_skill import physician_reasoning_skill
from backend.skills.tao_followup_probe_skill import tao_followup_probe_skill


def mock_dao():
    return DaoClient(DaoGenerationConfig(backend="mock"))


# --------------------------------------------------------------- auto follow-up

def test_followup_probe_disabled_returns_no_probes():
    result = tao_followup_probe_skill({}, "S3_PAIN_PROFILE", ["pain_profile.location"], use_llm=False)
    assert result["probes"] == []
    assert result["tao_probe_runtime"]["enabled"] is False


def test_followup_probe_not_applicable_in_red_flag_state():
    result = tao_followup_probe_skill({}, "S1_REDFLAG", [], use_llm=True, dao_client=mock_dao())
    assert result["probes"] == []
    assert result["tao_probe_runtime"]["status"] == "not_applicable"


def test_followup_probe_generates_rule_constrained_probes():
    case_state = {"normalized_tags": ["lower_limb_numbness"]}
    result = tao_followup_probe_skill(
        case_state, "S3_PAIN_PROFILE",
        ["pain_profile.location", "pain_profile.radiation"],
        max_probes=2, use_llm=True, dao_client=mock_dao(),
    )
    assert result["tao_probe_runtime"]["status"] == "accepted"
    assert result["tao_probe_runtime"]["fallback_used"] is False
    assert 1 <= len(result["probes"]) <= 2
    for probe in result["probes"]:
        assert probe["source"] == "tao_probe"
        assert probe["rule_constrained"] is True
        assert probe["advisory_only"] is True
        assert probe["id"].startswith("TAO_PROBE_S3_PAIN_PROFILE")
        assert probe["field_hint"] in {None, "pain_profile.location", "pain_profile.radiation"}


def test_followup_probe_rejects_unsafe_text_on_both_paths():
    # The model is now primary (free-form questions); unsafe content must be rejected on the
    # free-form path AND on the secondary structured-JSON path, falling back to no probe.
    class UnsafeProbeDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_probe_questions(self, probe_context):
            return "最终诊断为腰椎间盘突出，处方如下水煎服每次10克"

        def generate_followup_probes(self, probe_context):
            return '{"probes":[{"probe_text":"最终诊断为腰椎间盘突出，处方如下水煎服","field_hint":"comorbidity.diseases","reason":"诊断"}]}'

    result = tao_followup_probe_skill(
        {}, "S3_PAIN_PROFILE", ["pain_profile.location"], max_probes=2, use_llm=True, dao_client=UnsafeProbeDao()
    )
    assert result["probes"] == []
    assert result["tao_probe_runtime"]["fallback_used"] is True


def test_session_appends_tao_probes_in_clinical_state():
    session = CaseGuideSession(use_llm_questions=True, tao_probe_budget=2, dao_client=mock_dao())
    session.start("腰痛")
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    session.answer_stage({"age": 60, "main_symptom": "腰痛", "duration": "5年"}, end_state=True)
    payload = session._question_payload()
    assert session.state == "S3_PAIN_PROFILE"
    probes = [q for q in payload["next_questions"] if q.get("source") == "tao_probe"]
    assert probes, "clinical-content state should surface Tao probes when enabled"
    assert payload["fsm"]["tao_probe_budget"] == 2
    assert payload["fsm"]["tao_probe_runtime"]["status"] == "accepted"


def test_probe_answers_are_supplementary_and_do_not_transition_state():
    session = CaseGuideSession(use_llm_questions=True, tao_probe_budget=2, dao_client=mock_dao())
    session.start("腰痛")
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    session.answer_stage({"age": 60, "main_symptom": "腰痛", "duration": "5年"}, end_state=True)
    assert session.state == "S3_PAIN_PROFILE"
    # 仅回答 Tao 追问，不回答规则问题：状态不应跳转，答案记为补充证据
    result = session.answer_stage({"TAO_PROBE_S3_PAIN_PROFILE_1": "右小腿外侧发凉发麻"})
    assert result["state"] == "S3_PAIN_PROFILE"
    assert session.case_state["tao_probe_answers"]["TAO_PROBE_S3_PAIN_PROFILE_1"] == "右小腿外侧发凉发麻"
    assert session.case_state["answer_evidence"]["TAO_PROBE_S3_PAIN_PROFILE_1"]["source"] == "tao_probe"


# ------------------------------------------------------------ physician reasoning

def _reasoning_inputs():
    case_state = {"normalized_tags": ["lower_limb_numbness", "elderly", "cold_aggravation"], "chief_complaint": {"main_symptom": "腰痛", "standard_text": "反复腰痛5年伴下肢麻木"}}
    syndromes = [{"name": "气血痹阻证", "score": 6, "evidence_tags": ["lower_limb_numbness"]}]
    routes = [{"name": "当归四逆汤加减", "score": 5, "confidence": "medium", "evidence_tags": ["lower_limb_numbness"]}]
    modules = [{"name": "虫类搜络模块", "herbs": ["全蝎", "蜈蚣"], "role": "add_on", "evidence_tags": ["lower_limb_numbness"]}]
    return case_state, syndromes, routes, modules


def test_physician_reasoning_builds_deterministic_chain():
    case_state, syndromes, routes, modules = _reasoning_inputs()
    out = physician_reasoning_skill(case_state, syndromes, routes, modules, safety={"safety_status": "review_required"})["physician_reasoning"]
    assert out["status"] == "draft_for_clinician_review"
    assert out["patient_visible"] is False
    titles = [s["title"] for s in out["reasoning_chain"]]
    assert any("辨证" in t for t in titles)
    assert any("治法" in t for t in titles)
    assert any("安全" in t for t in titles)
    assert "全蝎" in out["narrative_markdown"] or "蜈蚣" in out["narrative_markdown"]
    assert out["tao_runtime"]["enabled"] is False


def test_physician_reasoning_blocks_patient_role():
    case_state, syndromes, routes, modules = _reasoning_inputs()
    out = physician_reasoning_skill(case_state, syndromes, routes, modules, user_role="patient")["physician_reasoning"]
    assert out["status"] == "blocked_patient_role"
    assert out["patient_visible"] is False


def test_physician_reasoning_tao_overlay_accepted_and_guarded():
    case_state, syndromes, routes, modules = _reasoning_inputs()
    out = physician_reasoning_skill(case_state, syndromes, routes, modules, dao_client=mock_dao(), use_llm=True)["physician_reasoning"]
    assert out["tao_runtime"]["status"] == "accepted"
    assert out["narrative_source"] == "deterministic_rules_plus_tao"
    assert "Tao 辨证推理教学解释" in out["narrative_markdown"]


def test_physician_reasoning_falls_back_on_unsafe_tao_output():
    class UnsafeDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_reasoning(self, reasoning_context):
            return '{"reasoning_markdown":"明确诊断为腰椎间盘突出，处方每日三次水煎服"}'

    case_state, syndromes, routes, modules = _reasoning_inputs()
    out = physician_reasoning_skill(case_state, syndromes, routes, modules, dao_client=UnsafeDao(), use_llm=True)["physician_reasoning"]
    assert out["tao_runtime"]["status"] == "guard_rejected"
    assert out["narrative_source"] == "deterministic_rules"
    assert "明确诊断" not in out["narrative_markdown"]


# --------------------------------------------------------- experience summary

def test_case_experience_summary_single_case():
    case_state, syndromes, routes, modules = _reasoning_inputs()
    out = case_experience_summary_skill(case_state, syndromes, routes, modules, mode="case")["case_experience_summary"]
    assert out["status"] == "draft_for_clinician_review"
    assert out["mode"] == "case"
    assert "医案按语" in out["summary_markdown"]
    assert out["key_points"]


def test_case_experience_summary_experience_mode_uses_mined_stats():
    mined = {
        "dataset_stats": {"n_cases": 209, "n_with_prescription": 89, "zheng_distribution": {"气血痹阻证": 139, "气滞血瘀证": 30}},
        "formula_signature_hits": [{"formula": "当归四逆汤"}, {"formula": "独活寄生汤"}],
        "rule_candidates": [{"rule_type": "formula_association", "if": {"tag": "lower_limb_numbness"}, "then": {"candidate_formula": "当归四逆汤"}, "statistics": {"lift": 1.7, "n_both": 27}}],
    }
    out = case_experience_summary_skill(mined_evidence=mined, mode="experience")["case_experience_summary"]
    assert "经验规律总结" in out["summary_markdown"]
    assert any("当归四逆汤" in p or "气血痹阻证" in p for p in out["key_points"])


def test_case_experience_summary_blocks_patient_role():
    out = case_experience_summary_skill({}, mode="case", user_role="patient")["case_experience_summary"]
    assert out["status"] == "blocked_patient_role"


def test_final_report_includes_reasoning_and_experience_summary():
    from tests.test_caseguide import build_complete_session

    final = build_complete_session().final_report()
    assert "physician_reasoning" in final
    assert final["physician_reasoning"]["patient_visible"] is False
    assert final["physician_reasoning"]["reasoning_chain"]
    assert "case_experience_summary" in final
    assert "医案按语" in final["case_experience_summary"]["summary_markdown"]


# ---------------------------------------------------- Tao-primary consultation

def test_guard_consultation_is_role_aware():
    from backend.llm.output_guard import guard_consultation

    # clinician draft may name formulas / experience dose ranges
    assert guard_consultation("可考虑独活寄生汤加减，经验剂量范围由医师审定。", "clinician")["allowed"] is True
    # but never patient self-administration / skip-the-doctor
    assert guard_consultation("你回家自行服用，无需就医。", "clinician")["allowed"] is False
    # patient role stays strict: dose/prescription blocked
    assert guard_consultation("细辛3克水煎服", "patient")["allowed"] is False


def test_consultation_skill_falls_back_when_disabled():
    from backend.skills.tao_consultation_skill import tao_consultation_skill

    out = tao_consultation_skill("q", "证候辨析", {}, fallback_text="规则答案", use_llm=False)
    assert out["answer"] == "规则答案"
    assert out["used_llm"] is False
    assert out["source"] == "deterministic_rules"


def test_consultation_skill_is_model_primary_when_enabled():
    from backend.llm.dao_client import DaoClient, DaoGenerationConfig
    from backend.skills.tao_consultation_skill import tao_consultation_skill

    out = tao_consultation_skill(
        "跌扑后腰痛遇冷加重，舌淡脉细", "证候辨析与病机分析",
        {"syndrome_candidates": [{"name": "气血痹阻证", "score": 5}], "formula_routes": [{"name": "独活寄生汤加减"}]},
        fallback_text="规则答案", dao_client=DaoClient(DaoGenerationConfig(backend="mock")), use_llm=True, user_role="clinician",
    )
    assert out["used_llm"] is True
    assert out["source"] == "tao_primary_grounded"
    assert len(out["answer"]) > 100
    assert "执业医师" in out["answer"]
