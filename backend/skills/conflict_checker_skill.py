from __future__ import annotations

from typing import Any

from backend.engine.conflict_resolver import check_conflicts, check_interactions


def conflict_checker_skill(
    matched_modules: list[dict[str, Any]],
    formula_route: dict[str, Any] | None = None,
    medications: list[str] | None = None,
    conditions: list[str] | None = None,
) -> dict[str, Any]:
    """Check route conflicts, herb-drug interactions and comorbidity contraindications.

    Output is a draft for clinician review, never patient-executable. Alerts are
    tiered to limit alert fatigue: 'interruptive' alerts are intended to require
    explicit physician acknowledgement in the UI before the draft can proceed
    (any interruptive alert sets requires_dual_signoff); 'advisory' alerts are
    passive notes shown alongside the draft. Calling without medications and
    conditions preserves the legacy behavior: route conflicts only, with an
    empty interaction_alerts list.
    """
    items = list(matched_modules)
    if formula_route:
        items.append({"effect": {"core_module": formula_route.get("core_module", [])}})
    conflicts = check_conflicts(items)
    interaction_alerts = check_interactions(items, medications, conditions)
    all_alerts = conflicts + interaction_alerts
    interruptive = sum(1 for alert in all_alerts if alert.get("alert_level") == "interruptive")
    advisory = sum(1 for alert in all_alerts if alert.get("alert_level", "advisory") == "advisory")
    return {
        "conflicts": conflicts,
        "interaction_alerts": interaction_alerts,
        "alert_summary": {
            "interruptive": interruptive,
            "advisory": advisory,
            "requires_dual_signoff": interruptive > 0,
        },
    }
