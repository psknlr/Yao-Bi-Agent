"""Conversational skill router — the language model autonomously picks which skill to call.

多轮问答的核心：用户自由提问 → 路由器把问题映射到一个**已注册技能**（intent）→ 由确定性
规则/挖掘数据回答。语言模型的自主性体现在“从受限技能清单里选技能”（受约束的工具选择 /
function-calling），而不是自由生成临床文本——因此安全边界不变：

* 路由先用确定性关键词匹配（始终可用、可回退）；
* 开启 Tao 时叠加语言模型选择，但只能从 ``allowed_intents`` 选择，越界/解析失败即回退关键词结果；
* 患者请求最终诊断/处方/可执行剂量时，由 ``patient_request_guard_skill`` 拦截到安全 intent。
"""

from __future__ import annotations

from typing import Any

from backend.llm.dao_client import DaoClient, DaoRuntimeError
from backend.llm.json_repair import JsonRepairError, loads_with_repair
from backend.skills.patient_request_guard_skill import patient_request_guard_skill

# 每个 intent 既是“语言模型可调用的技能”，也携带引导用户的示例问题。
INTENTS: list[dict[str, Any]] = [
    {
        "intent": "syndrome_inquiry", "label": "证候辨析", "group": "辨证论治",
        "description": "根据当前病例标签给出候选证型与依据。",
        "keywords": ["证型", "证候", "辨证", "什么证", "寒湿", "血瘀", "肝肾", "少阳", "气血"],
        "examples": ["这个病人偏向什么证型？", "为什么考虑气血痹阻证？"],
    },
    {
        "intent": "reasoning_inquiry", "label": "辨证推理", "group": "辨证论治",
        "description": "给出症状→证候→治法→方剂→安全的医师经验推理链。",
        "keywords": ["思路", "推理", "为什么这样", "辨证论治", "怎么分析", "治法"],
        "examples": ["讲讲这个案子的辨证思路", "从症状到治法是怎么推的？"],
    },
    {
        "intent": "formula_inquiry", "label": "方剂路线", "group": "辨证论治",
        "description": "给出候选方剂路线信号（非处方）。",
        "keywords": ["方剂", "方子", "用什么方", "主方", "路线", "独活寄生", "当归四逆", "桂枝芍药知母"],
        "examples": ["可以考虑哪些方剂路线？", "下肢麻木倾向哪条方剂路线？"],
    },
    {
        "intent": "herb_inquiry", "label": "用药模块", "group": "辨证论治",
        "description": "按功效给出药物模块草案（需医师审核，无剂量）。",
        "keywords": ["药物", "用药", "中药", "模块", "功效", "通络", "祛风", "补肝肾", "虫类"],
        "examples": ["对应哪些用药功效模块？", "有没有通络相关的药物模块？"],
    },
    {
        "intent": "safety_inquiry", "label": "安全审查", "group": "安全与风险",
        "description": "给出用药安全状态、冲突与高风险药物提示。",
        "keywords": ["安全", "风险", "冲突", "禁忌", "高风险", "毒性", "配伍"],
        "examples": ["这个方案有什么用药安全风险？", "有没有配伍冲突？"],
    },
    {
        "intent": "red_flag_inquiry", "label": "红旗排查", "group": "安全与风险",
        "description": "列出需要立即排查的危险信号（红旗）。",
        "keywords": ["红旗", "危险信号", "急诊", "马尾", "肿瘤", "感染", "骨折", "无力"],
        "examples": ["有哪些危险信号需要排查？", "什么情况要立刻去医院？"],
    },
    {
        "intent": "dose_inquiry", "label": "剂量经验", "group": "安全与风险",
        "description": "给出重点药物的经验剂量分布（医师端研究用，非可执行医嘱）。",
        "keywords": ["剂量", "用量", "多少克", "几克", "克数"],
        "examples": ["细辛在数据里常用多少量？", "附片的剂量分布是怎样的？"],
    },
    {
        "intent": "mining_inquiry", "label": "数据挖掘", "group": "数据挖掘",
        "description": "根据问题查询脱敏医案统计与规律（证型/方剂/症状关联）。",
        "keywords": ["数据", "多少例", "最多", "统计", "规律", "占比", "分布", "关联", "几个"],
        "examples": ["数据里哪个证型最多？", "气血痹阻证最常用什么方？", "下肢麻木对应什么方剂？"],
    },
    {
        "intent": "evidence_inquiry", "label": "证据回溯", "group": "数据挖掘",
        "description": "给出与当前病例匹配的挖掘候选规则及其 support/confidence/lift。",
        "keywords": ["证据", "依据", "support", "confidence", "lift", "回溯", "规则来源", "出处"],
        "examples": ["这些建议的挖掘证据是什么？", "匹配到哪些候选规则？"],
    },
    {
        "intent": "experience_inquiry", "label": "经验总结", "group": "经验与系统",
        "description": "生成单案医案按语或脱敏经验规律总结。",
        "keywords": ["总结", "按语", "经验", "复盘", "医案小结", "规律总结"],
        "examples": ["总结一下这个医案", "概括沈老腰痹用药经验规律"],
    },
    {
        "intent": "agent_inquiry", "label": "协作机制", "group": "经验与系统",
        "description": "说明多智能体如何在共享黑板上协作。",
        "keywords": ["智能体", "协作", "agent", "编排", "黑板", "怎么工作", "流程"],
        "examples": ["这些智能体是怎么协作的？", "红旗命中后流程怎么走？"],
    },
    {
        "intent": "capabilities", "label": "功能引导", "group": "经验与系统",
        "description": "列出可以提问的功能与示例问题。",
        "keywords": ["能问什么", "帮助", "怎么用", "功能", "可以做什么", "help"],
        "examples": ["我可以问你哪些问题？", "你能做什么？"],
    },
]

