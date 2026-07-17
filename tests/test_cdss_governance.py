"""Tests for the CDSS governance layer: provenance, audit, feedback, metrics, uncertainty."""

from __future__ import annotations

import importlib
import json

from backend.audit.audit_log import AuditLog, Counters, text_digest
from backend.provenance import get_provenance, rules_fingerprint
from backend.skills.pipeline import run_case_pipeline
from backend.skills.uncertainty_skill import uncertainty_markdown, uncertainty_skill


def _server(monkeypatch, tmp_path):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    monkeypatch.setenv("YAOBI_AUDIT_DIR", str(tmp_path))
    import backend.server as server_module

    return importlib.reload(server_module)


# -- provenance ------------------------------------------------------------------------

def test_rules_fingerprint_is_stable_and_covers_all_rule_files():
    fp1 = rules_fingerprint(refresh=True)
    fp2 = rules_fingerprint()
    assert fp1["rules_version"] == fp2["rules_version"]
    assert len(fp1["rules_version"]) == 12
    assert "02_syndrome_rules.yaml" in fp1["rule_files"]
    assert "06_conflict_rules.yaml" in fp1["rule_files"]


def test_provenance_includes_model_runtime_when_config_given():
    class Cfg:
        model_id = "CMLM/Dao1-30b-a3b"
        backend = "mock"
        torch_dtype = "float16"
        load_in_4bit = False
        load_in_8bit = False

    block = get_provenance(Cfg())
    assert block["app_version"]
    assert block["model_runtime"]["backend"] == "mock"
    assert block["decision_basis"] == "deterministic_rules_first"


def test_pipeline_report_carries_provenance_and_uncertainty():
    result = run_case_pipeline("患者女，68岁，腰痛反复5年，畏寒，下肢麻木，舌暗苔白腻，既往骨质疏松。")
    assert result["provenance"]["rules_version"]
    assert result["uncertainty"]["non_final"] is True
    assert "决策出处（Provenance）" in result["markdown_report"]
    assert "判读可信度与鉴别提示" in result["markdown_report"]


# -- uncertainty / abstention ----------------------------------------------------------

def test_uncertainty_abstains_when_no_candidates():
    block = uncertainty_skill([], ["lumbar_pain"])["uncertainty"]
    assert block["abstain"] is True
    assert block["abstain_reason"] == "no_candidate"
    assert "证据不足" in uncertainty_markdown(block)


def test_uncertainty_abstains_on_weak_top_score():
    block = uncertainty_skill([{"name": "气血痹阻证", "score": 2, "evidence_tags": []}], [])["uncertainty"]
    assert block["abstain"] is True
    assert block["abstain_reason"] == "insufficient_evidence"


def test_uncertainty_reports_narrow_separation_with_differential_gaps():
    cands = [
        {"name": "肝肾不足证", "score": 5, "evidence_tags": ["elderly"]},
        {"name": "气血痹阻证", "score": 4, "evidence_tags": ["dark_tongue"]},
    ]
    block = uncertainty_skill(cands, ["elderly", "dark_tongue"])["uncertainty"]
    assert block["abstain"] is False
    assert block["separation"] == "narrow"
    assert block["differential_gaps"], "narrow separation must offer discriminators"
    assert any("区分" in g["suggestion"] for g in block["differential_gaps"])


def test_uncertainty_clear_separation():
    cands = [
        {"name": "肝肾不足证", "score": 8, "evidence_tags": []},
        {"name": "气血痹阻证", "score": 3, "evidence_tags": []},
    ]
    block = uncertainty_skill(cands, [])["uncertainty"]
    assert block["separation"] == "clear"
    assert block["abstain"] is False


# -- audit log --------------------------------------------------------------------------

def test_audit_log_appends_jsonl_records(tmp_path):
    log = AuditLog(directory=tmp_path, enabled=True)
    rec = log.record("api_decision", {"endpoint": "/api/chat", "intent": "syndrome_inquiry"})
    assert rec["seq"] == 1
    files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(files) == 1
    stored = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert stored["event"] == "api_decision"
    assert stored["intent"] == "syndrome_inquiry"


def test_audit_log_disabled_writes_nothing(tmp_path):
    log = AuditLog(directory=tmp_path, enabled=False)
    assert log.record("x", {}) is None
    assert list(tmp_path.glob("*.jsonl")) == []


def test_text_digest_never_stores_content():
    digest = text_digest("患者腰痛五年，大小便正常")
    assert set(digest) == {"sha256_16", "chars"}
    assert "腰痛" not in json.dumps(digest, ensure_ascii=False)


def test_counters_thread_safe_shape():
    c = Counters()
    c.increment("requests:/api/chat")
    c.increment("requests:/api/chat")
    assert c.snapshot()["requests:/api/chat"] == 2


# -- server endpoints -------------------------------------------------------------------

