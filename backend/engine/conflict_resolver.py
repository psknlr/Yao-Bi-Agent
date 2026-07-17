from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.engine.rule_engine import RULES_DIR, load_yaml


def flatten_herbs(items: list[dict[str, Any]]) -> set[str]:
    herbs: set[str] = set()
    for item in items:
        herbs.update(item.get("herbs") or [])
        effect = item.get("effect") or {}
        herbs.update(effect.get("core_module") or [])
    return herbs


def check_conflicts(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    config = load_yaml(Path(RULES_DIR) / "06_conflict_rules.yaml") or {}
    herbs = flatten_herbs(items)
    conflicts = []
    for rule in config.get("conflicts", []):
        group_a = set(rule.get("group_a") or [])
        group_b = set(rule.get("group_b") or [])
        if herbs & group_a and herbs & group_b:
            conflicts.append({
                "id": rule["id"],
                "type": "route_conflict",
                "description": rule["meaning"],
                "resolution": rule.get("action", "require_clinician_review"),
                "alert_level": rule.get("alert_level", "advisory"),
            })
    return conflicts


def _terms_match(reported: str, rule_term: str) -> bool:
    # Substring-tolerant in both directions: a reported "阿司匹林肠溶片" matches
    # the rule term "阿司匹林", and a reported "溃疡" matches "消化性溃疡".
    # Empty strings never match — "" is a substring of everything.
    reported = reported.strip()
    rule_term = rule_term.strip()
    if not reported or not rule_term:
        return False
    if rule_term in reported:
        # Guard against free-text denials leaking in ("...没有高血压也没有心脏病"):
        # shared polarity resolution treats denied mentions as non-matches.
        from backend.skills.clinical_entity_skill import is_affirmed

        return is_affirmed(reported, rule_term)
    # The reverse direction ("溃疡" reported, rule term "消化性溃疡") requires the
    # reported string to be a plausible short term, not a single character or sentence.
    return 2 <= len(reported) <= 12 and reported in rule_term


def _matched_reported_terms(reported_list: list[str], rule_terms: list[Any]) -> list[str]:
    matched = {
        str(reported).strip()
        for reported in reported_list
        if any(_terms_match(str(reported), str(term)) for term in rule_terms)
    }
    return sorted(matched)


def check_interactions(
    items: list[dict[str, Any]],
    medications: list[str] | None = None,
    conditions: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Match the draft herb pool against herb-drug and comorbidity rules.

    Every alert is draft_for_clinician_review material: 'interruptive' entries
    are meant to block the UI until a physician explicitly acknowledges them,
    'advisory' entries are passive notes (alert-fatigue tiering).
    """
    config = load_yaml(Path(RULES_DIR) / "06_conflict_rules.yaml") or {}
    herbs = flatten_herbs(items)
    alerts: list[dict[str, Any]] = []

    for rule in config.get("herb_drug_interactions", []):
        herbs_involved = sorted(herbs & set(rule.get("herbs") or []))
        matched_drugs = _matched_reported_terms(medications or [], rule.get("drugs") or [])
        if herbs_involved and matched_drugs:
            alerts.append({
                "id": rule["id"],
                "type": "herb_drug",
                "description": rule["meaning"],
                "resolution": rule.get("action", "require_clinician_review"),
                "alert_level": rule.get("alert_level", "advisory"),
                "herbs_involved": herbs_involved,
                "matched_drugs": matched_drugs,
            })

    for rule in config.get("comorbidity_contraindications", []):
        herbs_involved = sorted(herbs & set(rule.get("herbs") or []))
        matched_conditions = _matched_reported_terms(conditions or [], rule.get("conditions") or [])
        if herbs_involved and matched_conditions:
            alerts.append({
                "id": rule["id"],
                "type": "comorbidity_contraindication",
                "description": rule["meaning"],
                "resolution": rule.get("action", "require_clinician_review"),
                "alert_level": rule.get("alert_level", "advisory"),
                "herbs_involved": herbs_involved,
                "matched_conditions": matched_conditions,
            })

    return alerts
