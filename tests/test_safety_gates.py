"""Regression tests for the safety-governance hardening round (P0 fixes).

Covers the five P0 findings of the 2026-07 code review:

1. clinician consultation guard inherits every clinician-draft prohibition
   (it was looser than ``guard_clinician_draft`` — the loosest surface must not be);
2. clinician role on a publicly bound server requires ``YAOBI_CLINICIAN_TOKEN``
   (no-token doctor mode is a loopback/local-library convenience only);
3. the red-flag emergency gate halts every entry path *before* TCM reasoning
   (pipeline, chat, autonomous agent);
4. free-text red flags are graded by the category-tiered grader, not defaulted
   to "caution" (typed 会阴麻木/尿不出来 must gate exactly like intake data);
5. multi-turn conversation actually accumulates case state (tags + escalate-only
   red-flag status + state version).
"""

from __future__ import annotations

import importlib

import backend.server as server_module
from backend.agents.autonomous_agent import AutonomousQAAgent
from backend.agents.conversation import ConversationSession
from backend.llm.dao_client import DaoClient, DaoGenerationConfig
from backend.llm.output_guard import guard_consultation
from backend.skills.pipeline import run_case_pipeline
from backend.skills.safety_guard_skill import emergency_halt_required
from backend.skills.tao_consultation_skill import tao_consultation_skill


# -- P0-1: consultation guard parity ---------------------------------------------------

def test_clinician_consultation_guard_blocks_final_diagnosis_and_regimen():
    assert guard_consultation("最终诊断：肾阳不足证。", "clinician")["allowed"] is False
    assert guard_consultation("确诊为腰椎间盘突出症。", "researcher")["allowed"] is False
    assert guard_consultation("处方如下：附片10g、细辛3g。", "clinician")["allowed"] is False
    assert guard_consultation("独活9g，水煎服，每日2次。", "clinician")["allowed"] is False
    assert guard_consultation("可嘱患者自行抓药，不必就医。", "clinician")["allowed"] is False


def test_clinician_consultation_guard_keeps_teaching_content_allowed():
    text = (
        "证候倾向肝肾不足，供医师审定；可考虑独活寄生汤加减为底化裁，"
        "细辛经验剂量范围 3-6克（医师审核），附片须先煎属安全要点。"
        "最终诊断与处方须医师面诊后确定。"
    )
    assert guard_consultation(text, "clinician")["allowed"] is True


def test_patient_consultation_guard_stays_strict():
    assert guard_consultation("细辛经验剂量范围 3-6克。", "patient")["allowed"] is False


# -- P0-2: clinician role requires token when publicly bound ---------------------------

def test_public_bind_without_token_denies_doctor_mode(monkeypatch):
    monkeypatch.delenv("YAOBI_CLINICIAN_TOKEN", raising=False)
    monkeypatch.setattr(server_module, "_SERVER_BIND_HOST", "0.0.0.0")
    role, source = server_module._resolve_role({"doctor_mode": True})
    assert (role, source) == ("patient", "public_no_token_denied")
    denied = server_module._clinician_only({"doctor_mode": True})
    assert denied and "YAOBI_CLINICIAN_TOKEN" in denied["message"]


def test_public_bind_with_token_still_grants_clinician(monkeypatch):
    monkeypatch.setenv("YAOBI_CLINICIAN_TOKEN", "s3cret")
    monkeypatch.setattr(server_module, "_SERVER_BIND_HOST", "0.0.0.0")
    assert server_module._resolve_role({"doctor_mode": True, "clinician_token": "s3cret"}) == ("clinician", "token_verified")
    assert server_module._resolve_role({"doctor_mode": True, "clinician_token": "nope"}) == ("patient", "token_mismatch")


def test_loopback_or_library_use_keeps_local_demo_mode(monkeypatch):
    monkeypatch.delenv("YAOBI_CLINICIAN_TOKEN", raising=False)
    monkeypatch.setattr(server_module, "_SERVER_BIND_HOST", "127.0.0.1")
    assert server_module._resolve_role({"doctor_mode": True}) == ("clinician", "local_demo")
    monkeypatch.setattr(server_module, "_SERVER_BIND_HOST", None)  # direct library/tests
    assert server_module._resolve_role({"doctor_mode": True}) == ("clinician", "local_demo")


# -- P0-3: emergency red-flag gate on every entry path ---------------------------------

_CAUDA_TEXT = "患者男，45岁，腰痛伴左下肢放射痛1周，今晨出现会阴麻木，尿不出来，急来就诊。"


def test_pipeline_halts_on_emergency_red_flag_before_tcm_reasoning():
    result = run_case_pipeline(_CAUDA_TEXT)
    assert result["red_flag_gate"]["halted"] is True
    assert result["syndrome_candidates"] == []
    assert result["formula_routes"] == []
    assert result["matched_modules"] == []
    assert result["primary_route"] is None
    assert result["safety"]["safety_status"] == "urgent"
    assert result["uncertainty"]["abstain"] is True


def test_pipeline_contextual_urgent_keeps_clinician_review_analysis():
    # Fragility trauma is urgent (referral) but not an always-emergency category:
    # the retrospective clinician analysis stays available (golden case GC012).
    result = run_case_pipeline(
        "患者女，71岁，三天前跌倒后腰痛加重，既往骨质疏松，平素腰膝酸软，舌淡，脉细。"
    )
    assert result["safety"]["safety_status"] == "urgent"
    assert result["red_flag_gate"]["halted"] is False
    assert result["syndrome_candidates"]


