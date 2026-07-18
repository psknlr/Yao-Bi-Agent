from __future__ import annotations

from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.llm.output_guard import guard_clinician_draft
from backend.skills.physician_reasoning_skill import SYNDROME_THERAPY, TAG_CN


def _cn(tags: list[str]) -> str:
    return "、".join(TAG_CN.get(t, t) for t in tags) or "暂无"


def _build_case_summary(
    case_state: dict[str, Any],
    syndrome_candidates: list[dict[str, Any]],
    formula_routes: list[dict[str, Any]],
    matched_modules: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    tags = sorted(set(case_state.get("normalized_tags") or []))
    chief = case_state.get("chief_complaint", {})
    top = syndrome_candidates[0] if syndrome_candidates else None
    therapy = SYNDROME_THERAPY.get(top.get("name")) if top else "待辨证后确立"
    routes = "、".join(r.get("name") for r in formula_routes[:3]) or "待医师确立"
    modules = "、".join(m.get("name") for m in matched_modules[:6]) or "待医师确立"
    key_points = [
        f"辨证要点：{_cn(top.get('evidence_tags', []) if top else tags)} → 证候倾向「{top.get('name') if top else '待补充'}」",
        f"治法倾向：{therapy}",
        f"选方路线：{routes}",
        f"用药模块特色：{modules}",
    ]
    md = (
        "# 医案按语（教学复盘 · 非诊断非处方）\n\n"
        f"## 一、主诉与病程\n{chief.get('standard_text') or chief.get('main_symptom') or '腰痛'}。\n\n"
        f"## 二、辨证要点\n关键线索：{_cn(tags)}；证候倾向「{top.get('name') if top else '信息不足，待补充'}」。\n\n"
        f"## 三、治法治则\n可考虑：{therapy}（待医师审定）。\n\n"
        f"## 四、选方用药思路\n方剂路线：{routes}；功效模块：{modules}。具体方药、加减与剂量由医师审定。\n\n"
        "## 五、沈老经验体现\n体现温通经络、益气养血、顾护肝肾脾胃与少阳枢机的整体思路。\n\n"
        "## 六、随访复诊要点\n关注疼痛/麻木变化、睡眠与胃纳、有无新发无力或二便异常；若进行性加重需及时复诊与转诊。\n\n"
        "> 本按语为科研教学复盘，非针对患者的最终诊断或可执行处方。"
    )
    return md, key_points


def _build_experience_summary(mined_evidence: dict[str, Any]) -> tuple[str, list[str]]:
    stats = mined_evidence.get("dataset_stats") or {}
    zheng = stats.get("zheng_distribution") or {}
    routes = mined_evidence.get("formula_signature_hits") or []
    assoc = [r for r in (mined_evidence.get("rule_candidates") or []) if "association" in str(r.get("rule_type"))]
    top_zheng = "、".join(list(zheng)[:4]) or "数据不足"
    top_routes = "、".join(h.get("formula") for h in routes[:5]) or "数据不足"
    key_points = [
        f"高频证候：{top_zheng}",
        f"核心方剂路线：{top_routes}",
        f"关联规律：共 {len(assoc)} 条（support/confidence/lift 见规则挖掘页）",
    ]
    assoc_lines = "\n".join(
        f"- {r.get('if', {}).get('tag', '')} → {list(r.get('then', {}).values())[0] if r.get('then') else ''}"
        f"（lift={r.get('statistics', {}).get('lift', '—')}，n={r.get('statistics', {}).get('n_both', '—')}）"
        for r in assoc[:8]
    )
    md = (
        "# 沈钦荣腰痹经验规律总结（脱敏统计 · 教学科研用）\n\n"
        f"## 一、样本概况\n脱敏病例 {stats.get('n_cases', '—')} 例，含处方 {stats.get('n_with_prescription', '—')} 例。\n\n"
        f"## 二、高频证候\n{top_zheng}。\n\n"
        f"## 三、核心方剂路线\n{top_routes}。\n\n"
        f"## 四、症状—方药关联规律（部分）\n{assoc_lines or '数据不足'}\n\n"
        "## 五、用药与剂量经验\n重点药物剂量分布见证据回溯页，仅作经验研究区间，非可执行医嘱。\n\n"
        "> 全部为脱敏聚合统计与待专家审核的研究信号，不构成诊断或处方依据。"
    )
    return md, key_points


def case_experience_summary_skill(
    case_state: dict[str, Any] | None = None,
    syndrome_candidates: list[dict[str, Any]] | None = None,
    formula_routes: list[dict[str, Any]] | None = None,
    matched_modules: list[dict[str, Any]] | None = None,
    mined_evidence: dict[str, Any] | None = None,
    mode: str = "case",
    dao_client: DaoClient | None = None,
    use_llm: bool = False,
    user_role: str = "clinician",
) -> dict[str, Any]:
    """Auto-generate a TCM physician case-experience summary (rule-first, Tao overlay).

    ``mode="case"`` 生成单案「医案按语」；``mode="experience"`` 基于脱敏挖掘统计生成
    「经验规律总结」。确定性总结始终作为事实来源与回退；Tao 仅在通过守卫时叠加润色，
    不得新增数据外结论，不得产出最终诊断、可执行处方或剂量。患者角色一律拦截。
    """

    if user_role not in {"clinician", "licensed_physician", "researcher"}:
        return {"case_experience_summary": {"status": "blocked_patient_role", "patient_visible": False, "message": "案例经验总结仅供医生/研究者界面，患者端不显示。"}}

    if mode == "experience":
        deterministic_md, key_points = _build_experience_summary(mined_evidence or {})
    else:
        deterministic_md, key_points = _build_case_summary(case_state or {}, syndrome_candidates or [], formula_routes or [], matched_modules or [])

    meta: dict[str, Any] = {
        "enabled": use_llm,
        "status": "not_requested" if not use_llm else "pending",
        "fallback_used": True,
        "backend": getattr(getattr(dao_client, "config", None), "backend", None),
        "json_repair": None,
        "guard": None,
    }
    result = {
        "case_experience_summary": {
            "status": "draft_for_clinician_review",
            "patient_visible": False,
            "mode": mode,
            "summary_markdown": deterministic_md,
            "deterministic_summary_markdown": deterministic_md,
            "key_points": key_points,
            "summary_source": "deterministic_rules",
            "tao_runtime": meta,
        }
    }
    if not use_llm:
        return result

    client = dao_client or DaoClient()
    meta["backend"] = client.config.backend
    payload = {
        "task": "case_experience_summary",
        "mode": mode,
        "key_points_seed": key_points,
        "syndrome_candidates": (syndrome_candidates or [])[:3],
        "formula_routes": (formula_routes or [])[:3],
        "dataset_stats": (mined_evidence or {}).get("dataset_stats") if mode == "experience" else None,
        "output_contract": {"required_key": "summary_markdown", "forbidden_keys": ["final_diagnosis", "complete_prescription", "patient_executable_dose", "administration_instruction"]},
    }
    try:
        raw = client.generate_experience_summary(payload)
        parsed, repair_meta = loads_with_repair(raw)
        if not isinstance(parsed, dict):
            raise JsonRepairError("Tao summary output must be a JSON object.")
        markdown = str(parsed.get("summary_markdown") or "").strip()
        if not markdown:
            raise JsonRepairError("Tao summary output must include summary_markdown.")
        # Clinician-only skill (patient role is rejected at entry) — soft draft guard keeps
        # prompt-invited experience dose *ranges* while blocking executable regimens.
        guard = guard_clinician_draft(markdown, parsed)
        meta.update({"status": "accepted" if guard["allowed"] else "guard_rejected", "json_repair": repair_meta, "guard": guard})
        if guard["allowed"]:
            meta["fallback_used"] = False
            result["case_experience_summary"]["summary_markdown"] = deterministic_md + "\n\n---\n\n## Tao 经验总结润色（已通过安全校验）\n" + markdown
            result["case_experience_summary"]["summary_source"] = "deterministic_rules_plus_tao"
            if isinstance(parsed.get("key_points"), list):
                merged = key_points + [str(p) for p in parsed["key_points"] if str(p) not in key_points]
                result["case_experience_summary"]["key_points"] = merged[:8]
    except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
        meta.update({"status": "fallback", "error": str(exc), "fallback_used": True})
    return result
