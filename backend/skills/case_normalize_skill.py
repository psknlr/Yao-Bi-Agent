from __future__ import annotations

from typing import Any

from backend.engine.rule_engine import load_rule_file
from backend.skills.clinical_entity_skill import is_affirmed

# Structured-field value → tag mapping (kept for extractor keyword values).
TAG_MAP = {
    "腰痛": "lumbar_pain",
    "腰腿痛": "lumbar_leg_pain",
    "下肢麻木": "lower_limb_numbness",
    "腿麻": "lower_limb_numbness",
    "畏寒": "cold_aversion",
    "怕冷": "cold_aversion",
    "舌暗": "dark_tongue",
    "舌紫暗": "purple_tongue",
    "苔白腻": "white_greasy_coating",
    "苔腻": "greasy_coating",
    "脉缓": "slow_pulse",
    "脉细": "thin_pulse",
    "口苦": "bitter_taste",
    "口干": "dry_mouth",
    "夜寐差": "insomnia",
    "睡眠差": "insomnia",
    "胃纳差": "poor_appetite",
    "胃脘不适": "epigastric_discomfort",
    "乏力": "fatigue",
    "骨质疏松": "osteoporosis",
    "腰膝酸软": "lumbar_knee_soreness",
}

# Tags whose alias hits come from red-flag screening, not case narrative normalization —
# they are handled by safety_guard_skill on the extractor's *polarity- and
# temporality-resolved* red-flag entities. Routing them through the narrative alias scan
# would bypass temporal semantics ("一周前发热，现已痊愈" would become a current
# fever_or_infection tag and hard-stop the case). Questionnaire-provided tags (client
# intake) still reach safety grading directly via the tag path.
_ALIAS_SKIP_TAGS = {
    "elderly", "very_elderly",
    "trauma_fracture_risk", "cauda_equina_symptoms", "progressive_weakness",
    "fever_or_infection", "cancer_history", "unexplained_weight_loss",
}

_ALIAS_INDEX: list[tuple[str, str]] | None = None


def _alias_index() -> list[tuple[str, str]]:
    """(alias, tag) pairs from rules/01_tags.yaml — the single source of truth for aliases.

    Loading the controlled vocabulary here is what lets rules like R002 (气滞血瘀,
    needs fixed_pain/stabbing_pain) and R006 (脾虚不运, needs poor_appetite 等) actually
    trigger from free text instead of depending on the small hard-coded TAG_MAP.
    """

    global _ALIAS_INDEX
    if _ALIAS_INDEX is None:
        pairs: list[tuple[str, str]] = []
        try:
            tags_cfg = (load_rule_file("01_tags.yaml") or {}).get("tags") or {}
        except OSError:
            tags_cfg = {}
        for tag, spec in tags_cfg.items():
            if tag in _ALIAS_SKIP_TAGS:
                continue
            for alias in (spec or {}).get("aliases") or []:
                pairs.append((str(alias), tag))
        # Longest alias first so 舌紫暗 wins before 舌紫 when both are present.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        _ALIAS_INDEX = pairs
    return _ALIAS_INDEX


def case_normalize_skill(case_json: dict[str, Any]) -> dict[str, Any]:
    tags: set[str] = set()
    evidence: dict[str, list[str]] = {}
    age = case_json.get("age")
    if isinstance(age, int):
        if age >= 60:
            tags.add("elderly")
            evidence.setdefault("elderly", []).append(f"年龄{age}岁")
        if age >= 73:
            tags.add("very_elderly")
            evidence.setdefault("very_elderly", []).append(f"年龄{age}岁")
        if age <= 40:
            tags.add("young_patient")
            evidence.setdefault("young_patient", []).append(f"年龄{age}岁")
    if case_json.get("duration_class") == "久病":
        tags.update(["chronic_yabi", "long_duration"])
        evidence.setdefault("chronic_yabi", []).append(str(case_json.get("duration")))
    for field in ["symptoms", "tongue", "pulse", "western_diagnosis"]:
        for value in case_json.get(field) or []:
            tag = TAG_MAP.get(value)
            if tag:
                tags.add(tag)
                evidence.setdefault(tag, []).append(value)
    text = (case_json.get("evidence") or {}).get("raw_text", "")
    # Polarity-aware: "无放射痛"、"不向小腿放射" must not tag radiating_leg_pain.
    if any(is_affirmed(text, term) for term in ["放射", "坐骨", "小腿", "足部"]):
        tags.add("radiating_leg_pain")
        evidence.setdefault("radiating_leg_pain", []).append("原文提示放射或远端下肢受累")
    # Alias scan over the raw narrative using the controlled vocabulary (01_tags.yaml).
    # Denied mentions ("无口苦"、"夜寐可") stay out of the tag set — negated narrative
    # evidence polluting syndrome scoring is exactly the failure mode this guards.
    if text:
        for alias, tag in _alias_index():
            if tag not in tags and alias in text and is_affirmed(text, alias):
                tags.add(tag)
                evidence.setdefault(tag, []).append(alias)
    return {"normalized_tags": sorted(tags), "tag_evidence": evidence}
