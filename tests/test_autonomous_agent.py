"""Tests for the autonomous multi-step QA agent (Plan → subagent delegation → synthesize)."""

from backend.agents.autonomous_agent import AutonomousQAAgent, plan_question
from backend.agents.skill_router import ALLOWED_INTENTS
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


# ----------------------------------------------------------------- planning

def test_plan_question_decomposes_multi_intent_question():
    out = plan_question("这个病人是什么证型，可以用什么方剂，有什么安全风险？", max_steps=4)
    intents = [s["intent"] for s in out["plan"]]
    assert "syndrome_inquiry" in intents
    assert "formula_inquiry" in intents
    assert "safety_inquiry" in intents
    assert len(out["plan"]) >= 3
    assert all(s["intent"] in ALLOWED_INTENTS for s in out["plan"])


def test_plan_question_single_intent_for_simple_question():
    out = plan_question("有哪些危险信号需要排查？")
    assert [s["intent"] for s in out["plan"]] == ["red_flag_inquiry"]


def test_plan_question_llm_overlay_stays_in_allowlist_and_falls_back():
    out = plan_question("证型和方剂", use_llm=True, dao_client=mock_dao())
    assert out["method"] == "llm"
    assert out["llm_runtime"]["status"] == "accepted"
    assert all(s["intent"] in ALLOWED_INTENTS for s in out["plan"])

    class BadDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def plan_skills(self, plan_context):
            return '{"plan":[{"intent":"make_prescription"},{"intent":"diagnose"}]}'

    out2 = plan_question("证型和方剂", use_llm=True, dao_client=BadDao())
    assert out2["method"] == "keyword"  # invalid intents -> fallback to deterministic plan
    assert all(s["intent"] in ALLOWED_INTENTS for s in out2["plan"])


# --------------------------------------------------------------- delegation

def test_agent_runs_multi_step_and_delegates_to_subagents():
    agent = AutonomousQAAgent(case_state=_case(), max_steps=4)
    turn = agent.run("这个病人是什么证型，对应什么方剂路线，有什么用药安全风险？")
    assert turn["multi_step"] is True
    assert set(turn["subagents_used"]) >= {"syndrome_inquiry", "formula_inquiry", "safety_inquiry"}
    # synthesized answer references each delegated subagent
    assert "自主规划了" in turn["answer"]
    assert turn["answer"].count("###") >= 3
    # ReAct-style trace: thought -> action -> observation per step, plus a final
    # critique entry (observe -> critique closes the loop after execution).
    assert len(turn["trace"]) == len(turn["steps"]) + 1
    for entry in turn["trace"][:-1]:
        assert "delegate→" in entry["action"] or "replan→" in entry["action"]
        assert entry["observation"]
    assert turn["trace"][-1]["action"].startswith("critique")
    assert turn["agent_loop"][:5] == ["understand", "plan", "execute", "observe", "critique"]
    assert "critique" in turn


def test_agent_single_step_returns_direct_answer():
    agent = AutonomousQAAgent(case_state=_case())
    turn = agent.run("有哪些危险信号需要排查？")
    assert turn["multi_step"] is False
    assert turn["subagents_used"] == ["red_flag_inquiry"]
    assert "###" not in turn["answer"]


def test_agent_blocks_patient_prescription_request():
    agent = AutonomousQAAgent(case_state=_case(), user_role="patient")
    turn = agent.run("直接给我开完整处方和剂量")
    assert turn["blocked"] is True
    assert turn["plan"] == []
    assert "不能生成最终诊断" in turn["answer"]


def test_agent_marks_llm_used_for_reasoning_step_when_tao_enabled():
    agent = AutonomousQAAgent(case_state=_case(), use_llm=True, dao_client=mock_dao(), max_steps=4)
    turn = agent.run("从症状到治法的推理过程，并总结这个医案")
    assert "reasoning_inquiry" in turn["subagents_used"]
    assert "experience_inquiry" in turn["subagents_used"]
    assert turn["used_llm"] is True


def test_agent_mining_step_uses_real_dataset():
    agent = AutonomousQAAgent(case_state=_case())
    turn = agent.run("数据里哪个证型最多，气血痹阻证常用什么方？")
    assert "mining_inquiry" in turn["subagents_used"]
    assert any("例" in s["answer"] for s in turn["steps"])