def test_health_exposes_provenance(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    health = server.handle_health({})
    assert health["provenance"]["rules_version"]
    assert health["provenance"]["app_version"]


def test_feedback_endpoint_validates_and_counts(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    bad = server.handle_feedback({"action": "meh", "doctor_mode": True})
    assert bad["ok"] is False
    ok = server.handle_feedback({
        "action": "confirmed", "target": "chat_turn", "intent": "syndrome_inquiry",
        "used_llm": True, "reason": "辨证合理", "doctor_mode": True,
    })
    assert ok["ok"] is True
    assert ok["recorded"]["action"] == "confirmed"
    metrics = server.handle_metrics({})
    assert metrics["counters"]["feedback_confirmed"] >= 1
    assert metrics["feedback_summary"]["total"] >= 1


def test_metrics_endpoint_reports_audit_and_provenance(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    server.handle_chat({"question": "这个病人是什么证型？", "tags": ["dark_tongue", "chronic_yabi"], "doctor_mode": True})
    metrics = server.handle_metrics({})
    assert metrics["ok"] is True
    assert metrics["audit"]["enabled"] is True
    assert metrics["provenance"]["rules_version"]
    assert metrics["uptime_seconds"] >= 0


def test_chat_decision_is_audited(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    result = server.handle_chat({"question": "有哪些危险信号要排查？", "doctor_mode": True})
    summary = server._decision_summary("/api/chat", {"question": "有哪些危险信号要排查？"}, result)
    assert summary["intent"]
    assert summary["question"]["chars"] > 0
    assert "危险" not in json.dumps(summary["question"], ensure_ascii=False)


def test_interview_emergency_is_counted(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    result = server.handle_interview({"session_id": "audit-rf", "message": "我腰痛，今天小便失禁了，会阴发麻"})
    server._decision_summary("/api/interview", {"message": "x"}, result)
    assert server.COUNTERS.snapshot().get("red_flag_emergency_stop", 0) >= 1


# -- final report / caseguide governance keys -------------------------------------------

def test_caseguide_final_report_has_governance_blocks():
    from tests.test_caseguide import build_complete_session

    final = build_complete_session().final_report()
    assert final["provenance"]["rules_version"]
    assert "abstain" in final["uncertainty"]


# -- adversarial-review regression fixes --------------------------------------------------

def test_interview_negated_conditions_do_not_fire_interruptive_alerts(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
    from backend.llm.dao_client import DaoClient

    engine = YaoBiInterviewEngine(dao_client=DaoClient(), use_llm=True)
    case = YaoBiCaseState(session_id="neg-cond")
    engine.run_turn(case, "我今年60岁，男，腰痛3个月")
    engine.run_turn(case, "医生看片子说是腰椎间盘突出。我没有高血压也没有心脏病，也没怀孕。")
    report = engine._build_report(case)
    assert report["interaction_alerts"] == []
    assert report["alert_summary"].get("requires_dual_signoff") is not True


def test_interview_report_surfaces_alert_summary_and_uncertainty(monkeypatch):
    monkeypatch.setenv("TAO_BACKEND", "mock")
    from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
    from backend.llm.dao_client import DaoClient

    engine = YaoBiInterviewEngine(dao_client=DaoClient(), use_llm=True)
    case = YaoBiCaseState(session_id="surface")
    engine.run_turn(case, "我68岁女，腰痛5年，畏寒，舌暗苔白腻，骨质疏松")
    report = engine._build_report(case)
    for key in ("interaction_alerts", "alert_summary", "uncertainty"):
        assert key in report


def test_interview_string_comorbidities_do_not_crash_report():
    from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine

    engine = YaoBiInterviewEngine(use_llm=False)
    case = YaoBiCaseState(session_id="str-comorb")
    case.history_slots["comorbidities"] = "高血压、糖尿病"
    case.history_slots["medication_history"] = "华法林"
    report = engine._build_report(case)  # must not raise TypeError
    assert "interaction_alerts" in report


def test_interview_uncertainty_uses_raw_rule_scores():
    from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine

    engine = YaoBiInterviewEngine(use_llm=False)
    case = YaoBiCaseState(session_id="raw-score")
    case.pain_slots.update({"numbness": True, "radiation": True})
    case.tcm_slots.update({"cold_heat": "怕冷"})
    case.demographics["age"] = 68
    case.history_slots["osteoporosis"] = True
    case.candidate_patterns, _ = engine._infer_patterns(case)
    assert case.candidate_patterns, "expected syndrome candidates"
    # Raw score carried through — never the prob*10 rescale.
    assert all(isinstance(p.get("score"), int) for p in case.candidate_patterns)


def test_pipeline_fires_herb_drug_interruptive_alert_from_narrative():
    result = run_case_pipeline("患者男，70岁，腰痛10年，刺痛固定，舌紫暗，长期服用华法林，有高血压。")
    ids = {a["id"] for a in result.get("interaction_alerts") or []}
    assert "anticoagulant_x_huoxue_herbs" in ids
    assert result["alert_summary"]["requires_dual_signoff"] is True
    assert "需医师确认" in result["markdown_report"]


def test_pipeline_negated_medication_does_not_fire():
    result = run_case_pipeline("患者腰痛，刺痛，舌紫暗，没有高血压，未服用阿司匹林。")
    assert (result.get("interaction_alerts") or []) == []


def test_terms_match_hardened_against_short_and_negated_input():
    from backend.engine.conflict_resolver import _terms_match

    assert _terms_match("心", "心脏病") is False  # single char must not over-match
    assert _terms_match("阿司匹林肠溶片", "阿司匹林") is True
    assert _terms_match("没有高血压", "高血压") is False  # negation window
    assert _terms_match("高血压病史", "高血压") is True


def test_rules_fingerprint_reacts_to_rule_edits(tmp_path, monkeypatch):
    import backend.provenance as prov

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "01_tags.yaml").write_text("tags: {}\n", encoding="utf-8")
    monkeypatch.setattr(prov, "RULES_DIR", rules_dir)
    v1 = prov.rules_fingerprint(refresh=True)["rules_version"]
    (rules_dir / "01_tags.yaml").write_text("tags: {x: {}}\n", encoding="utf-8")
    v2 = prov.rules_fingerprint()["rules_version"]
    assert v1 != v2


def test_feedback_bounds_client_supplied_fields(monkeypatch, tmp_path):
    server = _server(monkeypatch, tmp_path)
    result = server.handle_feedback({"action": "rejected", "intent": "x" * 999, "answer_source": "y" * 999, "doctor_mode": True})
    assert len(result["recorded"]["intent"]) <= 120
    assert len(result["recorded"]["answer_source"]) <= 120
