"""Clinical scope router: is this case inside the system's approved task domain?

The system is a *lumbar Bi (腰痹) experience CDSS prototype*. Before any TCM reasoning,
every case must be routed: chest pain, vascular emergencies, open fractures, cervical
myelopathy and non-lumbar orthopedic complaints must never fall through into the lumbar
formula chain just because the pipeline exists. The emergency safety kernel
(red-flag gate) runs FIRST and owns resuscitation-level findings; this router then
decides whether the remaining, non-emergency case is in scope for lumbar-Bi syndrome
support or should be referred with education/triage-only output.

Priority order (v0.11 — a bare 腰 anchor no longer wins over a fracture/post-op
context): emergency > fracture/dislocation & post-operative follow-up > lumbar Bi >
other joint region > unknown. "腰椎压缩性骨折术后复查" is a fracture-follow-up task
(imaging, healing, complications), not ordinary lumbar-Bi differentiation — the lumbar
formula capability is explicitly blocked with a reason code.

Deterministic, keyword-anchored and deliberately conservative: when no lumbar anchor is
present the case is out of scope — safety triage is the only always-allowed capability.
"""

from __future__ import annotations

from typing import Any

from backend.skills.clinical_entity_skill import is_affirmed, scan_term
from backend.skills.safety_guard_skill import EMERGENCY_HALT_CATEGORIES

# Anchors that put a case inside the lumbar-Bi domain (腰 covers 腰痛/腰背/腰椎/腰膝…).
_LUMBAR_ANCHORS = ("腰", "坐骨神经", "臀部放射")

# Non-lumbar body-region anchors → the case belongs to another orthopedic domain.
_JOINT_ANCHORS = ("膝", "肩", "肘", "腕", "踝", "髋", "颈椎", "颈部", "手指", "足跟", "足踝")

# Fracture / dislocation / post-operative context: even with a lumbar anchor present,
# the clinical task is fracture care & post-op surveillance, not lumbar-Bi formulas.
_FRACTURE_POSTOP_ANCHORS = ("骨折", "脱位", "术后", "内固定", "椎体成形", "钢板", "螺钉", "置换术")

# High-energy trauma-domain anchors (fracture care / trauma triage, not lumbar Bi).
_TRAUMA_ANCHORS = ("车祸", "坠落", "挤压伤", "砸伤")

ALWAYS_ALLOWED = ["safety_triage"]
LUMBAR_CAPABILITIES = ["safety_triage", "lumbar_bi_syndrome_support", "lumbar_bi_formula_route"]
FOLLOWUP_CAPABILITIES = ["safety_triage", "followup_questionnaire"]


def _active(text: str, anchor: str) -> bool:
    """Anchor is affirmed AND current — a resolved/historical/other-person mention
    ("十年前车祸"、"肩关节脱位已复位"、"父亲车祸") must not steer the domain."""

    entity = scan_term(text, anchor)
    return bool(
        entity
        and entity["polarity"] == "affirmed"
        and entity.get("temporality") == "current"
        and entity.get("experiencer", "patient") == "patient"
    )


