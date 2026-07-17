from __future__ import annotations

from typing import Any

from backend.engine.conformal import conformal_prediction_set
from backend.llm.dao_client import DaoClient
from backend.provenance import get_provenance
from backend.skills.safety_guard_skill import ACTION_LEVEL_POLICY, emergency_halt_required
from backend.skills.tao_report_generation_skill import tao_report_generation_skill
from backend.tools import get_registry


def run_case_pipeline(raw_text: str, use_llm: bool = False, dao_client: DaoClient | None = None) -> dict[str, Any]:
    # Every deterministic step goes through the tool registry: schema-validated input,
    # validated output, audit span. `call` raises the classified ToolError on failure,
    # which is correct for this in-process pipeline (there is no planner to re-plan).
    tools = get_registry()
    case_json = tools.call("case_extract_skill", raw_text=raw_text)
    normalized = tools.call("case_normalize_skill", case_json=case_json)

    # Red-flag gate BEFORE any TCM reasoning (global invariant, mirrored by the chat /
    # autonomous / orchestrator entry paths): confirmed emergency-category red flags
    # (cauda equina, progressive weakness, infection) hard-halt the clinical chain — no
    # syndrome, formula or herb output is produced, only the emergency referral report.
    # Contextual-urgent flags (e.g. fragility trauma) keep the case marked urgent while
    # the retrospective clinician-review analysis continues.
    gate_safety = tools.call("safety_guard_skill", case_json=case_json, matched_modules=None,
                             normalized_tags=normalized["normalized_tags"])
    if emergency_halt_required(gate_safety):
        return _halted_result(case_json, normalized, gate_safety, use_llm=use_llm, dao_client=dao_client)

    # Scope router AFTER the emergency kernel: a non-emergency case must still belong
    # to the approved lumbar-Bi task domain before any syndrome/formula reasoning —
    # a knee complaint or an unrecognized narrative gets safety triage + referral only.
    scope = tools.call(
        "clinical_scope_router_skill", raw_text=raw_text,
        red_flag_categories=[f.get("category") for f in gate_safety.get("confirmed_red_flags") or []],
    )
    if not scope["in_scope"]:
        return _out_of_scope_result(case_json, normalized, gate_safety, scope,
                                    use_llm=use_llm, dao_client=dao_client)

    routed = tools.call("syndrome_router_skill", normalized_tags=normalized["normalized_tags"])
    formula = tools.call("formula_base_selector_skill", normalized_tags=normalized["normalized_tags"],
                         syndrome_candidates=routed["syndrome_candidates"])
    modules = tools.call("herb_module_composer_skill", normalized_tags=normalized["normalized_tags"],
                         formula_route=formula.get("primary_route"))
    conflicts = tools.call(
        "conflict_checker_skill",
        matched_modules=modules["matched_modules"], formula_route=formula.get("primary_route"),
        medications=case_json.get("medications") or [],
        conditions=(case_json.get("comorbidity_conditions") or []) + (case_json.get("western_diagnosis") or []),
    )
    # The primary formula route's core herbs join the safety scan: a route carrying
    # 附片/细辛 must surface a clinician-review caution even when no herb module matched.
    safety_pool = modules["matched_modules"] + ([formula["primary_route"]] if formula.get("primary_route") else [])
    safety = tools.call("safety_guard_skill", case_json=case_json, matched_modules=safety_pool,
                        normalized_tags=normalized["normalized_tags"])
    uncertainty = tools.call(
        "uncertainty_skill",
        syndrome_candidates=routed["syndrome_candidates"], normalized_tags=normalized["normalized_tags"],
        missing_fields=case_json.get("missing_fields"),
    )
    # Conformal differential: the project-calibrated set of syndromes the engine cannot
    # rule out under its own scoring (see backend/engine/conformal.py for caveats).
    try:
        uncertainty["uncertainty"]["conformal"] = conformal_prediction_set(routed["syndrome_candidates"])
    except (OSError, ValueError):
        # Missing/corrupt calibration file must never break the clinical pipeline.
        uncertainty["uncertainty"]["conformal"] = None
    provenance = get_provenance(getattr(dao_client, "config", None) if use_llm else None)
    report = tao_report_generation_skill(
        case_json=case_json,
        normalized_tags=normalized["normalized_tags"],
        syndrome_candidates=routed["syndrome_candidates"],
        formula_route=formula.get("primary_route"),
        matched_modules=modules["matched_modules"],
        conflicts=conflicts["conflicts"],
        safety=safety,
        rule_hits=routed["rule_hits"] + formula["formula_rule_hits"],
        dao_client=dao_client,
        use_llm=use_llm,
        uncertainty=uncertainty["uncertainty"],
        interaction_alerts=conflicts.get("interaction_alerts"),
        provenance=provenance,
    )
    return {
        "case_json": case_json,
        **normalized,
        **routed,
        **formula,
        **modules,
        **conflicts,
        "safety": safety,
        "scope": scope,
        "red_flag_gate": {
            "halted": False,
            "status": gate_safety.get("safety_status"),
            "confirmed_red_flags": [f.get("term") or f.get("id") for f in gate_safety.get("confirmed_red_flags") or []],
        },
        **uncertainty,
        "clinical_mode": "urgent_workup_priority" if safety.get("safety_status") == "urgent" else "standard_support",
        "action_card": _action_card(safety, scope, formula=formula, uncertainty=uncertainty["uncertainty"]),
        # Machine-readable release contract (v0.14): downstream consumers enforce
        # this instead of relying on UI conventions. At A1 the formula/herb objects
        # in this payload are clinician-review-only and must never reach a patient
        # surface; the patient-role API filters key off this block.
        "capability_policy": {
            "action_level": safety.get("action_level"),
            **ACTION_LEVEL_POLICY.get(safety.get("action_level") or "A3", ACTION_LEVEL_POLICY["A3"]),
            "clinician_review_only_keys": (
                ["formula_routes", "primary_route", "matched_modules", "syndrome_candidates"]
                if safety.get("action_level") == "A1" else []
            ),
        },
        "provenance": provenance,
        **report,
    }


