"""YaoBi LLM-driven interview agent (Tao 模型 + Yao-Bi 智能体).

A conversational, frontier-style consultation FSM for the lumbar-Bi / low-back-and-leg-pain
domain. Each turn:

    用户自由文本
      → Tao 抽取结构化槽位 (DaoClient.extract_slots)
      → 合并到 YaoBiCaseState
      → 规则红旗筛查 (硬安全门控)
      → FSM 判断阶段 / 缺失槽位
      → 规则引擎给出候选证候 (syndrome_router, 用于鉴别性追问)
      → Tao 自主生成下一轮追问 (DaoClient.generate_interview_question)
      → 信息充分则用 Tao-primary 会诊生成报告 (tao_consultation_skill)

The language model is the primary reasoner (extraction / free-form follow-up / report);
the deterministic rule engine grounds candidate patterns, red flags and the safety floor.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_consultation
from backend.skills.active_questioning import expected_information_gain
from backend.skills.clinical_entity_skill import is_affirmed
from backend.skills.conflict_checker_skill import conflict_checker_skill
from backend.skills.formula_base_selector_skill import formula_base_selector_skill
from backend.skills.herb_module_composer_skill import herb_module_composer_skill
from backend.skills.safety_guard_skill import safety_guard_skill
from backend.skills.syndrome_router_skill import syndrome_router_skill
from backend.skills.tao_consultation_skill import tao_consultation_skill
from backend.skills.uncertainty_skill import uncertainty_skill


class YaoBiState(str, Enum):
    INIT = "INIT"
    SAFETY_TRIAGE = "SAFETY_TRIAGE"
    CHIEF_COMPLAINT = "CHIEF_COMPLAINT"
    PAIN_PROFILE = "PAIN_PROFILE"
    ORTHO_NEURO_SCREEN = "ORTHO_NEURO_SCREEN"
    TCM_PATTERN_COLLECTION = "TCM_PATTERN_COLLECTION"
    PAST_HISTORY = "PAST_HISTORY"
    TARGETED_FOLLOWUP = "TARGETED_FOLLOWUP"
    DECISION_OUTPUT = "DECISION_OUTPUT"
    SAFETY_REFERRAL = "SAFETY_REFERRAL"
    END = "END"


STATE_ORDER = [
    YaoBiState.SAFETY_TRIAGE, YaoBiState.CHIEF_COMPLAINT, YaoBiState.PAIN_PROFILE,
    YaoBiState.ORTHO_NEURO_SCREEN, YaoBiState.TCM_PATTERN_COLLECTION, YaoBiState.PAST_HISTORY,
    YaoBiState.TARGETED_FOLLOWUP,
]

STATE_GOAL = {
    YaoBiState.SAFETY_TRIAGE: "急危重症与红旗筛查",
    YaoBiState.CHIEF_COMPLAINT: "主诉与病程采集",
    YaoBiState.PAIN_PROFILE: "疼痛部位、性质、放射、诱因与缓解",
    YaoBiState.ORTHO_NEURO_SCREEN: "骨伤与神经压迫风险筛查",
    YaoBiState.TCM_PATTERN_COLLECTION: "中医寒热、湿、气血、舌脉等四诊信息",
    YaoBiState.PAST_HISTORY: "既往史、影像与用药史",
    YaoBiState.TARGETED_FOLLOWUP: "围绕候选证候的鉴别性追问",
    YaoBiState.DECISION_OUTPUT: "输出腰痹辅助决策报告",
    YaoBiState.SAFETY_REFERRAL: "高风险线下/急诊转诊提示",
}

# Required slots gating each FSM stage (slot resolved via the per-group dicts on the state).
REQUIRED_SLOTS = {
    YaoBiState.CHIEF_COMPLAINT: [("chief_complaint", None)],
    YaoBiState.PAIN_PROFILE: [("pain_slots", "pain_location"), ("pain_slots", "pain_nature"), ("pain_slots", "radiation")],
    YaoBiState.ORTHO_NEURO_SCREEN: [("pain_slots", "numbness"), ("pain_slots", "weakness")],
    YaoBiState.TCM_PATTERN_COLLECTION: [("tcm_slots", "cold_heat"), ("tcm_slots", "tongue_body"), ("tcm_slots", "pulse")],
    YaoBiState.PAST_HISTORY: [("history_slots", "western_diagnosis")],
}

# Slot → existing rule-engine tag, so the deterministic syndrome rules can score the case.
SLOT_TAGS: list[tuple[tuple[str, str], Any, list[str]]] = [
    (("pain_slots", "numbness"), True, ["lower_limb_numbness"]),
    (("pain_slots", "radiation"), True, ["radiating_leg_pain", "lumbar_leg_pain"]),
    (("pain_slots", "cold_damp_trigger"), True, ["cold_aggravation", "warmth_relieves"]),
    (("pain_slots", "trauma_history"), True, ["strain_or_sprain"]),
    (("pain_slots", "night_pain"), True, ["night_pain"]),
    (("pain_slots", "pain_nature"), "刺痛", ["stabbing_pain"]),
    # Tag names must match the 02_syndrome_rules trigger vocabulary exactly, or the
    # answer never reaches the rule engine (deep_cold_pain feeds R004 肾阳不足).
    (("pain_slots", "pain_nature"), "冷痛", ["deep_cold_pain"]),
    (("tcm_slots", "fixed_pain"), True, ["fixed_pain"]),
    (("tcm_slots", "cold_heat"), "怕冷", ["cold_aversion", "cold_aggravation"]),
    (("tcm_slots", "limb_heaviness"), True, ["white_greasy_coating"]),
    (("tcm_slots", "waist_knee_soreness"), True, ["lumbar_knee_soreness"]),
    (("tcm_slots", "tongue_body"), "暗紫", ["dark_tongue", "purple_tongue"]),
    (("tcm_slots", "tongue_body"), "淡", ["pale_tongue"]),
    (("tcm_slots", "tongue_coating"), "白腻", ["white_greasy_coating"]),
    (("tcm_slots", "tongue_coating"), "黄腻", ["yellow_greasy_coating"]),
    (("history_slots", "osteoporosis"), True, ["osteoporosis"]),
]

# Slot key → union of rule tags it can produce (drives EIG-based question selection).
SLOT_TAG_MAP: dict[str, set[str]] = {}
for (_group, _key), _expected, _mapped in SLOT_TAGS:
    SLOT_TAG_MAP.setdefault(_key, set()).update(_mapped)

# Discriminative slots per candidate pattern (drives the next follow-up target selection).
DISCRIMINATIVE = {
    "寒湿痹阻证": [("tcm_slots", "cold_pain"), ("pain_slots", "cold_damp_trigger"), ("tcm_slots", "tongue_coating")],
    "湿热痹阻证": [("tcm_slots", "tongue_coating"), ("tcm_slots", "thirst"), ("tcm_slots", "urine")],
    "气滞血瘀证": [("tcm_slots", "fixed_pain"), ("pain_slots", "pain_nature"), ("pain_slots", "night_pain")],
    "气血痹阻证": [("pain_slots", "radiation"), ("pain_slots", "numbness"), ("tcm_slots", "tongue_body")],
    "肝肾不足证": [("tcm_slots", "waist_knee_soreness"), ("pain_slots", "duration"), ("tcm_slots", "pulse")],
    "肾阳不足证": [("tcm_slots", "cold_heat"), ("tcm_slots", "urine"), ("tcm_slots", "stool")],
    "少阳证类": [("tcm_slots", "sleep"), ("tcm_slots", "thirst")],
}

SAFETY_SLOTS = [
    ("ortho_neuro_slots", "bowel_bladder_dysfunction"), ("ortho_neuro_slots", "saddle_anesthesia"),
    ("ortho_neuro_slots", "progressive_weakness"), ("ortho_neuro_slots", "severe_trauma"),
    ("ortho_neuro_slots", "fever"), ("ortho_neuro_slots", "tumor_history"),
]

MAX_TURNS = 8

# Deterministic red-flag keyword safety net: the LLM slot extractor is the primary
# channel, but red-flag detection must never depend on it alone — a missed slot key or
# an unexpected phrasing would otherwise let a cauda-equina presentation slip through.
RED_FLAG_TEXT_KEYWORDS: dict[str, list[str]] = {
    "bowel_bladder_dysfunction": ["大小便失禁", "小便失禁", "大便失禁", "尿不出", "解不出小便", "排尿困难", "大小便控制不"],
    "saddle_anesthesia": ["会阴麻木", "会阴部麻", "会阴发麻", "肛周麻木", "鞍区麻木"],
    "progressive_weakness": ["进行性无力", "越来越没力", "腿越来越软", "走路拖脚", "踩棉花感"],
    "severe_trauma": ["车祸", "摔伤", "高处坠落", "重物砸"],
    "fever": ["发烧", "发热", "寒战"],
    "tumor_history": ["肿瘤", "癌症"],
    "unexplained_weight_loss": ["体重下降", "不明原因消瘦"],
}
# String slot values that mean "the patient denied it" — a red-flag slot holding
# "否"/"正常" must not count as positive (the LLM may return strings, not booleans).
_NEGATIVE_SLOT_STRINGS = {"否", "没有", "无", "正常", "不是", "没", "无异常", "否认", "阴性", "false", "no", "none", "unknown"}


def _slot_positive(value: Any) -> bool:
    """Truthiness for red-flag slots that treats denial strings as negative."""

    if isinstance(value, str):
        v = value.strip().lower()
        return bool(v) and v not in _NEGATIVE_SLOT_STRINGS and not v.startswith(("否", "没", "无", "不", "未"))
    return bool(value)


def _as_term_list(value: Any) -> list[str]:
    """Normalize an LLM slot value into a clean term list for interaction matching.

    The extractor is asked for lists but may return a bare string ("高血压、糖尿病") —
    split on common Chinese separators instead of crashing on list concatenation.
    """

    if value in (None, "", [], "unknown"):
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in re.split(r"[，、,;；/和及\s]+", value)]
        return [p for p in parts if p and p not in _NEGATIVE_SLOT_STRINGS]
    if isinstance(value, list):
        terms: list[str] = []
        for item in value:
            terms.extend(_as_term_list(item))
        return terms
    return [str(value)]


@dataclass
class YaoBiCaseState:
    session_id: str
    state: YaoBiState = YaoBiState.INIT
    turn_count: int = 0
    dialogue_history: list[dict[str, str]] = field(default_factory=list)
    demographics: dict[str, Any] = field(default_factory=dict)
    chief_complaint: Any = None
    pain_slots: dict[str, Any] = field(default_factory=dict)
    ortho_neuro_slots: dict[str, Any] = field(default_factory=dict)
    tcm_slots: dict[str, Any] = field(default_factory=dict)
    history_slots: dict[str, Any] = field(default_factory=dict)
    red_flags: list[str] = field(default_factory=list)
    safety_level: str = "unknown"
    candidate_patterns: list[dict[str, Any]] = field(default_factory=list)
    uncertainty_score: float = 1.0
    # Physician confirmation/revision/override of the safety referral.
    physician_review: dict[str, Any] = field(default_factory=dict)
    # SCOPED overrides (v0.12): each entry suppresses only the SPECIFIC flags the
    # physician reviewed, at the state they reviewed them. New red flags appearing
    # later (new urinary retention, fever, trauma…) MUST alarm again — the old
    # session-wide `red_flags_overridden: bool` was a permanent global bypass.
    red_flag_overrides: list[dict[str, Any]] = field(default_factory=list)
    # Two-phase override approval in flight (see backend/runtime/approvals.py).
    pending_approval: dict[str, Any] = field(default_factory=dict)

    def overridden_flags(self) -> set[str]:
        out: set[str] = set()
        for override in self.red_flag_overrides:
            out |= set(override.get("overridden_flags") or [])
        return out
    # EIG ranking of the latest follow-up targets (BED-style question selection audit).
    last_question_selection: list[dict[str, Any]] = field(default_factory=list)

    def group(self, name: str) -> dict[str, Any]:
        return getattr(self, name)

    def slot(self, group: str, key: str | None) -> Any:
        return self.chief_complaint if key is None else self.group(group).get(key)


class YaoBiInterviewEngine:
    def __init__(self, dao_client: DaoClient | None = None, use_llm: bool = False) -> None:
        self.dao_client = dao_client
        self.use_llm = use_llm

    # -- main turn ------------------------------------------------------------
    def run_turn(self, case: YaoBiCaseState, user_text: str) -> dict[str, Any]:
        user_text = (user_text or "").strip()
        if case.state == YaoBiState.INIT:
            case.state = YaoBiState.SAFETY_TRIAGE
        if user_text:
            case.dialogue_history.append({"role": "user", "content": user_text})
            case.turn_count += 1
            # SAFETY BEFORE ANY MODEL CALL (v0.14 review P0-7): the deterministic
            # red-flag scan + shared kernel run FIRST on the raw text. An emergency
            # turn (a) answers immediately from local rules without waiting on a
            # model and (b) never sends the emergency narrative to an LLM at all —
            # slot extraction is skipped for this turn.
            self._scan_red_flag_text(case, user_text)
            self._detect_red_flags(case)
            if case.safety_level == "emergency":
                case.state = YaoBiState.SAFETY_REFERRAL
                referral = self._build_referral(case)
                message = referral["message"]
                case.dialogue_history.append({"role": "assistant", "content": message})
                return self._pack(case, message, done=True, referral=referral)
            self._merge(case, self._extract(user_text))
            # Deterministic safety net re-run: LLM extraction may have surfaced red
            # flags the keyword scan missed (paraphrases into structured slots).
            self._scan_red_flag_text(case, user_text)

        self._detect_red_flags(case)
        if case.safety_level in ("high", "emergency"):
            case.state = YaoBiState.SAFETY_REFERRAL
            referral = self._build_referral(case)
            message = referral["message"]
            case.dialogue_history.append({"role": "assistant", "content": message})
            # Emergency (cauda equina) = hard terminal stop; high = advisory, user can clarify.
            return self._pack(case, message, done=(case.safety_level == "emergency"), referral=referral)

        case.state = self._transition(case)
        case.candidate_patterns, case.uncertainty_score = self._infer_patterns(case)

        if self._is_sufficient(case):
            report = self._build_report(case)
            case.state = YaoBiState.END
            case.dialogue_history.append({"role": "assistant", "content": report["answer"]})
            return self._pack(case, report["answer"], done=True, report=report)

        targets = self._select_target_slots(case)
        question = self._ask(case, targets)
        case.dialogue_history.append({"role": "assistant", "content": question})
        return self._pack(case, question, done=False, target_slots=targets)

    # -- Tao calls ------------------------------------------------------------
    def _extract(self, user_text: str) -> dict[str, Any]:
        if not self.use_llm:
            return {}
        client = self.dao_client or DaoClient()
        try:
            parsed, _ = loads_with_repair(client.extract_slots(user_text))
            return parsed if isinstance(parsed, dict) else {}
        except (DaoRuntimeError, JsonRepairError, ValueError, TypeError):
            return {}

    def _ask(self, case: YaoBiCaseState, targets: list[str]) -> str:
        deterministic = self._rule_question(case, targets)
        if not self.use_llm:
            return deterministic
        client = self.dao_client or DaoClient()
        try:
            text = (client.generate_interview_question({
                "stage": case.state.value,
                "stage_goal": STATE_GOAL.get(case.state, ""),
                "target_slots": targets,
                "candidate_patterns": case.candidate_patterns[:4],
                "case_summary": self._summary(case),
            }) or "").strip()
            return text or deterministic
        except DaoRuntimeError:
            return deterministic

    def _build_report(self, case: YaoBiCaseState) -> dict[str, Any]:
        # Full deterministic evidence bundle: the report scope covers 证型→治法→方药→随访,
        # so the model must be grounded in the rule engine's formula routes, herb modules
        # and safety review — not just syndrome candidates.
        tags = self._tags(case)
        candidates = [
            # Raw rule scores (uncertainty thresholds are calibrated on the raw scale;
            # prob*10 would fabricate scores and flip abstention verdicts).
            {"name": p["pattern"], "score": p.get("score") or 0, "evidence_tags": p.get("evidence", [])}
            for p in case.candidate_patterns
        ]
        formula = formula_base_selector_skill(tags, candidates)
        routes = formula.get("formula_routes") or []
        modules = herb_module_composer_skill(tags, formula.get("primary_route"))
        matched = modules.get("matched_modules") or []
        safety = safety_guard_skill({"evidence": {"raw_text": self._case_text(case)}, "red_flags": case.red_flags}, matched, tags)
        uncertainty = uncertainty_skill(candidates, tags)["uncertainty"]
        history = case.history_slots
        interactions = conflict_checker_skill(
            matched, formula.get("primary_route"),
            medications=_as_term_list(history.get("medication_history")),
            # Only structured comorbidity terms — never the free-text western_diagnosis
            # sentence, whose embedded negations ("没有高血压") would false-fire
            # interruptive contraindication alerts via substring matching.
            conditions=_as_term_list(history.get("comorbidities")),
        )
        evidence = {
            "normalized_tags": tags,
            "syndrome_candidates": candidates,
            "formula_routes": [{"name": r["name"], "confidence": r.get("confidence"), "score": r.get("score")} for r in routes[:4]],
            "herb_modules": [{"name": m["name"], "role": m.get("role"), "herbs": (m.get("herbs") or [])[:6]} for m in matched[:6]],
            "safety": {"status": safety.get("safety_status"), "risks": safety.get("medication_risks") or []},
            "interaction_alerts": [
                {"level": a.get("alert_level"), "description": a.get("description")}
                for a in (interactions.get("interaction_alerts") or [])[:5]
            ],
            # Epistemic self-assessment: the model must voice (not hide) low separation or abstention.
            "uncertainty": {
                "abstain": uncertainty.get("abstain"),
                "assessment_note": uncertainty.get("assessment_note"),
                "differential_gaps": [g.get("suggestion") for g in uncertainty.get("differential_gaps") or []],
            },
            "case_state": self._summary(case),
            "red_flags": case.red_flags,
        }
        res = tao_consultation_skill(
            self._case_text(case), "腰痹全面会诊：证型→治法→方药→康复→随访（结合沈氏经验）",
            evidence, fallback_text=self._rule_report(case),
            dao_client=self.dao_client, use_llm=self.use_llm, user_role="clinician",
        )
        # Surface the tiered medication-safety verdict on the API payload itself —
        # an interruptive alert must reach the physician UI, not just the LLM prompt.
        res["interaction_alerts"] = interactions.get("interaction_alerts") or []
        res["alert_summary"] = interactions.get("alert_summary") or {}
        res["uncertainty"] = uncertainty
        return res

    # -- rules / grounding ----------------------------------------------------
    def _merge(self, case: YaoBiCaseState, extracted: dict[str, Any]) -> None:
        if not isinstance(extracted, dict):
            return
        if extracted.get("chief_complaint") and not case.chief_complaint:
            case.chief_complaint = extracted["chief_complaint"]
        for group in ("demographics", "pain_slots", "ortho_neuro_slots", "tcm_slots", "history_slots"):
            incoming = extracted.get(group)
            if isinstance(incoming, dict):
                for key, value in incoming.items():
                    if value not in (None, "", [], "unknown"):
                        case.group(group)[key] = value

    def _scan_red_flag_text(self, case: YaoBiCaseState, user_text: str) -> None:
        """Deterministic keyword scan for red flags (union with LLM slot extraction).

        Polarity resolution is shared with the extraction pipeline
        (``clinical_entity_skill``): denials ("没有大小便失禁"、"大小便正常") and
        questions ("会不会发热？") never set a red-flag slot — only affirmed
        mentions do.
        """

        for slot, keywords in RED_FLAG_TEXT_KEYWORDS.items():
            if _slot_positive(case.ortho_neuro_slots.get(slot)):
                continue
            if any(is_affirmed(user_text, keyword) for keyword in keywords):
                case.ortho_neuro_slots[slot] = True

    def _user_narrative(self, case: YaoBiCaseState) -> str:
        return "。".join(d["content"] for d in case.dialogue_history if d.get("role") == "user")

    @staticmethod
    def _safety_fact_digest(case: YaoBiCaseState) -> str:
        """Digest of the safety-relevant facts an override approval binds to.

        Any new user turn, red-flag change or safety-level change alters the digest,
        so a confirm that arrives after the case moved on is invalidated instead of
        applying a stale physician decision (v0.14 approval fact binding).
        """

        import hashlib as _hashlib

        basis = "|".join([
            ";".join(sorted(str(f) for f in case.red_flags)),
            str(case.safety_level),
            str(case.turn_count),
        ])
        return _hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def _detect_red_flags(self, case: YaoBiCaseState) -> None:
        o, p, h = case.ortho_neuro_slots, case.pain_slots, case.history_slots
        # (flag_text, is_emergency): keeping the level per-flag lets scoped overrides
        # recompute the emergency status over the REMAINING flags only.
        found: list[tuple[str, bool]] = []

        # SHARED SAFETY KERNEL (v0.11): the interview must grade red flags with the
        # same category-tiered, temporality- and experiencer-aware kernel as every
        # other entry — its own slot logic below stays as an *additional* channel,
        # never the only one. This is what previously let 高能量创伤/发热/开放骨折
        # reach only "high" here while the pipeline hard-halted (entry review P0-2).
        try:
            from backend.skills.case_extract_skill import case_extract_skill
            from backend.skills.case_normalize_skill import case_normalize_skill
            from backend.skills.safety_guard_skill import emergency_halt_required, safety_guard_skill

            narrative = self._user_narrative(case)
            if narrative.strip():
                case_json = case_extract_skill(narrative)
                normalized_tags = case_normalize_skill(case_json).get("normalized_tags") or []
                kernel = safety_guard_skill(case_json, None, normalized_tags)
                kernel_emergency = emergency_halt_required(kernel)
                if kernel.get("safety_status") == "urgent":
                    for f in kernel.get("confirmed_red_flags") or []:
                        found.append((f.get("message") or str(f.get("term")), kernel_emergency))
        except Exception:
            # FAIL CLOSED: if the shared kernel crashes, the interview must not
            # silently continue as if the case were safe.
            found.append(("安全内核解析异常，本轮按高风险处理，请线下评估（已记录待人工复核）。", False))
        # Cauda equina syndrome: hard stop, must go to ER immediately.
        # _slot_positive keeps a denial string ("否"/"正常") from firing a false emergency.
        if _slot_positive(o.get("bowel_bladder_dysfunction")):
            found.append(("大小便功能异常，需警惕马尾神经受压 → 立即急诊", True))
        if _slot_positive(o.get("saddle_anesthesia")):
            found.append(("会阴区麻木，需警惕马尾神经受压 → 立即急诊", True))
        # High-risk: serious but can be clarified/corrected by the user.
        if _slot_positive(o.get("progressive_weakness")):
            found.append(("进行性下肢无力，需排查神经受压", False))
        if _slot_positive(o.get("severe_trauma")):
            found.append(("明显外伤后腰痛，需排查骨折", False))
        if _slot_positive(h.get("osteoporosis")) and (_slot_positive(p.get("pain_severity")) or _slot_positive(o.get("severe_trauma"))):
            found.append(("骨质疏松合并急性剧痛，需排查压缩性骨折", False))
        if _slot_positive(o.get("fever")):
            found.append(("发热合并腰背痛，需排查感染", False))
        if _slot_positive(o.get("tumor_history")) and _slot_positive(p.get("night_pain")):
            found.append(("肿瘤病史合并夜间痛，需排查转移", False))
        if _slot_positive(o.get("unexplained_weight_loss")):
            found.append(("不明原因消瘦，需排查肿瘤", False))

        # SCOPED override consumption (v0.12): suppress only the specific flags the
        # physician reviewed; a flag NOT in the overridden set — i.e. newly appearing
        # evidence — alarms again, and the emergency status is recomputed over the
        # REMAINING flags only. An override of "会阴麻木" cannot silence a later
        # "发热寒战" or a new trauma.
        overridden = case.overridden_flags()
        seen: set[str] = set()
        remaining: list[tuple[str, bool]] = []
        suppressed = 0
        for text, is_emergency in found:
            if text in seen:
                continue
            seen.add(text)
            if text in overridden:
                suppressed += 1
            else:
                remaining.append((text, is_emergency))
        flags = [text for text, _ in remaining]
        if suppressed and flags:
            flags.append(f"（另有 {suppressed} 项红旗已由医师审批覆盖；以上为覆盖后仍在报警的线索）")
        emergency = any(is_emergency for _, is_emergency in remaining)
        case.red_flags = flags
        case.safety_level = "emergency" if emergency else ("high" if flags else "low")

    def _has_missing(self, case: YaoBiCaseState, state: YaoBiState) -> bool:
        for group, key in REQUIRED_SLOTS.get(state, []):
            if case.slot(group, key) in (None, "", [], "unknown"):
                return True
        return False

    def _transition(self, case: YaoBiCaseState) -> YaoBiState:
        if case.state in (YaoBiState.END, YaoBiState.DECISION_OUTPUT):
            return case.state
        # advance through the ordered stages, stopping at the first with missing required slots
        for state in STATE_ORDER:
            if state == YaoBiState.TARGETED_FOLLOWUP:
                return YaoBiState.TARGETED_FOLLOWUP
            if self._has_missing(case, state):
                return state
        return YaoBiState.TARGETED_FOLLOWUP

    def _tags(self, case: YaoBiCaseState) -> list[str]:
        tags: set[str] = set()
        for (group, key), expected, mapped in SLOT_TAGS:
            value = case.slot(group, key)
            if value is None:
                continue
            if expected is True and value not in (False, None, "", "否"):
                tags.update(mapped)
            elif isinstance(expected, str) and isinstance(value, str) and expected in value:
                tags.update(mapped)
        age = case.demographics.get("age")
        if isinstance(age, (int, float)) and age >= 60:
            tags.add("elderly")
        dur = str((case.pain_slots.get("duration") or ""))
        if "年" in dur:
            tags.update({"chronic_yabi", "long_duration"})
        if case.pain_slots.get("pain_location"):
            tags.add("lumbar_pain")
        return sorted(tags)

    def _infer_patterns(self, case: YaoBiCaseState) -> tuple[list[dict[str, Any]], float]:
        cands = syndrome_router_skill(self._tags(case)).get("syndrome_candidates") or []
        scores = [max(0.1, float(c.get("score") or 1)) for c in cands]
        total = sum(scores) or 1.0
        patterns = [
            # Keep the raw rule score alongside the share-normalized prob: downstream
            # uncertainty thresholds are calibrated on raw scores, not probabilities.
            {"pattern": c["name"], "prob": round(s / total, 3), "score": int(c.get("score") or 0), "evidence": c.get("evidence_tags", [])}
            for c, s in zip(cands, scores)
        ]
        probs = [p["prob"] for p in patterns if p["prob"] > 0]
        if probs:
            entropy = -sum(p * math.log(p + 1e-9) for p in probs)
            uncertainty = entropy / (math.log(len(probs)) + 1e-9) if len(probs) > 1 else 0.3
        else:
            uncertainty = 1.0
        return patterns, round(uncertainty, 3)

    def _select_target_slots(self, case: YaoBiCaseState) -> list[str]:
        unanswered = lambda pairs: [(k or g) for g, k in pairs if case.slot(g, k) in (None, "", [], "unknown")]
        # 1) required slots of the current FSM stage
        req = unanswered(REQUIRED_SLOTS.get(case.state, []))
        # 2) always make sure the core safety slots are covered
        safety = unanswered(SAFETY_SLOTS)
        # 3) discriminative slots for the leading candidate patterns, re-ranked by
        # expected information gain (BED-style active questioning): ask what most
        # reduces the entropy of the syndrome posterior, not a fixed slot order.
        disc_pairs: list[tuple[str, str]] = []
        for pattern in case.candidate_patterns[:3]:
            disc_pairs += DISCRIMINATIVE.get(pattern["pattern"], [])
        disc = unanswered(disc_pairs)
        eig_ranked = expected_information_gain(case.candidate_patterns, SLOT_TAG_MAP, disc)
        case.last_question_selection = [
            {"slot": item["slot"], "eig_bits": item["eig_bits"]} for item in eig_ranked[:6]
        ]
        disc = [item["slot"] for item in eig_ranked]
        ordered: list[str] = []
        for key in req + safety[:2] + disc:
            if key not in ordered:
                ordered.append(key)
        return ordered[:4] or ["pain_nature", "cold_heat", "tongue_body"]

    def _is_sufficient(self, case: YaoBiCaseState) -> bool:
        if case.turn_count >= MAX_TURNS:
            return True
        if case.state != YaoBiState.TARGETED_FOLLOWUP:
            return False
        if not case.candidate_patterns:
            return False
        top = case.candidate_patterns[0]["prob"]
        second = case.candidate_patterns[1]["prob"] if len(case.candidate_patterns) > 1 else 0.0
        clear = top > 0.5 and (top - second) > 0.12
        return clear or case.uncertainty_score < 0.5

    # -- deterministic fallbacks / formatting ---------------------------------
    def _rule_question(self, case: YaoBiCaseState, targets: list[str]) -> str:
        phrase = {
            "chief_complaint": "目前最主要的不适是什么、痛了多久了",
            "pain_location": "疼痛主要在腰部、臀部还是下肢", "pain_nature": "疼痛是酸痛、胀痛、刺痛还是冷痛",
            "radiation": "腰痛会不会向腿脚放射", "numbness": "腿脚有没有发麻", "weakness": "腿有没有发软无力",
            "cold_damp_trigger": "受凉或阴雨天会不会加重", "duration": "这次痛多久了",
            "cold_heat": "平时怕冷还是怕热", "tongue_body": "舌质偏淡、偏红还是偏暗紫",
            "tongue_coating": "舌苔是薄白、白腻还是黄腻", "pulse": "脉象偏细、偏弦还是偏沉",
            "waist_knee_soreness": "有没有腰膝酸软", "western_diagnosis": "之前医院诊断过什么",
            "bowel_bladder_dysfunction": "大小便是否正常", "fever": "有没有发热",
            "saddle_anesthesia": "会阴部有没有发麻", "progressive_weakness": "下肢无力是否进行性加重",
            "tumor_history": "有没有肿瘤病史或近期消瘦", "night_pain": "夜里痛得明显吗",
            "trauma_history": "近期有没有外伤", "stool": "大便情况", "urine": "小便清还是黄",
            "limb_heaviness": "肢体是否困重", "thirst": "口干口渴吗", "appetite": "胃口怎么样",
        }
        qs = [phrase.get(t, t) for t in targets][:4]
        return "想再了解几点：" + "；".join(qs) + "？"

    def _summary(self, case: YaoBiCaseState) -> dict[str, Any]:
        return {
            "demographics": case.demographics, "chief_complaint": case.chief_complaint,
            "pain": case.pain_slots, "ortho_neuro": case.ortho_neuro_slots,
            "tcm": case.tcm_slots, "history": case.history_slots,
        }

    def _case_text(self, case: YaoBiCaseState) -> str:
        return "；".join(m["content"] for m in case.dialogue_history if m["role"] == "user") or str(case.chief_complaint or "腰痛")

    def _rule_report(self, case: YaoBiCaseState) -> str:
        top = case.candidate_patterns[0]["pattern"] if case.candidate_patterns else "信息待补充"
        lines = [
            "# 腰痹问诊小结（确定性规则）", "",
            f"- 主诉：{case.chief_complaint or '腰痛'}",
            f"- 候选证候（倾向）：{('、'.join(p['pattern'] for p in case.candidate_patterns[:3]) or '待补充')}",
            f"- 安全状态：{case.safety_level}",
            "", "> 供执业医师审核，非最终诊断/处方。",
        ]
        return "\n".join(lines)

    def _build_referral(self, case: YaoBiCaseState) -> dict[str, Any]:
        """Rule-based safety warning + optional Tao emergency clinical guidance.

        EMERGENCY IS MODEL-FREE (v0.14): at ``safety_level == "emergency"`` the full
        referral — warning AND clinician guidance — renders from local deterministic
        templates. No model call may delay the emergency response, fail it, or
        receive the emergency narrative. The LLM guidance overlay remains available
        only for the advisory ("high") level, where the deterministic warning has
        already been produced and urgency is not model-dependent.
        """

        if case.safety_level == "emergency":
            action = "**请立即拨打急救电话（120）或前往最近急诊科**，本问诊终止常规辨证。"
        else:
            action = "建议尽快线下骨科/脊柱外科或急诊评估，本问诊暂停常规辨证。"
        base = "检测到需要重点排查的危险信号：\n- " + "\n- ".join(case.red_flags) + f"\n\n{action}"

        if case.safety_level == "emergency":
            from backend.llm.dao_client import deterministic_emergency_guidance

            guidance = deterministic_emergency_guidance(case.red_flags, "emergency")
            return {
                "message": base + "\n\n---\n\n" + guidance, "tao_guidance": None,
                "used_llm": False, "source": "deterministic_rules_emergency",
            }
        if not self.use_llm:
            return {"message": base, "tao_guidance": None, "used_llm": False, "source": "deterministic_rules"}

        client = self.dao_client or DaoClient()
        try:
            raw = client.generate_emergency_referral({
                "red_flags": case.red_flags,
                "safety_level": case.safety_level,
                "case_summary": self._summary(case),
            })
            text = (raw or "").strip()
            guard = guard_consultation(text, "clinician")
            if text and guard["allowed"]:
                full_message = base + "\n\n---\n\n" + text
                return {"message": full_message, "tao_guidance": text, "used_llm": True, "source": "tao_emergency_guidance"}
        except DaoRuntimeError:
            pass
        return {"message": base, "tao_guidance": None, "used_llm": False, "source": "deterministic_rules_fallback"}

    def run_review(
        self,
        case: "YaoBiCaseState",
        action: str,
        physician_notes: str = "",
        override_reason: str = "",
        reviewer_id: str = "",
        confirm_override: bool = False,
        approval_id: str = "",
    ) -> dict[str, Any]:
        """Physician confirmation / revision / override of the safety referral.

        ``confirm``  — physician endorses the referral; interview remains stopped (done=True).
        ``revise``   — physician amends with notes; interview remains stopped (done=True).
        ``override`` — the highest-risk action in the system (clears confirmed red flags,
        resumes questioning). It is a first-class two-phase approval, not a string flag:
        phase 1 requires ``reviewer_id`` + a non-empty reason and creates a *pending*
        ApprovalRequest without changing clinical state; phase 2 re-submits with the
        ``approval_id`` and ``confirm_override=True`` from the *same* reviewer, which
        marks the approval approved (audited) and only then executes the override.
        """

        ts = int(time.time())
        reviewer = (reviewer_id or "").strip()
        # Confirming or revising an emergency referral is a physician act with clinical
        # accountability — anonymous confirmation is not allowed (v0.12 P0-7). The
        # reviewer id arrives from the server's AUTHENTICATED subject, never from the
        # request body.
        if action in {"confirm", "revise"} and not reviewer:
            message = "⚠️ 医师确认/修订需要已认证的医师身份（reviewer 由服务端认证派生），未执行任何更改。"
            pack = self._pack(case, message, done=False)
            pack["approval_error"] = "authenticated_reviewer_required"
            return pack

        if action == "confirm":
            case.physician_review = {
                "status": "confirmed",
                "physician_notes": physician_notes or "",
                "reviewer_id": reviewer,
                "reviewed_at": ts,
            }
            note = f"（备注：{physician_notes}）" if physician_notes else ""
            message = f"医师（{reviewer}）已确认急诊转诊建议。{note}"
            case.dialogue_history.append({"role": "physician", "content": message})
            return self._pack(case, message, done=True)

        if action == "revise":
            if not (physician_notes or "").strip():
                message = "⚠️ 修订转诊建议必须附修订理由/备注，未执行任何更改。"
                pack = self._pack(case, message, done=False)
                pack["approval_error"] = "revision_notes_required"
                return pack
            case.physician_review = {
                "status": "revised",
                "physician_notes": physician_notes,
                "reviewer_id": reviewer,
                "reviewed_at": ts,
            }
            message = f"医师（{reviewer}）已修订转诊建议。\n\n**医师备注：**{physician_notes}"
            case.dialogue_history.append({"role": "physician", "content": message})
            return self._pack(case, message, done=True)

        if action == "override":
            from backend.runtime.approvals import AuditWriteError, get_approval_manager

            reason = (override_reason or "").strip()
            if not reviewer or not reason:
                message = (
                    "⚠️ 覆盖红旗评估为高风险操作，未执行任何更改。"
                    "必须同时提供 reviewer_id（医师工号/ID，审计归责）与具体覆盖理由。"
                )
                pack = self._pack(case, message, done=False)
                pack["approval_error"] = "reviewer_id_and_reason_required"
                return pack

            manager = get_approval_manager()
            pending = case.pending_approval or {}
            if confirm_override and approval_id and pending.get("approval_id") == approval_id:
                try:
                    decided = manager.decide(approval_id, decision="approve", reviewer_id=reviewer,
                                             current_case_digest=self._safety_fact_digest(case))
                except AuditWriteError:
                    # Fail closed: an override that cannot be audited is not committed.
                    message = "⚠️ 审计写入失败：高风险覆盖不予执行（fail-closed），请联系管理员检查审计存储。"
                    pack = self._pack(case, message, done=False)
                    pack["approval_error"] = "audit_unavailable_high_risk_denied"
                    return pack
                if decided is not None and decided.status == "invalidated_by_new_facts":
                    case.pending_approval = {}
                    message = (
                        "⚠️ 覆盖审批已失效：申请后病例出现了新的临床事实（新红旗/新主诉），"
                        "原审批不再适用。请基于当前病例重新评估并重新发起覆盖申请。"
                    )
                    pack = self._pack(case, message, done=False)
                    pack["approval_error"] = "approval_invalidated_by_new_facts"
                    return pack
                if decided is None or decided.status != "approved":
                    message = "⚠️ 覆盖确认失败：审批不存在、已决、确认人不符合审批人策略，未执行任何更改。"
                    pack = self._pack(case, message, done=False)
                    pack["approval_error"] = "approval_confirmation_failed"
                    return pack
                overridden_flags = list(case.red_flags)
                case.pending_approval = {}
                case.physician_review = {
                    "status": "overridden",
                    "override_reason": reason,
                    "physician_notes": physician_notes,
                    "reviewer_id": reviewer,
                    "approval_id": approval_id,
                    "overridden_red_flags": overridden_flags,
                    "reviewed_at": ts,
                }
                # SCOPED override (v0.12): only the flags the physician actually
                # reviewed are suppressed, bound to this approval and the state at
                # review time. Red-flag detection keeps running — new findings
                # (new retention, fever, trauma) alarm again.
                case.red_flag_overrides.append({
                    "approval_id": approval_id,
                    "reviewer_id": reviewer,
                    "overridden_flags": overridden_flags,
                    "approved_turn_count": case.turn_count,
                    "scope": "existing_evidence_only",
                    "reviewed_at": ts,
                })
                case.red_flags = []
                case.safety_level = "low"
                override_msg = (
                    f"医师（{reviewer}）已二次确认覆盖红旗评估，恢复问诊。\n**覆盖理由：**{reason}"
                )
                case.dialogue_history.append({"role": "physician", "content": override_msg})
                # Resume the FSM — empty text triggers no slot extraction, just the next question.
                return self.run_turn(case, "")

            # Phase 1: create the pending approval; clinical state is untouched.
            try:
                request = manager.create(
                    action_type="override_emergency_referral",
                    session_id=case.session_id,
                    reviewer_id=reviewer,
                    reason=reason,
                    payload={"red_flags": list(case.red_flags), "safety_level": case.safety_level},
                    # Fact binding (v0.14): the approval is only valid against the
                    # exact safety facts the physician reviewed — new facts void it.
                    case_digest=self._safety_fact_digest(case),
                )
            except AuditWriteError:
                message = "⚠️ 审计写入失败：无法创建高风险覆盖审批（fail-closed），请联系管理员检查审计存储。"
                pack = self._pack(case, message, done=False)
                pack["approval_error"] = "audit_unavailable_high_risk_denied"
                return pack
            case.pending_approval = {
                "approval_id": request.approval_id,
                "action_type": request.action_type,
                "reviewer_id": reviewer,
            }
            message = (
                "🔐 覆盖红旗评估需二次确认（高风险审批已创建，红旗状态未变更）。\n"
                f"审批号：{request.approval_id}。请同一医师携带该审批号并附 confirm_override=true 再次提交以执行覆盖。"
            )
            pack = self._pack(case, message, done=False)
            pack["pending_approval"] = request.to_dict()
            return pack

        message = f"未知医师操作：{action}"
        return self._pack(case, message, done=False)

    def _pack(self, case: YaoBiCaseState, message: str, *, done: bool, report: dict[str, Any] | None = None, target_slots: list[str] | None = None, referral: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "session_id": case.session_id,
            "message": message,
            "state": case.state.value,
            "state_goal": STATE_GOAL.get(case.state, ""),
            "turn_count": case.turn_count,
            "safety_level": case.safety_level,
            "red_flags": case.red_flags,
            "candidate_patterns": case.candidate_patterns,
            "uncertainty": case.uncertainty_score,
            "case_summary": self._summary(case),
            "target_slots": target_slots or [],
            # EIG audit trail: why these follow-ups were chosen (bits of expected
            # entropy reduction over the syndrome posterior).
            "question_selection": case.last_question_selection,
            "done": done,
            "report": report["answer"] if report else None,
            "report_source": (report.get("source") if report else None),
            "used_llm": self.use_llm,
            # Emergency referral details (present when state == SAFETY_REFERRAL).
            "referral": referral,
            "referral_tao_guidance": (referral or {}).get("tao_guidance"),
            # Physician review state (persists across turns once set).
            "physician_review": case.physician_review,
            "physician_review_required": case.state == YaoBiState.SAFETY_REFERRAL and not case.physician_review.get("status"),
        }
