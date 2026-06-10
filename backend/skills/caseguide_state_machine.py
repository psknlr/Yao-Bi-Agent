from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.llm.dao_client import DaoClient

from backend.skills.adaptive_question_planner_skill import adaptive_question_planner_skill
from backend.skills.case_quality_check_skill import case_quality_check_skill
from backend.skills.case_structuring_skill import case_structuring_skill
from backend.skills.caseguide_utils import apply_answer, empty_case_state, get_path, is_missing, load_caseguide_questions
from backend.skills.chief_complaint_skill import chief_complaint_skill
from backend.skills.clinician_handoff_skill import clinician_handoff_skill
from backend.skills.clinician_review_package_skill import clinician_review_package_skill
from backend.skills.cdss_recommendation_skill import cdss_recommendation_skill
from backend.skills.comorbidity_medication_skill import comorbidity_medication_skill
from backend.skills.consent_privacy_skill import consent_privacy_skill
from backend.skills.neuro_ortho_screen_skill import neuro_ortho_screen_skill
from backend.skills.pain_profile_skill import pain_profile_skill
from backend.skills.red_flag_screen_skill import red_flag_screen_skill
from backend.skills.shen_rule_signal_skill import shen_rule_signal_skill
from backend.skills.tcm_four_diagnosis_skill import tcm_four_diagnosis_skill
from backend.skills.tao_question_planner_skill import tao_question_planner_skill
from backend.skills.formula_base_selector_skill import formula_base_selector_skill
from backend.skills.herb_module_composer_skill import herb_module_composer_skill
from backend.skills.safety_guard_skill import safety_guard_skill
from backend.skills.syndrome_router_skill import syndrome_router_skill

STATES = {
    "S0_CONSENT": {"goal": "知情提示、非诊疗声明、隐私脱敏", "next": "S1_REDFLAG"},
    "S1_REDFLAG": {"goal": "排除需要立即就医的危险信号", "next_if_safe": "S2_BASIC", "next_if_danger": "S_EMERGENCY_NOTICE"},
    "S2_BASIC": {"goal": "采集年龄、性别、职业、主诉、病程", "next": "S3_PAIN_PROFILE"},
    "S3_PAIN_PROFILE": {"goal": "采集疼痛部位、性质、程度、诱因、缓解因素", "next": "S4_NEURO_ORTHO"},
    "S4_NEURO_ORTHO": {"goal": "采集放射痛、麻木、无力、大小便、影像诊断", "next": "S5_TCM_CORE"},
    "S5_TCM_CORE": {"goal": "采集中医寒热、湿、气血、舌象、脉象等信息", "next": "S6_SHEN_SIGNAL"},
    "S6_SHEN_SIGNAL": {"goal": "针对沈老经验规则补问高价值变量", "next": "S7_COMORBIDITY"},
    "S7_COMORBIDITY": {"goal": "采集骨质疏松、糖尿病、高血压、NSAIDs、肌松药等", "next": "S8_ADAPTIVE_REPAIR"},
    "S8_ADAPTIVE_REPAIR": {"goal": "动态补问缺失字段", "next": "S9_CASE_SUMMARY"},
    "S9_CASE_SUMMARY": {"goal": "生成医案草稿，让患者确认", "next": "S10_FINAL_REPORT"},
    "S10_FINAL_REPORT": {"goal": "输出标准化医案、标签、规则命中、医生复核清单"},
}

STATE_QUESTION_KEYS = {
    "S1_REDFLAG": "red_flag_questions",
    "S2_BASIC": "chief_complaint_questions",
    "S3_PAIN_PROFILE": "pain_questions",
    "S4_NEURO_ORTHO": "neuro_ortho_questions",
    "S5_TCM_CORE": "tcm_four_diagnosis_questions",
    "S7_COMORBIDITY": "comorbidity_questions",
}

MAX_FOLLOWUPS_PER_STATE = 3