def _action_card(
    safety: dict[str, Any],
    scope: dict[str, Any] | None,
    *,
    formula: dict[str, Any] | None = None,
    uncertainty: dict[str, Any] | None = None,
    halted: bool = False,
) -> dict[str, Any]:
    """Action-card-first output: the clinician sees level → why → next step → what is
    blocked, before any long report (the report stays available below it)."""

    confirmed = safety.get("confirmed_red_flags") or []
    why = [f.get("message") or f.get("term") or f.get("id") for f in confirmed[:5]]
    if scope and not scope.get("in_scope") and scope.get("out_of_scope_reason"):
        why.append(scope["out_of_scope_reason"])
    level = safety.get("action_level") or ("A0" if halted else "A3")
    blocked: list[str] = []
    next_steps: list[str] = []
    if halted:
        blocked = ["中医辨证", "方药路线", "药物模块组合", "LLM 扩写", "常规追问流程"]
        next_steps = ["保持制动/脊柱保护，避免自行行走或负重", "立即急诊/线下评估（创伤、神经血管、影像由临床人员判断）"]
    elif scope and not scope.get("in_scope"):
        blocked = ["腰痹辨证", "方药路线"]
        next_steps = ["至相应专科面诊评估", "如出现危险信号请急诊就诊"]
    else:
        if level == "A1":
            next_steps = ["当日紧急专科评估", "患者端不提供方药内容；医师复盘分析仅供回顾研究"]
        elif level == "A2":
            next_steps = ["尽快线下面诊与必要检查", "补充缺失的鉴别信息"]
        else:
            next_steps = ["常规门诊决策支持流程", "由执业医师审核候选证型与方路信号"]
        # v0.14: blocked reflects the ACTION_LEVEL_POLICY contract, not an empty
        # display default — at A1 the payload says explicitly that patient-facing
        # formula release and executable care content are denied.
        blocked.extend(ACTION_LEVEL_POLICY.get(level, {}).get("blocked") or [])
        if formula is not None and not (formula.get("route_gate") or {}).get("allowed", True):
            blocked.append("方药路线（" + str((formula.get("route_gate") or {}).get("note") or "证据不足弃权") + "）")
    gaps = list(safety.get("need_further_inquiry") or [])
    if uncertainty and uncertainty.get("abstain"):
        gaps.append(str(uncertainty.get("assessment_note") or "证据不足，建议补充关键信息"))
    return {
        "action_level": level,
        "action_meaning": safety.get("action_meaning"),
        "why": [w for w in why if w][:6],
        "next_steps": next_steps,
        "blocked": blocked,
        "evidence_gaps": gaps[:6],
        "drivers": safety.get("drivers"),
    }


