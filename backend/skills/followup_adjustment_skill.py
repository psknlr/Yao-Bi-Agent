from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.engine.rule_engine import RULES_DIR, load_yaml, trigger_matches


def followup_adjustment_skill(followup_tags: list[str]) -> dict[str, Any]:
    config = load_yaml(Path(RULES_DIR) / "08_followup_rules.yaml") or {}
    tag_set = set(followup_tags)
    hits = []
    for rule in config.get("followup_rules", []):
        # Shared all/any/at_least semantics — a conjunctive rule (痛减而麻存) must not
        # fire on a single tag. Previously this used a bare set-intersection that read
        # every trigger as "any", so pain_reduced alone tripped numbness_remaining.
        if trigger_matches(rule.get("trigger") or {}, tag_set):
            hits.append({"id": rule["id"], "action": rule.get("action", {}), "meaning": rule.get("meaning", "")})
    stage = "巩固期" if "pain_reduced" in tag_set else "观察期"
    return {"followup_stage": stage, "rule_hits": hits, "non_prescriptive": True}
