from __future__ import annotations

from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_tao_output


def tao_question_planner_skill(
    case_state: dict[str, Any],
    state: str,
    deterministic_questions: list[dict[str, Any]],
    rule_context: dict[str, Any] | None = None,
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Overlay Tao reasoning on deterministic CaseGuide questions.

    The deterministic finite-state machine still decides the candidate question ids.
    Tao may only reorder/rephrase those existing ids and enrich reasons; it cannot add
    diagnosis/prescription content or invent new question ids. Unsafe or malformed
    model output falls back to the deterministic list.
    """

    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "fallback_used": True,
        "guard": None,
        "json_repair": None,
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
    }
    if not use_llm or not deterministic_questions:
        return {"questions": deterministic_questions, "tao_question_runtime": meta}

    allowed_by_id = {question.get("id"): question for question in deterministic_questions if question.get("id")}
    payload = {
        "task": "caseguide_question_planning",
        "state": state,
        "case_state": case_state,
        "rule_context": rule_context or {},
        "candidate_questions": deterministic_questions,
        "output_contract": {
            "format": "json_object",
            "allowed_question_ids": list(allowed_by_id),
            "max_questions": len(deterministic_questions),
            "allowed_actions": ["reorder_existing_questions", "patient_friendly_rewrite", "explain_reason"],
            "forbidden_actions": ["new_question_id", "final_diagnosis", "complete_prescription", "dose_instruction"],
        },
    }
    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend
    try:
        raw = client.generate_question_plan(payload)
        parsed, repair_meta = loads_with_repair(raw)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("questions"), list):
            raise JsonRepairError("Tao question output must be an object with a questions list.")
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        model_preserved_allowed_id = False
        invalid_ids: list[str] = []
        guard_text_parts: list[str] = []
        for item in parsed["questions"]:
            if not isinstance(item, dict):
                continue
            guard_text_parts.extend([str(item.get("question", "")), str(item.get("reason", ""))])
            qid = item.get("id")
            if qid not in allowed_by_id:
                invalid_ids.append(str(qid))
                continue
            if qid in seen:
                continue
            model_preserved_allowed_id = True
            base = dict(allowed_by_id[qid])
            if item.get("question"):
                base["question"] = str(item["question"])
            if item.get("reason"):
                base["reason"] = str(item["reason"])
            base["tao_enhanced"] = True
            guard_text_parts.extend([str(base.get("question", "")), str(base.get("reason", ""))])
            merged.append(base)
            seen.add(qid)
        if invalid_ids:
            raise JsonRepairError(f"Tao question output invented non-candidate ids: {', '.join(invalid_ids)}")
        if not model_preserved_allowed_id:
            raise JsonRepairError("Tao question output did not preserve any allowed question ids.")
        for qid, base in allowed_by_id.items():
            if qid not in seen and len(merged) < len(deterministic_questions):
                merged.append(base)
        guard = guard_tao_output("\n".join(guard_text_parts), parsed)
        meta.update({"json_repair": repair_meta, "guard": guard, "status": "accepted" if guard["allowed"] else "guard_rejected"})
        if not guard["allowed"]:
            return {"questions": deterministic_questions, "tao_question_runtime": meta}
        meta["fallback_used"] = False
        return {"questions": merged[: len(deterministic_questions)], "tao_question_runtime": meta}
    except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
        meta.update({"status": "fallback", "error": str(exc), "fallback_used": True})
        return {"questions": deterministic_questions, "tao_question_runtime": meta}
