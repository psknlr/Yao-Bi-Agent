"""Independent critics — each inspects ONE dimension and cannot see the others' verdicts.

A system that generates a conclusion with one set of logic and then checks itself with
the same logic shares its own blind spots. These critics are deliberately narrow and
independent: the contradiction critic never reads candidate formulas (so it cannot be
anchored by them), the policy critic only sees role + intents, the evidence critic only
sees whether steps carry evidence refs. The executor composes their outputs; no critic
reads another critic's verdict.

All critics are deterministic and cheap — they run on every autonomous turn.
"""

from __future__ import annotations

from typing import Any

# Opposing clinical sign axes (sourced from the rule base's `contra` lists): both sides
# present at once is exactly the situation where a single-syndrome conclusion is most
# likely to be wrong and physician review most valuable.
_CONTRADICTION_AXES: list[dict[str, Any]] = [
    {
        "axis": "寒象与热象并见",
        "left_label": "寒象", "left": {"cold_aversion", "deep_cold_pain", "cold_aggravation", "warmth_relieves", "cold_extremities"},
        "right_label": "热象", "right": {"yellow_greasy_coating", "burning_pain", "dark_urine", "red_tongue", "heat_aggravation", "thirst"},
        "note": "寒热错杂或病史叙述混杂，单一证型结论需谨慎，建议医师重点复核舌脉与时间线。",
    },
    {
        "axis": "湿浊与阴伤线索并见",
        "left_label": "湿浊", "left": {"white_greasy_coating", "greasy_coating", "heavy_lower_limb", "dampness"},
        "right_label": "津伤", "right": {"thirst", "dry_mouth", "dark_urine"},
        "note": "湿邪与津伤线索同现，需鉴别湿热伤津与寒湿兼燥，建议补充口渴喜饮情况与舌面润燥。",
    },
]

# Intents whose answers are clinical reasoning — the patient role must never receive them
# (the session enforces this upstream; the policy critic is independent defense-in-depth).
_CLINICAL_INTENTS = {
    "syndrome_inquiry", "formula_inquiry", "herb_inquiry", "reasoning_inquiry",
    "experience_inquiry", "dose_inquiry", "evidence_inquiry", "mining_inquiry",
}


def contradiction_critic(normalized_tags: list[str] | None) -> list[dict[str, Any]]:
    """Actively hunt counter-evidence: opposing sign axes present in the same case."""

    tags = set(normalized_tags or [])
    findings: list[dict[str, Any]] = []
    for axis in _CONTRADICTION_AXES:
        left_hits = sorted(tags & axis["left"])
        right_hits = sorted(tags & axis["right"])
        if left_hits and right_hits:
            findings.append({
                "axis": axis["axis"],
                axis["left_label"]: left_hits,
                axis["right_label"]: right_hits,
                "note": axis["note"],
            })
    return findings


def policy_critic(user_role: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Role/output-level check only: did any clinical-reasoning step reach a patient run?"""

    violations = [
        s.get("intent") for s in steps
        if user_role == "patient" and s.get("intent") in _CLINICAL_INTENTS
        and "patient_scope_guard" not in (s.get("skills") or [])
    ]
    return {"role": user_role, "violations": violations, "ok": not violations}


def evidence_critic(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Evidence coverage only: which steps carry no rule/mined evidence refs at all."""

    ungrounded = [s.get("intent") for s in steps if not s.get("evidence")]
    return {"ungrounded_steps": ungrounded, "grounded_steps": len(steps) - len(ungrounded)}
