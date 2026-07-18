"""Entry-consistency matrix (v0.11) — the central P0 of the entry review.

Same patient, same narrative, different API ⇒ SAME safety/scope handling. Before this
round a knee complaint got 当归四逆汤 from chat, the autonomous agent AND the
multi-agent collaboration while the pipeline correctly refused it. Every high-risk /
out-of-scope / temporal case here is asserted across all entries.
"""

from __future__ import annotations

import importlib

from backend.agents.autonomous_agent import AutonomousQAAgent
from backend.agents.conversation import ConversationSession
from backend.agents.orchestrator import AgentOrchestrator
from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
from backend.skills.pipeline import run_case_pipeline

_FORMULA_NAMES = ("独活寄生汤", "当归四逆汤", "桂枝芍药知母汤", "黄芪桂枝五物汤", "柴胡类方", "补肾类方", "四妙丸")


def _no_formula(text: str) -> bool:
    return not any(name in text for name in _FORMULA_NAMES)


# -- knee complaint: out of scope at EVERY entry -------------------------------------------

_KNEE_QUESTION = "患者右膝关节肿痛，伴下肢麻木、怕冷、脉缓，可以考虑哪些方剂路线？"


def test_knee_complaint_pipeline_out_of_scope():
    result = run_case_pipeline("患者男，50岁，右膝关节肿痛三天，伴下肢麻木、怕冷、脉缓。")
    assert result["scope"]["in_scope"] is False
    assert result["primary_route"] is None
    assert result["clinical_mode"] == "out_of_scope_triage"


def test_knee_complaint_chat_refuses_formula():
    session = ConversationSession(case_state={"normalized_tags": []}, user_role="clinician")
    turn = session.ask(_KNEE_QUESTION)
    assert turn.get("red_flag_gated") or "不属于本系统获准处理" in turn["answer"]
    assert _no_formula(turn["answer"])


def test_knee_complaint_autonomous_refuses_plan():
    agent = AutonomousQAAgent(case_state={"normalized_tags": []}, user_role="clinician")
    turn = agent.run(_KNEE_QUESTION)
    assert turn.get("scope_gated") or turn.get("red_flag_gated")
    assert _no_formula(turn["answer"])
    assert turn["run"]["stop_reason"] in {"policy_denied", "safety_halt"}


def test_knee_complaint_collaboration_blocks_formula_capability():
    # The server computes the scope decision from the narrative and it travels with the
    # case_state; the ScopeGateAgent halts and the formula capability is never granted.
    result = AgentOrchestrator().run({
        "normalized_tags": ["lower_limb_numbness", "cold_aversion", "slow_pulse"],
        "red_flags": {"status": "safe"},
        "scope": {"in_scope": False, "out_of_scope_reason": "膝关节主诉", "reason_codes": ["NON_LUMBAR_REGION"]},
    })
    assert result["halted"] is True
    assert result["blackboard"].get("formula") is None
    assert any(step["agent"] == "ScopeGateAgent" and step["status"] == "halt" for step in result["collaboration_trace"])


def test_knee_complaint_via_server_chat_and_autonomous(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "disabled")
    import backend.server as server_module

    server = importlib.reload(server_module)
    chat = server.handle_chat({"question": _KNEE_QUESTION, "tags": [], "doctor_mode": True})["turn"]
    assert _no_formula(chat["answer"])
    auto = server.handle_autonomous({"question": _KNEE_QUESTION, "tags": [], "doctor_mode": True})["turn"]
    assert _no_formula(auto["answer"])


def test_capability_token_blocks_formula_agent_even_without_halt():
    # Defense in depth: even if a future orchestrator forgets the scope halt, the
    # formula agent itself refuses without the capability token.
    from backend.agents.base import Blackboard
    from backend.agents.clinical_agents import FormulaReasoningAgent

    bb = Blackboard(case_state={"normalized_tags": ["chronic_yabi"]})
    bb.capabilities = {"syndrome_reasoning"}  # formula_draft NOT granted
    result = FormulaReasoningAgent().run(bb)
    assert result.status == "blocked"
    assert bb.get("formula") is None


# -- emergency case: hard stop at EVERY entry -----------------------------------------------

_OPEN_FRACTURE = "摔伤后小腿明显畸形，伤口见骨，足背动脉摸不到。"


