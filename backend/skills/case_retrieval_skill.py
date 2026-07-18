"""Case-cohort evidence retrieval + engine↔cohort route concordance (v0.15, G1).

The 209 de-identified outpatient cases are mined into aggregate signals only
(`rules/11_mined_rule_candidates.yaml`); no case-level corpus is persisted, so true
individual-case retrieval is out of scope here (P2, needs a governed de-identified
corpus). What this skill adds over `mined_evidence_skill` — which filters the mined
candidates by global lift/support — is two things a case-based-reasoning layer needs:

1. **Case-conditioned ranking**: mined formula-route signals are ranked by relevance to
   *this* case's syndrome ranking + cohort support, not by a global order.
2. **Engine↔cohort concordance**: a cross-check answering the question a clinician
   actually asks of a name-physician CDSS — "does the rule engine's recommended formula
   route match what the master physician actually did in similar cohort cases?" —
   emitted as corroborated / supplementary / divergent.

Everything here is clinician-review-only, non-blocking, provenance-cited (de-identified
row numbers, n/183), and never patient-facing — identical governance to mined evidence.
"""

from __future__ import annotations

import re
from typing import Any

from backend.skills.mined_evidence_skill import DISCLAIMER as MINED_DISCLAIMER
from backend.skills.mined_evidence_skill import load_mined_rules

# Suffixes/qualifiers that differ between the engine's route name ("独活寄生汤加减") and the
# mined cohort's route label ("独活寄生汤") but denote the same base formula. Stripped
# before matching so the concordance check is not defeated by cosmetic naming.
_ROUTE_NOISE_RE = re.compile(r"(加减|加味|化裁|类|\(.*?\)|（.*?）|汤剂?|方)")


def _route_core(name: str | None) -> str:
    if not name:
        return ""
    core = _ROUTE_NOISE_RE.sub("", str(name)).strip()
    return core


def _routes_match(engine_route: str, cohort_route: str) -> bool:
    """Same base formula if either core name contains the other (min length 2)."""

    a, b = _route_core(engine_route), _route_core(cohort_route)
    if len(a) < 2 or len(b) < 2:
        return False
    return a in b or b in a


def case_retrieval_skill(
    normalized_tags: list[str] | None,
    syndrome_candidates: list[dict[str, Any]] | None = None,
    primary_route: dict[str, Any] | str | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    data = load_mined_rules()
    candidates = data.get("rule_candidates") or []

    # Case syndrome ranking → relevance weight (top syndrome weighs most).
    ranked = [c.get("name") for c in (syndrome_candidates or []) if isinstance(c, dict) and c.get("name")]
    syndrome_weight = {name: 1.0 / (idx + 1) for idx, name in enumerate(ranked)}
    case_syndromes = set(ranked)

    formula_hits: list[dict[str, Any]] = []
    for rule in candidates:
        if rule.get("rule_type") != "formula_route":
            continue
        cohort_zheng = set((rule.get("if") or {}).get("zheng_any") or [])
        stats = rule.get("statistics") or {}
        support = float(stats.get("support", 0) or 0)
        top_zheng = stats.get("top_zheng")
        # Case-conditioned relevance: how strongly does the case's own syndrome ranking
        # intersect this cohort signal, plus the cohort's empirical support.
        overlap = case_syndromes & cohort_zheng
        syndrome_relevance = sum(syndrome_weight.get(z, 0.0) for z in overlap)
        if top_zheng in case_syndromes:
            syndrome_relevance += syndrome_weight.get(top_zheng, 0.0)
        relevance = round(2.0 * syndrome_relevance + support, 4)
        if relevance <= 0:
            continue
        formula_hits.append({
            "candidate_formula_route": (rule.get("then") or {}).get("candidate_formula_route"),
            "relevance": relevance,
            "matched_syndromes": sorted(overlap),
            "cohort_top_syndrome": top_zheng,
            "n_cases": stats.get("n_cases"),
            "support": support,
            "evidence": rule.get("evidence"),
            "strength": rule.get("strength"),
            "rule_id": rule.get("rule_id"),
        })

    formula_hits.sort(key=lambda h: (h["relevance"], h["support"]), reverse=True)
    retrieved = formula_hits[:top_k]

    concordance = _route_concordance(primary_route, formula_hits)

    return {
        "retrieved_cohort_evidence": retrieved,
        "route_concordance": concordance,
        "cohort_available": bool(candidates),
        "dataset_stats": data.get("dataset_stats") or {},
        "disclaimer": MINED_DISCLAIMER,
        "clinician_only": True,
        "non_prescriptive": True,
    }


def _route_concordance(
    primary_route: dict[str, Any] | str | None,
    formula_hits: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Cross-check the engine's chosen route against the case-relevant cohort routes."""

    engine_name = primary_route.get("name") if isinstance(primary_route, dict) else primary_route
    if not engine_name or not formula_hits:
        return None

    # Cohort routes relevant to this case (relevance-ranked, already filtered > 0).
    relevant = [h for h in formula_hits if h["relevance"] > 0]
    matched = next((h for h in relevant if _routes_match(engine_name, h.get("candidate_formula_route") or "")), None)
    top_cohort = relevant[0] if relevant else None

    if matched is not None:
        verdict = "corroborated"
        note = (
            f"规则引擎方路「{engine_name}」与经验队列相似病例的高频用方一致"
            f"（{matched.get('candidate_formula_route')}，{matched.get('evidence')}）。"
        )
    elif top_cohort is not None:
        verdict = "supplementary"
        note = (
            f"规则引擎方路「{engine_name}」未直接出现在本案证型的经验队列高频用方中；"
            f"队列提示可另行参考「{top_cohort.get('candidate_formula_route')}」"
            f"（{top_cohort.get('evidence')}），供医师复核鉴别。"
        )
    else:
        verdict = "divergent"
        note = f"规则引擎方路「{engine_name}」在经验队列中无相似证型支持信号，请医师重点复核。"

    return {
        "verdict": verdict,
        "engine_route": engine_name,
        "cohort_match": matched.get("candidate_formula_route") if matched else None,
        "note": note,
        "clinician_review_required": True,
    }
