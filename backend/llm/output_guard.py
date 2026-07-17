from __future__ import annotations

import re
import unicodedata
from typing import Any

# Chinese numerals that appear in colloquial dose / frequency instructions ("三克"、"一日三次").
_CHN_NUM = "一二三四五六七八九十两半"


def _normalize(text: str) -> str:
    """NFKC-fold before pattern matching: full-width digits/letters (３克、ｇ) and other
    compatibility forms must not slip past ASCII-anchored dose patterns."""

    return unicodedata.normalize("NFKC", text or "")

FORBIDDEN_PATTERNS = {
    "final_diagnosis": [r"最终诊断", r"明确诊断", r"诊断为", r"可诊断为", r"确诊为"],
    "patient_executable_prescription": [
        r"处方如下",
        r"完整处方",
        r"请按.*服用",
        r"水煎服",
        # Frequency instructions in Arabic or Chinese numerals: 每日2次 / 一日三次 / 每天三服 / 日3次.
        rf"[每一]?[日天]\s*[{_CHN_NUM}\d]+\s*[次服]",
        # Solid-dose frequency ("每日三丸/一日两片/每天2粒/每日一袋") — the numeral+unit form
        # that the 次/服 pattern above misses entirely.
        rf"[每一][日天]\s*[{_CHN_NUM}\d]+\s*[丸片粒袋帖贴]",
        rf"分[{_CHN_NUM}\d]+次",
        # Colloquial regimen phrasings that dodge the numeral patterns:
        # 早晚各服一回 / 每日早晚各一次 / 照此煎服 / 依上述比例配齐 / 按常规量使用.
        rf"早晚各?服?[{_CHN_NUM}\d]*[次回]",
        # Administration verbs that finish an executable regimen without a numeral
        # ("早晚分服" / "睡前温服" / "空腹顿服" / "开水送服" / "黄酒冲服"), the bypass the
        # numeral-anchored patterns above cannot see.
        r"(?:早晚|晨昏|睡前|临睡|空腹|饭前|饭后|餐后|温开?水?|黄酒)?[^，。;；\n]{0,4}(?:分服|顿服|温服|送服|冲服|噙服|含服)",
        r"照此(?:执行|服用|煎服|用药)",
        r"依(?:上述|此)比例",
        r"按常规[量法]",
        # Numbered course of treatment ("两个疗程"), not the bare teaching word 疗程.
        rf"[{_CHN_NUM}\d]+\s*个?疗程",
    ],
    "dose_instruction": [
        # Arabic-digit doses: 3g / 3 克 / 500mg / 3钱 (excludes alphabetic runs like "IgG4").
        r"\d+(?:\.\d+)?\s*(?:g(?![a-zA-Z])|克|mg(?![a-zA-Z])|毫克|钱)",
        # Liquid / decoction volumes ("煎取400毫升" / "浓煎至150ml" / "取汁200毫升"): the
        # standard patient-facing 汤剂 dosing unit, absent from the mass-only set above.
        r"\d+(?:\.\d+)?\s*(?:毫升|ml(?![a-zA-Z])|mL|升)",
        # Solid-form counts ("每次三丸" handled below; bare "三丸/两片/一袋" here).
        rf"[{_CHN_NUM}\d]+\s*[丸片粒袋帖贴]",
        # Chinese-numeral doses: 三克 / 两钱 — the classic bypass of digit-only regexes.
        rf"[{_CHN_NUM}]+\s*[克钱]",
        r"先煎",
        r"后下",
        r"饭后服",
        # Per-dose instructions ("每次一袋/每次服6克"), not innocent phrases like "每次复诊".
        rf"每次[^，。;；\n]{{0,6}}(?:\d|[{_CHN_NUM}]|服|克|丸|片|袋|毫升|ml)",
        # Classical hand-measure dosing ("以三指撮为度") — a dose instruction in disguise.
        r"[一二三]指撮",
    ],
    "replacement_for_clinician": [r"无需就医", r"不用看医生", r"可以自行", r"自行购买"],
}

# Structured JSON contract: these keys must stay null in every Tao JSON overlay.
_FORBIDDEN_STRUCTURED_KEYS = (
    "final_diagnosis",
    "complete_prescription",
    "patient_executable_dose",
    "administration_instruction",
)


