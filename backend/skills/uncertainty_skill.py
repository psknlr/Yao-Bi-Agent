"""Uncertainty quantification and differential guidance for syndrome candidates.

Top-tier CDSS practice ("epistemic humility"): the system must say how sure it
is, when it should abstain, and what missing information would actually change
the assessment — instead of always emitting a confident-looking top candidate.

All logic is deterministic and rule-derived: differential discriminators are the
trigger tags of the competing syndrome rules that the case has not yet provided.
"""

from __future__ import annotations

from typing import Any

from backend.engine.rule_engine import load_rule_file

# Minimum top score for the engine to voice a syndrome tendency at all.
ABSTAIN_SCORE_THRESHOLD = 3
# Margin (top1 - top2) at or above which the leading candidate is "clear".
CLEAR_MARGIN = 3

TAG_CN = {
    "dark_tongue": "舌暗", "purple_tongue": "舌紫暗", "white_greasy_coating": "苔白腻",
    "greasy_coating": "苔腻", "chronic_yabi": "久病腰痹", "lumbar_leg_pain": "腰腿痛",
    "lumbar_pain": "腰痛", "fixed_pain": "痛处固定", "stabbing_pain": "刺痛",
    "strain_or_sprain": "扭伤/劳损史", "emotional_constraint": "情志不畅",
    "elderly": "高龄", "osteoporosis": "骨质疏松", "lumbar_knee_soreness": "腰膝酸软",
    "long_duration": "病程迁延", "slow_pulse": "脉缓", "thin_pulse": "脉细",
    "cold_aversion": "畏寒", "cold_extremities": "四肢不温", "deep_cold_pain": "冷痛",
    "bitter_taste": "口苦", "dry_mouth": "口干", "insomnia": "夜寐差",
    "young_patient": "年龄偏轻", "poor_appetite": "胃纳差", "epigastric_discomfort": "胃脘不适",
    "teeth_marks": "舌边齿痕", "fatigue": "乏力", "weak_stomach": "素体胃弱",
    "lower_limb_numbness": "下肢麻木", "radiating_leg_pain": "下肢放射痛",
    "cold_aggravation": "遇冷加重", "warmth_relieves": "得温则减",
    "yellow_greasy_coating": "苔黄腻", "red_tongue": "舌红", "burning_pain": "腰部灼热",
    "dark_urine": "小便黄", "thirst": "口渴", "heat_aggravation": "遇热加重",
    "heavy_lower_limb": "肢体困重", "night_pain": "夜间痛",
}


def _cn(tags: list[str] | set[str]) -> str:
    return "、".join(TAG_CN.get(t, t) for t in sorted(tags))


def _syndrome_triggers() -> dict[str, set[str]]:
    """Syndrome name → the full trigger tag set of its rule(s) (from 02_syndrome_rules)."""

    triggers: dict[str, set[str]] = {}
    for rule in load_rule_file("02_syndrome_rules.yaml") or []:
        syndrome = (rule.get("effect") or {}).get("syndrome")
        if not syndrome:
            continue
        trig = rule.get("trigger") or {}
        tags = set(trig.get("all") or []) | set(trig.get("any") or [])
        triggers.setdefault(syndrome, set()).update(tags)
    return triggers


