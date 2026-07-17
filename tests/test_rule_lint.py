"""Rule-base consistency lint (CI gate).

Guards the invariant behind "rule-first": every tag a rule can trigger on must have a
real production path — either a controlled-vocabulary entry in rules/01_tags.yaml
(text aliases / computed fields) or a declared syndrome-derived tag
(formula_base_selector_skill.DERIVED_TAGS). A tag that exists only inside a rule's
trigger list is a *dead condition*: the rule silently loses part of its intended
sensitivity and nobody notices. That exact bug shipped once (ganshen_buzu-style tags
referenced by 03_formula_rules.yaml with no producer), hence this lint.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from backend.skills.case_normalize_skill import TAG_MAP
from backend.skills.formula_base_selector_skill import DERIVED_TAGS

RULES_DIR = Path(__file__).resolve().parents[1] / "rules"


def _load(name: str):
    with open(RULES_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _registry_tags() -> set[str]:
    return set((_load("01_tags.yaml") or {}).get("tags") or {})


def _rule_list_tags(rules: list[dict]) -> dict[str, set[str]]:
    """rule_id -> referenced tags (trigger.any/all + contra)."""

    out: dict[str, set[str]] = {}
    for rule in rules or []:
        trigger = rule.get("trigger") or {}
        tags = set(trigger.get("any") or []) | set(trigger.get("all") or []) | set(rule.get("contra") or [])
        out[str(rule.get("id"))] = tags
    return out


def test_syndrome_and_formula_rule_tags_are_producible():
    known = _registry_tags() | DERIVED_TAGS
    problems = []
    for filename in ("02_syndrome_rules.yaml", "03_formula_rules.yaml"):
        for rule_id, tags in _rule_list_tags(_load(filename)).items():
            dead = tags - known
            if dead:
                problems.append(f"{filename}:{rule_id} references undefined tags {sorted(dead)}")
    assert not problems, "dead rule conditions (no producer for tag):\n" + "\n".join(problems)


def test_module_trigger_tags_are_producible():
    known = _registry_tags() | DERIVED_TAGS
    modules = (_load("04_module_rules.yaml") or {}).get("modules") or {}
    problems = []
    for key, module in modules.items():
        dead = set(module.get("triggers") or []) - known
        if dead:
            problems.append(f"04_module_rules.yaml:{key} references undefined tags {sorted(dead)}")
    assert not problems, "dead module triggers (no producer for tag):\n" + "\n".join(problems)


def test_hardcoded_tag_map_targets_exist_in_registry():
    registry = _registry_tags()
    missing = {tag for tag in TAG_MAP.values() if tag not in registry}
    assert not missing, f"case_normalize_skill.TAG_MAP maps to unregistered tags: {sorted(missing)}"


def test_rules_have_id_category_rationale_and_unique_ids():
    seen: set[str] = set()
    for filename, expected_category in (("02_syndrome_rules.yaml", "syndrome"), ("03_formula_rules.yaml", "formula")):
        for rule in _load(filename) or []:
            rule_id = rule.get("id")
            assert rule_id, f"{filename}: rule without id: {rule.get('name')}"
            assert rule_id not in seen, f"duplicate rule id {rule_id}"
            seen.add(rule_id)
            assert rule.get("category") == expected_category, f"{rule_id}: category mismatch"
            assert rule.get("rationale"), f"{rule_id}: missing rationale"
            assert rule.get("effect"), f"{rule_id}: missing effect"


def test_formula_rules_carry_route_and_core_module():
    for rule in _load("03_formula_rules.yaml") or []:
        effect = rule.get("effect") or {}
        assert effect.get("formula_route"), f"{rule.get('id')}: formula rule without formula_route"
        assert effect.get("core_module"), f"{rule.get('id')}: formula rule without core_module"


def test_derived_tags_do_not_shadow_registry_aliases():
    # A derived tag must not also be alias-extractable under a *different* concept —
    # the derivation is its single source of truth.
    tags_cfg = (_load("01_tags.yaml") or {}).get("tags") or {}
    for derived in DERIVED_TAGS:
        spec = tags_cfg.get(derived)
        assert spec is None or not (spec or {}).get("aliases"), (
            f"derived tag {derived} must not carry text aliases (double producer)"
        )