def _structured_violations(structured_output: dict[str, Any] | None) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for key in _FORBIDDEN_STRUCTURED_KEYS:
        if (structured_output or {}).get(key):
            violations.append({"category": key, "pattern": f"structured.{key}"})
    return violations


def guard_tao_output(text: str, structured_output: dict[str, Any] | None = None) -> dict[str, Any]:
    """Strict patient-floor guard: no diagnosis, prescription, or executable dose at all."""

    text = _normalize(text)
    violations: list[dict[str, str]] = []
    for category, patterns in FORBIDDEN_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                violations.append({"category": category, "pattern": pattern})
                break

    violations.extend(_structured_violations(structured_output))

    return {
        "allowed": not violations,
        "violations": violations,
        "fallback_required": bool(violations),
        "guardrail": "no_final_diagnosis_no_patient_executable_prescription_no_dose",
    }


# Patient-directed self-administration / "skip the doctor" phrasing — forbidden for every role.
PATIENT_SELF_ADMIN_PATTERNS = [
    r"自行服用", r"自己煎", r"自行煎", r"回家.{0,6}煎", r"回家.{0,6}服", r"自行抓药", r"自行购买",
    r"无需就医", r"不用看医生", r"不必就医", r"可以自行", r"自己买药",
]

# Content forbidden even in a clinician-facing draft: assertive final verdicts and
# complete executable regimens. Experience dose *ranges*, 方义, 先煎/后下 safety notes
# and the bare word 疗程 are deliberately allowed — that is the point of a teaching draft.
# The diagnosis pattern targets *assertive* verdicts ("最终诊断：X"、"确诊为X"), not the
# mandated boilerplate "最终诊断与处方须医师面诊后确定" or the advice "完善影像以明确诊断".
_CLINICIAN_DRAFT_FORBIDDEN = [
    (r"最终诊断[为是：:]|明确诊断为|诊断明确为|确诊为|可以?确诊", "assertive_final_diagnosis"),
    (r"处方如下|完整处方|请按.*服用|按方抓药", "complete_prescription"),
    (
        rf"水煎服|[每一][日天]\s*[{_CHN_NUM}\d]+\s*[次服丸片粒袋帖贴]|分[{_CHN_NUM}\d]+次(?:服|口服)"
        rf"|早晚各?服?[{_CHN_NUM}\d]*[次回]"
        # Administration verbs that finish an executable regimen (early/evening split
        # dose, bedtime warm dose, decoct-and-take): forbidden even in a teaching draft,
        # while experience dose *ranges* (3-6g，医师审核) stay allowed above.
        rf"|(?:早晚|晨昏|睡前|临睡|空腹|饭前|饭后|餐后|温开?水?|黄酒)?[^，。;；\n]{{0,4}}(?:分服|顿服|温服|送服|冲服|噙服|含服)"
        rf"|照此(?:执行|服用|煎服|用药)|依(?:上述|此)比例|按常规[量法]",
        "executable_regimen",
    ),
]


def guard_clinician_draft(text: str, structured_output: dict[str, Any] | None = None) -> dict[str, Any]:
    """Soft guard for clinician/research Tao overlays (report / reasoning / experience notes).

    A clinician draft may name candidate syndromes, formula routes, 方义 and experience
    dose ranges ("细辛经验剂量 3-6g，医师审核") — those are what a teaching note is for.
    It may not assert a final diagnosis, issue a complete executable regimen, or tell
    the patient to self-medicate / skip the physician.
    """

    text = _normalize(text)
    violations: list[dict[str, str]] = []
    for pattern in PATIENT_SELF_ADMIN_PATTERNS + FORBIDDEN_PATTERNS["replacement_for_clinician"]:
        if re.search(pattern, text, flags=re.IGNORECASE):
            violations.append({"category": "patient_self_administration", "pattern": pattern})
            break
    for pattern, category in _CLINICIAN_DRAFT_FORBIDDEN:
        if re.search(pattern, text, flags=re.IGNORECASE):
            violations.append({"category": category, "pattern": pattern})

    violations.extend(_structured_violations(structured_output))

    return {
        "allowed": not violations,
        "violations": violations,
        "fallback_required": bool(violations),
        "guardrail": "clinician_draft_no_final_verdict_no_executable_regimen_no_self_administration",
    }