def clinical_scope_router_skill(raw_text: str, red_flag_categories: list[str] | None = None) -> dict[str, Any]:
    """Route a case narrative to a domain + allowed capability set.

    ``red_flag_categories`` are the confirmed categories from the safety grading —
    when an emergency category is present the router defers to the safety kernel
    (domain "emergency", triage only), regardless of body region.
    """

    text = raw_text or ""
    categories = set(red_flag_categories or [])
    lumbar = any(_active(text, anchor) for anchor in _LUMBAR_ANCHORS)
    fracture_postop = any(_active(text, anchor) for anchor in _FRACTURE_POSTOP_ANCHORS)
    trauma = any(_active(text, anchor) for anchor in _TRAUMA_ANCHORS)
    joint = any(_active(text, anchor) for anchor in _JOINT_ANCHORS)

    if categories & EMERGENCY_HALT_CATEGORIES:
        return {
            "domain": "emergency", "task": "triage", "in_scope": False, "scope_confidence": 0.95,
            "out_of_scope_reason": "急症安全内核已接管：先急诊/线下评估，不进入腰痹辨证。",
            "reason_codes": ["EMERGENCY_KERNEL_PRIORITY"],
            "allowed_capabilities": list(ALWAYS_ALLOWED),
            "blocked_capabilities": ["lumbar_bi_syndrome_support", "lumbar_bi_formula_route"],
        }
    if fracture_postop or trauma:
        # Fracture / dislocation / post-op surveillance outranks a bare lumbar anchor:
        # the correct task is fracture care & complication follow-up, not 腰痹方药.
        domain = "spine_fracture_followup" if (fracture_postop and lumbar) else ("fracture_followup" if fracture_postop else "trauma")
        return {
            "domain": domain, "task": "postoperative_followup" if fracture_postop else "triage",
            "in_scope": False, "scope_confidence": 0.85,
            "out_of_scope_reason": "骨折/脱位或术后随访任务优先于腰痹辨证：请走创伤/术后复查路径，由医师评估影像与愈合情况。",
            "reason_codes": ["FRACTURE_POSTOPERATIVE_PRIORITY"],
            "allowed_capabilities": list(FOLLOWUP_CAPABILITIES),
            "blocked_capabilities": ["lumbar_bi_syndrome_support", "lumbar_bi_formula_route"],
        }
    if lumbar:
        return {
            "domain": "spine", "task": "diagnosis_support", "in_scope": True,
            "scope_confidence": 0.9 if not joint else 0.75,
            "out_of_scope_reason": None,
            "reason_codes": ["LUMBAR_ANCHOR_PRESENT"],
            "allowed_capabilities": list(LUMBAR_CAPABILITIES),
            "blocked_capabilities": [],
        }
    if joint:
        return {
            "domain": "joint", "task": "triage", "in_scope": False, "scope_confidence": 0.8,
            "out_of_scope_reason": "非腰部骨伤主诉不属于本系统获准的腰痹任务域，请至相应专科面诊。",
            "reason_codes": ["NON_LUMBAR_REGION"],
            "allowed_capabilities": list(ALWAYS_ALLOWED),
            "blocked_capabilities": ["lumbar_bi_syndrome_support", "lumbar_bi_formula_route"],
        }
    return {
        "domain": "unknown", "task": "triage", "in_scope": False, "scope_confidence": 0.5,
        "out_of_scope_reason": "未识别到腰痹相关主诉，无法确认属于本系统任务域。",
        "reason_codes": ["NO_DOMAIN_ANCHOR"],
        "allowed_capabilities": list(ALWAYS_ALLOWED),
        "blocked_capabilities": ["lumbar_bi_syndrome_support", "lumbar_bi_formula_route"],
    }


# ---------------------------------------------------------------- conversational gate
# Lumbar-context tags: questionnaire intakes are lumbar-domain by construction, so a
# tag-only session carrying these stays in scope even when a follow-up question has no
# body-region anchor of its own.
LUMBAR_CONTEXT_TAGS = {
    "lumbar_pain", "lumbar_leg_pain", "chronic_yabi", "lumbar_knee_soreness",
    "radiating_leg_pain",
}


# Case-directed intents that must NOT default into the lumbar-Bi domain without a
# lumbar anchor: they produce syndrome/formula/herb content for THIS case. Dataset
# statistics (dose distributions, mined evidence) stay intent-agnostic.
ANCHOR_REQUIRED_INTENTS = {"syndrome_inquiry", "formula_inquiry", "herb_inquiry", "reasoning_inquiry", "experience_inquiry"}

_OUT_OF_DOMAIN_MESSAGE = (
    "⛔ **该主诉不属于本系统获准处理的腰痹任务域，未进行辨证与方药分析。**\n\n"
    "本系统仅支持腰痹（腰痛/腰腿痛）的经验规则研究；膝、肩、颈等其他部位骨伤问题、"
    "骨折/脱位及术后随访请至相应专科面诊。如出现危险信号（外伤后剧痛、肢体苍白发凉、"
    "大小便异常等）请立即急诊。"
)