def _halted_result(
    case_json: dict[str, Any],
    normalized: dict[str, Any],
    safety: dict[str, Any],
    *,
    use_llm: bool,
    dao_client: DaoClient | None,
) -> dict[str, Any]:
    """Emergency-halted pipeline output: same shape as the normal result, empty clinical chain.

    The deterministic report still renders (referral notice + red flags); the language
    model is deliberately not invoked — an emergency notice must not depend on a model.
    """

    provenance = get_provenance(getattr(dao_client, "config", None) if use_llm else None)
    report = tao_report_generation_skill(
        case_json=case_json,
        normalized_tags=normalized["normalized_tags"],
        syndrome_candidates=[],
        formula_route=None,
        matched_modules=[],
        conflicts=[],
        safety=safety,
        rule_hits=[],
        dao_client=None,
        use_llm=False,
        uncertainty=None,
        interaction_alerts=[],
        provenance=provenance,
    )
    return {
        "case_json": case_json,
        **normalized,
        "syndrome_candidates": [],
        "rule_hits": [],
        "formula_routes": [],
        "primary_route": None,
        "formula_rule_hits": [],
        "matched_modules": [],
        "conflicts": [],
        "interaction_alerts": [],
        "alert_summary": {"interruptive": 0, "advisory": 0, "requires_dual_signoff": False},
        "safety": safety,
        "scope": {
            "domain": "emergency", "task": "triage", "in_scope": False, "scope_confidence": 0.95,
            "out_of_scope_reason": "急症安全内核已接管。",
            "allowed_capabilities": ["safety_triage"],
        },
        "red_flag_gate": {
            "halted": True,
            "status": safety.get("safety_status"),
            "reason": "确认急诊级红旗（骨伤科急症本体），中止辨证与方药推理，仅输出急诊转诊提示。",
            "confirmed_red_flags": [f.get("term") or f.get("id") for f in safety.get("confirmed_red_flags") or []],
        },
        "uncertainty": {
            "abstain": True,
            "assessment_note": "急诊红旗未排除，本例不进行证候与方药评估。",
            "conformal": None,
        },
        "clinical_mode": "emergency_halt",
        "action_card": _action_card(safety, None, halted=True),
        "capability_policy": {
            "action_level": safety.get("action_level") or "A0",
            **ACTION_LEVEL_POLICY.get(safety.get("action_level") or "A0", ACTION_LEVEL_POLICY["A0"]),
            "clinician_review_only_keys": [],
        },
        "provenance": provenance,
        **report,
    }


def _out_of_scope_result(
    case_json: dict[str, Any],
    normalized: dict[str, Any],
    gate_safety: dict[str, Any],
    scope: dict[str, Any],
    *,
    use_llm: bool,
    dao_client: DaoClient | None,
) -> dict[str, Any]:
    """Out-of-domain case: safety triage output only, no lumbar-Bi clinical chain."""

    provenance = get_provenance(getattr(dao_client, "config", None) if use_llm else None)
    report = tao_report_generation_skill(
        case_json=case_json,
        normalized_tags=normalized["normalized_tags"],
        syndrome_candidates=[],
        formula_route=None,
        matched_modules=[],
        conflicts=[],
        safety=gate_safety,
        rule_hits=[],
        dao_client=None,
        use_llm=False,
        uncertainty=None,
        interaction_alerts=[],
        provenance=provenance,
    )
    return {
        "case_json": case_json,
        **normalized,
        "syndrome_candidates": [],
        "rule_hits": [],
        "formula_routes": [],
        "primary_route": None,
        "formula_rule_hits": [],
        "matched_modules": [],
        "conflicts": [],
        "interaction_alerts": [],
        "alert_summary": {"interruptive": 0, "advisory": 0, "requires_dual_signoff": False},
        "safety": gate_safety,
        "scope": scope,
        "red_flag_gate": {
            "halted": False,
            "status": gate_safety.get("safety_status"),
            "confirmed_red_flags": [f.get("term") or f.get("id") for f in gate_safety.get("confirmed_red_flags") or []],
        },
        "uncertainty": {
            "abstain": True,
            "assessment_note": "主诉不属于本系统获准的腰痹任务域，不进行证候与方药评估。",
            "conformal": None,
        },
        "clinical_mode": "out_of_scope_triage",
        "action_card": _action_card(gate_safety, scope),
        "capability_policy": {
            "action_level": gate_safety.get("action_level"),
            **ACTION_LEVEL_POLICY.get(gate_safety.get("action_level") or "A3", ACTION_LEVEL_POLICY["A3"]),
            "clinician_review_only_keys": [],
        },
        "provenance": provenance,
        **report,
    }
