"""Surface xlsx-mined rule candidates as clinician-only research evidence.

候选规则来自 backend.mining.xlsx_case_miner 的脱敏聚合产物
（rules/11_mined_rule_candidates.yaml）。所有规则 status=pending_expert_review，
只作为医师端研究证据展示，不参与自动决策，不向患者输出。
"""

from __future__ import annotations

from typing import Any

from backend.engine.rule_engine import RULES_DIR, load_yaml

MINED_RULES_FILE = RULES_DIR / "11_mined_rule_candidates.yaml"

DISCLAIMER = "挖掘候选规则均为脱敏统计信号，待专家审核（pending_expert_review）；仅供医师复核与科研教学，不构成诊断或处方依据，不向患者展示。"


def load_mined_rules() -> dict[str, Any]:
    if not MINED_RULES_FILE.exists():
        return {}
    return load_yaml(MINED_RULES_FILE) or {}


def mined_evidence_skill(
    normalized_tags: list[str] | None,
    syndrome_candidates: list[dict[str, Any]] | None = None,
    max_rules: int = 8,
) -> dict[str, Any]:
    data = load_mined_rules()
    candidates = data.get("rule_candidates") or []
    tags = set(normalized_tags or [])
    zheng_names = {item.get("name") for item in (syndrome_candidates or []) if isinstance(item, dict)}

    matches: list[dict[str, Any]] = []
    for rule in candidates:
        condition = rule.get("if") or {}
        hit = False
        if "tag" in condition:
            tag = str(condition["tag"])
            if tag.startswith("zheng::"):
                hit = tag.split("::", 1)[1] in zheng_names
            else:
                hit = tag in tags
        elif "zheng_any" in condition:
            hit = bool(set(condition["zheng_any"]) & zheng_names)
        if hit:
            matches.append(rule)

    matches.sort(
        key=lambda r: (
            float((r.get("statistics") or {}).get("lift", 0) or 0),
            float((r.get("statistics") or {}).get("support", 0) or 0),
        ),
        reverse=True,
    )
    return {
        "mined_evidence": matches[:max_rules],
        "mined_rules_available": bool(candidates),
        "dataset_stats": data.get("dataset_stats") or {},
        "data_quality": data.get("data_quality") or {},
        "disclaimer": DISCLAIMER,
    }
