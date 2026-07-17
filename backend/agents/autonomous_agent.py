"""Autonomous multi-step QA agent (plan → execute → observe → critique → replan → synthesize).

This upgrades the single-intent conversational router into a closed-loop agent:

* **Plan** — decompose a question into an ordered plan of skill calls. Deterministic
  keyword planning is always available; an optional, guarded language-model planner may
  reorder/expand the plan but only with intents from the registered allowlist.
* **Execute / observe** — each plan step is delegated to the subagent that owns that
  intent (``ConversationSession.invoke``); observations accumulate in the trace.
* **Critique** — after execution a deterministic critic reviews the observations:
  a *safety critic* checks whether an unaddressed red-flag/risk state exists, an
  *evidence critic* flags steps whose answers carry no rule/mined evidence, and an
  *uncertainty critic* asks whether the rule engine would abstain and what missing
  facts would change the assessment.
* **Replan** — a failed safety critique injects a red-flag screening step; an abstaining
  engine turns the answer into follow-up questions instead of a confident conclusion.
* **Synthesize** — combine observations + critique into one answer with a ReAct-style
  trace (thought → action → observation) for audit and UI.

Safety invariants are unchanged: subagents only run registered skills over deterministic
rules / de-identified mined data; patient requests for diagnosis/prescription/dose are
blocked; the language model only selects/sequences skills, never invents clinical content.
"""

from __future__ import annotations

from typing import Any

from backend.agents.conversation import ConversationSession
from backend.agents.critics import contradiction_critic, evidence_critic, policy_critic
from backend.agents.skill_router import ALLOWED_INTENTS, INTENT_BY_ID, INTENTS, keyword_route
from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.runtime.execution_context import use_run
from backend.runtime.run_context import AgentRun, StopReason
from backend.tools import get_registry


def _tool(name, **kwargs):
    return get_registry().call(name, role="system", **kwargs)


def plan_question(
    question: str,
    max_steps: int = 3,
    use_llm: bool = False,
    dao_client: DaoClient | None = None,
) -> dict[str, Any]:
    """Decompose a question into an ordered, deduplicated plan of skill intents."""

    text = (question or "").lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in INTENTS:
        if item["intent"] == "capabilities":
            continue
        hits = [kw for kw in item["keywords"] if kw.lower() in text]
        if hits:
            scored.append((len(hits), {"intent": item["intent"], "reason": f"问题包含「{'、'.join(hits[:3])}」线索，需要{item['label']}。"}))
    scored.sort(key=lambda x: x[0], reverse=True)
    hint_plan = [step for _, step in scored[:max_steps]]
    if not hint_plan:
        intent = keyword_route(question)[0]
        hint_plan = [{"intent": intent, "reason": f"按默认路由调用{INTENT_BY_ID.get(intent, {}).get('label', intent)}。"}]

    method = "keyword"
    plan = hint_plan
    runtime: dict[str, Any] = {"enabled": use_llm, "status": "not_requested" if not use_llm else "pending", "fallback_used": True, "backend": getattr(getattr(dao_client, "config", None), "backend", None)}

    if use_llm:
        client = dao_client or DaoClient()
        runtime["backend"] = client.config.backend
        payload = {
            "question": question,
            "allowed_intents": ALLOWED_INTENTS,
            "intent_catalog": [{"intent": i["intent"], "label": i["label"], "description": i["description"]} for i in INTENTS],
            "hint_plan": hint_plan,
            "max_steps": max_steps,
        }
        try:
            raw = client.plan_skills(payload)
            parsed, repair_meta = loads_with_repair(raw)
            runtime["json_repair"] = repair_meta
            steps = parsed.get("plan") if isinstance(parsed, dict) else None
            cleaned: list[dict[str, Any]] = []
            seen: set[str] = set()
            for step in steps or []:
                intent = step.get("intent") if isinstance(step, dict) else None
                if intent in INTENT_BY_ID and intent not in seen:
                    seen.add(intent)
                    cleaned.append({"intent": intent, "reason": str(step.get("reason", ""))[:160] or INTENT_BY_ID[intent]["description"]})
            if cleaned:
                plan, method = cleaned[:max_steps], "llm"
                runtime.update({"status": "accepted", "fallback_used": False})
            else:
                runtime.update({"status": "fallback", "error": "no valid intents in plan"})
        except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
            runtime.update({"status": "fallback", "error": str(exc)})

    return {"plan": plan, "method": method, "hint_plan": hint_plan, "llm_runtime": runtime}


