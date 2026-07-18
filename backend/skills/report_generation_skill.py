from __future__ import annotations

from typing import Any

from backend.skills.uncertainty_skill import uncertainty_markdown


def _join(items: list[Any]) -> str:
    return "、".join(str(item) for item in items) if items else "未提供"


def _interaction_lines(interaction_alerts: list[dict[str, Any]] | None) -> str:
    """Tiered rendering: interruptive alerts first (需医师双签确认), then advisory."""

    alerts = interaction_alerts or []
    if not alerts:
        return ""
    interruptive = [a for a in alerts if a.get("alert_level") == "interruptive"]
    advisory = [a for a in alerts if a.get("alert_level") != "interruptive"]
    lines: list[str] = ["", "药物相互作用与合并病禁忌（分级告警）："]
    for a in interruptive:
        lines.append(f"- 🔴 **需医师确认**：{a.get('description')}（处理：{a.get('resolution')}）")
    for a in advisory:
        lines.append(f"- 🟡 提示：{a.get('description')}（处理：{a.get('resolution')}）")
    return "\n".join(lines)


def report_generation_skill(
    case_json: dict[str, Any],
    normalized_tags: list[str],
    syndrome_candidates: list[dict[str, Any]],
    formula_route: dict[str, Any] | None,
    matched_modules: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    safety: dict[str, Any],
    rule_hits: list[dict[str, Any]] | None = None,
    uncertainty: dict[str, Any] | None = None,
    interaction_alerts: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, str]:
    syndrome_rows = "\n".join(
        f"| {c['name']} | {c['score']} | {_join(c.get('evidence_tags', []))} | {c['confidence']} |"
        for c in syndrome_candidates
    ) or "| 未命中 | 0 | 未提供 | low |"
    module_rows = "\n".join(
        f"| {m['name']} | {_join(m.get('herbs', []))} | {_join(m.get('evidence_tags', []))} | {m.get('note', '')} |"
        for m in matched_modules
    ) or "| 未命中 | - | - | - |"
    rule_lines = "\n".join(
        f"- {h['rule_id']} {h['rule_name']}：证据标签 {_join(h.get('evidence_tags', []))}；解释：{h.get('rationale', '')}"
        for h in (rule_hits or [])
    ) or "- 暂无规则命中。"
    conflict_lines = "\n".join(f"- {c.get('description')}（处理：{c.get('resolution')}）" for c in conflicts) or "- 未发现主要路线冲突。"
    red_flag_lines = "\n".join(f"- {f.get('message')}" for f in safety.get("red_flags", [])) or "- 未见已录入红旗线索。"
    risk_lines = "\n".join(f"- {r}" for r in safety.get("medication_risks", [])) or "- 未见已命中特殊药物风险；仍需医生复核。"
    route_text = formula_route.get("name") if formula_route else "未形成稳定路线"
    route_reason = formula_route.get("rationale") if formula_route else "信息不足，需补充问诊与医生复核。"
    uncertainty_block = ("\n" + uncertainty_markdown(uncertainty) + "\n") if uncertainty else ""
    interaction_block = _interaction_lines(interaction_alerts)
    provenance_line = ""
    if provenance:
        provenance_line = (
            "\n---\n**决策出处（Provenance）**："
            f"规则库版本 `{provenance.get('rules_version', 'unknown')}` · "
            f"应用版本 `{provenance.get('app_version', 'unknown')}` · "
            "决策基础：确定性规则优先，模型仅作守卫后的解释叠加。\n"
        )
    report = f"""# 沈钦荣腰痹经验规则分析报告

## 1. 医案摘要
- 年龄/性别：{case_json.get('age', 'unknown')} / {case_json.get('sex', 'unknown')}
- 主诉：{case_json.get('main_complaint', 'unknown')}
- 病程：{case_json.get('duration', 'unknown')}（{case_json.get('duration_class', 'unknown')}）
- 主要症状：{_join(case_json.get('symptoms', []))}
- 舌脉：{_join(case_json.get('tongue', []))}；{_join(case_json.get('pulse', []))}
- 西医诊断/既往检查：{_join(case_json.get('western_diagnosis', []))}

## 2. 结构化标签
{_join(normalized_tags)}

## 3. 候选证型
| 证型 | 得分 | 命中证据 | 置信度 |
|---|---:|---|---|
{syndrome_rows}
{uncertainty_block}
## 4. 方剂路线（经验规则线索，非处方）
- 主路线：{route_text}
- 选择理由：{route_reason}
- 说明：此处仅为沈老经验规则路线解释，不是患者可执行处方。

## 5. 命中规则
{rule_lines}

## 6. 药物模块（非处方化展示）
| 模块 | 代表药物 | 触发证据 | 作用解释 |
|---|---|---|---|
{module_rows}

## 7. 互斥/冲突与安全提醒
{conflict_lines}{interaction_block}

## 8. 红旗与用药风险
红旗线索：
{red_flag_lines}

特殊药物/合规风险：
{risk_lines}

## 9. 信息缺口
{_join(case_json.get('missing_fields', []))}

## 10. 教学总结
本报告按“确定性规则引擎 + 中医教学解释”的方式整理医案：久病、年龄、舌脉、寒热、麻木、骨质疏松等作为规则证据；证型与方剂路线均为待医生复核的经验分析，不替代面诊、查体与影像判断。

## 11. 免责声明
{safety.get('disclaimer', '本结果仅用于名老中医经验研究、医案复盘与教学讨论，不构成诊断、处方或治疗建议。')}
{provenance_line}"""
    return {"markdown_report": report}