_FRACTURE_POSTOP_MESSAGE = (
    "⛔ **骨折/脱位或术后随访任务优先于腰痹辨证，未进行方药分析。**\n\n"
    "腰椎骨折、术后复查、内固定/椎体成形等属于创伤/术后随访任务域：正确路径是影像复查、"
    "愈合评估与并发症随访，由医师面诊决策，而不是腰痹方药推理。如出现危险信号请立即急诊。"
)

_NO_ANCHOR_MESSAGE = (
    "⚠️ **未识别到腰痹相关主诉，本轮不进行辨证与方药分析。**\n\n"
    "本系统仅支持腰痹（腰痛/腰腿痛）任务域的经验规则研究。请先补充腰部主诉与相关病史"
    "（或通过问诊问卷录入），再进行证型/方药层面的分析。"
)


def question_scope_gate(question: str, case_state: dict[str, Any] | None = None, intent: str | None = None) -> dict[str, Any]:
    """Scope decision for conversational entries (chat / autonomous agent).

    Mirrors the full router's priority order at the conversational surface
    (v0.14 — the previous "any lumbar anchor passes" logic was the cross-entry
    inconsistency P0 of the v0.13 review):

    1. fracture / post-op / trauma context in the CURRENT question outranks a bare
       lumbar anchor ("腰椎压缩性骨折术后…独活寄生汤是否合适" is a fracture-followup
       task at every entry, exactly as the pipeline routes it);
    2. an out-of-domain body region in the CURRENT question blocks regardless of
       sticky lumbar state (domain shift: "现在主要是右膝关节肿痛" must not ride on
       last turn's lumbar scope);
    3. for case-directed clinical intents (``ANCHOR_REQUIRED_INTENTS``), a question
       with NO lumbar anchor anywhere (question or accumulated case facts) is not
       assumed to be lumbar — the old default let any "可以考虑哪些方剂" enter the
       formula chain. ``intent=None`` applies the same rule (conservative default).
    """

    text = question or ""
    state = case_state or {}
    lumbar_in_question = any(_active(text, anchor) for anchor in _LUMBAR_ANCHORS)
    # Lumbar evidence in accumulated state: intake tags, the questionnaire chief
    # complaint (case FACTS), or a scope decision. The scope field is safe to trust
    # here because the server strips client-supplied scope from case state (v0.14,
    # server._case_state) — what remains was computed server-side this session.
    chief = state.get("chief_complaint") or {}
    chief_text = f"{chief.get('standard_text') or ''}{chief.get('main_symptom') or ''}"
    lumbar_in_state = (
        bool(set(state.get("normalized_tags") or []) & LUMBAR_CONTEXT_TAGS)
        or "腰" in chief_text
        or (state.get("scope") or {}).get("in_scope") is True
    )
    fracture_postop = any(_active(text, anchor) for anchor in _FRACTURE_POSTOP_ANCHORS + _TRAUMA_ANCHORS)
    joint = any(_active(text, anchor) for anchor in _JOINT_ANCHORS)

    if fracture_postop:
        return {"allowed": False, "reason_codes": ["FRACTURE_POSTOPERATIVE_PRIORITY"], "message": _FRACTURE_POSTOP_MESSAGE}
    if joint and not lumbar_in_question:
        return {"allowed": False, "reason_codes": ["NON_LUMBAR_COMPLAINT_IN_QUESTION", "DOMAIN_SHIFT_DETECTED"], "message": _OUT_OF_DOMAIN_MESSAGE}
    if (intent is None or intent in ANCHOR_REQUIRED_INTENTS) and not lumbar_in_question and not lumbar_in_state:
        return {"allowed": False, "reason_codes": ["NO_LUMBAR_ANCHOR"], "message": _NO_ANCHOR_MESSAGE}
    return {"allowed": True, "reason_codes": [], "message": None}
