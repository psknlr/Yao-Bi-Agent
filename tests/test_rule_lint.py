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


# ----------------------------------------------------------------------------- #
# Reachability lints (v0.15): the rule base extracts high-risk drug/comorbidity   #
# terms and questionnaire options, but nothing guaranteed each one could reach an  #
# interaction rule. The CRITICAL gap this guards: 冠心病 / 肾功能异常 / 地塞米松 were    #
# all extracted yet triggered no interruptive contraindication alert.             #
# ----------------------------------------------------------------------------- #

# Terms that are deliberately NOT expected to hit an interaction rule (benign, or
# handled elsewhere): keep this list tiny and justified — it is the audited escape
# hatch, not a dumping ground.
_INTERACTION_VOCAB_EXEMPT = {
    "二甲双胍",   # 降糖药，无直接草药相互作用规则（记录用途）
    "乙哌立松",   # 肌松药，暂无叠加禁忌规则
    "甲钴胺",     # 维生素 B12，无相互作用
    "糖尿病",     # 病史采集用途，非草药禁忌
    "骨质疏松",   # 触发红旗/随访逻辑，非相互作用规则
    "肿瘤病史",   # 触发红旗筛查，非相互作用规则
    "过敏史",     # 走 C004 过敏路径
}


def _interaction_terms(section: str, field: str) -> set[str]:
    rules = (_load("06_conflict_rules.yaml") or {}).get(section) or []
    terms: set[str] = set()
    for rule in rules:
        terms.update(str(t) for t in (rule.get(field) or []))
    return terms


def _reachable(term: str, rule_terms: set[str]) -> bool:
    from backend.engine.conflict_resolver import _terms_match

    return any(_terms_match(term, rt) for rt in rule_terms)


def test_extracted_medications_reach_an_interaction_rule():
    from backend.skills.case_extract_skill import MEDICATION_TERMS

    drug_terms = _interaction_terms("herb_drug_interactions", "drugs")
    unreachable = [
        m for m in MEDICATION_TERMS
        if m not in _INTERACTION_VOCAB_EXEMPT and not _reachable(m, drug_terms)
    ]
    assert not unreachable, (
        "extracted medications with no interaction rule (silent contraindication miss): "
        f"{unreachable} — add them to rules/06_conflict_rules.yaml drugs lists or exempt"
    )


def test_extracted_comorbidities_reach_an_interaction_rule():
    from backend.skills.case_extract_skill import COMORBIDITY_TERMS

    cond_terms = _interaction_terms("comorbidity_contraindications", "conditions")
    unreachable = [
        c for c in COMORBIDITY_TERMS
        if c not in _INTERACTION_VOCAB_EXEMPT and not _reachable(c, cond_terms)
    ]
    assert not unreachable, (
        "extracted comorbidities with no interaction rule (silent contraindication miss): "
        f"{unreachable} — add them to rules/06_conflict_rules.yaml conditions lists or exempt"
    )


def test_questionnaire_high_risk_options_reach_an_interaction_rule():
    """C001 disease / C002 medication options are passed verbatim into check_interactions;
    every non-benign option must be able to trip a rule (the FSM-path analogue of the
    extraction lints above)."""

    questions = (_load("10_case_guide_questions.yaml") or {})
    by_id = {q["id"]: q for group in questions.values() if isinstance(group, list)
             for q in group if isinstance(q, dict) and q.get("id")}
    drug_terms = _interaction_terms("herb_drug_interactions", "drugs")
    cond_terms = _interaction_terms("comorbidity_contraindications", "conditions")

    problems: list[str] = []
    for opt in (by_id.get("C001", {}).get("options") or []):
        base = str(opt).split("/")[0]  # "胃溃疡/胃炎" -> reach on the first token
        if base in _INTERACTION_VOCAB_EXEMPT or base in {"无", "不清楚", "高血压"}:
            continue
        if not _reachable(base, cond_terms):
            problems.append(f"C001 option {opt!r}")
    for opt in (by_id.get("C002", {}).get("options") or []):
        if opt in _INTERACTION_VOCAB_EXEMPT or opt in {"其他", "没有", "不清楚"}:
            continue
        if not _reachable(str(opt), drug_terms):
            problems.append(f"C002 option {opt!r}")
    # 高血压 is covered by mahuang/gancao rules; assert that explicitly rather than skipping.
    assert _reachable("高血压", cond_terms), "高血压 must reach a comorbidity rule"
    assert not problems, "questionnaire high-risk options with no interaction rule: " + ", ".join(problems)


