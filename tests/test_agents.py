"""Tests for the multi-agent orchestration layer.

Verifies the autonomous collaboration mechanism: shared-blackboard handoffs, the
red-flag agent's autonomous halt of downstream clinical agents, language-model-in-the-loop
marking on the LLM agents, and that the deterministic safety invariants survive.
"""

from backend.agents.orchestrator import AgentOrchestrator
from backend.llm.dao_client import DaoClient, DaoGenerationConfig
from backend.skills.caseguide_state_machine import CaseGuideSession


def _safe_case():
    return {
        "normalized_tags": ["lower_limb_numbness", "elderly", "cold_aggravation", "osteoporosis"],
        "chief_complaint": {"main_symptom": "腰痛", "standard_text": "反复腰痛5年伴下肢麻木"},
        "neuro_ortho": {"numbness": "经常有", "weakness": "没有", "bowel_bladder": None},
        "comorbidity": {"diseases": ["骨质疏松"]},
        "red_flags": {"status": "safe", "positive_items": []},
        "tcm_inquiry": {"tongue": {}},
    }


def test_orchestrator_runs_full_agent_chain_with_handoffs():
    out = AgentOrchestrator().run(_safe_case(), use_llm=False)
    assert out["halted"] is False
    names = [m["agent"] for m in out["collaboration_trace"]]
    assert names[0] == "CaseStructuringAgent"
    assert "RedFlagAgent" in names
    assert "TcmSyndromeAgent" in names
    assert names[-1] == "PhysicianReviewAgent"
    # every step records role / kind / handoff (collaboration is explicit)
    for msg in out["collaboration_trace"]:
        assert msg["role"] and msg["kind"] in {"rule", "llm", "hybrid"}
        assert "handoff_to" in msg
    # blackboard carries upstream outputs for downstream agents
    assert out["blackboard"]["routed"]["syndrome_candidates"]
    assert out["blackboard"]["safety"]["safety_status"]


def test_red_flag_agent_autonomously_halts_downstream():
    case = _safe_case()
    case["red_flags"] = {"status": "urgent", "positive_items": ["大小便控制异常/会阴区麻木"]}
    out = AgentOrchestrator().run(case, use_llm=False)
    assert out["halted"] is True
    by_name = {m["agent"]: m for m in out["collaboration_trace"]}
    assert by_name["RedFlagAgent"]["status"] == "halt"
    # downstream clinical agents are skipped, emergency agent runs
    assert by_name["TcmSyndromeAgent"]["status"] == "skipped"
    assert by_name["PhysicianReviewAgent"]["status"] == "skipped"
    assert "EmergencyNoticeAgent" in by_name
    assert by_name["EmergencyNoticeAgent"]["status"] == "blocked"


def test_llm_agents_marked_in_loop_when_tao_enabled():
    dao = DaoClient(DaoGenerationConfig(backend="mock"))
    out = AgentOrchestrator().run(_safe_case(), use_llm=True, dao_client=dao)
    assert out["llm_in_loop"] is True
    assert "ReasoningAgent" in out["used_llm_agents"]
    assert "ExperienceAgent" in out["used_llm_agents"]
    by_name = {m["agent"]: m for m in out["collaboration_trace"]}
    assert by_name["ReasoningAgent"]["llm_runtime"]["status"] == "accepted"


def test_llm_not_in_loop_when_disabled():
    out = AgentOrchestrator().run(_safe_case(), use_llm=False)
    assert out["llm_in_loop"] is False
    assert out["used_llm_agents"] == []


def test_ortho_risk_agent_escalates_on_osteoporosis():
    out = AgentOrchestrator().run(_safe_case(), use_llm=False)
    by_name = {m["agent"]: m for m in out["collaboration_trace"]}
    assert by_name["OrthoRiskAgent"]["status"] == "escalate"
    assert "fracture" in out["blackboard"]["ortho_risk"]["elevated"]


def test_describe_exposes_agent_graph():
    roster = AgentOrchestrator().describe()
    names = [a["name"] for a in roster]
    assert "CaseStructuringAgent" in names and "EmergencyNoticeAgent" in names
    assert all("handoff_to" in a for a in roster)


def test_final_report_embeds_agent_collaboration_trace():
    from tests.test_caseguide import build_complete_session

    final = build_complete_session().final_report()
    collab = final["agent_collaboration"]
    assert collab["halted"] is False
    assert collab["agent_count"] >= 10
    assert collab["collaboration_trace"][0]["agent"] == "CaseStructuringAgent"
    # legacy keys still present (orchestrator is the single brain, parity preserved)
    assert "腰痹医案草稿" in final["standard_case_markdown"]
    assert final["clinician_review_package"]["prescription_review"]["complete_prescription_generated"] is False


def test_session_run_agent_collaboration_uses_session_tao_flag():
    session = CaseGuideSession(use_llm_questions=True, dao_client=DaoClient(DaoGenerationConfig(backend="mock")))
    session.case_state.update(_safe_case())
    out = session.run_agent_collaboration()
    assert out["llm_in_loop"] is True
    assert out["agent_roster"]
