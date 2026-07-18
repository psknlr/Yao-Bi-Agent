"""Built-in tool registrations: every skill the system exposes, with governance metadata.

Schemas are derived from the real function signatures (``schema_from_callable``), so
``config/hermes_tools.json`` — generated from this registry — can no longer drift from
the code. ``dao_client`` parameters are excluded from schemas; tools that need a model
runtime are marked ``execution="direct"`` (schema is still the single source of truth,
but execution stays with their host component which owns the DaoClient).

Role sets:
* ALL     — patient-facing intake / education / guard tools;
* CLIN    — clinician/researcher clinical-draft tools (server RBAC mirrors this);
* DOCTOR  — physician-authored records only (the model never calls these).
"""

from __future__ import annotations

from backend.tools.registry import ToolRegistry, ToolSpec, schema_from_callable

ALL_ROLES = frozenset({"system", "patient", "clinician", "researcher"})
CLIN_ROLES = frozenset({"system", "clinician", "researcher"})
DOCTOR_ONLY = frozenset({"clinician"})

_REGISTRY: ToolRegistry | None = None


def _build() -> ToolRegistry:
    from backend.skills.adaptive_question_planner_skill import adaptive_question_planner_skill
    from backend.skills.case_extract_skill import case_extract_skill
    from backend.skills.case_normalize_skill import case_normalize_skill
    from backend.skills.case_quality_check_skill import case_quality_check_skill
    from backend.skills.case_structuring_skill import case_structuring_skill
    from backend.skills.cdss_recommendation_skill import cdss_recommendation_skill
    from backend.skills.chief_complaint_skill import chief_complaint_skill
    from backend.skills.clinical_scope_router_skill import clinical_scope_router_skill
    from backend.skills.clinician_handoff_skill import clinician_handoff_skill
    from backend.skills.clinician_review_package_skill import clinician_review_package_skill
    from backend.skills.comorbidity_medication_skill import comorbidity_medication_skill
    from backend.skills.conflict_checker_skill import conflict_checker_skill
    from backend.skills.consent_privacy_skill import consent_privacy_skill
    from backend.skills.formula_base_selector_skill import formula_base_selector_skill
    from backend.skills.herb_module_composer_skill import herb_module_composer_skill
    from backend.skills.mined_evidence_skill import mined_evidence_skill
    from backend.skills.neuro_ortho_screen_skill import neuro_ortho_screen_skill
    from backend.skills.pain_profile_skill import pain_profile_skill
    from backend.skills.patient_request_guard_skill import patient_request_guard_skill
    from backend.skills.physician_review_skill import create_physician_review_task, physician_review_skill
    from backend.skills.red_flag_screen_skill import red_flag_screen_skill
    from backend.skills.report_generation_skill import report_generation_skill
    from backend.skills.safety_guard_skill import safety_guard_skill
    from backend.skills.shen_rule_signal_skill import shen_rule_signal_skill
    from backend.skills.syndrome_router_skill import syndrome_router_skill
    from backend.skills.tao_question_planner_skill import tao_question_planner_skill
    from backend.skills.tao_report_generation_skill import tao_report_generation_skill
    from backend.skills.tcm_four_diagnosis_skill import tcm_four_diagnosis_skill
    from backend.skills.uncertainty_skill import uncertainty_skill

    registry = ToolRegistry()

    ROLE_ENUM = ["patient", "clinician", "researcher", "system"]

    # Output contracts for the highest-value tools: a result that fails these is
    # discarded (classified ToolOutputValidationError → audit → deterministic
    # fallback/raise), never passed downstream (harness review v0.12 P1-10).
    OUTPUT_SCHEMAS: dict[str, dict] = {
        "safety_guard_skill": {
            "type": "object",
            "required": ["safety_status", "action_level", "confirmed_red_flags", "need_further_inquiry"],
            "properties": {
                "safety_status": {"type": "string", "enum": ["safe", "caution", "urgent"]},
                "action_level": {"type": "string", "enum": ["A0", "A1", "A2", "A3"]},
                "confirmed_red_flags": {"type": "array", "items": {"type": "object"}},
                "denied_red_flags": {"type": "array", "items": {"type": "object"}},
                "uncertain_red_flags": {"type": "array", "items": {"type": "object"}},
                "historical_red_flags": {"type": "array", "items": {"type": "object"}},
                "policy_flags": {"type": "array", "items": {"type": "object"}},
                "need_further_inquiry": {"type": "array", "items": {"type": "string"}},
            },
        },
        "syndrome_router_skill": {
            "type": "object",
            "required": ["syndrome_candidates", "rule_hits"],
            "properties": {
                "syndrome_candidates": {
                    "type": "array",
                    "items": {"type": "object", "required": ["name", "score", "confidence"],
                              "properties": {"name": {"type": "string"}, "score": {"type": "integer"},
                                             "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}},
                },
                "rule_hits": {"type": "array", "items": {"type": "object"}},
            },
        },
        "formula_base_selector_skill": {
            "type": "object",
            "required": ["formula_routes", "formula_rule_hits", "route_gate"],
            "properties": {
                "formula_routes": {"type": "array", "items": {"type": "object"}},
                "primary_route": {"type": ["object", "null"]},
                "formula_rule_hits": {"type": "array", "items": {"type": "object"}},
                "route_gate": {"type": "object", "required": ["allowed"]},
            },
        },
        "clinical_scope_router_skill": {
            "type": "object",
            "required": ["domain", "in_scope", "allowed_capabilities", "reason_codes"],
            "properties": {
                "domain": {"type": "string",
                           "enum": ["spine", "emergency", "trauma", "fracture_followup",
                                    "spine_fracture_followup", "joint", "unknown"]},
                "in_scope": {"type": "boolean"},
                "allowed_capabilities": {"type": "array", "items": {"type": "string"}},
                "blocked_capabilities": {"type": "array", "items": {"type": "string"}},
                "reason_codes": {"type": "array", "items": {"type": "string"}},
            },
        },
    }
    ROLE_ENUM_TOOLS = {
        "patient_request_guard_skill", "cdss_recommendation_skill", "consent_privacy_skill",
        "tao_report_generation_skill",
    }

    def add(fn, description: str, roles: frozenset[str], risk: str, *,
            execution: str = "registry", idempotent: bool = True, timeout: float = 5.0) -> None:
        enums = {"user_role": ROLE_ENUM} if fn.__name__ in ROLE_ENUM_TOOLS else None
        registry.register(ToolSpec(
            name=fn.__name__,
            description=description,
            handler=fn,
            parameters=schema_from_callable(fn, param_enums=enums),
            allowed_roles=roles,
            risk_level=risk,
            idempotent=idempotent,
            timeout_seconds=timeout,
            execution=execution,
            output_schema=OUTPUT_SCHEMAS.get(fn.__name__, {"type": "object"}),
        ))

    # --- deterministic clinical chain -------------------------------------------------
    add(clinical_scope_router_skill, "Route a case to its clinical domain and approved capability set; non-lumbar complaints never enter the lumbar-Bi formula chain.", ALL_ROLES, "read")
    add(case_extract_skill, "Extract structured clinical features from a de-identified lumbar Bi case text without diagnosis.", ALL_ROLES, "read")
    add(case_normalize_skill, "Normalize extracted TCM clinical features into controlled rule tags.", ALL_ROLES, "read")
    add(syndrome_router_skill, "Score candidate TCM patterns based on Shen Qinrong lumbar Bi rules.", CLIN_ROLES, "clinical_draft")
    add(formula_base_selector_skill, "Select candidate classical formula route as non-prescriptive research signal.", CLIN_ROLES, "clinical_draft")
    add(herb_module_composer_skill, "Compose non-prescriptive herb modules for research explanation.", CLIN_ROLES, "clinical_draft")
    add(conflict_checker_skill, "Check herb-herb conflicts, herb-drug interactions and comorbidity contraindications.", CLIN_ROLES, "clinical_draft")
    add(safety_guard_skill, "Grade red flags (candidate → polarity → confirmed, category-tiered urgency) and screen toxic/high-risk herbs.", ALL_ROLES, "read")
    add(uncertainty_skill, "Self-assess candidate separation, abstention and missing discriminating evidence.", CLIN_ROLES, "read")
    add(mined_evidence_skill, "Match de-identified mined rule candidates (pending expert review) to the case tags.", CLIN_ROLES, "clinical_draft")
    add(report_generation_skill, "Generate the deterministic research/teaching report from rule engine outputs.", CLIN_ROLES, "clinical_draft")
    add(tao_report_generation_skill, "Deterministic report plus optional guarded Tao teaching overlay (falls back to rules).", CLIN_ROLES, "clinical_draft", execution="direct")

    # --- interview / caseguide tools ---------------------------------------------------
    add(consent_privacy_skill, "Show consent/privacy notice and desensitize patient input.", ALL_ROLES, "read")
    add(red_flag_screen_skill, "Ask and score urgent lumbar-pain red flags before any TCM inquiry.", ALL_ROLES, "read")
    add(chief_complaint_skill, "Convert patient wording into a standard chief complaint.", ALL_ROLES, "read")
    add(pain_profile_skill, "Collect and tag pain location, radiation, nature, severity, aggravating and relieving factors.", ALL_ROLES, "read")
    add(neuro_ortho_screen_skill, "Collect numbness, weakness, walking limitation, imaging and western diagnosis fields.", ALL_ROLES, "read")
    add(tcm_four_diagnosis_skill, "Collect patient-friendly cold/heat, dampness, sleep, appetite, tongue and pulse fields.", ALL_ROLES, "read")
    add(shen_rule_signal_skill, "Derive high-value Shen Qinrong rule signals from current case state.", CLIN_ROLES, "read")
    add(comorbidity_medication_skill, "Collect comorbidities and medications relevant to safety and rule explanation.", ALL_ROLES, "read")
    add(adaptive_question_planner_skill, "Select next 1-3 high-yield questions from safety, missing fields and information gain.", ALL_ROLES, "read")
    add(case_structuring_skill, "Generate a standard lumbar Bi case draft without diagnosis or prescription.", ALL_ROLES, "read")
    add(case_quality_check_skill, "Score case completeness and recommend follow-up questions.", ALL_ROLES, "read")
    add(tao_question_planner_skill, "Overlay Tao reordering/rewriting on deterministic CaseGuide questions (id-constrained).", ALL_ROLES, "read", execution="direct")

    # --- clinician-review surface ------------------------------------------------------
    add(clinician_handoff_skill, "Generate a concise clinician handoff summary for review.", CLIN_ROLES, "clinical_draft")
    add(clinician_review_package_skill, "Assemble clinician-review hypotheses and prescription-experience signals (no final diagnosis/prescription/dose).", frozenset({"system", "clinician"}), "clinical_draft")
    add(patient_request_guard_skill, "Classify and block final-diagnosis/prescription/dose/self-medication requests with safe alternatives.", ALL_ROLES, "read")
    add(create_physician_review_task, "Create a clinician-only review task for a licensed physician (model does not fill final fields).", frozenset({"system", "clinician"}), "clinical_draft")
    add(physician_review_skill, "Store a physician-authored, signed final record; rejects model-generated diagnosis/prescription/dose.", DOCTOR_ONLY, "high_risk", idempotent=False)
    add(cdss_recommendation_skill, "Generate clinician-facing CDSS draft candidates (draft_for_clinician_review, never patient-visible).", frozenset({"system", "clinician"}), "clinical_draft")

    return registry


def get_registry() -> ToolRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build()
    return _REGISTRY
