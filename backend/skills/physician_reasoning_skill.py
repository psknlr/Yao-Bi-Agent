from __future__ import annotations

from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_clinician_draft

HIGH_RISK_HERBS = {"附片", "细辛", "蜈蚣", "全蝎", "制川乌", "制草乌", "乌头", "麻黄"}

# 证候 → 治法（沈钦荣腰痹常用治则，倾向性表述，非最终处方）。
SYNDROME_THERAPY = {
    "气血痹阻证": "益气养血、通络止痛",
    "气滞血瘀证": "行气活血、化瘀通络",
    "寒湿痹阻证": "温经散寒、祛湿通络",
    "湿热痹阻证": "清热利湿、通络止痛",
    "肝肾不足证": "补益肝肾、强筋壮骨",
    "肾阳不足证": "温补肾阳、散寒止痛",
    "肾阴不足证": "滋补肾阴、濡养筋骨",
    "脾虚不运证": "健脾益气、化湿和中",
    "脾虚湿困证": "健脾化湿、通络止痛",
    "少阳证类": "和解少阳、疏利枢机",
    "少阳气郁证": "和解少阳、疏利枢机",
    "气血不足证": "补益气血、荣筋止痛",
    "肺肾阴虚证": "滋阴润燥、金水相生",
}

TAG_CN = {
    "lower_limb_numbness": "下肢麻木", "radiating_leg_pain": "下肢放射痛", "cold_aggravation": "遇冷加重",
    "warmth_relieves": "得温则减", "elderly": "高龄", "chronic_yabi": "病程迁延", "osteoporosis": "骨质疏松",
    "dark_tongue": "舌质暗", "white_greasy_coating": "苔白腻", "bitter_taste": "口苦", "insomnia": "失眠寐差",
    "lumbar_knee_soreness": "腰膝酸软", "poor_appetite": "纳差", "distending_pain": "胀痛", "stabbing_pain": "刺痛",
}


def _cn_tags(tags: list[str]) -> str:
    return "、".join(TAG_CN.get(t, t) for t in tags) or "暂无显著标签"