def guard_consultation(text: str, user_role: str = "clinician") -> dict[str, Any]:
    """Role-aware guard for the Tao-primary professional consultation answer.

    The clinician/researcher draft is allowed to name candidate syndromes, formula routes,
    方义 and **experience dose ranges** (the whole point of a teaching/research note) — but
    it inherits every clinician-draft prohibition: no assertive final diagnosis, no
    complete executable regimen (处方如下 / 水煎服 / 一日X次), and no patient
    self-administration phrasing. For the patient role we keep the strict floor
    (no diagnosis / prescription / executable dose at all).

    Consultation is the *loosest* clinician-facing surface (the model is the primary
    reasoner there), so it must never be looser than ``guard_clinician_draft`` — that
    inversion was exactly the P0 gap this delegation closes.
    """

    if user_role == "patient":
        return guard_tao_output(text)
    return guard_clinician_draft(text)


# ------------------------------------------------------------------ patient payload floor
# Strict whitelist of turn fields a patient-facing API response may expose. Everything
# else (clinician drafts, herb modules, rule traces, dose statistics…) is dropped
# server-side — the patient view is an allowlist, not a blocklist.
PATIENT_TURN_VISIBLE_FIELDS = (
    "question", "intent", "intent_label", "blocked", "disclaimer",
    "suggested_followups", "safety_notice", "trace",
)

_PATIENT_GUARDED_FALLBACK = (
    "为了您的安全，这部分内容需要执业医师当面评估后才能提供。"
    "如腰痛持续加重、夜间痛明显、伴发热、外伤后疼痛或大小便异常，请尽快线下就诊。"
)


_TRACE_REDACTED = "（该步骤含仅供医师查看的内容，已在患者视图隐去）"


def _patient_safe_trace_step(step: Any) -> Any:
    """Redact a trace step's free-text observation if it fails the strict patient guard."""

    if not isinstance(step, dict):
        return step
    obs = step.get("observation")
    if isinstance(obs, str) and not guard_tao_output(obs)["allowed"]:
        return {**step, "observation": _TRACE_REDACTED}
    return step


def filter_patient_payload(turn: dict[str, Any]) -> dict[str, Any]:
    """Reduce a turn payload to the patient-visible structured schema.

    The answer text must still pass the strict patient guard — a clinician-grade
    draft that leaked into a patient turn is replaced by the safe fallback. Fields
    like ``medication_advice`` are pinned to ``null`` by construction so no upstream
    change can accidentally expose prescribing content to patients.
    """

    answer = str(turn.get("answer") or "")
    guard = guard_tao_output(answer)
    filtered: dict[str, Any] = {key: turn[key] for key in PATIENT_TURN_VISIBLE_FIELDS if key in turn}
    # The top-level answer is re-guarded, but ``trace`` is a free-text container whose
    # per-step ``observation`` carries the full step answer — a leaked draft or a
    # dose-instruction (e.g. 附子先煎) that was blocked at the top level could survive
    # inside the trace. Re-run the strict patient guard over every observation and
    # replace any violating one (the trace stays visible as a reasoning skeleton).
    if "trace" in filtered and isinstance(filtered["trace"], list):
        filtered["trace"] = [_patient_safe_trace_step(step) for step in filtered["trace"]]
    filtered.update({
        "role": "patient",
        "patient_visible_message": answer if guard["allowed"] else _PATIENT_GUARDED_FALLBACK,
        "answer": answer if guard["allowed"] else _PATIENT_GUARDED_FALLBACK,
        "forbidden_content_detected": not guard["allowed"],
        "guard_violations": [v["category"] for v in guard["violations"]],
        "requires_doctor_review": True,
        "medication_advice": None,
        "clinician_draft": None,
        "answer_source": "patient_safe_view",
    })
    return filtered


def guard_probe(text: str) -> dict[str, Any]:
    """Probes are questions only: block any diagnosis verdict / prescription / dose leakage."""

    text = _normalize(text)
    bad = (
        FORBIDDEN_PATTERNS["final_diagnosis"]
        + FORBIDDEN_PATTERNS["patient_executable_prescription"]
        + FORBIDDEN_PATTERNS["dose_instruction"]
    )
    for pattern in bad:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return {"allowed": False, "violations": [{"category": "probe_leak", "pattern": pattern}]}
    return {"allowed": True, "violations": []}
