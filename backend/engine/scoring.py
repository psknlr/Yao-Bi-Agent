from __future__ import annotations

from typing import Iterable


def confidence_from_score(score: int) -> str:
    # Single rule per syndrome/route caps the base score at 5; the evidence-richness
    # bonus (see RuleEngine.score_syndromes) makes >=7 reachable for well-supported hits.
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    return "low"


def score_overlap(tags: Iterable[str], triggers: Iterable[str], weight: int = 1) -> tuple[int, list[str]]:
    overlap = sorted(set(tags) & set(triggers))
    return len(overlap) * weight, overlap
