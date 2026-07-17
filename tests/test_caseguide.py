from backend.skills.adaptive_question_planner_skill import adaptive_question_planner_skill
from backend.skills.caseguide_state_machine import CaseGuideSession
from backend.skills.consent_privacy_skill import consent_privacy_skill
from backend.skills.red_flag_screen_skill import red_flag_screen_skill
from backend.skills.patient_request_guard_skill import patient_request_guard_skill
from backend.skills.clinician_review_package_skill import clinician_review_package_skill


def build_complete_session():
    session = CaseGuideSession()
    session.start("姓名张三，电话13812345678，我腰痛")
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    session.answer_stage({"age": 68, "sex": "女", "main_symptom": "腰痛", "duration": "5年", "acute_worsening": "1月", "associated_symptom": "右下肢麻木", "recurrent_status": "反复发作，这次加重"})
    session.answer_stage({"P001": ["腰骶部"], "P002": "到小腿", "P003": ["酸痛", "麻痛"], "P004": 6, "P005": ["久坐", "受凉"], "P006": ["热敷"]})
    session.answer_stage({"N001": "经常有", "N002": ["小腿外侧"], "N003": "没有", "N004": "否", "N005": ["做过MRI", "做过骨密度"], "N006": ["骨质疏松"]})
    session.answer_stage({"T001": "怕冷", "T002": "遇冷加重，热敷舒服", "T004": "轻微", "T016": "睡不踏实", "T018": "胃口差", "T024": "偏暗紫", "T025": "白腻"})
    session.answer_stage({})
    session.answer_stage({"C001": ["骨质疏松", "高血压"], "C002": ["塞来昔布", "乙哌立松"], "C003": "否", "C004": "没有"})
    return session


def test_consent_desensitizes_and_preserves_boundary():
    result = consent_privacy_skill(raw_input="姓名张三，电话13812345678，地址北京市某区某街道，我想开方")
    assert "13812345678" not in result["sanitized_input"]
    assert "不构成诊断、处方或治疗建议" in result["required_homepage_notice"]
    assert "临床处方" in result["forbidden_outputs"]


