"""Tao-primary grounded consultation: the language model is the main reasoner.

The deterministic rule engine and de-identified mining provide *grounding evidence*
(candidate syndromes, formula routes, herb modules, safety flags, Shen experience signals,
mined statistics); the model then combines that with its own TCM knowledge and writes a
deep, professional clinician-facing analysis. This inverts the overlay-only design for the
answer text while keeping the safety floor:

* the answer passes ``guard_consultation`` (clinician draft may name formulas / 方义 /
  experience dose ranges, but never patient self-administration; patient role stays strict);
* on any failure / guard rejection / disabled runtime it falls back to the deterministic
  rule answer, so the system never regresses below the rule baseline.
"""

from __future__ import annotations

import math
import os
from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.output_guard import guard_consultation
from backend.skills.groundedness_skill import _entities_in_text, check_groundedness

_DISCLAIMER = "\n\n> 本分析为供执业医师审核的研究 / 教学草案，最终诊断与处方须医师面诊后确定，患者不可据此自行用药。"

# Optional semantic self-consistency (semantic-entropy-lite, Farquhar et al. 2024 —
# see docs/research_grounding.md): sample N answers, cluster them by the *clinical
# meaning* of their conclusions (the set of syndromes+formulas they commit to), and
# report the cluster entropy / agreement. High disagreement across samples marks the
# conclusion as unstable — a confabulation signal — without needing ground truth.
# Default off (TAO_SELF_CONSISTENCY unset or < 2): each extra sample is a full
# generation, which is expensive on a 30B model.


def _conclusion_signature(text: str) -> frozenset[str]:
    entities = _entities_in_text(text or "")
    return frozenset({f"s:{n}" for n in entities["syndrome"]} | {f"f:{n}" for n in entities["formula"]})


def _cluster_by_meaning(signatures: list[frozenset[str]]) -> list[int]:
    """Greedy clustering by Jaccard >= 0.5 on conclusion signatures; returns cluster sizes."""

    clusters: list[tuple[frozenset[str], int]] = []
    for sig in signatures:
        for i, (rep, size) in enumerate(clusters):
            union = len(rep | sig)
            if union == 0 or len(rep & sig) / union >= 0.5:
                clusters[i] = (rep | sig, size + 1)
                break
        else:
            clusters.append((sig, 1))
    return [size for _rep, size in clusters]


def _semantic_consistency(client: DaoClient, context: dict[str, Any], first_text: str) -> dict[str, Any] | None:
    try:
        n_samples = int(os.getenv("TAO_SELF_CONSISTENCY", "0"))
    except ValueError:
        n_samples = 0
    if n_samples < 2:
        return None
    signatures = [_conclusion_signature(first_text)]
    for _ in range(n_samples - 1):
        try:
            signatures.append(_conclusion_signature(client.generate_consultation(context) or ""))
        except DaoRuntimeError:
            break
    sizes = _cluster_by_meaning(signatures)
    n = sum(sizes)
    entropy = -sum((s / n) * math.log2(s / n) for s in sizes) if n else 0.0
    agreement = max(sizes) / n if n else 0.0
    return {
        "n_samples": n,
        "n_clusters": len(sizes),
        "cluster_entropy_bits": round(entropy, 4) + 0.0,
        "agreement": round(agreement, 3),
        "verdict": "stable" if agreement >= 0.6 else "unstable",
        "method": "semantic_entropy_lite_entity_clusters",
    }


def tao_consultation_skill(
    question: str,
    scope: str,
    evidence: dict[str, Any],
    *,
    fallback_text: str,
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
    user_role: str = "clinician",
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "fallback_used": True,
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
        "guard": None,
    }
    if not use_llm:
        return {"answer": fallback_text, "source": "deterministic_rules", "used_llm": False, "tao_runtime": meta}

    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend
    try:
        raw = client.generate_consultation({"question": question, "scope": scope, "evidence": evidence, "user_role": user_role})
        text = (raw or "").strip()
        guard = guard_consultation(text, user_role)
        meta["guard"] = guard
        if not text or not guard["allowed"]:
            meta["status"] = "guard_rejected" if text else "empty"
            return {"answer": fallback_text, "source": "deterministic_rules_fallback", "used_llm": False, "tao_runtime": meta}
        if "执业医师" not in text[-160:]:
            text += _DISCLAIMER
        # Faithfulness transparency: label which clinical entities are rule-backed vs
        # model-own-knowledge, so the physician reviews the right spots.
        groundedness = check_groundedness(text, evidence)
        # Groundedness floor (blocking in exactly one situation): when the rule engine
        # offered *no* syndrome candidate and *no* formula route, the model must abstain,
        # not fill the gap with default TCM content. A formula/syndrome entity that the
        # user did not mention and no rule backs is treated as confabulation and the
        # answer falls back to the deterministic abstain text. Teaching questions that
        # name the formula themselves ("独活寄生汤的方义?") stay answerable.
        has_rule_basis = bool((evidence.get("syndrome_candidates") or []) or (evidence.get("formula_routes") or []))
        if not has_rule_basis:
            invented = [
                name
                for kind in ("formula", "syndrome")
                for name in (groundedness.get("ungrounded") or {}).get(kind, [])
                if name not in question
            ]
            if invented:
                meta.update({"status": "ungrounded_no_rule_basis", "ungrounded_entities": invented[:6]})
                return {
                    "answer": fallback_text, "source": "deterministic_rules_fallback", "used_llm": False,
                    "tao_runtime": meta, "groundedness": groundedness,
                }
        meta.update({"status": "accepted", "fallback_used": False})
        consistency = _semantic_consistency(
            client, {"question": question, "scope": scope, "evidence": evidence, "user_role": user_role}, text,
        )
        if consistency and consistency["verdict"] == "unstable":
            text += (
                f"\n\n> ⚠️ 语义一致性提示：{consistency['n_samples']} 次独立生成得到 "
                f"{consistency['n_clusters']} 种不同结论（一致率 {consistency['agreement']:.0%}），"
                "本答案结论不稳定，医师复核时请格外谨慎。"
            )
        return {
            "answer": text, "source": "tao_primary_grounded", "used_llm": True, "tao_runtime": meta,
            "groundedness": groundedness, "semantic_consistency": consistency,
        }
    except DaoRuntimeError as exc:
        meta.update({"status": "fallback", "error": str(exc)})
        return {"answer": fallback_text, "source": "deterministic_rules_fallback", "used_llm": False, "tao_runtime": meta}