class AutonomousQAAgent:
    """A planning agent that delegates each step to a skill subagent."""

    def __init__(self, case_state: dict[str, Any] | None = None, use_llm: bool = False, dao_client: DaoClient | None = None, user_role: str = "clinician", max_steps: int = 3) -> None:
        self.session = ConversationSession(case_state=case_state, use_llm=use_llm, dao_client=dao_client, user_role=user_role)
        self.use_llm = use_llm
        self.dao_client = dao_client
        self.user_role = user_role
        self.max_steps = max_steps
        self.history: list[dict[str, Any]] = []

    def run(self, question: str) -> dict[str, Any]:
        # Unified run lifecycle: one status machine + one budget + one stop reason,
        # instead of inferring the outcome from ad-hoc field combinations.
        run = AgentRun(goal=question, user_role=self.user_role)
        run.start()

        guard = _tool("patient_request_guard_skill", user_request=question, user_role=self.user_role)
        if guard["blocked"] and self.user_role == "patient":
            run.finish(StopReason.POLICY_DENIED, note="patient_request_guard blocked the request")
            turn = {
                "question": question, "blocked": True, "plan": [], "plan_method": "patient_request_guard",
                "steps": [], "subagents_used": [], "used_llm": False,
                "answer": guard["message"], "trace": [{"step": 1, "thought": "检测到最终诊断/处方/剂量请求，安全护栏拦截。", "action": "patient_request_guard", "observation": guard["message"]}],
                "disclaimer": "患者端不提供最终诊断、完整处方或可执行剂量。",
                "run": run.to_dict(),
            }
            self.history.append(turn)
            return turn

        # Cumulative case memory + red-flag hard gate BEFORE planning (same invariant as
        # the pipeline / chat paths): an urgent, unexcluded red flag replaces the whole
        # plan with the emergency screening step — no clinical reasoning subagent runs.
        state_updates = self.session.absorb_question_facts(question)
        if (self.session.case_state.get("red_flags") or {}).get("status") == "urgent":
            observation = self.session.invoke("red_flag_inquiry", question)
            positives = (self.session.case_state.get("red_flags") or {}).get("positive_items") or []
            run.finish(StopReason.SAFETY_HALT, note="red_flag_gate replaced the plan")
            turn = {
                "question": question, "blocked": False, "red_flag_gated": True, "run": run.to_dict(),
                "plan": [{"intent": "red_flag_inquiry", "label": observation["label"], "reason": "红旗未排除，先行急诊/危险信号排查。"}],
                "plan_method": "red_flag_gate", "state_updates": state_updates,
                "steps": [{"step": 1, "intent": "red_flag_inquiry", "label": observation["label"],
                           "reason": "红旗未排除，先行急诊/危险信号排查。", "answer": observation["answer"],
                           "skills": observation["skills"], "evidence": observation["evidence"],
                           "used_llm": observation["used_llm"]}],
                "subagents_used": ["red_flag_inquiry"], "used_llm": observation["used_llm"],
                "answer": (
                    "⛔ **红旗危险信号未排除，自主智能体中止辨证与方药规划，仅执行危险信号排查。**\n\n"
                    + (f"命中线索：{('、'.join(str(p) for p in positives[:6]))}。\n\n" if positives else "")
                    + observation["answer"]
                ),
                "trace": [{"step": 1, "thought": "红旗门控：急诊级危险信号未排除，替换全部计划为红旗排查。",
                           "action": "red_flag_gate→red_flag_inquiry", "observation": observation["answer"],
                           "skills": observation["skills"]}],
                "disclaimer": "红旗未排除前不输出证型、方剂或药物模块；请先完成急诊/线下评估。",
            }
            self.history.append(turn)
            return turn

        # Scope gate BEFORE planning (same invariant as the pipeline scope router and
        # the chat gate): out-of-domain complaints get triage/referral, never a plan
        # that delegates to lumbar-Bi clinical subagents.
        from backend.skills.clinical_scope_router_skill import question_scope_gate

        scope_gate = question_scope_gate(question, self.session.case_state)
        # Whole-run hard block only for DOMAIN CONFLICTS (fracture/post-op priority,
        # out-of-domain complaint, domain shift). A merely anchorless question
        # (NO_LUMBAR_ANCHOR) still plans — dataset/safety sub-intents are legitimate;
        # each CLINICAL sub-intent is then blocked individually by the per-intent
        # scope gate inside ConversationSession._dispatch (v0.14).
        domain_conflict = bool(set(scope_gate["reason_codes"]) & {
            "FRACTURE_POSTOPERATIVE_PRIORITY", "NON_LUMBAR_COMPLAINT_IN_QUESTION", "DOMAIN_SHIFT_DETECTED",
        })
        if (not scope_gate["allowed"] and domain_conflict) or self.session.case_state.get("safety_extraction_failed"):
            answer = scope_gate["message"] or "⚠️ 安全信息解析异常，本轮不进行辨证与方药分析（已记录待人工复核）。"
            run.finish(StopReason.POLICY_DENIED, note="out_of_scope_or_fail_closed")
            turn = {
                "question": question, "blocked": False, "scope_gated": True, "run": run.to_dict(),
                "plan": [], "plan_method": "clinical_scope_gate", "state_updates": state_updates,
                "steps": [], "subagents_used": [], "used_llm": False, "answer": answer,
                "trace": [{"step": 1, "thought": "范围门控：主诉不属于腰痹任务域或安全解析失败，拒绝临床规划。",
                           "action": "clinical_scope_gate", "observation": answer}],
                "disclaimer": "本系统仅支持腰痹任务域；域外主诉请至相应专科面诊。",
            }
            self.history.append(turn)
            return turn

        # The ambient run context makes real tool/model calls charge THIS run's budget
        # at the execution points (ToolRegistry.invoke / DaoClient._dispatch) — the
        # planner no longer guesses "1 tool call per intent" while a handler executes
        # 5–8 underlying tools.
        with use_run(run):
            planned = plan_question(question, max_steps=self.max_steps, use_llm=self.use_llm, dao_client=self.dao_client)
            plan = planned["plan"]
            steps: list[dict[str, Any]] = []
            trace: list[dict[str, Any]] = []
            subagents: list[str] = []
            used_llm = False
            budget_stop: StopReason | None = None
            for i, step in enumerate(plan, start=1):
                budget_stop = run.budget.charge("iteration")
                if budget_stop:
                    trace.append({"step": i, "thought": "预算管理器：运行预算耗尽，停止剩余计划步骤。",
                                  "action": "budget_stop", "observation": run.budget.snapshot()})
                    break
                intent = step["intent"]
                observation = self.session.invoke(intent, question)
                subagents.append(intent)
                used_llm = used_llm or observation["used_llm"]
                steps.append({"step": i, "intent": intent, "label": observation["label"], "reason": step.get("reason", ""), "answer": observation["answer"], "skills": observation["skills"], "evidence": observation["evidence"], "used_llm": observation["used_llm"]})
                trace.append({"step": i, "thought": step.get("reason", ""), "action": f"delegate→{observation['label']}({intent})", "observation": observation["answer"], "skills": observation["skills"]})

            critique = self._critique_and_replan(question, steps, trace, subagents)
        answer = self._synthesize(question, planned, steps, critique)
        loop = ["understand", "plan", "execute", "observe", "critique"] + (["replan"] if critique["replanned"] else [])
        if budget_stop:
            run.finish(StopReason.BUDGET_EXHAUSTED, note="plan truncated by run budget")
        elif critique.get("abstain"):
            run.finish(StopReason.INSUFFICIENT_EVIDENCE, note="rule engine abstained; follow-up questions returned")
        else:
            run.finish(StopReason.GOAL_COMPLETED)
        turn = {
            "question": question, "blocked": False, "run": run.to_dict(),
            "plan": [{"intent": s["intent"], "label": INTENT_BY_ID.get(s["intent"], {}).get("label", s["intent"]), "reason": s.get("reason", "")} for s in plan],
            "plan_method": planned["method"], "plan_runtime": planned["llm_runtime"],
            "steps": steps, "trace": trace, "subagents_used": subagents,
            "critique": critique, "agent_loop": loop, "state_updates": state_updates,
            "multi_step": len(steps) > 1, "used_llm": used_llm, "answer": answer,
            "disclaimer": "自主智能体仅规划与调用受限技能，回答基于确定性规则与脱敏数据；不构成最终诊断、处方或可执行剂量。",
        }
        self.history.append(turn)
        return turn

    def _critique_and_replan(
        self,
        question: str,
        steps: list[dict[str, Any]],
        trace: list[dict[str, Any]],
        subagents: list[str],
    ) -> dict[str, Any]:
        """Deterministic critic pass after plan execution (observe → critique → replan).

        Safety critic: if the case's own red-flag/risk state is not "safe" and the plan
        never addressed safety, a red-flag screening step is injected (replanning).
        Evidence critic: steps whose answers carry no rule/mined evidence are flagged so
        the synthesis marks them as weakly grounded. Uncertainty critic: when the rule
        engine abstains, the missing discriminating facts become follow-up questions.
        """

        tags = self.session.case_state.get("normalized_tags") or []
        red = self.session.case_state.get("red_flags") or {}
        safety = _tool(
            "safety_guard_skill",
            case_json={"evidence": {"raw_text": question}, "red_flags": red.get("positive_items") or []},
            matched_modules=None, normalized_tags=tags,
        )
        replanned = False
        if safety["safety_status"] != "safe" and not {"safety_inquiry", "red_flag_inquiry"} & set(subagents):
            observation = self.session.invoke("red_flag_inquiry", question)
            step_no = len(steps) + 1
            steps.append({
                "step": step_no, "intent": "red_flag_inquiry", "label": observation["label"],
                "reason": "安全批判者发现未复核的红旗/风险线索，重规划补充红旗排查。",
                "answer": observation["answer"], "skills": observation["skills"],
                "evidence": observation["evidence"], "used_llm": observation["used_llm"],
            })
            trace.append({
                "step": step_no, "thought": "安全批判者：案情存在未复核的红旗/风险线索，重规划补充红旗排查。",
                "action": f"replan→{observation['label']}(red_flag_inquiry)",
                "observation": observation["answer"], "skills": observation["skills"],
            })
            subagents.append("red_flag_inquiry")
            replanned = True

        # Independent critics: each inspects one dimension and cannot see the others'
        # verdicts (see backend/agents/critics.py) — shared-blind-spot mitigation.
        evidence_view = evidence_critic(steps)
        contradictions = contradiction_critic(tags)
        policy_view = policy_critic(self.user_role, steps)
        candidates = _tool("syndrome_router_skill", normalized_tags=tags).get("syndrome_candidates") or []
        uncertainty = _tool("uncertainty_skill", syndrome_candidates=candidates, normalized_tags=tags)["uncertainty"]
        critique = {
            "safety_status": safety["safety_status"],
            "confirmed_red_flags": [f.get("term") or f.get("id") for f in safety.get("confirmed_red_flags") or []],
            "need_further_inquiry": safety.get("need_further_inquiry") or [],
            "ungrounded_steps": evidence_view["ungrounded_steps"],
            "contradictions": contradictions,
            "policy": policy_view,
            "abstain": bool(uncertainty.get("abstain")),
            "assessment_note": uncertainty.get("assessment_note") or "",
            "missing_facts": [g.get("suggestion") for g in uncertainty.get("differential_gaps") or []][:3],
            "replanned": replanned,
        }
        trace.append({
            "step": len(steps) + 1,
            "thought": "独立批判者复核：安全、证据、反证与角色策略各自独立评估。",
            "action": "critique(safety+evidence+contradiction+policy+uncertainty)",
            "observation": (
                f"安全状态 {critique['safety_status']}；"
                f"弱证据步骤 {len(critique['ungrounded_steps'])} 个；"
                f"反证轴 {len(contradictions)} 条；"
                + ("角色策略违规！" if not policy_view["ok"] else "")
                + ("规则引擎建议弃权，需补充关键信息。" if critique["abstain"] else "证据强度可接受。")
            ),
            "skills": ["safety_guard_skill", "uncertainty_skill", "critics"],
        })
        return critique

    def _synthesize(self, question: str, planned: dict[str, Any], steps: list[dict[str, Any]], critique: dict[str, Any] | None = None) -> str:
        if not steps:
            return "未能形成执行计划，请换一种问法或参考功能引导。"
        if len(steps) == 1:
            body = steps[0]["answer"]
        else:
            plan_line = "为回答此问题，自主规划了 " + str(len(steps)) + " 步并委派给对应子智能体：" + " → ".join(s["label"] for s in steps) + "。"
            body = plan_line + "\n\n" + "\n\n".join(f"### {s['step']}. {s['label']}（{s['intent']}）\n{s['answer']}" for s in steps)
        notes: list[str] = []
        if critique:
            if critique["safety_status"] != "safe":
                notes.append(f"⚠️ 安全批判者：当前安全状态为 **{critique['safety_status']}**，红旗/风险线索须优先线下复核。")
            if critique["abstain"]:
                notes.append("ℹ️ 不确定性批判者：现有证据不足以形成稳定结论，建议先补充关键信息再判断。")
            for finding in critique.get("contradictions") or []:
                notes.append(f"⚖️ 反证批判者（{finding['axis']}）：{finding['note']}")
            notes.extend(f"- {fact}" for fact in critique.get("missing_facts") or [])
        if notes:
            body += "\n\n---\n\n**批判者复核（critique）**\n\n" + "\n".join(notes)
        return body

    def starters(self) -> list[dict[str, Any]]:
        return self.session.starters()