def test_effect_and_followup_modules_exist_in_module_registry():
    """Every module name referenced by a syndrome rule (effect.modules) or a followup
    rule (*_modules) must be a real module in 04_module_rules.yaml — a dangling name
    shows the clinician a 'recommended module' the composer can never build."""

    module_names = {
        m.get("name") for m in ((_load("04_module_rules.yaml") or {}).get("modules") or {}).values()
        if isinstance(m, dict) and m.get("name")
    }
    problems: list[str] = []
    for rule in _load("02_syndrome_rules.yaml") or []:
        for mod in (rule.get("effect") or {}).get("modules") or []:
            if mod not in module_names:
                problems.append(f"02:{rule.get('id')} effect.modules -> {mod!r}")
    for rule in (_load("08_followup_rules.yaml") or {}).get("followup_rules") or []:
        action = rule.get("action") or {}
        for key in ("consider_modules", "add_modules", "reduce_modules"):
            for mod in action.get(key) or []:
                if mod not in module_names:
                    problems.append(f"08:{rule.get('id')}.{key} -> {mod!r}")
    assert not problems, "dangling module references (composer can never build them):\n" + "\n".join(problems)


# Tags a questionnaire option may emit that legitimately have no *rule* consumer.
# This is the audited escape hatch: adding a tag here is a conscious decision that the
# answer is captured for the record but not (yet) wired into deterministic scoring.
# CI fails on any NEW orphan, forcing that decision to be explicit instead of silent.
_QUESTIONNAIRE_TAG_EXEMPT = {
    # Structural / record-only tags that drive red-flag, followup or UI paths, not scoring.
    "osteoporosis", "cancer_history", "anticoagulant_use", "compression_fracture_history",
    # Consumed by the signal skills (shen_rule_signal / case_structuring), not by a rule.
    "cold_damp_signal",
    # RECORDED-ONLY (captured in the case, not yet routed into a syndrome rule):
    #   nerve_root_irritation_possible — 咳嗽打喷嚏加重（Dejerine 征）神经根刺激线索，
    #       属神经根定位信息，无对应腰痹证型规则，供医师复核，暂不参与证型打分。
    #   night_sweat — 盗汗多属阴虚线索，本规则库无阴虚证型，作为病历记录项保留。
    "nerve_root_irritation_possible", "night_sweat",
}


def test_questionnaire_tag_mappings_have_a_consumer():
    """Reverse of the producer lint: every tag a questionnaire option emits (10_case_guide
    tag_mapping) must reach a rule that triggers on it — otherwise the patient's answer is
    silently dropped (the 冷痛->cold_pain / 阴雨天->damp_weather_aggravation dead-label bug).
    Tags with no rule consumer must be listed in _QUESTIONNAIRE_TAG_EXEMPT (audited)."""

    consumed: set[str] = set()
    # Tags consumed by rule triggers/contra across the syndrome+formula base + module triggers.
    for filename in ("02_syndrome_rules.yaml", "03_formula_rules.yaml"):
        for tags in _rule_list_tags(_load(filename)).values():
            consumed |= tags
    for module in ((_load("04_module_rules.yaml") or {}).get("modules") or {}).values():
        consumed |= set(module.get("triggers") or [])

    questions = (_load("10_case_guide_questions.yaml") or {})
    problems: list[str] = []
    for group in questions.values():
        if not isinstance(group, list):
            continue
        for q in group:
            if not isinstance(q, dict):
                continue
            for _opt, tags in (q.get("tag_mapping") or {}).items():
                for tag in tags:
                    if tag not in consumed and tag not in _QUESTIONNAIRE_TAG_EXEMPT:
                        problems.append(f"{q.get('id')}:{_opt} -> {tag} (no rule consumes it)")
    assert not problems, (
        "questionnaire tag_mappings with no rule consumer (answer silently dropped) — "
        "wire to a rule or add to _QUESTIONNAIRE_TAG_EXEMPT with justification:\n" + "\n".join(problems)
    )


def test_questionnaire_evidence_reaches_syndrome_engine():
    """Behavioural guard for the dead-label fix: the concrete questionnaire answers that
    lost their syndrome candidate now produce one through the real router."""

    from backend.skills.syndrome_router_skill import syndrome_router_skill

    def names(tags):
        return {c["name"] for c in syndrome_router_skill(tags).get("syndrome_candidates") or []}

    # 冷痛 (P003) + 怕冷 → 肾阳不足证 must surface (was empty under cold_pain).
    assert "肾阳不足证" in names(["deep_cold_pain", "cold_aversion"])
    # 阴雨天 (P005) + 白腻苔 → 寒湿痹阻证 must surface (was empty under damp_weather_aggravation).
    assert "寒湿痹阻证" in names(["cold_aggravation", "white_greasy_coating"])