def test_open_fracture_consistent_hard_stop_across_entries(monkeypatch):
    # Pipeline
    result = run_case_pipeline("患者男，30岁，" + _OPEN_FRACTURE)
    assert result["red_flag_gate"]["halted"] is True and result["primary_route"] is None
    # Chat (facts absorbed from the question escalate the session red-flag state)
    session = ConversationSession(case_state={"normalized_tags": []}, user_role="clinician")
    turn = session.ask(_OPEN_FRACTURE + "可以用什么方？")
    assert turn["red_flag_gated"] is True and _no_formula(turn["answer"])
    # Autonomous
    agent = AutonomousQAAgent(case_state={"normalized_tags": []}, user_role="clinician")
    aturn = agent.run(_OPEN_FRACTURE + "可以用什么方？")
    assert aturn["red_flag_gated"] is True and aturn["run"]["status"] == "SAFETY_HALTED"
    # Collaboration (urgent status travels in case_state)
    collab = AgentOrchestrator().run({"normalized_tags": [], "red_flags": {"status": "urgent", "positive_items": ["伤口见骨"]}})
    assert collab["halted"] is True
    # Interview (shared kernel)
    engine = YaoBiInterviewEngine()
    case = YaoBiCaseState(session_id="ec-openfx")
    out = engine.run_turn(case, _OPEN_FRACTURE)
    assert out["safety_level"] == "emergency" and out["done"] is True


# -- historical event: consistently NOT a current emergency ---------------------------------

_HISTORICAL_CRASH = "十年前车祸后偶有腰痛，最近久坐后加重，舌暗，苔白腻。"


def test_historical_crash_consistently_not_emergency():
    result = run_case_pipeline("患者男，50岁，" + _HISTORICAL_CRASH)
    assert result["red_flag_gate"]["halted"] is False
    assert result["safety"]["safety_status"] != "urgent"
    session = ConversationSession(case_state={"normalized_tags": []}, user_role="clinician")
    turn = session.ask(_HISTORICAL_CRASH + "是什么证型？")
    assert not turn.get("red_flag_gated")
    engine = YaoBiInterviewEngine()
    case = YaoBiCaseState(session_id="ec-hist")
    out = engine.run_turn(case, _HISTORICAL_CRASH)
    assert out["safety_level"] != "emergency"


# -- fail-closed: crashed safety extraction abstains, never silently proceeds ----------------

def test_safety_extraction_failure_fails_closed(monkeypatch):
    import backend.server as server_module

    server = importlib.reload(server_module)
    def boom(_text):
        raise RuntimeError("extractor crashed")
    monkeypatch.setattr(server, "case_extract_skill", boom)
    merged = server._enrich_with_question({"tags": ["dark_tongue", "chronic_yabi"]}, "这个病人是什么证型？")
    assert merged.get("safety_extraction_failed") is True
    state = server._case_state(merged)
    session = ConversationSession(case_state=state, user_role="clinician")
    # Simulate the same failure inside the session's own absorption step.
    session.case_state["safety_extraction_failed"] = True
    result = session._dispatch("syndrome_inquiry", "这个病人是什么证型？")
    assert "安全信息解析异常" in result["answer"]
    assert "safety_fail_closed" in result["skills"]


# -- ops endpoints are locked on public binds -------------------------------------------------

def test_metrics_and_warmup_locked_on_public_bind(monkeypatch):
    import backend.server as server_module

    server = importlib.reload(server_module)
    monkeypatch.delenv("YAOBI_CLINICIAN_TOKEN", raising=False)
    monkeypatch.setattr(server, "_SERVER_BIND_HOST", "0.0.0.0")
    assert server.handle_metrics({}).get("error") == "ops_endpoint_locked"
    assert server.handle_warmup({}).get("error") == "ops_endpoint_locked"
    monkeypatch.setenv("YAOBI_CLINICIAN_TOKEN", "s3cret")
    assert server.handle_metrics({"clinician_token": "s3cret"}).get("ok") is True
    monkeypatch.setattr(server, "_SERVER_BIND_HOST", None)  # library/tests: open
    monkeypatch.delenv("YAOBI_CLINICIAN_TOKEN", raising=False)
    assert server.handle_metrics({}).get("ok") is True
