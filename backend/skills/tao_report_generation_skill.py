from __future__ import annotations

from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_clinician_draft, guard_tao_output
from backend.skills.report_generation_skill import report_generation_skill


def _structured_payload(
    case_json: dict[str, Any],
    normalized_tags: list[str],
    syndrome_candidates: list[dict[str, Any]],
    formula_route: dict[str, Any] | None,
    matched_modules: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    safety: dict[str, Any],
    rule_hits: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    return {
        "case_json": case_json,
        "normalized_tags": normalized_tags,
        "syndrome_candidates": syndrome_candidates,
        "formula_route": formula_route,
        "matched_modules": matched_modules,
        "conflicts": conflicts,
        "safety": safety,
        "rule_hits": rule_hits or [],
        "output_contract": {
            "required_key": "markdown_report",
            "forbidden_keys": ["final_diagnosis", "complete_prescription", "patient_executable_dose", "administration_instruction"],
            "boundary": "research_education_clinician_review_only",
        },
    }


def tao_report_generation_skill(
    case_json: dict[str, Any],
    normalized_tags: list[str],
    syndrome_candidates: list[dict[str, Any]],
    formula_route: dict[str, Any] | None,
    matched_modules: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    safety: dict[str, Any],
    rule_hits: list[dict[str, Any]] | None = None,
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
    user_role: str = "clinician",
    uncertainty: dict[str, Any] | None = None,
    interaction_alerts: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate a deterministic report plus optional Tao teaching overlay.

    The deterministic report is always produced first and remains the fallback. Tao
    output may only replace/add to the report when it parses after JSON repair and
    passes the role-aware medical-output guard: the clinician/research draft keeps
    teaching content like 方义 and experience dose ranges, while the patient role
    keeps the strict no-diagnosis/no-prescription/no-dose floor.
    """

    deterministic = report_generation_skill(
        case_json=case_json,
        normalized_tags=normalized_tags,
        syndrome_candidates=syndrome_candidates,
        formula_route=formula_route,
        matched_modules=matched_modules,
        conflicts=conflicts,
        safety=safety,
        rule_hits=rule_hits,
        uncertainty=uncertainty,
        interaction_alerts=interaction_alerts,
        provenance=provenance,
    )
    payload = _structured_payload(
        case_json,
        normalized_tags,
        syndrome_candidates,
        formula_route,
        matched_modules,
        conflicts,
        safety,
        rule_hits,
    )
    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
        "json_repair": None,
        "guard": None,
        "fallback_used": True,
    }
    if not use_llm:
        return {**deterministic, "deterministic_markdown_report": deterministic["markdown_report"], "tao_runtime": meta}

    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend
    try:
        raw_output = client.generate(payload)
        parsed, repair_meta = loads_with_repair(raw_output)
        if not isinstance(parsed, dict):
            raise JsonRepairError("Tao output must be a JSON object.")
        markdown = str(parsed.get("markdown_report") or parsed.get("teaching_explanation") or "").strip()
        if not markdown:
            raise JsonRepairError("Tao JSON output must include markdown_report or teaching_explanation.")
        guard = guard_tao_output(markdown, parsed) if user_role == "patient" else guard_clinician_draft(markdown, parsed)
        meta.update({"status": "accepted" if guard["allowed"] else "guard_rejected", "json_repair": repair_meta, "guard": guard})
        if not guard["allowed"]:
            return {**deterministic, "deterministic_markdown_report": deterministic["markdown_report"], "tao_runtime": meta}
        hybrid_report = deterministic["markdown_report"] + "\n\n---\n\n## 12. Tao 教学解释补充（已通过安全校验）\n" + markdown
        meta["fallback_used"] = False
        return {"markdown_report": hybrid_report, "deterministic_markdown_report": deterministic["markdown_report"], "tao_runtime": meta}
    except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
        meta.update({"status": "fallback", "error": str(exc), "fallback_used": True})
        return {**deterministic, "deterministic_markdown_report": deterministic["markdown_report"], "tao_runtime": meta}