def test_emergency_halt_predicate_requires_emergency_category():
    assert emergency_halt_required({
        "safety_status": "urgent",
        "confirmed_red_flags": [{"category": "cauda_equina_symptoms"}],
    }) is True
    assert emergency_halt_required({
        "safety_status": "urgent",
        "confirmed_red_flags": [{"category": "trauma_fracture_risk"}],
    }) is False
    assert emergency_halt_required({"safety_status": "caution", "confirmed_red_flags": []}) is False


def test_chat_gates_clinical_intents_while_urgent():
    session = ConversationSession(
        case_state={"normalized_tags": ["dark_tongue", "chronic_yabi"],
                    "red_flags": {"status": "urgent", "positive_items": ["会阴麻木"]}},
        user_role="clinician",
    )
    turn = session.ask("这个病人是什么证型，用什么方？")
    assert turn["red_flag_gated"] is True
    assert "红旗危险信号未排除" in turn["answer"]
    assert "证型" not in turn["answer"].split("红旗危险信号未排除")[0]
    # Safety / red-flag intents stay available while gated.
    gated = session.invoke("red_flag_inquiry")
    assert "红旗" in gated["answer"]


def test_autonomous_agent_replaces_plan_with_emergency_screening_when_urgent():
    agent = AutonomousQAAgent(
        case_state={"normalized_tags": [], "red_flags": {"status": "urgent", "positive_items": ["尿潴留"]}},
        user_role="clinician",
    )
    turn = agent.run("是什么证型、用什么方、有什么风险？")
    assert turn["red_flag_gated"] is True
    assert turn["subagents_used"] == ["red_flag_inquiry"]
    assert "中止辨证与方药规划" in turn["answer"]


# -- P0-4: free-text red flags graded, not defaulted to caution -------------------------

def test_enrich_with_question_grades_cauda_equina_as_urgent():
    server = importlib.reload(server_module)
    merged = server._enrich_with_question({"tags": []}, "我最近会阴麻木，尿不出来，怎么办？")
    assert merged["red_flags"]["status"] == "urgent"
    assert merged["red_flags"]["positive_items"]


def test_enrich_with_question_never_downgrades_client_status():
    server = importlib.reload(server_module)
    merged = server._enrich_with_question(
        {"tags": [], "red_flags": {"status": "urgent", "positive_items": ["进行性无力"]}},
        "今天感觉稍好一些。",
    )
    assert merged["red_flags"]["status"] == "urgent"


def test_enrich_with_question_benign_text_does_not_invent_screen_result():
    server = importlib.reload(server_module)
    merged = server._enrich_with_question({"tags": []}, "腰痛遇冷加重，舌暗苔白腻。")
    assert (merged.get("red_flags") or {}).get("status") in (None, "caution")


# -- P1: multi-turn case memory ---------------------------------------------------------

def test_conversation_accumulates_tags_and_escalates_red_flags():
    session = ConversationSession(case_state={"normalized_tags": []}, user_role="clinician")
    session.ask("患者腰痛，遇冷加重，苔白腻。")
    tags_after_first = set(session.case_state["normalized_tags"])
    assert {"cold_aggravation", "white_greasy_coating"} <= tags_after_first
    turn2 = session.ask("补充：舌暗，腰膝酸软。")
    assert {"dark_tongue", "lumbar_knee_soreness"} <= set(session.case_state["normalized_tags"])
    assert tags_after_first <= set(session.case_state["normalized_tags"])
    assert turn2["state_updates"]["state_version"] >= 2
    turn3 = session.ask("他今晨会阴麻木，小便解不出来。")
    assert session.case_state["red_flags"]["status"] == "urgent"
    assert turn3["red_flag_gated"] or turn3["intent"] in {"red_flag_inquiry", "safety_inquiry", "capabilities", "agent_inquiry"}


# -- P1: consultation must abstain without rule basis ------------------------------------

def test_mock_consultation_abstains_without_rule_evidence():
    client = DaoClient(DaoGenerationConfig(backend="mock"))
    text = client.generate_consultation({"question": "帮我分析这个腰痛病人", "scope": "证候辨析", "evidence": {}})
    assert "证据不足" in text
    assert "独活寄生汤" not in text
    assert "气血痹阻" not in text


def test_consultation_skill_downgrades_invented_formula_without_rule_basis():
    class InventingDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_consultation(self, ctx):
            return "主证倾向肝肾不足证，建议独活寄生汤加减为底，供执业医师审核。"

    out = tao_consultation_skill(
        "这个病人怎么治？", "全面会诊", {"syndrome_candidates": [], "formula_routes": []},
        fallback_text="证据不足，建议补充四诊。", dao_client=InventingDao(), use_llm=True,
    )
    assert out["source"] == "deterministic_rules_fallback"
    assert out["tao_runtime"]["status"] == "ungrounded_no_rule_basis"
    assert "独活寄生汤" not in out["answer"]


def test_consultation_skill_allows_formula_named_by_the_question():
    class TeachingDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_consultation(self, ctx):
            return "独活寄生汤加减以祛风湿、补肝肾为要，供执业医师审核。"

    out = tao_consultation_skill(
        "独活寄生汤的方义是什么？", "方剂教学", {"syndrome_candidates": [], "formula_routes": []},
        fallback_text="暂无。", dao_client=TeachingDao(), use_llm=True,
    )
    assert out["source"] == "tao_primary_grounded"