def test_red_flag_screen_stops_on_urgent():
    result = red_flag_screen_skill({"RF001": "否", "RF002": "是", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    assert result["red_flag_status"] == "urgent"
    assert result["next_action"] == "stop_and_refer"


def test_caseguide_generates_complete_case_and_handoff_without_prescription():
    session = build_complete_session()
    final = session.final_report()
    assert final["state"] == "S10_FINAL_REPORT"
    assert "腰痹医案草稿" in final["standard_case_markdown"]
    assert "医生复核摘要" in final["clinician_handoff_markdown"]
    assert "radiating_leg_pain" in final["case_state"]["normalized_tags"]
    assert final["shen_signals"]["danggui_sini_signal"] is True
    assert "不构成诊断、处方或治疗建议" in final["standard_case_markdown"]
    package = final["clinician_review_package"]
    assert package["diagnosis_review"]["non_final_diagnosis"] is True
    assert package["prescription_review"]["complete_prescription_generated"] is False
    assert package["prescription_review"]["patient_executable_dose_generated"] is False


def test_adaptive_planner_prioritizes_cold_heat_for_elderly_numbness_case():
    session = CaseGuideSession()
    session.case_state["patient_profile"]["age"] = 68
    session.case_state["chief_complaint"]["duration"] = "5年"
    session.case_state["normalized_tags"] = ["elderly", "chronic_yabi", "lower_limb_numbness", "osteoporosis"]
    result = adaptive_question_planner_skill(session.case_state, max_questions=3)
    questions = [item["question"]["question"] for item in result["next_questions"]]
    assert any("遇冷" in q or "热敷" in q for q in questions)


def test_patient_request_guard_blocks_final_diagnosis_prescription_and_dose():
    guard = patient_request_guard_skill("请给出最终诊断、完整处方和患者可执行剂量，标注需医师审核")
    assert guard["blocked"] is True
    assert set(guard["requested_outputs"]) == {"final_diagnosis", "complete_prescription", "patient_executable_dose"}
    assert "complete_clinical_prescription" in guard["forbidden_outputs"]
    package = clinician_review_package_skill({"normalized_tags": ["lower_limb_numbness"]}, requested_outputs=guard["requested_outputs"])["clinician_review_package"]
    assert package["request_guard"]["blocked"] is True
    assert package["prescription_review"]["complete_prescription_generated"] is False
    assert package["prescription_review"]["patient_executable_dose_generated"] is False


def test_physician_review_allows_signed_manual_content_and_rejects_model_generated():
    from backend.skills.physician_review_skill import create_physician_review_task, physician_review_skill

    case_state = {"normalized_tags": ["lower_limb_numbness"]}
    task = create_physician_review_task(case_state)["review_task"]
    assert task["status"] == "pending_physician_review"
    assert "final_diagnosis_generation" in task["model_forbidden_actions"]

    reviewer = {
        "role": "licensed_physician",
        "physician_id": "DOC-001",
        "physician_name": "审核医师",
        "license_id": "LICENSE-001",
        "signed": True,
    }
    rejected = physician_review_skill(
        case_state,
        reviewer,
        final_diagnosis={"source": "model_generated", "text": "腰椎间盘突出症"},
    )
    assert rejected["status"] == "rejected_model_generated_diagnosis"

    signed = physician_review_skill(
        case_state,
        reviewer,
        final_diagnosis={"source": "physician_entered", "text": "医师手工录入诊断"},
        prescription=[{"source": "physician_entered", "herb": "细辛", "dose": "医师手工录入剂量"}],
        administration={"source": "physician_entered", "text": "医师手工录入煎服法"},
    )["physician_review_record"]
    assert signed["status"] == "signed_physician_review"
    assert signed["audit"]["model_generated_complete_prescription"] is False
    assert signed["audit"]["physician_signed"] is True
    assert signed["warnings"]


def test_cdss_recommendation_generates_clinician_draft_not_signed_order():
    from backend.skills.cdss_recommendation_skill import cdss_recommendation_skill

    result = cdss_recommendation_skill(
        {"normalized_tags": ["lower_limb_numbness", "radiating_leg_pain"], "neuro_ortho": {"numbness": "经常有"}},
        syndrome_candidates=[{"name": "气血痹阻证", "score": 6, "evidence_tags": ["lower_limb_numbness"]}],
        formula_routes=[{"name": "当归四逆汤加减", "score": 5, "core_module": ["当归", "桂枝", "细辛"], "evidence_tags": ["lower_limb_numbness"]}],
        matched_modules=[{"name": "虫类搜络模块", "herbs": ["蜈蚣", "全蝎"], "evidence_tags": ["lower_limb_numbness"], "role": "add_on"}],
        user_role="clinician",
    )["cdss_recommendation"]
    assert result["status"] == "draft_for_clinician_review"
    assert result["patient_visible"] is False
    assert result["prescription_strategy_draft"]["complete_prescription_generated"] is False
    assert result["prescription_strategy_draft"]["patient_executable_dose_generated"] is False
    assert result["safety"]["high_risk_herbs_in_modules"] == ["全蝎", "蜈蚣"]


def test_cdss_recommendation_blocks_patient_role():
    from backend.skills.cdss_recommendation_skill import cdss_recommendation_skill

    result = cdss_recommendation_skill({"normalized_tags": []}, user_role="patient")["cdss_recommendation"]
    assert result["status"] == "blocked_patient_role"
    assert result["patient_visible"] is False


def test_caseguide_state_stays_for_deepening_and_limits_to_three_followups():
    session = CaseGuideSession()
    started = session.start("腰痛")
    assert started["fsm"]["max_followups_per_state"] == 3
    assert [q["id"] for q in started["next_questions"]] == ["RF001", "RF002", "RF003"]

    first = session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否"})
    assert first["state"] == "S1_REDFLAG"
    assert [q["id"] for q in first["next_questions"]] == ["RF004", "RF005", "RF006"]
    assert first["fsm"]["remaining_followups"] == 2

    second = session.answer_red_flags({"RF004": "否", "RF005": "否", "RF006": "否"})
    assert second["state"] == "S2_BASIC"

    still_basic = session.answer_stage({"age": 68})
    assert still_basic["state"] == "S2_BASIC"
    assert still_basic["next_questions"]
    assert all(q["state"] == "S2_BASIC" for q in still_basic["next_questions"])

    session.answer_stage({"main_symptom": "腰痛"})
    forced_next = session.answer_stage({"duration": "5年"})
    assert forced_next["state"] == "S3_PAIN_PROFILE"


def test_caseguide_manual_end_current_state_advances_before_three_turns():
    session = CaseGuideSession()
    session.start("腰痛")
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    session.answer_stage({"age": 68})
    result = session.end_current_state()
    assert result["manual_end_accepted"] is True
    assert result["state"] == "S3_PAIN_PROFILE"
    assert result["fsm"]["turn_index"] == 0


def test_caseguide_shen_signal_state_applies_deepening_answers():
    session = CaseGuideSession()
    session.start("腰痛")
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    session.answer_stage({"age": 68, "main_symptom": "腰痛", "duration": "5年"}, end_state=True)
    session.answer_stage({"P001": ["腰骶部"], "P002": "到小腿", "P003": ["酸痛"]}, end_state=True)
    session.answer_stage({"N001": "经常有", "N002": ["小腿外侧"], "N003": "没有"}, end_state=True)
    session.answer_stage({"T001": "怕冷", "T002": "遇冷加重，热敷舒服", "T016": "睡不踏实"}, end_state=True)
    assert session.state == "S6_SHEN_SIGNAL"
    result = session.answer_stage({"T018": "胃口差"})
    assert result["state"] == "S6_SHEN_SIGNAL"
    assert session.case_state["tcm_inquiry"]["appetite"] == "胃口差"
    assert "poor_appetite" in session.case_state["normalized_tags"]


def test_caseguide_followup_budget_is_configurable():
    session = CaseGuideSession(max_followups_per_state=1, questions_per_turn=2)
    started = session.start("腰痛")
    assert started["fsm"]["max_followups_per_state"] == 1
    assert started["fsm"]["questions_per_turn"] == 2
    assert len(started["next_questions"]) == 2

    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"})
    assert session.state == "S2_BASIC"
    forced = session.answer_stage({"age": 68})
    assert forced["state"] == "S3_PAIN_PROFILE"  # 单轮预算用尽后自动终止追问并进入下一状态。

    assert session.set_max_followups(0) == 1  # 下限保护
    assert session.set_questions_per_turn(5) == 5


def test_caseguide_red_flag_end_state_flag_cannot_skip_unanswered_red_flags():
    session = CaseGuideSession()
    session.start("腰痛")
    result = session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否"}, end_state=True)
    assert result["state"] == "S1_REDFLAG"
    assert [q["id"] for q in result["next_questions"]] == ["RF004", "RF005", "RF006"]


def test_run_scripted_interview_completes_autonomously():
    answers = {
        "RF001": "否", "RF002": "否", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否",
        "age": 68, "sex": "女", "main_symptom": "腰痛", "duration": "5年", "recurrent_status": "反复发作，这次加重",
        "P001": ["腰骶部"], "P002": "到小腿", "P003": ["酸痛", "麻痛"], "P004": 6, "P005": ["久坐", "受凉"], "P006": ["热敷"],
        "N001": "经常有", "N002": ["小腿外侧"], "N003": "没有", "N004": "否", "N005": ["做过MRI"], "N006": ["骨质疏松"],
        "T001": "怕冷", "T002": "遇冷加重，热敷舒服", "T004": "轻微", "T016": "睡不踏实", "T018": "胃口差", "T024": "偏暗紫", "T025": "白腻",
        "C001": ["骨质疏松", "高血压"], "C002": ["塞来昔布"], "C003": "否", "C004": "没有",
    }
    session = CaseGuideSession()
    result = session.run_scripted_interview(answers, raw_input="我腰痛")
    assert result["stopped_reason"] == "completed"
    assert result["state"] == "S10_FINAL_REPORT"
    assert result["transcript"]
    assert "radiating_leg_pain" in result["case_state"]["normalized_tags"]
    assert any(step.get("action") == "auto_end_followups" for step in result["transcript"])
    package = result["clinician_review_package"]
    assert package["prescription_review"]["complete_prescription_generated"] is False


def test_run_scripted_interview_hard_stops_on_urgent_red_flag():
    answers = {"RF001": "否", "RF002": "是", "RF003": "否", "RF004": "否", "RF005": "否", "RF006": "否"}
    session = CaseGuideSession()
    result = session.run_scripted_interview(answers, raw_input="我腰痛，尿不出来")
    assert result["stopped_reason"] == "red_flag_urgent"
    assert result["state"] == "S_EMERGENCY_NOTICE"


def test_run_scripted_interview_blocks_when_red_flags_unanswered():
    session = CaseGuideSession()
    result = session.run_scripted_interview({}, raw_input="腰痛")
    assert result["stopped_reason"] == "blocked_unanswered_red_flags"
    assert result["state"] == "S1_REDFLAG"


def test_caseguide_cannot_manually_skip_unanswered_red_flags():
    session = CaseGuideSession()
    session.start("腰痛")
    blocked = session.end_current_state()
    assert blocked["manual_end_accepted"] is False
    assert blocked["state"] == "S1_REDFLAG"


def test_caseguide_questions_can_use_tao_overlay_without_changing_candidate_ids():
    from backend.llm.dao_client import DaoClient, DaoGenerationConfig

    session = CaseGuideSession(use_llm_questions=True, dao_client=DaoClient(DaoGenerationConfig(backend="mock")))
    result = session.start("腰痛")
    assert result["fsm"]["tao_question_runtime"]["status"] == "accepted"
    assert result["fsm"]["tao_question_runtime"]["fallback_used"] is False
    assert [q["id"] for q in result["next_questions"]] == ["RF001", "RF002", "RF003"]
    assert all(q.get("tao_enhanced") is True for q in result["next_questions"])


def test_tao_question_overlay_rejects_new_ids_and_prescriptive_text():
    from backend.llm.dao_client import DaoClient, DaoGenerationConfig

    class UnsafeQuestionDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_question_plan(self, question_context):
            return '{"questions":[{"id":"NEW001","question":"最终诊断是什么？处方如下可以自行服用吗？","reason":"诊断为腰痹"}]}'

    session = CaseGuideSession(use_llm_questions=True, dao_client=UnsafeQuestionDao())
    result = session.start("腰痛")
    assert result["fsm"]["tao_question_runtime"]["fallback_used"] is True
    assert [q["id"] for q in result["next_questions"]] == ["RF001", "RF002", "RF003"]
    assert all("最终诊断" not in q["question"] for q in result["next_questions"])


def test_tao_question_overlay_rejects_harmless_new_ids_without_fallback_leak():
    from backend.llm.dao_client import DaoClient, DaoGenerationConfig

    class InventingQuestionDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))

        def generate_question_plan(self, question_context):
            return '{"questions":[{"id":"NEW001","question":"这是新增问题吗？","reason":"测试新增 id"}]}'

    session = CaseGuideSession(use_llm_questions=True, dao_client=InventingQuestionDao())
    result = session.start("腰痛")
    assert result["fsm"]["tao_question_runtime"]["fallback_used"] is True
    assert result["fsm"]["tao_question_runtime"]["status"] == "fallback"
    assert [q["id"] for q in result["next_questions"]] == ["RF001", "RF002", "RF003"]
    assert all(not q.get("tao_enhanced") for q in result["next_questions"])


def test_caseguide_transition_check_does_not_call_tao_twice_per_answer():
    from backend.llm.dao_client import DaoClient, DaoGenerationConfig

    class CountingQuestionDao(DaoClient):
        def __init__(self):
            super().__init__(DaoGenerationConfig(backend="mock"))
            self.calls = 0

        def generate_question_plan(self, question_context):
            self.calls += 1
            return super().generate_question_plan(question_context)

    dao = CountingQuestionDao()
    session = CaseGuideSession(use_llm_questions=True, dao_client=dao)
    session.start("腰痛")
    assert dao.calls == 1
    session.answer_red_flags({"RF001": "否", "RF002": "否", "RF003": "否"})
    assert dao.calls == 2
