"""Tests for the multi-turn conversational skill-router agent.

Covers deterministic keyword routing, guarded LLM intent selection (allowlist +
fallback), question-driven mining, patient-safety blocking, suggested-question
guidance, and multi-turn history.
"""

from backend.agents.conversation import ConversationSession, query_mined
from backend.agents.skill_router import ALLOWED_INTENTS, keyword_route, route_intent, suggested_questions
from backend.llm.dao_client import DaoClient, DaoGenerationConfig


def _case():
    return {
        "normalized_tags": ["lower_limb_numbness", "elderly", "cold_aggravation", "osteoporosis"],
        "chief_complaint": {"main_symptom": "腰痛", "standard_text": "反复腰痛5年伴下肢麻木"},
        "red_flags": {"status": "safe", "positive_items": []},
        "tcm_inquiry": {"tongue": {}},
    }


def mock_dao():
    return DaoClient(DaoGenerationConfig(backend="mock"))


# ----------------------------------------------------------------- routing

def test_keyword_route_picks_expected_intents():
    assert keyword_route("这个病人偏向什么证型？")[0] == "syndrome_inquiry"
    assert keyword_route("可以考虑哪些方剂路线？")[0] == "formula_inquiry"
    assert keyword_route("有什么用药安全风险？")[0] == "safety_inquiry"
    assert keyword_route("数据里哪个证型最多？")[0] == "mining_inquiry"
    assert keyword_route("你能做什么？")[0] == "capabilities"


def test_route_intent_llm_overlay_stays_within_allowlist():
    out = route_intent("讲讲这个案子的辨证思路", use_llm=True, dao_client=mock_dao())
    assert out["intent"] in ALLOWED_INTENTS
    assert out["method"] == "llm"
    assert out["llm_runtime"]["status"] == "accepted"


def test_route_intent_falls_back_when_llm_returns_invalid_intent():
    class BadDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def route_skill(self, routing_context):
            return '{"intent":"make_prescription","reason":"unsafe"}'

    out = route_intent("用什么方？", use_llm=True, dao_client=BadDao())
    assert out["intent"] in ALLOWED_INTENTS  # fell back to keyword hint
    assert out["method"] == "keyword"
    assert out["llm_runtime"]["status"] == "fallback"


def test_patient_request_for_prescription_is_blocked():
    out = route_intent("直接给我开个完整处方和剂量", use_llm=False, user_role="patient")
    assert out["blocked"] is True
    assert out["intent"] == "safety_block"


# ----------------------------------------------------------------- mining

def test_query_mined_answers_syndrome_and_symptom_questions():
    mined = {
        "dataset_stats": {"n_cases": 209, "n_with_prescription": 89, "zheng_distribution": {"气血痹阻证": 139, "气滞血瘀证": 30}},
        "dose_table": {"细辛": {"n": 65, "min_g": 3.0, "max_g": 3.0, "mode_g": 3.0}},
        "rule_candidates": [
            {"rule_id": "MINED-FORMULA_ROUTE-001", "rule_type": "formula_route", "then": {"candidate_formula_route": "当归四逆汤"}, "statistics": {"n_cases": 35, "support": 0.19, "top_zheng": "气血痹阻证"}},
            {"rule_id": "MINED-FA-012", "rule_type": "formula_association", "if": {"tag": "lower_limb_numbness"}, "then": {"candidate_formula": "当归四逆汤"}, "statistics": {"support": 0.15, "confidence": 0.33, "lift": 1.7}},
            {"rule_id": "MINED-ZA-001", "rule_type": "module_association", "if": {"tag": "zheng::气血痹阻证"}, "then": {"candidate_module": "活血通络"}, "statistics": {"lift": 1.5}},
        ],
    }
    syn = query_mined("气血痹阻证最常用什么方？", mined)
    assert syn["query_kind"] == "syndrome" and "139" in syn["answer"]
    sym = query_mined("下肢麻木对应什么方剂？", mined)
    assert sym["query_kind"] == "symptom" and "当归四逆汤" in sym["answer"]
    dose = query_mined("细辛常用多少量？", mined)
    assert dose["query_kind"] == "dose" and "3.0" in dose["answer"]
    overview = query_mined("数据整体情况如何？", mined)
    assert overview["query_kind"] == "overview" and "209" in overview["answer"]


# ------------------------------------------------------------ conversation

def test_conversation_autonomously_invokes_skills_across_turns():
    s = ConversationSession(case_state=_case())
    t1 = s.ask("这个病人偏向什么证型？")
    assert t1["intent"] == "syndrome_inquiry"
    assert "syndrome_router_skill" in t1["skills"]
    t2 = s.ask("那可以考虑哪些方剂路线？")
    assert t2["intent"] == "formula_inquiry"
    assert "formula_base_selector_skill" in t2["skills"]
    t3 = s.ask("有哪些危险信号要排查？")
    assert t3["intent"] == "red_flag_inquiry"
    assert len(s.history) == 3
    for turn in s.history:
        assert turn["suggested_followups"]
        assert "最终诊断" in turn["disclaimer"]


def test_conversation_reasoning_and_experience_use_tao_when_enabled():
    s = ConversationSession(case_state=_case(), use_llm=True, dao_client=mock_dao())
    t = s.ask("从症状到治法的推理过程是怎样的？")
    assert t["intent"] == "reasoning_inquiry"
    assert t["used_llm"] is True
    e = s.ask("总结一下这个医案")
    assert e["intent"] == "experience_inquiry"
    assert e["used_llm"] is True


def test_conversation_patient_role_blocks_prescription_request():
    s = ConversationSession(case_state=_case(), user_role="patient")
    t = s.ask("请直接开方并给我剂量")
    assert t["intent"] == "safety_block"
    assert "不能生成最终诊断" in t["answer"]


def test_suggested_questions_cover_capability_groups():
    groups = suggested_questions()
    names = {g["group"] for g in groups}
    assert {"辨证论治", "安全与风险", "数据挖掘", "经验与系统"} <= names
    for g in groups:
        for item in g["items"]:
            assert item["examples"]
