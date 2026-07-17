"""Regression tests for the feature-review hardening pass.

Covers: http-backend fallback guarantee, layered output guards (clinician-soft vs
patient-strict, Chinese-numeral doses), deterministic red-flag safety net in the
conversational interview, alias-driven rule coverage, per-task inference profiles,
and whole-round voiding of leaking Tao probes.
"""

from __future__ import annotations

import importlib

import pytest

from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
from backend.engine.rule_engine import RuleEngine
from backend.llm.dao_client import DaoClient, DaoGenerationConfig, DaoRuntimeError
from backend.llm.output_guard import guard_clinician_draft, guard_probe, guard_tao_output
from backend.skills.case_extract_skill import case_extract_skill
from backend.skills.case_normalize_skill import case_normalize_skill
from backend.skills.shen_rule_signal_skill import shen_rule_signal_skill
from backend.skills.tao_followup_probe_skill import _freeform_probes
from backend.skills.tao_report_generation_skill import tao_report_generation_skill


# -- http backend: every network failure must become DaoRuntimeError (fallback guarantee) --

def _http_client() -> DaoClient:
    client = DaoClient(DaoGenerationConfig(backend="http", endpoint_url="http://127.0.0.1:9/v1/chat/completions", timeout_seconds=1))
    return client


def test_http_backend_network_failure_raises_dao_runtime_error(monkeypatch):
    client = _http_client()
    monkeypatch.setattr(DaoClient, "_HTTP_BACKOFF_SECONDS", 0.0)
    with pytest.raises(DaoRuntimeError):
        client.generate({"normalized_tags": ["lumbar_pain"]})


def test_report_skill_falls_back_when_http_backend_unreachable(monkeypatch):
    monkeypatch.setattr(DaoClient, "_HTTP_BACKOFF_SECONDS", 0.0)
    result = tao_report_generation_skill(
        case_json={"age": 68}, normalized_tags=["lumbar_pain"], syndrome_candidates=[],
        formula_route=None, matched_modules=[], conflicts=[], safety={},
        dao_client=_http_client(), use_llm=True,
    )
    assert result["tao_runtime"]["status"] == "fallback"
    assert result["tao_runtime"]["fallback_used"] is True
    assert result["markdown_report"]  # deterministic report survives