# Per-state override defaults (can be further overridden at session level)
DEFAULT_STATE_MAX_FOLLOWUPS: dict[str, int] = {
    "S1_REDFLAG": 1,   # Red flag screening: only 1 round (all 6 questions asked at once)
    "S2_BASIC": 2,     # Basic info: 2 rounds usually sufficient
    "S6_SHEN_SIGNAL": 3,
    "S8_ADAPTIVE_REPAIR": 3,
}


@dataclass
class CaseGuideSession:
    state: str = "S0_CONSENT"
    case_state: dict[str, Any] | None = None
    max_followups_per_state: int = MAX_FOLLOWUPS_PER_STATE
    # Per-state override: e.g. {"S1_REDFLAG": 1, "S5_TCM_CORE": 4}
    state_max_followups: dict[str, int] | None = None
    use_llm_questions: bool = False
    dao_client: DaoClient | None = None

    def __post_init__(self) -> None:
        if self.case_state is None:
            self.case_state = empty_case_state()
        if self.state_max_followups is None:
            self.state_max_followups = dict(DEFAULT_STATE_MAX_FOLLOWUPS)
        self.case_state.setdefault("fsm", {"state_turn_counts": {}, "last_answers": {}, "last_question_ids": []})

    def effective_max_followups(self, state: str) -> int:
        """Return the effective max followups for a specific state.

        Priority: per-state override > global max_followups_per_state.
        """
        return (self.state_max_followups or {}).get(state, self.max_followups_per_state)

    def start(self, raw_input: str = "", user_role: str = "patient") -> dict[str, Any]:
        consent = consent_privacy_skill(user_role=user_role, raw_input=raw_input)
        self.state = "S1_REDFLAG"
        self._reset_state_turn(self.state)
        return {"state": self.state, **consent, **self._question_payload()}

    def answer_red_flags(self, answers: dict[str, Any], end_state: bool = False) -> dict[str, Any]:
        self._record_turn(answers)
        self.case_state.setdefault("asked_question_ids", [])
        for qid in answers:
            if qid not in self.case_state["asked_question_ids"]:
                self.case_state["asked_question_ids"].append(qid)
        self.case_state.setdefault("answer_evidence", {}).update({qid: {"question": qid, "answer": answer} for qid, answer in answers.items()})
        self.case_state.setdefault("red_flag_answers", {}).update(answers)
        result = red_flag_screen_skill(self.case_state["red_flag_answers"])
        self.case_state["red_flags"] = {"status": result["red_flag_status"], "positive_items": result["positive_flags"]}
        if result["red_flag_status"] == "urgent":
            self.state = "S_EMERGENCY_NOTICE"
        elif end_state or self._state_turn_limit_reached("S1_REDFLAG") or not self._deterministic_next_questions():
            self._advance_state()
        return {"state": self.state, **result, **self._question_payload()}

    def answer_stage(self, answers: dict[str, Any], end_state: bool = False) -> dict[str, Any]:
        self._record_turn(answers)
        if self.state == "S2_BASIC":
            self._apply_basic_answers(answers)
        elif self.state == "S3_PAIN_PROFILE":
            self.case_state = pain_profile_skill(self.case_state, answers)["case_state"]
        elif self.state == "S4_NEURO_ORTHO":
            self.case_state = neuro_ortho_screen_skill(self.case_state, answers)["case_state"]
        elif self.state == "S5_TCM_CORE":
            self.case_state = tcm_four_diagnosis_skill(self.case_state, answers)["case_state"]
        elif self.state == "S6_SHEN_SIGNAL":
            self._apply_any_answers(answers)
            self.case_state = shen_rule_signal_skill(self.case_state)["case_state"]
        elif self.state == "S7_COMORBIDITY":
            self.case_state = comorbidity_medication_skill(self.case_state, answers)["case_state"]
        elif self.state == "S8_ADAPTIVE_REPAIR":
            self._apply_any_answers(answers)
        self._refresh_rule_signals()
        if end_state or self._state_turn_limit_reached(self.state) or not self._deterministic_next_questions():
            self._advance_state()
        return {"state": self.state, "case_state": self.case_state, **self._question_payload()}

    def end_current_state(self, force: bool = False) -> dict[str, Any]:
        """Manual user action: stop asking within the current state and advance.

        Args:
            force: If True, bypass the red-flag guard (used by auto-terminate when
                   pool is exhausted but red flags are all answered as 'safe').
        """
        if self.state == "S1_REDFLAG" and not force:
            red_status = self.case_state.get("red_flags", {}).get("status")
            unanswered_red_flags = bool(self._deterministic_next_questions())
            if red_status == "urgent" or unanswered_red_flags:
                return {
                    "state": self.state,
                    "case_state": self.case_state,
                    **self._question_payload(),
                    "manual_end_accepted": False,
                    "reason": "红旗状态：危险信号仍有未回答项或已触发紧急就医，无法跳过。",
                }
        prev_state = self.state
        self._advance_state()
        return {
            "state": self.state,
            "prev_state": prev_state,
            "case_state": self.case_state,
            **self._question_payload(),
            "manual_end_accepted": True,
        }

    def next_questions(self, max_questions: int = 3) -> list[dict[str, Any]]:
        deterministic = self._deterministic_next_questions(max_questions)
        planned = tao_question_planner_skill(
            self.case_state,
            self.state,
            deterministic,
            rule_context=self.current_rule_context(),
            dao_client=self.dao_client,
            use_llm=self.use_llm_questions,
        )
        self.case_state.setdefault("fsm", {})["tao_question_runtime"] = planned["tao_question_runtime"]
        return planned["questions"]

    def _deterministic_next_questions(self, max_questions: int = 3) -> list[dict[str, Any]]:
        if self.state == "S6_SHEN_SIGNAL":
            self.case_state = shen_rule_signal_skill(self.case_state)["case_state"]
            planner = adaptive_question_planner_skill(self.case_state, max_questions=max_questions, patient_burden_count=self._current_turn_count())
            return [self._enrich_question(item["question"], item.get("reason")) for item in planner["next_questions"]]
        if self.state == "S8_ADAPTIVE_REPAIR":
            planner = adaptive_question_planner_skill(self.case_state, max_questions=max_questions, patient_burden_count=self._current_turn_count())
            return [self._enrich_question(item["question"], item.get("reason")) for item in planner["next_questions"]]
        questions = self._state_questions(self.state)
        asked = set(self.case_state.get("asked_question_ids") or [])
        candidates = []
        for question in questions:
            qid = question.get("id")
            if qid in asked:
                continue
            field = question.get("field")
            if field and not is_missing(get_path(self.case_state, field)):
                continue
            candidates.append(self._enrich_question(question, self._question_reason(question)))
        return candidates[:max_questions]

    def final_report(self) -> dict[str, Any]:
        shen = shen_rule_signal_skill(self.case_state)
        self.case_state = shen["case_state"]
        quality = case_quality_check_skill(self.case_state)
        self.case_state = quality["case_state"]
        normalized_tags = self.case_state.get("normalized_tags", [])
        routed = syndrome_router_skill(normalized_tags)
        formula = formula_base_selector_skill(normalized_tags, routed["syndrome_candidates"])
        modules = herb_module_composer_skill(normalized_tags, formula.get("primary_route"))
        safety = safety_guard_skill({"evidence": {"raw_text": ""}, "red_flags": self.case_state.get("red_flags", {}).get("positive_items", [])}, modules["matched_modules"], normalized_tags)
        structured = case_structuring_skill(self.case_state)
        handoff = clinician_handoff_skill(self.case_state, formula.get("formula_routes"), modules["matched_modules"], safety)
        review_package = clinician_review_package_skill(
            self.case_state,
            routed["syndrome_candidates"],
            formula.get("formula_routes"),
            modules["matched_modules"],
            safety,
        )
        cdss = cdss_recommendation_skill(
            self.case_state,
            routed["syndrome_candidates"],
            formula.get("formula_routes"),
            modules["matched_modules"],
            safety,
            user_role="clinician",
        )
        self.state = "S10_FINAL_REPORT"
        return {"state": self.state, "case_state": self.case_state, "shen_signals": shen["shen_signals"], "high_value_missing": shen["high_value_missing"], **quality, **routed, **formula, **modules, "safety": safety, **structured, **handoff, **review_package, **cdss}

    def _question_payload(self) -> dict[str, Any]:
        effective_max = self.effective_max_followups(self.state)
        turn_index    = self._current_turn_count()
        remaining     = max(0, effective_max - turn_index)
        pool_exhausted = not bool(self._deterministic_next_questions())
        at_limit       = self._state_turn_limit_reached(self.state)
        return {
            "next_questions": self.next_questions(),
            "fsm": {
                "state_goal": STATES.get(self.state, {}).get("goal"),
                "turn_index": turn_index,
                "max_followups_per_state": effective_max,
                "remaining_followups": remaining,
                "pool_exhausted": pool_exhausted,
                "at_limit": at_limit,
                "auto_advance_ready": pool_exhausted or at_limit,
                "can_end_state": self.state in STATES and self.state not in {"S0_CONSENT", "S_EMERGENCY_NOTICE", "S10_FINAL_REPORT"},
                "rule_context": self.current_rule_context(),
                "last_answers": self.case_state.get("fsm", {}).get("last_answers", {}).get(self.state, {}),
                "tao_question_runtime": self.case_state.get("fsm", {}).get("tao_question_runtime"),
            },
        }

    def current_rule_context(self) -> dict[str, Any]:
        tags = sorted(set(self.case_state.get("normalized_tags") or []))
        routed = syndrome_router_skill(tags) if tags else {"syndrome_candidates": [], "rule_hits": []}
        formula = formula_base_selector_skill(tags, routed["syndrome_candidates"]) if tags else {"formula_routes": [], "primary_route": None}
        return {
            "normalized_tags": tags,
            "top_syndrome_candidates": routed.get("syndrome_candidates", [])[:3],
            "top_formula_routes": formula.get("formula_routes", [])[:3],
        }

    def _advance_state(self) -> None:
        if self.state == "S1_REDFLAG":
            self.state = "S2_BASIC"
        else:
            self.state = STATES.get(self.state, {}).get("next", self.state)
        self._reset_state_turn(self.state)

    def _record_turn(self, answers: dict[str, Any]) -> None:
        fsm = self.case_state.setdefault("fsm", {"state_turn_counts": {}, "last_answers": {}, "last_question_ids": []})
        fsm.setdefault("state_turn_counts", {})[self.state] = fsm.setdefault("state_turn_counts", {}).get(self.state, 0) + 1
        fsm.setdefault("last_answers", {})[self.state] = answers
        fsm["last_question_ids"] = list(answers.keys())

    def _reset_state_turn(self, state: str) -> None:
        fsm = self.case_state.setdefault("fsm", {"state_turn_counts": {}, "last_answers": {}, "last_question_ids": []})
        fsm.setdefault("state_turn_counts", {}).setdefault(state, 0)
        fsm.setdefault("last_answers", {}).setdefault(state, {})

    def _current_turn_count(self) -> int:
        return int(self.case_state.get("fsm", {}).get("state_turn_counts", {}).get(self.state, 0))

    def _state_turn_limit_reached(self, state: str) -> bool:
        return int(self.case_state.get("fsm", {}).get("state_turn_counts", {}).get(state, 0)) >= self.effective_max_followups(state)

    def _state_questions(self, state: str) -> list[dict[str, Any]]:
        key = STATE_QUESTION_KEYS.get(state)
        return load_caseguide_questions().get(key, []) if key else []

    def _question_reason(self, question: dict[str, Any]) -> str:
        tags = set(self.case_state.get("normalized_tags") or [])
        field = question.get("field", "unknown")
        if question.get("id", "").startswith("RF"):
            return "红旗筛查优先，任何危险信号都会中止后续普通问诊。"
        if field in {"tcm_inquiry.cold_pain_relation", "tcm_inquiry.cold_extremities"} and tags & {"elderly", "chronic_yabi", "lower_limb_numbness"}:
            return "上一轮提示久病/麻木/高龄线索，本题用于区分寒湿、当归四逆与桂枝芍药知母等路线信号。"
        if field in {"neuro_ortho.numbness", "neuro_ortho.numbness_location", "pain_profile.radiation"}:
            return "根据腰腿痛线索深化神经根受压、麻木和通络规则变量。"
        if field in {"comorbidity.diseases", "neuro_ortho.imaging"}:
            return "用于补足骨质疏松、影像和现代医学风险背景，便于医生复核。"
        return f"补齐 {field}，提升本状态医案质量和规则分流信息量。"

    def _enrich_question(self, question: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        enriched = dict(question)
        enriched["state"] = self.state
        enriched["state_turn_index"] = self._current_turn_count() + 1
        enriched["max_followups_per_state"] = self.effective_max_followups(self.state)
        enriched["reason"] = reason or self._question_reason(question)
        enriched["rule_context"] = self.current_rule_context()
        return enriched

    def _refresh_rule_signals(self) -> None:
        shen = shen_rule_signal_skill(self.case_state)
        self.case_state = shen["case_state"]

    def _apply_basic_answers(self, answers: dict[str, Any]) -> None:
        questions_by_id = {q["id"]: q for q in self._state_questions("S2_BASIC")}
        alias = {"main_symptom": "CC001", "duration": "CC002", "recurrent_status": "CC003"}
        normalized_answers = dict(answers)
        for key, qid in alias.items():
            if key in answers:
                normalized_answers[qid] = answers[key]
        for qid, answer in normalized_answers.items():
            if qid in questions_by_id:
                self.case_state = apply_answer(self.case_state, questions_by_id[qid], answer)
        self.case_state["patient_profile"]["age"] = answers.get("age", self.case_state["patient_profile"].get("age"))
        self.case_state["patient_profile"]["sex"] = answers.get("sex", self.case_state["patient_profile"].get("sex"))
        self.case_state["patient_profile"]["occupation"] = answers.get("occupation", self.case_state["patient_profile"].get("occupation"))
        self.case_state["patient_profile"]["physical_labor"] = answers.get("physical_labor", self.case_state["patient_profile"].get("physical_labor"))
        main_symptom = answers.get("main_symptom", self.case_state["chief_complaint"].get("main_symptom"))
        duration = answers.get("duration", self.case_state["chief_complaint"].get("duration"))
        recurrent_status = answers.get("recurrent_status", self.case_state["chief_complaint"].get("recurrent_status"))
        acute_worsening = answers.get("acute_worsening", self.case_state["chief_complaint"].get("acute_worsening"))
        chief = chief_complaint_skill(
            main_symptom=main_symptom,
            duration=duration,
            recurrent_status=recurrent_status,
            acute_worsening=acute_worsening,
            associated_symptom=answers.get("associated_symptom"),
        )
        self.case_state["chief_complaint"].update({
            "main_symptom": main_symptom,
            "duration": duration,
            "acute_worsening": acute_worsening,
            "recurrent_status": recurrent_status,
            "standard_text": chief["chief_complaint"],
        })
        if isinstance(self.case_state["patient_profile"].get("age"), int):
            age = self.case_state["patient_profile"]["age"]
            if age >= 60:
                self.case_state.setdefault("normalized_tags", []).append("elderly")
            if age >= 73:
                self.case_state.setdefault("normalized_tags", []).append("very_elderly")
        if duration and "年" in str(duration):
            self.case_state.setdefault("normalized_tags", []).extend(["chronic_yabi", "long_duration"])
        self.case_state["normalized_tags"] = sorted(set(self.case_state.get("normalized_tags", [])))

    def _apply_any_answers(self, answers: dict[str, Any]) -> None:
        by_id = {q["id"]: q for values in load_caseguide_questions().values() if isinstance(values, list) for q in values if isinstance(q, dict) and "id" in q}
        for qid, answer in answers.items():
            if qid in by_id:
                self.case_state = apply_answer(self.case_state, by_id[qid], answer)