def _build_reasoning_chain(
    case_state: dict[str, Any],
    syndrome_candidates: list[dict[str, Any]],
    formula_routes: list[dict[str, Any]],
    matched_modules: list[dict[str, Any]],
    safety: dict[str, Any] | None,
    shen_signals: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tags = sorted(set(case_state.get("normalized_tags") or []))
    chief = case_state.get("chief_complaint", {})
    neuro = case_state.get("neuro_ortho", {})
    top_syndrome = syndrome_candidates[0] if syndrome_candidates else None
    therapy = SYNDROME_THERAPY.get(top_syndrome.get("name")) if top_syndrome else None

    chain: list[dict[str, Any]] = []
    chain.append({
        "step": 1, "title": "四诊与症状采集要点",
        "content": f"主诉「{chief.get('standard_text') or chief.get('main_symptom') or '腰痛'}」，关键线索：{_cn_tags(tags)}。",
        "evidence": tags,
    })
    if top_syndrome:
        others = "；".join(f"{c.get('name')}（{c.get('score')}分）" for c in syndrome_candidates[1:3])
        chain.append({
            "step": 2, "title": "辨证倾向（供医师审定）",
            "content": f"证候倾向「{top_syndrome.get('name')}」（{top_syndrome.get('score')}分），证据标签：{_cn_tags(top_syndrome.get('evidence_tags', []))}。" + (f" 次选：{others}。" if others else ""),
            "evidence": top_syndrome.get("evidence_tags", []),
        })
    else:
        chain.append({"step": 2, "title": "辨证倾向（供医师审定）", "content": "现有信息不足以形成稳定证候倾向，建议补充舌脉与四诊。", "evidence": []})
    if therapy:
        chain.append({"step": 3, "title": "治法（倾向）", "content": f"依据上述证候倾向，可考虑治法：{therapy}（待医师审定）。", "evidence": [top_syndrome.get("name")] if top_syndrome else []})
    if formula_routes:
        routes_txt = "；".join(f"{r.get('name')}（{r.get('confidence')}信度/{r.get('score')}分）" for r in formula_routes[:3])
        chain.append({
            "step": 4, "title": "方剂路线信号（路线倾向）",
            "content": f"规则命中方剂路线：{routes_txt}。仅为路线倾向，具体方药、加减与用量由医师审定。",
            "evidence": [r.get("name") for r in formula_routes[:3]],
        })
    if matched_modules:
        mod_txt = "；".join(f"{m.get('name')}（{m.get('role', '')}）" for m in matched_modules[:6])
        chain.append({"step": 5, "title": "药物功效模块（草案）", "content": f"可参考的功效模块：{mod_txt}。需医师取舍配伍。", "evidence": [m.get("name") for m in matched_modules[:6]]})
    high_risk = sorted({h for m in matched_modules for h in (m.get("herbs") or []) if h in HIGH_RISK_HERBS})
    safety_status = (safety or {}).get("safety_status", "unknown")
    chain.append({
        "step": 6, "title": "安全与禁忌复核",
        "content": f"安全状态：{safety_status}。" + (f" 高风险药物需医师重点复核：{('、'.join(high_risk))}。" if high_risk else " 未见显著高风险药物，仍需常规配伍与合并病复核。"),
        "evidence": high_risk,
    })
    active_signals = [k for k, v in (shen_signals or {}).items() if v is True]
    if active_signals:
        chain.append({"step": 7, "title": "沈老经验信号", "content": f"命中经验信号：{('、'.join(active_signals))}，体现温通、扶正、顾护中焦/肝肾与少阳枢机思路。", "evidence": active_signals})
    return chain


def _deterministic_narrative(chain: list[dict[str, Any]]) -> str:
    lines = ["# 医师经验辨证推理（规则派生，非最终诊断/处方）", ""]
    for step in chain:
        lines.append(f"## {step['step']}. {step['title']}")
        lines.append(step["content"])
        lines.append("")
    lines.append("> 本推理为确定性规则引擎结论的过程化表达，全部为倾向性、非最终口吻；最终诊断、处方、剂量须由执业医师审核签名。")
    return "\n".join(lines)


def physician_reasoning_skill(
    case_state: dict[str, Any],
    syndrome_candidates: list[dict[str, Any]] | None = None,
    formula_routes: list[dict[str, Any]] | None = None,
    matched_modules: list[dict[str, Any]] | None = None,
    safety: dict[str, Any] | None = None,
    shen_signals: dict[str, Any] | None = None,
    mined_evidence: list[dict[str, Any]] | None = None,
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
    user_role: str = "clinician",
) -> dict[str, Any]:
    """Rule-first physician reasoning with an optional, guarded Tao narrative overlay.

    确定性推理链始终生成并作为事实来源与回退；Tao 仅在通过 JSON 修复与输出守卫时，
    把推理链「语言化」为辨证教学解释，不得新增规则层没有的证型/方剂/药物，
    不得给出最终诊断、处方或剂量。患者角色一律拦截。
    """

    if user_role not in {"clinician", "licensed_physician", "researcher"}:
        return {"physician_reasoning": {"status": "blocked_patient_role", "patient_visible": False, "message": "医师经验推理仅供医生/研究者界面，患者端不显示。"}}

    syndrome_candidates = syndrome_candidates or []
    formula_routes = formula_routes or []
    matched_modules = matched_modules or []
    chain = _build_reasoning_chain(case_state, syndrome_candidates, formula_routes, matched_modules, safety, shen_signals)
    deterministic_md = _deterministic_narrative(chain)

    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "fallback_used": True,
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
        "json_repair": None,
        "guard": None,
    }
    result = {
        "physician_reasoning": {
            "status": "draft_for_clinician_review",
            "patient_visible": False,
            "automation_level": "model_rule_generated_reasoning_not_signed",
            "reasoning_chain": chain,
            "narrative_markdown": deterministic_md,
            "deterministic_narrative_markdown": deterministic_md,
            "narrative_source": "deterministic_rules",
            "mined_evidence_count": len(mined_evidence or []),
            "tao_runtime": meta,
        }
    }
    if not use_llm:
        return result

    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend
    payload = {
        "task": "physician_reasoning",
        "reasoning_chain": chain,
        "syndrome_candidates": syndrome_candidates[:3],
        "formula_routes": formula_routes[:3],
        "matched_modules": [{"name": m.get("name"), "role": m.get("role")} for m in matched_modules[:6]],
        "mined_evidence": [{"rule_id": r.get("rule_id"), "then": r.get("then"), "statistics": r.get("statistics")} for r in (mined_evidence or [])[:6]],
        "output_contract": {"required_key": "reasoning_markdown", "forbidden_keys": ["final_diagnosis", "complete_prescription", "patient_executable_dose", "administration_instruction"]},
    }
    try:
        raw = client.generate_reasoning(payload)
        parsed, repair_meta = loads_with_repair(raw)
        if not isinstance(parsed, dict):
            raise JsonRepairError("Tao reasoning output must be a JSON object.")
        markdown = str(parsed.get("reasoning_markdown") or "").strip()
        if not markdown:
            raise JsonRepairError("Tao reasoning output must include reasoning_markdown.")
        # Clinician-only skill (patient role is rejected at entry) — use the soft draft guard
        # so prompt-invited teaching content (方义/经验剂量区间) is not falsely rejected.
        guard = guard_clinician_draft(markdown, parsed)
        meta.update({"status": "accepted" if guard["allowed"] else "guard_rejected", "json_repair": repair_meta, "guard": guard})
        if guard["allowed"]:
            meta["fallback_used"] = False
            result["physician_reasoning"]["narrative_markdown"] = deterministic_md + "\n\n---\n\n## Tao 辨证推理教学解释（已通过安全校验）\n" + markdown
            result["physician_reasoning"]["narrative_source"] = "deterministic_rules_plus_tao"
    except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
        meta.update({"status": "fallback", "error": str(exc), "fallback_used": True})
    return result
