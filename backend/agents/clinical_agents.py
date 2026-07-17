"""Clinical agents — each wraps an existing, tested skill and collaborates via the blackboard.

Agents never re-implement clinical logic; they orchestrate the skills, declare confidence /
evidence / handoffs, mark whether the language model was used, and may take autonomous
control decisions (the red-flag agent can halt downstream collaboration).
"""

from __future__ import annotations

from typing import Any

from backend.agents.base import AgentResult, Blackboard
from backend.skills.case_experience_summary_skill import case_experience_summary_skill
from backend.skills.physician_reasoning_skill import physician_reasoning_skill
from backend.tools import get_registry

# Deterministic skills execute through the tool registry (role check, schema
# validation, execution-point budget charging, audit spans). Runtime-bound LLM
# overlays (reasoning / experience — they own the DaoClient) stay direct by design.


def _tool(name, **kwargs):
    return get_registry().call(name, role="system", **kwargs)

_CONF = {"high": 0.85, "medium": 0.6, "low": 0.35}


class CaseStructuringAgent:
    name, role, kind = "CaseStructuringAgent", "病例结构化与质量", "rule"
    handoff_to = ["RedFlagAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        shen = _tool("shen_rule_signal_skill", case_state=bb.case_state)
        bb.case_state = shen["case_state"]
        quality = _tool("case_quality_check_skill", case_state=bb.case_state)
        bb.case_state = quality["case_state"]
        structured = _tool("case_structuring_skill", case_state=bb.case_state)
        bb.put("shen", shen, producer=self.name)
        bb.put("quality", {k: v for k, v in quality.items() if k != "case_state"}, producer=self.name)
        bb.put("structured", structured, producer=self.name)
        score = quality.get("case_quality_score") or 0
        return AgentResult(
            self.name, self.role, self.kind, "ok",
            f"完成医案结构化，质量分 {score}，沈老经验信号已刷新。",
            confidence=round(min(1.0, score / 100), 2),
            evidence=sorted(set(bb.case_state.get("normalized_tags") or []))[:8],
            handoff_to=self.handoff_to,
        )


class RedFlagAgent:
    name, role, kind = "RedFlagAgent", "红旗硬门控", "rule"
    handoff_to = ["ScopeGateAgent"]
    runs_after_halt = False

    def run(self, bb: Blackboard) -> AgentResult:
        red = bb.case_state.get("red_flags", {}) or {}
        status = red.get("status")
        positives = red.get("positive_items") or []
        if status == "urgent":
            return AgentResult(
                self.name, self.role, self.kind, "halt",
                "命中红旗危险信号，自主中止下游临床协作并转急诊/线下评估。",
                confidence=1.0, evidence=positives,
                handoff_to=["EmergencyNoticeAgent"], halt_pipeline=True,
            )
        summary = "未见急诊级红旗，放行下游协作。" if status else "红旗信息不完整，按谨慎放行。"
        return AgentResult(self.name, self.role, self.kind, "ok", summary, confidence=1.0, evidence=positives, handoff_to=self.handoff_to)


class ScopeGateAgent:
    """Capability issuer: verifies the case belongs to the lumbar-Bi task domain and
    grants/blocks downstream clinical capabilities (the collaboration entry previously
    had no scope check at all — the cross-entry inconsistency P0 of the entry review).

    Scope evidence, in order: an explicit server-computed scope decision on the case
    state (from the free-text narrative), else lumbar-context tags from the intake.
    Questionnaire cases with neither lumbar evidence nor any out-of-domain marker keep
    the legacy behaviour (the clinician intake UI is lumbar-domain by construction)
    with a reduced-confidence note.
    """

    name, role, kind = "ScopeGateAgent", "任务域与能力授权", "rule"
    handoff_to = ["OrthoRiskAgent"]
    runs_after_halt = False

    FULL_CAPABILITIES = {"syndrome_reasoning", "formula_draft", "herb_module", "review_packaging"}
    TRIAGE_ONLY = {"review_packaging"}

    def run(self, bb: Blackboard) -> AgentResult:
        from backend.skills.clinical_scope_router_skill import LUMBAR_CONTEXT_TAGS

        scope = bb.case_state.get("scope") or {}
        tags = set(bb.case_state.get("normalized_tags") or [])
        if scope.get("in_scope") is False:
            bb.grant_capabilities(self.TRIAGE_ONLY, issuer=self.name)
            return AgentResult(
                self.name, self.role, self.kind, "halt",
                f"主诉不属于腰痹任务域（{scope.get('out_of_scope_reason') or '域外主诉'}），中止辨证与方药协作。",
                confidence=1.0, evidence=list(scope.get("reason_codes") or []),
                handoff_to=["EmergencyNoticeAgent"], halt_pipeline=True,
            )
        lumbar_evidence = bool(tags & LUMBAR_CONTEXT_TAGS) or scope.get("in_scope") is True
        bb.grant_capabilities(self.FULL_CAPABILITIES, issuer=self.name)
        summary = "腰痹任务域确认，授予证型/方药/药物模块能力。" if lumbar_evidence \
            else "未见明确腰痹锚点（问卷式病例按腰痹域放行），能力授予并标注低置信。"
        return AgentResult(self.name, self.role, self.kind, "ok", summary,
                           confidence=1.0 if lumbar_evidence else 0.6,
                           evidence=sorted(tags & LUMBAR_CONTEXT_TAGS), handoff_to=self.handoff_to)


class OrthoRiskAgent:
    name, role, kind = "OrthoRiskAgent", "骨伤科风险分层", "rule"
    handoff_to = ["TcmSyndromeAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        neuro = bb.case_state.get("neuro_ortho", {}) or {}
        tags = set(bb.case_state.get("normalized_tags") or [])
        comorbid = set((bb.case_state.get("comorbidity", {}) or {}).get("diseases") or [])
        weakness = neuro.get("weakness")
        strata = {
            "cauda_equina": "elevated" if neuro.get("bowel_bladder") not in (None, "无", "否") else "low",
            "tumor": "elevated" if ("cancer_history" in tags or "肿瘤病史" in comorbid) else "low",
            "infection": "elevated" if "fever_or_infection" in tags else "low",
            "fracture": "elevated" if ("osteoporosis" in tags or "骨质疏松" in comorbid) else "low",
            "progressive_neuro": "high" if weakness == "越来越重" else "moderate" if weakness in ("明显",) else "low",
        }
        elevated = [k for k, v in strata.items() if v in ("elevated", "high")]
        bb.put("ortho_risk", {"strata": strata, "elevated": elevated}, producer=self.name)
        status = "escalate" if elevated else "ok"
        summary = (f"风险分层提示需重点复核：{('、'.join(elevated))}。" if elevated else "四类骨伤科风险均为低风险背景。")
        return AgentResult(self.name, self.role, self.kind, status, summary, confidence=1.0, evidence=elevated, handoff_to=self.handoff_to)


class TcmSyndromeAgent:
    name, role, kind = "TcmSyndromeAgent", "中医证候路由", "rule"
    handoff_to = ["FormulaReasoningAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        # v0.14: syndrome reasoning is also capability-gated — an out-of-scope case
        # must not receive TCM pattern conclusions either, not just formulas.
        if not bb.capability_allowed("syndrome_reasoning"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（syndrome_reasoning）：任务域门控禁止证候推理。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        tags = bb.case_state.get("normalized_tags", [])
        routed = _tool("syndrome_router_skill", normalized_tags=tags)
        bb.put("routed", routed, producer=self.name)
        cands = routed.get("syndrome_candidates") or []
        top = cands[0] if cands else None
        conf = round(min(1.0, (top.get("score") or 0) / 8), 2) if top else None
        summary = (f"证候倾向「{top['name']}」（{top.get('score')}分），共 {len(cands)} 个候选。" if top else "信息不足，未形成稳定证候倾向。")
        return AgentResult(self.name, self.role, self.kind, "ok", summary, confidence=conf, evidence=(top.get("evidence_tags") if top else []), handoff_to=self.handoff_to)


class FormulaReasoningAgent:
    name, role, kind = "FormulaReasoningAgent", "方剂路径推理", "rule"
    handoff_to = ["HerbModuleAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        # Capability token check inside the agent (not only in the orchestrator):
        # a formula draft may only be produced when the scope gate granted it.
        if not bb.capability_allowed("formula_draft"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（formula_draft）：任务域门控禁止方剂推理。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        tags = bb.case_state.get("normalized_tags", [])
        routed = bb.get("routed", {})
        formula = _tool("formula_base_selector_skill", normalized_tags=tags, syndrome_candidates=routed.get("syndrome_candidates", []))
        bb.put("formula", formula, producer=self.name)
        primary = formula.get("primary_route")
        conf = _CONF.get((primary or {}).get("confidence"), 0.4) if primary else None
        summary = (f"主方路线「{primary['name']}」（{primary.get('confidence')}信度），路线候选 {len(formula.get('formula_routes') or [])} 条。" if primary else "暂无稳定方剂路线信号。")
        return AgentResult(self.name, self.role, self.kind, "ok", summary, confidence=conf, evidence=[r.get("name") for r in (formula.get("formula_routes") or [])[:3]], handoff_to=self.handoff_to)


class HerbModuleAgent:
    name, role, kind = "HerbModuleAgent", "药物功效模块", "rule"
    handoff_to = ["ConflictSafetyAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        if not bb.capability_allowed("herb_module"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（herb_module）：任务域门控禁止药物模块组合。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        tags = bb.case_state.get("normalized_tags", [])
        modules = _tool("herb_module_composer_skill", normalized_tags=tags, formula_route=(bb.get("formula", {}) or {}).get("primary_route"))
        bb.put("modules", modules, producer=self.name)
        matched = modules.get("matched_modules") or []
        return AgentResult(self.name, self.role, self.kind, "ok", f"组合 {len(matched)} 个功效模块草案，待安全审查。", confidence=1.0, evidence=[m.get("name") for m in matched[:6]], handoff_to=self.handoff_to)


class ConflictSafetyAgent:
    name, role, kind = "ConflictSafetyAgent", "冲突与安全审查", "rule"
    handoff_to = ["EvidenceTraceAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        modules = (bb.get("modules", {}) or {}).get("matched_modules") or []
        formula = bb.get("formula", {}) or {}
        comorbidity = bb.case_state.get("comorbidity", {}) or {}
        conflicts = _tool(
            "conflict_checker_skill",
            matched_modules=modules,
            formula_route=formula.get("primary_route"),
            medications=comorbidity.get("medications") or [],
            conditions=comorbidity.get("diseases") or [],
        )
        safety = _tool(
            "safety_guard_skill",
            case_json={"evidence": {"raw_text": ""}, "red_flags": bb.case_state.get("red_flags", {}).get("positive_items", [])},
            matched_modules=modules, normalized_tags=bb.case_state.get("normalized_tags", []),
        )
        bb.put("conflicts", conflicts, producer=self.name)
        bb.put("safety", safety, producer=self.name)
        risks = safety.get("medication_risks") or []
        alerts = conflicts.get("interaction_alerts") or []
        status = "escalate" if safety.get("safety_status") != "safe" or (conflicts.get("alert_summary") or {}).get("requires_dual_signoff") else "ok"
        return AgentResult(
            self.name, self.role, self.kind, status,
            f"安全状态：{safety.get('safety_status')}；冲突 {len(conflicts.get('conflicts') or [])} 项，相互作用/禁忌告警 {len(alerts)} 项，高风险用药 {len(risks)} 项。",
            confidence=1.0, evidence=risks[:6] + [a.get("id") for a in alerts[:4]], handoff_to=self.handoff_to,
        )


class EvidenceTraceAgent:
    name, role, kind = "EvidenceTraceAgent", "证据回溯", "rule"
    handoff_to = ["ReasoningAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        routed = bb.get("routed", {}) or {}
        mined = _tool("mined_evidence_skill", normalized_tags=bb.case_state.get("normalized_tags", []), syndrome_candidates=routed.get("syndrome_candidates", []))
        bb.put("mined", mined, producer=self.name)
        ev = mined.get("mined_evidence") or []
        return AgentResult(self.name, self.role, self.kind, "ok", f"匹配 {len(ev)} 条 xlsx 脱敏挖掘证据（待专家审核）。", confidence=1.0, evidence=[r.get("rule_id") for r in ev[:6]], handoff_to=self.handoff_to)


class ReasoningAgent:
    name, role, kind = "ReasoningAgent", "医师经验辨证推理", "llm"
    handoff_to = ["ExperienceAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        # LLM narrative reasoning is clinical content: same capability as syndrome
        # routing — an unauthorized case gets no experience-based clinical narrative.
        if not bb.capability_allowed("syndrome_reasoning"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（syndrome_reasoning）：任务域门控禁止经验推理叙述。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        routed = bb.get("routed", {}) or {}
        formula = bb.get("formula", {}) or {}
        modules = bb.get("modules", {}) or {}
        reasoning = physician_reasoning_skill(
            bb.case_state, routed.get("syndrome_candidates", []), formula.get("formula_routes"),
            modules.get("matched_modules"), bb.get("safety"),
            shen_signals=(bb.get("shen", {}) or {}).get("shen_signals"),
            mined_evidence=(bb.get("mined", {}) or {}).get("mined_evidence"),
            dao_client=bb.dao_client, use_llm=bb.use_llm, user_role="clinician",
        )
        bb.put("reasoning", reasoning, producer=self.name)
        pr = reasoning.get("physician_reasoning", {})
        runtime = pr.get("tao_runtime") or {}
        used = bool(runtime.get("enabled")) and not runtime.get("fallback_used", True)
        chain = pr.get("reasoning_chain") or []
        src = pr.get("narrative_source")
        summary = f"生成 {len(chain)} 步辨证推理链；" + ("Tao 语言化已采纳。" if src == "deterministic_rules_plus_tao" else "采用规则派生叙述（Tao 未启用或被守卫回退）。")
        return AgentResult(self.name, self.role, self.kind, "ok", summary, confidence=0.7, used_llm=used, evidence=[s.get("title") for s in chain[:6]], handoff_to=self.handoff_to, llm_runtime=runtime)


class ExperienceAgent:
    name, role, kind = "ExperienceAgent", "案例经验总结", "llm"
    handoff_to = ["PhysicianReviewAgent"]

    def run(self, bb: Blackboard) -> AgentResult:
        if not bb.capability_allowed("syndrome_reasoning"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（syndrome_reasoning）：任务域门控禁止经验总结叙述。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        routed = bb.get("routed", {}) or {}
        formula = bb.get("formula", {}) or {}
        modules = bb.get("modules", {}) or {}
        experience = case_experience_summary_skill(
            bb.case_state, routed.get("syndrome_candidates", []), formula.get("formula_routes"),
            modules.get("matched_modules"), mode="case",
            dao_client=bb.dao_client, use_llm=bb.use_llm, user_role="clinician",
        )
        bb.put("experience", experience, producer=self.name)
        ce = experience.get("case_experience_summary", {})
        runtime = ce.get("tao_runtime") or {}
        used = bool(runtime.get("enabled")) and not runtime.get("fallback_used", True)
        summary = "生成医案按语；" + ("Tao 润色已采纳。" if ce.get("summary_source") == "deterministic_rules_plus_tao" else "采用确定性模板（Tao 未启用或被守卫回退）。")
        return AgentResult(self.name, self.role, self.kind, "ok", summary, confidence=0.7, used_llm=used, evidence=ce.get("key_points", [])[:4], handoff_to=self.handoff_to, llm_runtime=runtime)


class PhysicianReviewAgent:
    name, role, kind = "PhysicianReviewAgent", "医师审核装配", "rule"
    handoff_to: list[str] = ["licensed_physician(human)"]

    def run(self, bb: Blackboard) -> AgentResult:
        if not bb.capability_allowed("review_packaging"):
            return AgentResult(self.name, self.role, self.kind, "blocked",
                               "能力未授权（review_packaging）：任务域门控禁止医师审核包装配。",
                               confidence=1.0, evidence=["capability_denied"], handoff_to=self.handoff_to)
        routed = bb.get("routed", {}) or {}
        formula = bb.get("formula", {}) or {}
        modules = bb.get("modules", {}) or {}
        safety = bb.get("safety")
        handoff = _tool("clinician_handoff_skill", case_state=bb.case_state, formula_routes=formula.get("formula_routes"), matched_modules=modules.get("matched_modules"), safety=safety)
        review_package = _tool("clinician_review_package_skill", case_state=bb.case_state, syndrome_candidates=routed.get("syndrome_candidates", []), formula_routes=formula.get("formula_routes"), matched_modules=modules.get("matched_modules"), safety=safety)
        cdss = _tool("cdss_recommendation_skill", case_state=bb.case_state, syndrome_candidates=routed.get("syndrome_candidates", []), formula_routes=formula.get("formula_routes"), matched_modules=modules.get("matched_modules"), safety=safety, user_role="clinician")
        bb.put("handoff", handoff, producer=self.name)
        bb.put("review_package", review_package, producer=self.name)
        bb.put("cdss", cdss, producer=self.name)
        return AgentResult(self.name, self.role, self.kind, "ok", "装配医生复核包与 CDSS 草案，移交执业医师签名（人类终审）。", confidence=1.0, evidence=["draft_for_clinician_review"], handoff_to=self.handoff_to)


class EmergencyNoticeAgent:
    name, role, kind = "EmergencyNoticeAgent", "急诊转诊提示", "rule"
    handoff_to: list[str] = ["licensed_physician(human)"]
    runs_after_halt = True

    def run(self, bb: Blackboard) -> AgentResult:
        positives = (bb.case_state.get("red_flags", {}) or {}).get("positive_items") or []
        return AgentResult(self.name, self.role, self.kind, "blocked", "已生成急诊/线下评估提示，停止常规辨证与方药协作。", confidence=1.0, evidence=positives, handoff_to=self.handoff_to)


DEFAULT_AGENTS = [
    CaseStructuringAgent(), RedFlagAgent(), ScopeGateAgent(), OrthoRiskAgent(), TcmSyndromeAgent(),
    FormulaReasoningAgent(), HerbModuleAgent(), ConflictSafetyAgent(), EvidenceTraceAgent(),
    ReasoningAgent(), ExperienceAgent(), PhysicianReviewAgent(),
]
EMERGENCY_AGENT = EmergencyNoticeAgent()
