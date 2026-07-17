from __future__ import annotations

import re
from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_probe, guard_tao_output

# 各状态的临床主题（约束 Tao 追问只能停留在本状态主题内）。
STATE_THEME = {
    "S3_PAIN_PROFILE": "疼痛部位、性质、程度、诱因与缓解因素",
    "S4_NEURO_ORTHO": "下肢放射痛、麻木、无力、行走与影像/既往诊断",
    "S5_TCM_CORE": "中医寒热、湿象、气血、舌象脉象等四诊信息",
    "S6_SHEN_SIGNAL": "沈老经验相关的寒湿、通络、肝肾、少阳枢机线索",
    "S7_COMORBIDITY": "合并疾病、止痛/消炎/抗凝/激素用药与过敏史",
    "S8_ADAPTIVE_REPAIR": "尚未补齐的高价值医案字段",
}

# 允许 Tao 自动追问的状态：红旗硬门控、知情、基础人口学不开放生成式追问。
PROBE_ENABLED_STATES = set(STATE_THEME)


def _parse_question_lines(raw: str | None, max_n: int) -> list[str]:
    """Robustly pull clarifying questions out of the model's free-form output."""

    out: list[str] = []
    for line in (raw or "").splitlines():
        s = re.sub(r"^[\-\*•·\d\.\、\)\）\s]+", "", line).strip()
        if len(s) < 5:
            continue
        out.append(s)
        if len(out) >= max_n:
            break
    return out


def _freeform_probes(raw: str | None, state: str, max_probes: int) -> list[dict[str, Any]]:
    questions = _parse_question_lines(raw, max_probes)
    # Safety contract: a single leaked diagnosis/prescription/dose voids the WHOLE round —
    # the model has shown it is off-theme, so none of its probes from this turn are trusted.
    if any(not guard_probe(question)["allowed"] for question in questions):
        return []
    probes: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        probes.append({
            "id": f"TAO_PROBE_{state}_{index + 1}",
            "question": question,
            "field_hint": None,
            "reason": "Tao 结合患者已述信息，在本状态主题内自主提出的澄清式追问（待医师复核）。",
            "source": "tao_probe",
            "rule_constrained": True,
            "state": state,
            "input_type": "free_text",
            "advisory_only": True,
        })
    return probes[:max_probes]


def tao_followup_probe_skill(
    case_state: dict[str, Any],
    state: str,
    allowed_fields: list[str],
    rule_context: dict[str, Any] | None = None,
    last_answers: dict[str, Any] | None = None,
    max_probes: int = 2,
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Let Tao **autonomously generate** rule-bounded follow-up probes (model is primary).

    The model freely asks the next clarifying questions for the current clinical theme
    (parsed robustly from free-form output and guarded so no diagnosis/prescription/dose
    leaks); a structured JSON contract is kept as a secondary path. Hard constraints remain:

    * only enabled in clinical-content states (red-flag / consent / demographics are off);
    * at most ``max_probes`` per turn, advisory only — they never drive a state jump;
    * every probe must pass ``guard_probe``; one leaked diagnosis/prescription/dose voids
      the whole round (falls back to deterministic rule questions only).
    """

    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "fallback_used": True,
        "guard": None,
        "json_repair": None,
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
        "rule_constrained": True,
    }
    if not use_llm or max_probes <= 0 or state not in PROBE_ENABLED_STATES:
        meta["status"] = "not_applicable" if state not in PROBE_ENABLED_STATES else meta["status"]
        return {"probes": [], "tao_probe_runtime": meta}

    allowed = list(allowed_fields or [])
    payload = {
        "task": "caseguide_followup_probe",
        "state": state,
        "current_state_theme": STATE_THEME.get(state, "本状态主题"),
        "allowed_fields": allowed,
        "last_answers": last_answers or {},
        "rule_context": rule_context or {},
        "normalized_tags": sorted(set(case_state.get("normalized_tags") or [])),
        "max_probes": max_probes,
        "output_contract": {
            "format": "json_object",
            "allowed_field_hints": allowed + [None],
            "forbidden_actions": ["final_diagnosis", "syndrome_verdict", "complete_prescription", "dose_instruction", "out_of_theme_question"],
        },
    }
    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend

    # Primary path: the model autonomously asks the questions (free-form, robustly parsed).
    try:
        probes = _freeform_probes(client.generate_probe_questions(payload), state, max_probes)
        if probes:
            meta.update({"status": "accepted", "fallback_used": False, "mode": "freeform"})
            return {"probes": probes, "tao_probe_runtime": meta}
    except (DaoRuntimeError, ValueError, TypeError) as exc:
        meta["freeform_error"] = str(exc)

    # Secondary path: structured JSON contract with field hints (also guarded).
    try:
        raw = client.generate_followup_probes(payload)
        parsed, repair_meta = loads_with_repair(raw)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("probes"), list):
            raise JsonRepairError("Tao probe output must be an object with a probes list.")
        allowed_set = set(allowed)
        probes = []
        guard_text_parts: list[str] = []
        for index, item in enumerate(parsed["probes"]):
            if not isinstance(item, dict):
                continue
            probe_text = str(item.get("probe_text") or "").strip()
            if not probe_text:
                continue
            field_hint = item.get("field_hint")
            if field_hint is not None and field_hint not in allowed_set:
                field_hint = None
            reason = str(item.get("reason") or "").strip()
            guard_text_parts.extend([probe_text, reason])
            probes.append({
                "id": f"TAO_PROBE_{state}_{index + 1}",
                "question": probe_text,
                "field_hint": field_hint,
                "reason": reason or "Tao在本状态主题内的澄清式追问（待医师复核）。",
                "source": "tao_probe",
                "rule_constrained": True,
                "state": state,
                "input_type": "free_text",
                "advisory_only": True,
            })
        guard = guard_tao_output("\n".join(guard_text_parts), parsed)
        meta.update({"json_repair": repair_meta, "guard": guard, "status": "accepted" if guard["allowed"] and probes else "guard_rejected", "mode": "structured"})
        if not guard["allowed"] or not probes:
            return {"probes": [], "tao_probe_runtime": meta}
        meta["fallback_used"] = False
        return {"probes": probes[:max_probes], "tao_probe_runtime": meta}
    except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
        meta.update({"status": "fallback", "error": str(exc), "fallback_used": True})
        return {"probes": [], "tao_probe_runtime": meta}