def test_http_payload_uses_plain_messages_not_templated_prompt(monkeypatch):
    captured: dict = {}

    class _FakeResponse:
        def read(self, limit=None):
            import json
            raw = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")
            return raw if limit is None else raw[:limit]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(request, timeout=0):
        import json
        captured.update(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = _http_client()
    client.generate({"normalized_tags": ["lumbar_pain"]})
    user_contents = [m["content"] for m in captured["messages"] if m["role"] == "user"]
    assert user_contents and "<|im_start|>" not in user_contents[0]
    assert sum(1 for m in captured["messages"] if m["role"] == "system") == 1


# -- inference profiles: structured JSON tasks decode greedily, long-form gets more budget --

def test_inference_profiles_resolved_per_task():
    client = DaoClient(DaoGenerationConfig(backend="mock"))
    structured = client._profile_params("structured_json")
    teaching = client._profile_params("teaching_explanation")
    assert structured["do_sample"] is False
    assert structured["max_new_tokens"] <= 1024
    assert teaching["max_new_tokens"] >= 3072


# -- layered guards ------------------------------------------------------------------------

def test_patient_guard_blocks_chinese_numeral_doses():
    assert guard_tao_output("建议细辛三克，一日三次。")["allowed"] is False
    assert guard_tao_output("独活九克，分两次服。")["allowed"] is False


def test_patient_guard_no_longer_false_kills_teaching_phrases():
    assert guard_tao_output("每次复诊时评估腰部活动度，随访疗程安排由医师决定。")["allowed"] is True


def test_clinician_draft_guard_allows_experience_dose_ranges():
    text = "细辛经验剂量范围 3-6克（医师审核），附片须先煎，方义为温经散寒。"
    assert guard_clinician_draft(text)["allowed"] is True


def test_clinician_draft_guard_blocks_executable_regimen_and_verdict():
    assert guard_clinician_draft("明确诊断为腰椎间盘突出症。")["allowed"] is False
    assert guard_clinician_draft("处方如下：独活9g，水煎服，每日2次。")["allowed"] is False
    assert guard_clinician_draft("你可以自行购买上述药物。")["allowed"] is False


def test_clinician_draft_guard_still_enforces_structured_contract():
    guard = guard_clinician_draft("倾向肝肾不足，供医师审定。", {"final_diagnosis": "腰椎间盘突出"})
    assert guard["allowed"] is False


def test_probe_guard_allows_frequency_questions():
    assert guard_probe("疼痛每次发作会持续多久？")["allowed"] is True


# -- probe leaks void the whole round ------------------------------------------------------

def test_freeform_probe_leak_voids_entire_round():
    raw = "疼痛是刺痛还是胀痛？\n建议服用独活寄生汤每日2次。"
    assert _freeform_probes(raw, "S3_PAIN_PROFILE", 2) == []


def test_freeform_probe_clean_round_survives():
    raw = "疼痛是刺痛还是胀痛？\n受凉后会不会加重？"
    probes = _freeform_probes(raw, "S3_PAIN_PROFILE", 2)
    assert len(probes) == 2


# -- interview red-flag safety net ----------------------------------------------------------

def test_interview_detects_cauda_equina_without_llm():
    engine = YaoBiInterviewEngine(use_llm=False)
    case = YaoBiCaseState(session_id="t-offline")
    out = engine.run_turn(case, "腰痛三天，今天开始小便失禁，会阴发麻")
    assert case.safety_level == "emergency"
    assert out["done"] is True


def test_interview_negated_red_flag_does_not_trigger():
    engine = YaoBiInterviewEngine(use_llm=False)
    case = YaoBiCaseState(session_id="t-neg")
    engine.run_turn(case, "腰痛两周，没有大小便失禁，也没有发热")
    assert case.safety_level == "low"
    assert case.red_flags == []


def test_interview_string_denial_slot_does_not_trigger_emergency():
    engine = YaoBiInterviewEngine(use_llm=False)
    case = YaoBiCaseState(session_id="t-str")
    case.ortho_neuro_slots["bowel_bladder_dysfunction"] = "否"
    case.ortho_neuro_slots["fever"] = "正常"
    engine._detect_red_flags(case)
    assert case.safety_level == "low"


# -- rule coverage unlocked by alias normalization ------------------------------------------

def test_qi_stagnation_blood_stasis_rule_triggers_from_text():
    text = "患者男，45岁，搬重物扭伤后腰痛2月，刺痛，痛处固定，舌紫暗。"
    tags = case_normalize_skill(case_extract_skill(text))["normalized_tags"]
    names = [c["name"] for c in RuleEngine().score_syndromes(tags)[0]]
    assert "气滞血瘀证" in names


def test_spleen_deficiency_rule_triggers_from_text():
    text = "患者女，55岁，腰痛1年，胃纳差，乏力，舌边齿痕。"
    tags = case_normalize_skill(case_extract_skill(text))["normalized_tags"]
    names = [c["name"] for c in RuleEngine().score_syndromes(tags)[0]]
    assert "脾虚不运证" in names


def test_cold_damp_signal_reachable_from_text():
    text = "患者女，68岁，腰痛5年，受凉加重，热敷缓解。"
    tags = case_normalize_skill(case_extract_skill(text))["normalized_tags"]
    signals = shen_rule_signal_skill({"normalized_tags": tags})["shen_signals"]
    assert signals["cold_damp_signal"] is True


def test_high_confidence_reachable_with_rich_evidence():
    text = "患者女，68岁，腰痛反复5年，加重1月，伴下肢麻木，畏寒，舌暗苔白腻，脉细缓，既往骨质疏松。"
    tags = case_normalize_skill(case_extract_skill(text))["normalized_tags"]
    top = RuleEngine().score_syndromes(tags)[0][0]
    assert top["confidence"] == "high"


# -- server session handling ----------------------------------------------------------------

def test_interview_without_session_id_gets_server_generated_isolated_sessions(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    import backend.server as server_module
    server = importlib.reload(server_module)
    first = server.handle_interview({"action": "answer", "message": "我腰痛五年了"})
    second = server.handle_interview({"action": "answer", "message": "我今年30岁，腰不痛"})
    assert first["session_id"] != second["session_id"]
    assert first["session_id"].startswith("srv-")