INTENT_BY_ID = {item["intent"]: item for item in INTENTS}
ALLOWED_INTENTS = [item["intent"] for item in INTENTS]


def keyword_route(question: str) -> tuple[str, float, list[str]]:
    """Deterministic intent match. Returns (intent, confidence, matched_keywords)."""

    text = (question or "").lower()
    best_intent, best_hits = "capabilities", []
    best_score = 0
    for item in INTENTS:
        hits = [kw for kw in item["keywords"] if kw.lower() in text]
        if len(hits) > best_score:
            best_score, best_intent, best_hits = len(hits), item["intent"], hits
    confidence = min(1.0, 0.4 + 0.2 * best_score) if best_score else 0.2
    return best_intent, round(confidence, 2), best_hits


def route_intent(
    question: str,
    use_llm: bool = False,
    dao_client: DaoClient | None = None,
    user_role: str = "clinician",
) -> dict[str, Any]:
    """Route a free-text question to one registered skill intent.

    Patient requests for diagnosis/prescription/dose are blocked first; otherwise a
    deterministic keyword route is computed, optionally refined by a guarded LLM choice.
    """

    guard = patient_request_guard_skill(question, user_role=user_role)
    if guard["blocked"] and user_role == "patient":
        return {
            "intent": "safety_block", "method": "patient_request_guard", "confidence": 1.0,
            "matched_keywords": [], "blocked": True, "guard": guard,
            "llm_runtime": {"enabled": use_llm, "status": "skipped_safety_block"},
        }

    hint_intent, confidence, hits = keyword_route(question)
    runtime: dict[str, Any] = {"enabled": use_llm, "status": "not_requested" if not use_llm else "pending", "fallback_used": True, "backend": getattr(getattr(dao_client, "config", None), "backend", None)}
    method = "keyword"
    intent = hint_intent

    if use_llm:
        client = dao_client or DaoClient()
        runtime["backend"] = client.config.backend
        payload = {
            "question": question,
            "allowed_intents": ALLOWED_INTENTS,
            "intent_catalog": [{"intent": i["intent"], "label": i["label"], "description": i["description"]} for i in INTENTS],
            "hint_intent": hint_intent,
        }
        try:
            raw = client.route_skill(payload)
            parsed, repair_meta = loads_with_repair(raw)
            chosen = parsed.get("intent") if isinstance(parsed, dict) else None
            runtime["json_repair"] = repair_meta
            if chosen in INTENT_BY_ID:
                intent, method, confidence = chosen, "llm", max(confidence, 0.7)
                runtime.update({"status": "accepted", "fallback_used": False, "reason": str(parsed.get("reason", ""))[:200]})
            else:
                runtime.update({"status": "fallback", "error": f"invalid intent: {chosen}"})
        except (DaoRuntimeError, JsonRepairError, ValueError, KeyError, TypeError) as exc:
            runtime.update({"status": "fallback", "error": str(exc)})

    return {
        "intent": intent, "method": method, "confidence": confidence,
        "matched_keywords": hits, "blocked": False, "guard": guard,
        "hint_intent": hint_intent, "llm_runtime": runtime,
    }


def suggested_questions(max_per_group: int = 2) -> list[dict[str, Any]]:
    """Grouped example questions to guide users (引导用户提问)."""

    groups: dict[str, dict[str, Any]] = {}
    for item in INTENTS:
        g = groups.setdefault(item["group"], {"group": item["group"], "items": []})
        g["items"].append({"intent": item["intent"], "label": item["label"], "examples": item["examples"][:max_per_group]})
    return list(groups.values())
