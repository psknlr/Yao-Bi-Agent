from __future__ import annotations

from collections import defaultdict
from typing import Any

from backend.engine.rule_engine import RuleEngine
from backend.engine.scoring import confidence_from_score

# Syndrome-level tags derived from the syndrome router's candidates — the registered
# bridge between the syndrome layer and formula/module triggers. The rule lint
# (tests/test_rule_lint.py) treats DERIVED_TAGS as the only tags a rule may reference
# without a text-alias entry in rules/01_tags.yaml.
SYNDROME_DERIVED_TAGS = {
    "肝肾不足证": "ganshen_buzu",
    "肾阳不足证": "kidney_yang_deficiency",
    "寒湿痹阻证": "cold_damp_obstruction",
    "气滞血瘀证": "stasis_pattern",
}
DERIVED_TAGS = frozenset(SYNDROME_DERIVED_TAGS.values())


# Route gate: a formula route may only be produced on top of a real syndrome
# grounding. Without any candidate — or with only a low-confidence one — the correct
# output is abstention, not a tag-driven route ("elderly alone ⇒ 独活寄生汤" was the
# failure mode this closes). Emergency cases never reach this skill at all (the
# red-flag gate halts upstream), so the four gates the review asked for are:
# in-scope (pipeline scope router) × not-emergency (red-flag gate) × syndrome
# candidate exists × candidate confidence above the abstention floor.
def _route_gate(syndrome_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    top = syndrome_candidates[0] if syndrome_candidates else None
    if top is None:
        return {"allowed": False, "reason": "no_syndrome_candidate",
                "note": "无证型候选：不生成方剂路线（先补充四诊/鉴别信息）。"}
    confidence = top.get("confidence") or confidence_from_score(int(top.get("score") or 0))
    if confidence == "low":
        return {"allowed": False, "reason": "low_confidence_syndrome",
                "note": f"首选证型「{top.get('name')}」置信不足（{confidence}），方剂路线弃权。"}
    return {"allowed": True, "reason": None, "note": None}


def formula_base_selector_skill(normalized_tags: list[str], syndrome_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    gate = _route_gate(syndrome_candidates or [])
    if not gate["allowed"]:
        return {"formula_routes": [], "primary_route": None, "formula_rule_hits": [], "route_gate": gate}

    # Syndrome-formula compatibility (v0.11): a route may only be derived from the
    # top-1 syndrome or medium+/high-confidence candidates — background tags alone
    # (elderly, osteoporosis) can no longer pull 独活寄生汤 into a 少阳/湿热 case.
    eligible_syndromes = {
        c.get("name") for i, c in enumerate(syndrome_candidates or [])
        if c.get("name") and (i == 0 or c.get("confidence") in ("medium", "high"))
    }

    tags = set(normalized_tags)
    for candidate in syndrome_candidates:
        if candidate.get("name") not in eligible_syndromes:
            continue  # low-confidence trailing candidates do not feed derived tags
        derived = SYNDROME_DERIVED_TAGS.get(candidate.get("name") or "")
        if derived:
            tags.add(derived)
    engine = RuleEngine(["03_formula_rules.yaml"])
    hits = [
        hit for hit in engine.match(tags, category="formula")
        if not hit.compatible_syndromes or set(hit.compatible_syndromes) & eligible_syndromes
    ]
    scores: dict[str, int] = defaultdict(int)
    route_hits: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in hits:
        route = hit.effect.get("formula_route")
        if route:
            # Same corroboration bonus as syndrome scoring: evidence beyond two tags
            # strengthens the route, making "high" confidence reachable. Contradicting
            # case tags (the rule's `contra` list) subtract, mirroring syndrome scoring.
            bonus = max(0, len(set(hit.evidence_tags)) - 2)
            penalty = RuleEngine.CONTRA_PENALTY * len(hit.contra_tags)
            scores[route] += int(hit.effect.get("route_score", 0)) + bonus - penalty
            route_hits[route].append(hit.to_dict())
    routes = []
    for route, score in sorted(scores.items(), key=lambda item: item[1], reverse=True):
        if score <= 0:
            continue
        first = route_hits[route][0]
        routes.append({
            "name": route,
            "score": score,
            "confidence": confidence_from_score(score),
            "core_module": first["effect"].get("core_module", []),
            "evidence_tags": first["evidence_tags"],
            "rationale": first["rationale"],
            "non_prescriptive": True,
        })
    return {"formula_routes": routes, "primary_route": routes[0] if routes else None,
            "formula_rule_hits": [h.to_dict() for h in hits], "route_gate": gate}
