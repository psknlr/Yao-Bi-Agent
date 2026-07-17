"""Expected-information-gain question selection (sequential Bayesian experimental design).

Following the BED-LLM line of work (Bayesian experimental design for adaptive
information gathering with LLMs; see docs/research_grounding.md), the next
follow-up question is chosen to maximize the expected reduction in Shannon
entropy of the syndrome posterior — instead of a fixed heuristic slot order.

Everything is deterministic and derived from the rule base itself:
- the posterior over syndromes comes from the share-normalized rule scores;
- the answer likelihood P(答=有 | 证型) comes from whether the slot's mapped
  tags participate in that syndrome rule's trigger set (a structural, not
  fitted, likelihood — no training data required, fully auditable);
- EIG(slot) = H(posterior) − E_answer[H(posterior | answer)] in bits.

Safety slots are never displaced by EIG ranking: red-flag coverage is a hard
clinical requirement, only the *discriminative* follow-ups are re-ordered.
"""

from __future__ import annotations

import math
from typing import Any

from backend.skills.uncertainty_skill import _syndrome_triggers

# Structural answer likelihood: how likely a positive answer is when the slot's
# evidence participates (or not) in the syndrome's trigger. Deliberately mild —
# expressing "informative but noisy patient answers", not fitted probabilities.
P_HIT = 0.8
P_MISS = 0.15


def _entropy_bits(probs: list[float]) -> float:
    return -sum(p * math.log2(p) for p in probs if p > 0)


def _normalized_posterior(patterns: list[dict[str, Any]]) -> dict[str, float]:
    weights = {p["pattern"]: max(float(p.get("prob") or 0.0), 1e-6) for p in patterns if p.get("pattern")}
    total = sum(weights.values())
    return {name: w / total for name, w in weights.items()} if total else {}


def expected_information_gain(
    patterns: list[dict[str, Any]],
    slot_tags: dict[str, set[str]],
    candidate_slots: list[str],
) -> list[dict[str, Any]]:
    """Rank candidate slots by the expected entropy reduction of asking them.

    Returns [{"slot", "eig_bits", "linked_tags"}] sorted by EIG descending.
    Slots without any tag linkage get EIG 0 (still askable, ranked last).
    """

    posterior = _normalized_posterior(patterns)
    if not posterior or len(posterior) < 2:
        # Nothing to discriminate — every question is equally (un)informative.
        return [{"slot": s, "eig_bits": 0.0, "linked_tags": sorted(slot_tags.get(s, set()))} for s in candidate_slots]

    triggers = _syndrome_triggers()
    prior_entropy = _entropy_bits(list(posterior.values()))
    ranked: list[dict[str, Any]] = []
    for slot in candidate_slots:
        tags = slot_tags.get(slot, set())
        if not tags:
            ranked.append({"slot": slot, "eig_bits": 0.0, "linked_tags": []})
            continue
        # P(answer=yes | syndrome): does this slot's evidence feed that syndrome's rule?
        likelihood_yes = {
            name: (P_HIT if tags & triggers.get(name, set()) else P_MISS)
            for name in posterior
        }
        p_yes = sum(posterior[name] * likelihood_yes[name] for name in posterior)
        p_no = 1.0 - p_yes
        expected_posterior_entropy = 0.0
        for answer_prob, answer_is_yes in ((p_yes, True), (p_no, False)):
            if answer_prob <= 0:
                continue
            unnormalized = {
                name: posterior[name] * (likelihood_yes[name] if answer_is_yes else 1.0 - likelihood_yes[name])
                for name in posterior
            }
            z = sum(unnormalized.values())
            if z <= 0:
                continue
            expected_posterior_entropy += answer_prob * _entropy_bits([v / z for v in unnormalized.values()])
        ranked.append({
            "slot": slot,
            "eig_bits": round(max(0.0, prior_entropy - expected_posterior_entropy), 4),
            "linked_tags": sorted(tags),
        })
    ranked.sort(key=lambda item: item["eig_bits"], reverse=True)
    return ranked
