"""Conformal syndrome prediction sets with finite-sample coverage guarantees.

Split conformal prediction (Vovk et al.; Angelopoulos & Bates 2023; see
docs/research_grounding.md) applied to the deterministic rule engine: the
labeled golden cases act as the calibration set, and the output is a
*prediction set* of syndromes guaranteed to contain the clinician-labeled
syndrome with probability >= 1 - alpha (marginally, under exchangeability).

A prediction set is the statistical formalization of a differential
diagnosis: instead of one top candidate with an ad-hoc confidence word, the
clinician sees exactly the set of syndromes the engine cannot rule out at
the requested error level.

Honesty notes baked into the output:
- with a small calibration set (n ~ 15) the finite-sample quantile is
  conservative — sets are wider than asymptotically necessary, never
  anti-conservative (the guarantee direction is preserved);
- coverage is marginal over cases, not per-syndrome (class-conditional
  coverage would need far more calibration data);
- the guarantee is relative to the golden-case labeling distribution, which
  is project-labeled and not yet independently adjudicated.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from backend.skills.case_extract_skill import case_extract_skill
from backend.skills.case_normalize_skill import case_normalize_skill
from backend.skills.syndrome_router_skill import syndrome_router_skill

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_CASES_PATH = ROOT / "evaluation" / "golden_cases.yaml"

DEFAULT_ALPHA = 0.1

_CALIBRATION_CACHE: dict[float, dict[str, Any]] | None = None


def _nonconformity(candidates: list[dict[str, Any]], true_syndrome: str) -> float:
    """Score-ratio nonconformity: 0 when the label leads, 1 when it is absent.

    alpha_i = 1 - score(true)/score(top). Monotone in "how far the engine's
    ranking is from the label", scale-free across cases with different
    absolute score levels.
    """

    if not candidates:
        return 1.0
    top_score = max(float(c.get("score") or 0) for c in candidates) or 1.0
    for c in candidates:
        if c.get("name") == true_syndrome:
            return 1.0 - (float(c.get("score") or 0) / top_score)
    return 1.0


def _rank_candidates(input_text: str) -> list[dict[str, Any]]:
    normalized = case_normalize_skill(case_extract_skill(input_text))
    return syndrome_router_skill(normalized["normalized_tags"]).get("syndrome_candidates") or []


def load_calibration_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Golden cases usable for calibration: labeled top-1 syndrome, no known gap."""

    with open(path or GOLDEN_CASES_PATH, encoding="utf-8") as f:
        cases = (yaml.safe_load(f) or {}).get("golden_cases") or []
    usable = []
    for case in cases:
        expected = case.get("expected") or {}
        if case.get("known_gap") or not expected.get("top1_syndrome"):
            continue
        usable.append({"id": case.get("id"), "input_text": case["input_text"], "label": expected["top1_syndrome"]})
    return usable


def _finite_sample_qhat(scores: list[float], alpha: float) -> float:
    """The split-conformal quantile: ceil((n+1)(1-alpha))/n empirical quantile."""

    n = len(scores)
    if n == 0:
        return 1.0
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        # Too few calibration points for this alpha — only the trivial (full) set
        # carries the guarantee. Reported as-is rather than silently sharpened.
        return 1.0
    return sorted(scores)[k - 1]


def calibrate(alpha: float = DEFAULT_ALPHA, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compute the conformal threshold q̂ from the golden calibration set (cached)."""

    global _CALIBRATION_CACHE
    if cases is None and _CALIBRATION_CACHE is not None and alpha in _CALIBRATION_CACHE:
        return _CALIBRATION_CACHE[alpha]

    calibration_cases = cases if cases is not None else load_calibration_cases()
    scores = [_nonconformity(_rank_candidates(c["input_text"]), c["label"]) for c in calibration_cases]
    result = {
        "alpha": alpha,
        "target_coverage": round(1 - alpha, 3),
        "q_hat": round(_finite_sample_qhat(scores, alpha), 4),
        "calibration_n": len(scores),
        "trivial": _finite_sample_qhat(scores, alpha) >= 1.0,
    }
    if cases is None:
        if _CALIBRATION_CACHE is None:
            _CALIBRATION_CACHE = {}
        _CALIBRATION_CACHE[alpha] = result
    return result


def conformal_prediction_set(
    candidates: list[dict[str, Any]],
    alpha: float = DEFAULT_ALPHA,
    calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prediction set for one case: every syndrome the engine cannot rule out.

    Membership rule mirrors the nonconformity score: a candidate is in the set
    iff 1 - score/top_score <= q̂, i.e. score >= (1 - q̂) * top_score.
    """

    cal = calibration or calibrate(alpha)
    q_hat = float(cal["q_hat"])
    names: list[str] = []
    if candidates:
        top_score = max(float(c.get("score") or 0) for c in candidates) or 1.0
        names = [c["name"] for c in candidates if 1.0 - (float(c.get("score") or 0) / top_score) <= q_hat]
    note = (
        f"项目内校准的候选证型集合（校准集 n={cal['calibration_n']}）：提示在当前规则打分下尚不能排除的证型；"
        f"目标覆盖率 {cal['target_coverage']:.0%} 仅相对项目内标注分布按边际意义成立，小样本下集合偏保守，"
        "不代表真实临床人群的诊断概率，不构成统计学意义上的临床正确性保证"
    )
    if cal.get("trivial"):
        note += "；校准样本过少，本集合退化为全部候选（无排除力）"
    return {
        "prediction_set": names,
        "set_size": len(names),
        "alpha": cal["alpha"],
        "target_coverage": cal["target_coverage"],
        "q_hat": cal["q_hat"],
        "calibration_n": cal["calibration_n"],
        "coverage_note": note,
        "method": "split_conformal_score_ratio",
    }


def leave_one_out_coverage(alpha: float = DEFAULT_ALPHA) -> dict[str, Any]:
    """Honest small-sample evaluation: LOO empirical coverage + average set size.

    For each labeled case, calibrate on the remaining cases and test whether the
    held-out label lands in its prediction set. This is the benchmark-facing
    check that the guarantee is not just theoretical.
    """

    cases = load_calibration_cases()
    if len(cases) < 3:
        return {"coverage": None, "avg_set_size": None, "n": len(cases), "note": "样本不足，无法评估"}
    hits = 0
    set_sizes: list[int] = []
    for i, held_out in enumerate(cases):
        cal = calibrate(alpha, cases=[c for j, c in enumerate(cases) if j != i])
        candidates = _rank_candidates(held_out["input_text"])
        result = conformal_prediction_set(candidates, alpha, calibration=cal)
        set_sizes.append(result["set_size"])
        if held_out["label"] in result["prediction_set"]:
            hits += 1
    return {
        "coverage": round(hits / len(cases), 3),
        "target_coverage": round(1 - alpha, 3),
        "avg_set_size": round(sum(set_sizes) / len(set_sizes), 2),
        "n": len(cases),
    }