def uncertainty_skill(
    syndrome_candidates: list[dict[str, Any]],
    normalized_tags: list[str],
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic uncertainty block for a scored case.

    Returns ``{"uncertainty": {...}}`` with: abstention verdict, top-1/top-2
    separation, what evidence would strengthen the leading candidate, and which
    missing discriminators would separate the competing candidates.
    """

    tags = set(normalized_tags or [])
    candidates = [c for c in (syndrome_candidates or []) if c.get("name")]
    triggers = _syndrome_triggers()

    block: dict[str, Any] = {
        "abstain": False,
        "abstain_reason": None,
        "top_margin": None,
        "separation": "none",
        "assessment_note": "",
        "strengthening_evidence": [],
        "differential_gaps": [],
        "evidence_sufficiency": {
            "tag_count": len(tags),
            "missing_fields": list(missing_fields or []),
        },
        "non_final": True,
    }

    if not candidates:
        block.update({
            "abstain": True,
            "abstain_reason": "no_candidate",
            "assessment_note": "现有信息未命中任何证型规则，系统不给出证型倾向；建议补充四诊（疼痛性质、寒热、舌象、脉象）后再评估。",
        })
        return {"uncertainty": block}

    top = candidates[0]
    top_score = int(top.get("score") or 0)
    if top_score < ABSTAIN_SCORE_THRESHOLD:
        block.update({
            "abstain": True,
            "abstain_reason": "insufficient_evidence",
            "assessment_note": (
                f"最高候选「{top['name']}」得分仅 {top_score} 分，证据强度不足以形成稳定证型倾向，"
                "系统建议以“证据不足待补充”呈现，而非给出结论。"
            ),
        })

    # Separation between the leading and the runner-up candidate.
    if len(candidates) == 1:
        block["separation"] = "single"
        if not block["abstain"]:
            block["assessment_note"] = f"仅「{top['name']}」一个候选证型（{top_score} 分），无竞争证型；仍需医师结合面诊确认。"
    else:
        second = candidates[1]
        margin = top_score - int(second.get("score") or 0)
        block["top_margin"] = margin
        block["separation"] = "clear" if margin >= CLEAR_MARGIN else "narrow"
        if not block["abstain"]:
            if block["separation"] == "clear":
                block["assessment_note"] = (
                    f"「{top['name']}」（{top_score} 分）领先次选「{second['name']}」{margin} 分，区分度较好；仍为倾向性判断，供医师审定。"
                )
            else:
                block["assessment_note"] = (
                    f"「{top['name']}」与「{second['name']}」得分接近（相差 {margin} 分），区分度不足，"
                    "建议先补充下列鉴别信息再作判断。"
                )

        # Which missing rule evidence would separate the two leading candidates.
        for competitor in candidates[1:3]:
            comp_missing = triggers.get(competitor["name"], set()) - tags
            top_missing = triggers.get(top["name"], set()) - tags
            discriminators = sorted(comp_missing | top_missing)
            if discriminators:
                block["differential_gaps"].append({
                    "between": [top["name"], competitor["name"]],
                    "missing_discriminators": discriminators,
                    "suggestion": f"补充「{_cn(discriminators)}」有助于区分{top['name']}与{competitor['name']}。",
                })

    # What would strengthen the leading candidate itself.
    top_gap = sorted(triggers.get(top["name"], set()) - tags)
    if top_gap:
        block["strengthening_evidence"] = [
            {"tag": t, "label": TAG_CN.get(t, t)} for t in top_gap
        ]

    return {"uncertainty": block}


def uncertainty_markdown(block: dict[str, Any]) -> str:
    """Render the uncertainty block as a report section (research/teaching wording)."""

    u = block or {}
    lines = ["### 判读可信度与鉴别提示（系统自评，非结论）"]
    if u.get("abstain"):
        lines.append(f"- ⚠️ **证据不足，暂不给出证型倾向**：{u.get('assessment_note', '')}")
    else:
        lines.append(f"- {u.get('assessment_note', '')}")
    conformal = u.get("conformal") or {}
    if conformal.get("prediction_set"):
        lines.append(
            f"- 共形候选集（项目内校准，尚不能排除的证型，非诊断概率）："
            f"{'、'.join(conformal['prediction_set'])}。{conformal.get('coverage_note', '')}"
        )
    for gap in u.get("differential_gaps") or []:
        lines.append(f"- {gap.get('suggestion', '')}")
    strengthening = u.get("strengthening_evidence") or []
    if strengthening and not u.get("abstain"):
        labels = "、".join(item["label"] for item in strengthening[:6])
        lines.append(f"- 若补充「{labels}」将进一步支持当前首选证型。")
    missing = (u.get("evidence_sufficiency") or {}).get("missing_fields") or []
    if missing:
        lines.append(f"- 尚缺关键四诊字段：{'、'.join(missing[:8])}。")
    return "\n".join(lines)
