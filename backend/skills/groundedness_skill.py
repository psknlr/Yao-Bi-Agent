"""Claim-level groundedness check: which clinical entities in a model answer are
backed by the rule/mining evidence, and which come from the model's own knowledge.

Inspired by the RAG-faithfulness / attribution literature (grounded attributions,
claim-level grounding, hallucination-by-omission-of-source; see
docs/research_grounding.md): instead of trusting or blocking the model wholesale,
every syndrome / formula / herb it names is checked against the evidence bundle
that grounded the generation. Ungrounded entities are not censored — the design
goal of the consultation is precisely that the model *adds* its own TCM knowledge —
they are **labeled**, so the reviewing physician knows which statements carry rule
provenance and which carry only model provenance.

Deterministic, lexicon-based, zero model calls: auditable and free.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from backend.engine.rule_engine import load_rule_file

# Classic formulas the prompts explicitly invite the model to discuss — they are
# legitimate model knowledge, but still labeled when absent from the evidence.
_CLASSIC_FORMULAS = [
    "独活寄生汤", "当归四逆汤", "桂枝芍药知母汤", "黄芪桂枝五物汤", "身痛逐瘀汤",
    "小柴胡汤", "四物汤", "八珍汤", "六味地黄丸", "金匮肾气丸", "右归丸", "左归丸",
    "四妙丸", "三妙丸", "甘姜苓术汤", "肾着汤", "补阳还五汤", "乌头汤",
]

_EXTRA_SYNDROMES = [
    "寒湿痹阻证", "湿热痹阻证", "肾阴不足证", "脾虚湿困证", "痰瘀互结证", "气血不足证",
]


def _base_formula(name: str) -> str:
    """Strip 加减/路线/类方 suffixes so route names match classic formula names."""

    return re.sub(r"(加减|路线|类方|类)$", "", str(name or "").strip())


@lru_cache(maxsize=1)
def _lexicons() -> dict[str, tuple[str, ...]]:
    syndromes = {(rule.get("effect") or {}).get("syndrome") for rule in load_rule_file("02_syndrome_rules.yaml") or []}
    syndromes.discard(None)
    syndromes.update(_EXTRA_SYNDROMES)

    formulas: set[str] = set(_CLASSIC_FORMULAS)
    for rule in load_rule_file("03_formula_rules.yaml") or []:
        route = (rule.get("effect") or {}).get("formula_route")
        if route:
            formulas.add(_base_formula(route))

    herbs: set[str] = set()
    modules_cfg = (load_rule_file("04_module_rules.yaml") or {}).get("modules") or {}
    for module in modules_cfg.values() if isinstance(modules_cfg, dict) else modules_cfg:
        herbs.update((module or {}).get("herbs") or [])
    herbs.update(((load_rule_file("05_dose_rules.yaml") or {}).get("dose_rules") or {}).keys())
    herbs.update((load_rule_file("07_safety_rules.yaml") or {}).get("toxic_or_high_risk_herbs") or [])
    conflict_cfg = load_rule_file("06_conflict_rules.yaml") or {}
    for section in ("conflicts",):
        for rule in conflict_cfg.get(section, []) or []:
            herbs.update(rule.get("group_a") or [])
            herbs.update(rule.get("group_b") or [])
    for section in ("herb_drug_interactions", "comorbidity_contraindications"):
        for rule in conflict_cfg.get(section, []) or []:
            herbs.update(rule.get("herbs") or [])

    # Longest-first so 独活寄生汤 wins before the herb 独活 inside the same span.
    return {
        "syndrome": tuple(sorted(syndromes, key=len, reverse=True)),
        "formula": tuple(sorted(formulas, key=len, reverse=True)),
        "herb": tuple(sorted(herbs, key=len, reverse=True)),
    }


def _entities_in_text(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {"syndrome": set(), "formula": set(), "herb": set()}
    remaining = text
    for kind in ("formula", "syndrome", "herb"):
        for entity in _lexicons()[kind]:
            if entity and entity in remaining:
                found[kind].add(entity)
                if kind == "formula":
                    # Mask matched formulas so their constituent herb names inside the
                    # formula name (独活寄生汤 → 独活) are not double-counted as herbs.
                    remaining = remaining.replace(entity, "□" * len(entity))
    return found


def _evidence_entities(evidence: dict[str, Any]) -> dict[str, set[str]]:
    ev: dict[str, set[str]] = {"syndrome": set(), "formula": set(), "herb": set()}
    for candidate in evidence.get("syndrome_candidates") or []:
        if candidate.get("name"):
            ev["syndrome"].add(candidate["name"])
    for route in evidence.get("formula_routes") or []:
        if route.get("name"):
            ev["formula"].add(_base_formula(route["name"]))
    for module in evidence.get("herb_modules") or []:
        ev["herb"].update(module.get("herbs") or [])
    return ev


# Assertive-certainty markers: rule evidence in this system is always draft-level
# (candidate syndromes / route signals pending physician review), so a sentence that
# commits to a syndrome or formula with certainty overstates what the evidence supports
# — even when the entity itself is rule-backed. This is the claim-modality layer on top
# of entity grounding (full claim-graph verification is roadmap; see docs).
_ASSERTIVE = re.compile(r"必须|无疑|必然(?:是|属)?|明确属|确定[为是]|即属|只能是|断为|毫无疑问")
_HEDGED = re.compile(r"倾向|可能|可虑|可考虑|供.{0,4}审|待医师|建议|提示|似|疑似")


def _overstatements(text: str, mentioned: dict[str, set[str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for sentence in re.split(r"[。！？!?\n;；]", text or ""):
        if not _ASSERTIVE.search(sentence) or _HEDGED.search(sentence):
            continue
        entities = [e for kind in ("syndrome", "formula") for e in mentioned[kind] if e in sentence]
        if entities:
            findings.append({"entities": entities, "sentence": sentence.strip()[:80]})
    return findings


def check_groundedness(text: str, evidence: dict[str, Any]) -> dict[str, Any]:
    """Compare clinical entities in the model text against the evidence bundle.

    Returns per-type grounded/ungrounded entity lists, an overall grounding ratio,
    assertiveness overstatement candidates, and a Chinese annotation for the
    reviewing physician. Never blocks output.
    """

    mentioned = _entities_in_text(text or "")
    backed = _evidence_entities(evidence or {})
    grounded: dict[str, list[str]] = {}
    ungrounded: dict[str, list[str]] = {}
    total = hits = 0
    for kind in ("syndrome", "formula", "herb"):
        names = mentioned[kind]
        kind_backed = backed[kind]
        if kind == "formula":
            names = {_base_formula(n) for n in names}
        grounded[kind] = sorted(names & kind_backed)
        ungrounded[kind] = sorted(names - kind_backed)
        total += len(names)
        hits += len(grounded[kind])

    ratio = round(hits / total, 3) if total else None
    overstatements = _overstatements(text or "", mentioned)
    ungrounded_flat = [f"{name}（{ {'syndrome': '证型', 'formula': '方剂', 'herb': '药物'}[kind] }）"
                       for kind in ("syndrome", "formula", "herb") for name in ungrounded[kind]]
    if total == 0:
        annotation = "未检出可核对的证型/方剂/药物实体。"
    elif not ungrounded_flat:
        annotation = f"证据接地率 {ratio:.0%}：文中全部证型/方剂/药物实体均见于规则/挖掘证据。"
    else:
        annotation = (
            f"证据接地率 {ratio:.0%}。以下实体来自模型自身知识、未见于本案规则证据，"
            f"需医师重点复核：{'、'.join(ungrounded_flat[:10])}。"
        )
    if overstatements:
        annotation += (
            f" 另检出 {len(overstatements)} 处断言强度超过证据级别的表述"
            "（规则证据仅为待审草案，不支持确定性结论），请医师复核相应句子。"
        )
    return {
        "grounding_ratio": ratio,
        "checked_entities": total,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "overstatements": overstatements,
        "annotation": annotation,
        "method": "lexicon_entity_grounding_plus_claim_modality",
        "blocking": False,
    }
