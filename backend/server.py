"""YaoBi-Skill HTTP server: serves the static UI **and** the genuine Tao-in-the-loop API.

This is the bridge the static frontend was missing. The browser UI calls these JSON
endpoints, so the language model **actually** drives skill selection (constrained
function-calling), multi-step planning, rule-bounded follow-up probes, and the
reasoning/experience agents — instead of the client-side keyword stubs.

Safety invariants are unchanged and enforced server-side: the model only *selects /
sequences / rewrites* registered skills; clinical content comes from deterministic rules
and de-identified mined data; patient requests for final diagnosis / prescription / dose
are blocked by ``patient_request_guard_skill``; Tao output passes JSON repair + output
guard and falls back to rules on any violation.

Zero extra dependencies (stdlib ``http.server`` only). The Tao runtime is selected purely
through environment variables consumed by ``DaoClient`` (``TAO_BACKEND``, ``TAO_MODEL_ID``,
``TAO_LOAD_IN_4BIT`` …), so the same server runs with mock, an HTTP endpoint, or a local
``transformers`` model such as ``CMLM/Dao1-30b-a3b``.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import json
import os
import sys
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from backend.agents.autonomous_agent import AutonomousQAAgent
from backend.agents.conversation import ConversationSession
from backend.agents.orchestrator import AgentOrchestrator
from backend.agents.skill_router import suggested_questions
from backend.agents.yaobi_interview import YaoBiCaseState, YaoBiInterviewEngine
from backend.audit import get_audit_log, get_counters
from backend.audit.audit_log import text_digest
from backend.llm.dao_client import OPENAI_COMPATIBLE_BACKENDS, DaoClient, DaoRuntimeError
from backend.llm.output_guard import filter_patient_payload
from backend.provenance import get_provenance
from backend.skills.case_extract_skill import case_extract_skill
from backend.skills.case_normalize_skill import case_normalize_skill
from backend.skills.clinical_scope_router_skill import question_scope_gate
from backend.skills.mined_evidence_skill import load_mined_rules
from backend.skills.safety_guard_skill import safety_guard_skill
from backend.skills.tao_followup_probe_skill import tao_followup_probe_skill

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"

# Shared, process-wide client (the heavy transformers model is cached on the class).
CLIENT = DaoClient()
TAO_ENABLED = CLIENT.config.backend in ({"mock", "transformers"} | OPENAI_COMPATIBLE_BACKENDS)

# Map frontend intake stages to FSM states and the fields Tao may hint at (must mirror the
# allowed fields so a generated probe cannot drive a state jump or invent a new field).
STAGE_TO_STATE = {
    "pain": "S3_PAIN_PROFILE",
    "neuro": "S4_NEURO_ORTHO",
    "tcm": "S5_TCM_CORE",
    "comorbidity": "S7_COMORBIDITY",
}
STAGE_FIELDS = {
    "pain": ["location", "radiation", "pain_nature", "severity", "aggravating", "relieving"],
    "neuro": ["numbness", "numbness_location", "weakness", "walking_limitation", "imaging", "western_diagnosis"],
    "tcm": ["cold_heat", "cold_relation", "dampness", "sleep", "appetite", "mouth_taste", "tongue_color", "tongue_coating"],
    "comorbidity": ["diseases", "medications", "anticoagulant", "allergy"],
}


def tao_info() -> dict[str, Any]:
    c = CLIENT.config
    status = CLIENT.load_status()
    return {
        "enabled": TAO_ENABLED,
        "backend": c.backend,
        "model_id": c.model_id,
        "quantization": "4bit" if c.load_in_4bit else "8bit" if c.load_in_8bit else "none",
        # Load lifecycle so the UI / Colab can poll readiness ("loading" vs "ready" vs
        # "error") instead of holding one long warmup request open and guessing on failure.
        "load_state": status["state"],
        "model_loaded": status["model_loaded"],
        "load_error": status["error"],
    }


def _case_state(data: dict[str, Any]) -> dict[str, Any]:
    """Bridge the frontend's computed tags into the backend case_state shape."""

    red = data.get("red_flags") or {}
    state = {
        "normalized_tags": list(data.get("tags") or []),
        "red_flags": {"status": red.get("status"), "positive_items": list(red.get("positive_items") or [])},
        "neuro_ortho": data.get("neuro_ortho") or {},
        "comorbidity": data.get("comorbidity") or {},
    }
    # SERVER-AUTHORITATIVE fields (v0.14): a scope decision is authorization data,
    # not case data — it only travels when computed server-side by
    # _enrich_with_question (marked _scope_source="server"). A client-supplied
    # {"scope": {"in_scope": true}} is dropped and audited as a suspicious field;
    # downstream gates recompute scope from the narrative each turn.
    if data.get("scope") is not None and data.get("_scope_source") == "server":
        state["scope"] = data["scope"]
    elif data.get("scope") is not None:
        AUDIT.record("client_scope_claim_dropped", {"claimed_scope": bool((data.get("scope") or {}).get("in_scope"))})
    # The fail-closed marker may come from the client too — that direction only
    # DENIES more (abstention), so honoring it is safe.
    if data.get("safety_extraction_failed"):
        state["safety_extraction_failed"] = True
    return state


# Host the HTTP server is actually bound to (set by make_server). None means the
# handlers are being driven directly as a library / in tests — no network exposure.
_SERVER_BIND_HOST: str | None = None
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _publicly_bound() -> bool:
    return _SERVER_BIND_HOST is not None and _SERVER_BIND_HOST not in _LOOPBACK_HOSTS


def _resolve_role(data: dict[str, Any]) -> tuple[str, str]:
    """Server-side role resolution — the client may *request* clinician mode, never grant it.

    Least privilege: the default role is ``patient``. A request only becomes
    ``clinician`` when it explicitly asks for doctor mode **and**:

    * a configured ``YAOBI_CLINICIAN_TOKEN`` is presented (body field
      ``clinician_token`` or ``X-Clinician-Token`` header, injected by the handler), or
    * no token is configured **and** the server is not bound to a public interface
      (local research demo / direct library use only).

    A publicly bound server (e.g. ``0.0.0.0`` behind Colab + ngrok) with no token
    configured therefore *cannot* grant clinician mode at all — forgetting to set the
    token locks the clinician surface instead of opening it to the internet.

    Returns ``(role, source)`` where source records how the decision was made,
    for the audit trail: ``default_patient`` / ``token_verified`` / ``token_mismatch``
    / ``local_demo`` / ``public_no_token_denied``.
    """

    return _resolve_auth(data)[:2]


def _clinician_token_map() -> dict[str, str]:
    """Per-identity tokens: YAOBI_CLINICIAN_TOKENS="dr-001:tokenA,dr-002:tokenB".

    Binds an authenticated *subject* to each token so review actions can be
    attributed to a specific clinician instead of a request-body string.
    """

    raw = os.getenv("YAOBI_CLINICIAN_TOKENS") or ""
    mapping: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            subject, token = pair.split(":", 1)
            if subject.strip() and token.strip():
                mapping[subject.strip()] = token.strip()
    return mapping


def _resolve_auth(data: dict[str, Any]) -> tuple[str, str, str | None]:
    """(role, source, subject_id) — the subject is the AUTHENTICATED identity.

    reviewer identity for approvals is derived from this subject, never from a
    request-body field: a caller holding a clinician token cannot claim to be a
    different physician (harness review v0.12 P0).
    """

    if not data.get("doctor_mode"):
        return "patient", "default_patient", None
    supplied = str(data.get("clinician_token") or "")
    token_map = _clinician_token_map()
    if token_map:
        for subject, token in token_map.items():
            if hmac.compare_digest(supplied, token):
                return "clinician", "token_verified", subject
        # fall through to the single shared token, if additionally configured
    expected = os.getenv("YAOBI_CLINICIAN_TOKEN") or ""
    if expected:
        if hmac.compare_digest(supplied, expected):
            return "clinician", "token_verified", os.getenv("YAOBI_CLINICIAN_ID") or "clinician-token-holder"
        return "patient", "token_mismatch", None
    if token_map:
        return "patient", "token_mismatch", None
    if _publicly_bound():
        return "patient", "public_no_token_denied", None
    return "clinician", "local_demo", "local-demo-clinician"


def _role(data: dict[str, Any]) -> str:
    return _resolve_auth(data)[0]


def _clinician_only(data: dict[str, Any]) -> dict[str, Any] | None:
    """Deny clinician-draft endpoints to non-clinician callers (server-side RBAC)."""

    role, source = _resolve_role(data)
    if role != "clinician":
        message = "该端点仅面向执业医师端。患者端请使用问诊、危险信号自查与健康教育功能。"
        if source == "public_no_token_denied":
            message = (
                "医生端已锁定：服务器绑定在公网地址且未配置 YAOBI_CLINICIAN_TOKEN。"
                "请在部署环境设置该令牌后，医生端携带 X-Clinician-Token 头访问。"
            )
        elif source == "token_mismatch":
            message = "医生端令牌校验失败，请携带正确的 X-Clinician-Token 头（或 clinician_token 字段）。"
        return {"error": "clinician_role_required", "role_source": source, "message": message}
    return None


# Red-flag status severity order for escalate-only merging: a client-declared or
# previously graded status may be *raised* by newly extracted findings, never lowered.
_RED_FLAG_SEVERITY = {None: 0, "": 0, "unknown": 0, "safe": 1, "caution": 2, "urgent": 3}


def _escalate_status(current: str | None, graded: str | None) -> str | None:
    # A graded "safe" is not an escalation and must not overwrite None (= not yet
    # screened): one free-text question finding nothing is not a completed screen.
    if graded in {"caution", "urgent"} and _RED_FLAG_SEVERITY.get(graded, 0) > _RED_FLAG_SEVERITY.get(current, 0):
        return graded
    return current


def _ops_guard(data: dict[str, Any]) -> dict[str, Any] | None:
    """Operational endpoints (/api/metrics, /api/warmup) are not patient surfaces:
    loopback / library use passes; a publicly bound server requires the clinician
    token; a public bind without a configured token locks them (they expose audit
    paths, runtime info, and warmup can burn GPU time anonymously)."""

    if not _publicly_bound():
        return None
    expected = os.getenv("YAOBI_CLINICIAN_TOKEN") or ""
    supplied = str(data.get("clinician_token") or "")
    if expected and hmac.compare_digest(supplied, expected):
        return None
    return {"error": "ops_endpoint_locked",
            "message": "运维端点在公网绑定下需要有效的 X-Clinician-Token（未配置令牌时锁定）。"}


def _enrich_with_question(data: dict[str, Any], question: str) -> dict[str, Any]:
    """Extract structured tags from the free-text question and merge with intake tags.

    This lets the chat answer a typed clinical description (e.g. "腰痛、下肢麻木、遇冷加重、
    舌暗苔白腻") with real rule-engine candidates, instead of only the questionnaire tags.
    Extraction stays deterministic; the language model still only selects which skill runs.

    Red flags found in the free text go through the *same* category-tiered grader as the
    pipeline (``safety_guard_skill``), not a flat "caution" default: typing 会阴麻木/尿不出来
    must grade urgent here exactly as it would in the intake flow, because the downstream
    red-flag gates key off ``red_flags.status``. Grading is escalate-only — it can raise a
    client-declared status, never lower it.
    """

    merged = dict(data)
    try:
        case_json = case_extract_skill(question)
        extracted = case_normalize_skill(case_json).get("normalized_tags") or []
        merged["tags"] = sorted(set(data.get("tags") or []) | set(extracted))
        red = dict(data.get("red_flags") or {})
        graded = safety_guard_skill(case_json, None, merged["tags"])
        confirmed_terms = [f.get("term") or f.get("id") for f in graded.get("confirmed_red_flags") or []]
        rf_items = list(red.get("positive_items") or []) + [t for t in confirmed_terms if t]
        if rf_items:
            red["positive_items"] = sorted(set(rf_items))
        red["status"] = _escalate_status(red.get("status"), graded.get("safety_status"))
        red["need_further_inquiry"] = sorted(
            set(red.get("need_further_inquiry") or []) | set(graded.get("need_further_inquiry") or [])
        )
        merged["red_flags"] = red
        gate = question_scope_gate(question, {"normalized_tags": merged["tags"]})
        if not gate["allowed"]:
            merged["scope"] = {
                "in_scope": False,
                "out_of_scope_reason": gate["message"],
                "reason_codes": gate["reason_codes"],
            }
            # Server-authoritative marker: _case_state only accepts scope decisions
            # stamped here — never a client-declared scope (v0.14).
            merged["_scope_source"] = "server"
    except Exception as exc:
        # FAIL CLOSED (entry review §6): a crashed safety extraction must not silently
        # fall through to clinical reasoning on stale/partial tags. The marker makes
        # every downstream gate abstain, and the failure is audited.
        merged["safety_extraction_failed"] = True
        AUDIT.record("safety_extraction_error", {"error": f"{type(exc).__name__}: {exc}"})
    return merged


# CDSS governance: append-only decision audit + in-memory metrics (see backend/audit/).
AUDIT = get_audit_log()
COUNTERS = get_counters()


def _decision_summary(path: str, data: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Extract the audit-relevant decision facts per endpoint (no raw patient text)."""

    summary: dict[str, Any] = {}
    turn = result.get("turn") if isinstance(result.get("turn"), dict) else None
    if path in {"/api/chat", "/api/autonomous"} and turn:
        summary = {
            "intent": turn.get("intent"),
            "method": turn.get("method") or turn.get("plan_method"),
            "used_llm": turn.get("used_llm"),
            "answer_source": turn.get("answer_source"),
            "blocked": turn.get("intent") == "safety_block",
            "question": text_digest(str(data.get("question") or "")),
            # Role provenance: token_verified / local_demo / token_mismatch / …
            "role": result.get("role"),
            "role_source": result.get("role_source"),
        }
        consult = turn.get("consult_runtime") or {}
        if consult.get("fallback_used"):
            COUNTERS.increment("llm_fallback")
        guard = (consult.get("guard") or {})
        if guard and not guard.get("allowed", True):
            COUNTERS.increment("guard_rejected")
        if summary["blocked"]:
            COUNTERS.increment("patient_request_blocked")
    elif path == "/api/interview":
        summary = {
            "session_id": result.get("session_id"),
            "state": result.get("state"),
            "safety_level": result.get("safety_level"),
            "done": result.get("done"),
            "red_flag_count": len(result.get("red_flags") or []),
            "review_action": data.get("review_action") or None,
            "message": text_digest(str(data.get("message") or "")),
        }
        if result.get("safety_level") == "emergency":
            COUNTERS.increment("red_flag_emergency_stop")
    elif path == "/api/collaboration":
        summary = {
            "halted": result.get("halted"),
            "halt_reason": result.get("halt_reason"),
            "agent_count": result.get("agent_count"),
            "used_llm_agents": result.get("used_llm_agents"),
        }
    elif path == "/api/followup_probe":
        runtime = result.get("tao_probe_runtime") or {}
        summary = {"probe_count": len(result.get("probes") or []), "status": runtime.get("status")}
    return summary


# --------------------------------------------------------------------------- handlers
def handle_health(_data: dict[str, Any]) -> dict[str, Any]:
    from backend.runtime.event_store import persistence_status

    provenance = get_provenance()
    persistence = persistence_status()
    # Readiness is persistence-aware (v0.14): a required-but-unavailable event store
    # must fail health checks, and a silent memory-only fallback must be VISIBLE.
    return {
        "ok": persistence.get("mode") != "required_but_unavailable",
        "tao": tao_info(),
        "mined_loaded": bool(load_mined_rules()),
        "persistence": persistence,
        "service": "yaobi-skill",
        "provenance": {"app_version": provenance["app_version"], "rules_version": provenance["rules_version"]},
    }


def handle_starters(_data: dict[str, Any]) -> dict[str, Any]:
    return {"starters": suggested_questions(), "tao": tao_info()}


def handle_chat(data: dict[str, Any]) -> dict[str, Any]:
    question = str(data.get("question") or "").strip()
    if not question:
        return {"error": "empty question"}
    role, role_source = _resolve_role(data)
    session = ConversationSession(case_state=_case_state(_enrich_with_question(data, question)), use_llm=TAO_ENABLED, dao_client=CLIENT, user_role=role)
    turn = session.ask(question)
    if role == "patient":
        # Strict allowlist view: patient responses expose only whitelisted fields and
        # a re-guarded answer text (see output_guard.filter_patient_payload).
        turn = filter_patient_payload(turn)
    return {"turn": turn, "role": role, "role_source": role_source, "tao": tao_info()}


def handle_autonomous(data: dict[str, Any]) -> dict[str, Any]:
    question = str(data.get("question") or "").strip()
    if not question:
        return {"error": "empty question"}
    role, role_source = _resolve_role(data)
    agent = AutonomousQAAgent(case_state=_case_state(_enrich_with_question(data, question)), use_llm=TAO_ENABLED, dao_client=CLIENT, user_role=role)
    turn = agent.run(question)
    if role == "patient":
        turn = filter_patient_payload(turn)
    return {"turn": turn, "role": role, "role_source": role_source, "tao": tao_info()}


def handle_followup_probe(data: dict[str, Any]) -> dict[str, Any]:
    stage = str(data.get("stage") or "")
    state = STAGE_TO_STATE.get(stage)
    if not state:
        return {"probes": [], "tao_probe_runtime": {"enabled": TAO_ENABLED, "status": "not_applicable"}, "tao": tao_info()}
    cs = _case_state(data)
    result = tao_followup_probe_skill(
        cs,
        state,
        STAGE_FIELDS.get(stage, []),
        rule_context={"normalized_tags": cs["normalized_tags"], "stage": stage},
        last_answers=data.get("last_answers") or {},
        max_probes=int(data.get("budget", 2) or 2),
        dao_client=CLIENT,
        use_llm=TAO_ENABLED,
    )
    result["tao"] = tao_info()
    return result


def handle_reasoning(data: dict[str, Any]) -> dict[str, Any]:
    denied = _clinician_only(data)
    if denied:
        return denied
    session = ConversationSession(case_state=_case_state(data), use_llm=TAO_ENABLED, dao_client=CLIENT, user_role="clinician")
    return {"result": session.invoke("reasoning_inquiry"), "tao": tao_info()}


def handle_summary(data: dict[str, Any]) -> dict[str, Any]:
    denied = _clinician_only(data)
    if denied:
        return denied
    session = ConversationSession(case_state=_case_state(data), use_llm=TAO_ENABLED, dao_client=CLIENT, user_role="clinician")
    return {"result": session.invoke("experience_inquiry"), "tao": tao_info()}


def handle_collaboration(data: dict[str, Any]) -> dict[str, Any]:
    denied = _clinician_only(data)
    if denied:
        return denied
    result = AgentOrchestrator().run(_case_state(data), use_llm=TAO_ENABLED, dao_client=CLIENT)
    result.pop("blackboard", None)  # internal working memory, not needed by the UI
    result["tao"] = tao_info()
    return result


# In-memory interview sessions (ephemeral; keyed by session_id from the UI).
# Bounded: the oldest-touched session is evicted past the cap so a long-running public
# deployment (Colab + ngrok) cannot grow memory without limit.
_INTERVIEWS: "OrderedDict[str, YaoBiCaseState]" = OrderedDict()
_MAX_INTERVIEW_SESSIONS = 256
# Registry lock plus per-session locks: turns within one session are serialized (two
# concurrent posts would otherwise mutate the same YaoBiCaseState mid-turn) while
# different sessions stay independent.
_INTERVIEW_LOCK = Lock()
_SESSION_LOCKS: dict[str, Lock] = {}


def handle_interview(data: dict[str, Any]) -> dict[str, Any]:
    """One turn of the Tao-driven conversational interview (extract → FSM → ask → report).

    Supports an additional ``review_action`` field for physician confirmation / revision /
    override of the safety referral (POST body: ``review_action``, ``physician_notes``,
    ``override_reason``).  Only meaningful when the session is in ``SAFETY_REFERRAL`` state.
    """

    # A missing session_id gets a server-generated one (returned to the caller) instead of
    # a shared "default" key that would leak state across unrelated clients.
    session_id = str(data.get("session_id") or "") or f"srv-{uuid.uuid4().hex[:12]}"
    with _INTERVIEW_LOCK:
        if data.get("reset"):
            _INTERVIEWS.pop(session_id, None)
            _SESSION_LOCKS.pop(session_id, None)
            return {"reset": True, "session_id": session_id, "tao": tao_info()}
        case = _INTERVIEWS.get(session_id)
        if case is None:
            case = YaoBiCaseState(session_id=session_id)
            _INTERVIEWS[session_id] = case
            while len(_INTERVIEWS) > _MAX_INTERVIEW_SESSIONS:
                evicted, _ = _INTERVIEWS.popitem(last=False)
                _SESSION_LOCKS.pop(evicted, None)
        else:
            _INTERVIEWS.move_to_end(session_id)
        session_lock = _SESSION_LOCKS.setdefault(session_id, Lock())
    engine = YaoBiInterviewEngine(dao_client=CLIENT, use_llm=TAO_ENABLED)
    review_action = str(data.get("review_action") or "").strip()
    with session_lock:
        if review_action:
            # Physician review actions are clinician-only, server-side. Before this
            # check, any caller could send review_action=override and clear confirmed
            # red flags — the highest-risk write in the system with no RBAC at all.
            denied = _clinician_only(data)
            if denied:
                return {**denied, "session_id": session_id, "tao": tao_info()}
            # Reviewer identity comes from the AUTHENTICATED subject, never from the
            # request body — a body reviewer_id is recorded as a claim in the audit
            # trail but cannot decide who the reviewer is (v0.12 P0).
            _, _, auth_subject = _resolve_auth(data)
            claimed = str(data.get("reviewer_id") or "")
            if claimed and auth_subject and claimed != auth_subject:
                AUDIT.record("reviewer_identity_claim_mismatch", {
                    "session_id": session_id, "auth_subject": auth_subject, "claimed": claimed[:64],
                })
            result = engine.run_review(
                case,
                action=review_action,
                physician_notes=str(data.get("physician_notes") or ""),
                override_reason=str(data.get("override_reason") or ""),
                reviewer_id=auth_subject or "",
                confirm_override=bool(data.get("confirm_override")),
                approval_id=str(data.get("approval_id") or ""),
            )
        else:
            result = engine.run_turn(case, str(data.get("message") or ""))
    result["tao"] = tao_info()
    return result


# Valid physician feedback verdicts (the learning loop of the CDSS governance cycle).
_FEEDBACK_ACTIONS = {"confirmed", "revised", "rejected"}
_FEEDBACK_TARGETS = {"chat_turn", "autonomous_turn", "interview_report", "form_report", "probe", "collaboration", "other"}


def handle_feedback(data: dict[str, Any]) -> dict[str, Any]:
    """Clinician feedback on a system output: 确认 / 需修订 / 不采纳 (+ optional reason).

    Feedback closes the learning loop: it is appended to the audit log and tallied in
    /api/metrics so rule curators can see which recommendations clinicians trust.
    """

    denied = _clinician_only(data)
    if denied:
        return {"ok": False, **denied}
    action = str(data.get("action") or "").strip()
    if action not in _FEEDBACK_ACTIONS:
        return {"ok": False, "error": f"action must be one of {sorted(_FEEDBACK_ACTIONS)}"}
    target = str(data.get("target") or "other")
    if target not in _FEEDBACK_TARGETS:
        target = "other"
    record = {
        "action": action,
        "target": target,
        "session_id": str(data.get("session_id") or "")[:64] or None,
        "intent": str(data.get("intent") or "")[:120] or None,
        "answer_source": str(data.get("answer_source") or "")[:120] or None,
        "used_llm": bool(data.get("used_llm")),
        # Physician-authored feedback comment: stored verbatim (bounded) by design —
        # it is the learning-loop payload for rule curators, not patient narrative.
        # Patient free text elsewhere is digest-only (see backend/audit/audit_log.py).
        "reason": str(data.get("reason") or "")[:500] or None,
        "user_role": _role(data),
    }
    COUNTERS.increment(f"feedback_{action}")
    if record["used_llm"]:
        COUNTERS.increment(f"feedback_{action}_llm")
    AUDIT.record("physician_feedback", record)
    return {"ok": True, "recorded": record, "tao": tao_info()}


def handle_metrics(data: dict[str, Any]) -> dict[str, Any]:
    """Operational + governance metrics: request counts, guard trips, fallbacks, feedback."""

    denied = _ops_guard(data)
    if denied:
        return denied
    counters = COUNTERS.snapshot()
    feedback = {k: v for k, v in counters.items() if k.startswith("feedback_")}
    confirmed = feedback.get("feedback_confirmed", 0)
    total_feedback = sum(v for k, v in feedback.items() if k in {"feedback_confirmed", "feedback_revised", "feedback_rejected"})
    return {
        "ok": True,
        "uptime_seconds": round(time.time() - COUNTERS.started_at, 1),
        "counters": counters,
        "feedback_summary": {
            "total": total_feedback,
            "acceptance_rate": round(confirmed / total_feedback, 3) if total_feedback else None,
        },
        "audit": {"enabled": AUDIT.enabled, "directory": str(AUDIT.directory), "write_errors": AUDIT.write_errors},
        "provenance": get_provenance(CLIENT.config),
        "tao": tao_info(),
    }


def handle_warmup(data: dict[str, Any]) -> dict[str, Any]:
    denied = _ops_guard(data)
    if denied:
        return denied
    if not TAO_ENABLED or CLIENT.config.backend == "disabled":
        return {"ok": False, "reason": "Tao backend disabled (set TAO_BACKEND).", "tao": tao_info()}
    started = time.time()
    try:
        reply = CLIENT.chat([], "请用一句话说明你在本系统中的角色边界。")
    except DaoRuntimeError as exc:
        return {"ok": False, "reason": str(exc), "tao": tao_info()}
    except Exception as exc:  # noqa: BLE001 — report the real cause instead of a 500
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "tao": tao_info()}
    return {"ok": True, "ms": int((time.time() - started) * 1000), "reply_preview": str(reply)[:160], "tao": tao_info()}


ROUTES_GET = {"/api/health": handle_health, "/api/starters": handle_starters, "/api/metrics": handle_metrics}
ROUTES_POST = {
    "/api/chat": handle_chat,
    "/api/autonomous": handle_autonomous,
    "/api/followup_probe": handle_followup_probe,
    "/api/reasoning": handle_reasoning,
    "/api/summary": handle_summary,
    "/api/collaboration": handle_collaboration,
    "/api/interview": handle_interview,
    "/api/feedback": handle_feedback,
    "/api/warmup": handle_warmup,
}
# POST endpoints whose decisions are audit-logged (feedback logs itself with full detail).
_AUDITED_ENDPOINTS = {"/api/chat", "/api/autonomous", "/api/interview", "/api/collaboration", "/api/followup_probe"}


# Fixed-window per-IP rate limiter (stdlib only). YAOBI_RATE_LIMIT=requests/minute;
# 0 disables. Bounds abuse on public deployments without external dependencies.
_RATE_LOCK = Lock()
_RATE_BUCKETS: dict[str, tuple[float, int]] = {}


def _rate_limited(ip: str) -> bool:
    try:
        limit = int(os.getenv("YAOBI_RATE_LIMIT", "120"))
    except ValueError:
        limit = 120
    if limit <= 0:
        return False
    now = time.time()
    with _RATE_LOCK:
        if len(_RATE_BUCKETS) > 4096:
            for key in [k for k, (start, _) in _RATE_BUCKETS.items() if now - start >= 60]:
                _RATE_BUCKETS.pop(key, None)
        start, count = _RATE_BUCKETS.get(ip, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        count += 1
        _RATE_BUCKETS[ip] = (start, count)
        return count > limit


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves /api/* as JSON and everything else as static files from frontend/."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(FRONTEND_DIR), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:  # keep Colab output quiet
        pass

    def end_headers(self) -> None:
        # Same-origin by default: the server hosts the frontend itself, so no CORS
        # header is needed. Cross-origin access (e.g. a separately hosted UI) must be
        # opted into explicitly via YAOBI_ALLOW_ORIGIN — never a blanket "*" default.
        allow_origin = os.getenv("YAOBI_ALLOW_ORIGIN", "")
        if allow_origin:
            self.send_header("Access-Control-Allow-Origin", allow_origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Clinician-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Cap POST bodies (1 MiB is far beyond any legitimate case text) so an oversized
    # Content-Length cannot balloon memory on a public deployment.
    MAX_BODY_BYTES = 1024 * 1024

    class _BadRequest(ValueError):
        pass

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.MAX_BODY_BYTES:
            raise self._BadRequest(f"request body too large ({length} bytes; max {self.MAX_BODY_BYTES})")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise self._BadRequest(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise self._BadRequest("request body must be a JSON object")
        return parsed

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        handler = ROUTES_GET.get(path)
        if handler is not None:
            if _rate_limited(self.client_address[0]):
                self._send_json({"error": "rate_limited", "message": "请求过于频繁，请稍后再试。"}, status=429)
                return
            data: dict[str, Any] = {}
            header_token = self.headers.get("X-Clinician-Token")
            if header_token:
                data["clinician_token"] = header_token
            self._dispatch(handler, data)
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        handler = ROUTES_POST.get(path)
        if handler is None:
            self._send_json({"error": f"unknown endpoint {path}"}, status=404)
            return
        if _rate_limited(self.client_address[0]):
            self._send_json({"error": "rate_limited", "message": "请求过于频繁，请稍后再试。"}, status=429)
            return
        try:
            data = self._read_json()
        except self._BadRequest as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        # Clinician credential travels as a header; expose it to role resolution
        # without letting a body field silently override it.
        header_token = self.headers.get("X-Clinician-Token")
        if header_token:
            data["clinician_token"] = header_token
        self._dispatch(handler, data)

    def _dispatch(self, handler: Any, data: dict[str, Any]) -> None:
        path = self.path.split("?", 1)[0]
        request_id = uuid.uuid4().hex[:16]
        started = time.time()
        COUNTERS.increment(f"requests:{path}")
        try:
            result = handler(data)
        except Exception as exc:  # never crash the UI; log the full cause server-side only
            tb = traceback.format_exc()
            print(f"[yaobi-server] {self.path} failed (request {request_id}):\n{tb}", file=sys.stderr, flush=True)
            COUNTERS.increment(f"errors:{path}")
            AUDIT.record("api_error", {"endpoint": path, "request_id": request_id, "error": f"{type(exc).__name__}: {exc}"})
            # Internal details stay server-side (stderr + audit); the client gets a
            # correlation id, not the exception text.
            self._send_json({"error": "internal_error", "message": "服务器内部错误，请稍后重试。", "request_id": request_id}, status=500)
            return
        if isinstance(result, dict):
            result.setdefault("request_id", request_id)
        if path in _AUDITED_ENDPOINTS:
            AUDIT.record("api_decision", {
                "endpoint": path,
                "request_id": request_id,
                "latency_ms": int((time.time() - started) * 1000),
                **_decision_summary(path, data, result if isinstance(result, dict) else {}),
            })
        self._send_json(result)


def make_server(port: int = 8000, host: str = "0.0.0.0") -> http.server.ThreadingHTTPServer:
    # Record the bind host for role resolution: a publicly bound server without a
    # configured YAOBI_CLINICIAN_TOKEN must never grant clinician mode (see _resolve_role).
    global _SERVER_BIND_HOST
    _SERVER_BIND_HOST = host
    if _publicly_bound() and not os.getenv("YAOBI_CLINICIAN_TOKEN"):
        print(
            "[yaobi-server] WARNING: 服务器绑定公网地址但未配置 YAOBI_CLINICIAN_TOKEN——"
            "医生端已锁定为患者视图。公网演示需要医生端时请设置该令牌。",
            file=sys.stderr, flush=True,
        )
    # allow_reuse_address (SO_REUSEADDR) is already set on the base class; being explicit makes
    # a quick restart (e.g. re-running the Colab launch cell) reuse the port instead of failing
    # with "Address already in use" and leaving nothing listening → Connection refused.
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    return http.server.ThreadingHTTPServer((host, port), Handler)


def _should_preload(cli_flag: bool | None, backend: str) -> bool:
    """Decide whether to eagerly load the model at startup.

    Priority: explicit ``--preload/--no-preload`` > ``TAO_PRELOAD`` env > default (on for the
    heavy ``transformers`` backend so the load — and any failure — happens visibly at startup
    in a background thread, rather than hidden inside the first warmup request).
    """

    if cli_flag is not None:
        return cli_flag
    env = os.getenv("TAO_PRELOAD")
    if env is not None and env.strip() != "":
        return env.strip().lower() in {"1", "true", "yes", "on"}
    return backend == "transformers"


def _start_background_preload() -> None:
    """Load the model in a daemon thread so the HTTP port is up immediately.

    The server answers /api/health (reporting ``load_state``) while the weights load, so a
    client can poll readiness instead of holding one multi-minute request open. Any *catchable*
    failure is logged with its real cause; a hard OOM-kill still takes the process down, which
    is exactly why the launcher must also watch the process and surface its captured log.
    """

    def _run() -> None:
        info = tao_info()
        print(
            f"[yaobi-server] preloading Tao model in background: backend={info['backend']} "
            f"model={info['model_id']} quant={info['quantization']} "
            f"(a 30B FP16 model can take many minutes / tens of GB to load)...",
            flush=True,
        )
        started = time.time()
        status = CLIENT.preload()
        secs = int(time.time() - started)
        if status.get("ok"):
            print(f"[yaobi-server] Tao model ready in {secs}s (state={status.get('state')}).", flush=True)
        else:
            print(
                f"[yaobi-server] Tao preload did not complete after {secs}s "
                f"(state={status.get('state')}): {status.get('reason')}",
                file=sys.stderr,
                flush=True,
            )

    Thread(target=_run, name="tao-preload", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the YaoBi-Skill UI + Tao-in-the-loop API")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="0.0.0.0")
    preload_group = parser.add_mutually_exclusive_group()
    preload_group.add_argument("--preload", dest="preload", action="store_true", default=None,
                               help="Eagerly load the model at startup (default for TAO_BACKEND=transformers).")
    preload_group.add_argument("--no-preload", dest="preload", action="store_false",
                               help="Skip eager loading; the model loads lazily on the first request.")
    args = parser.parse_args()
    try:
        httpd = make_server(args.port, args.host)
    except OSError as exc:
        print(f"[yaobi-server] failed to bind {args.host}:{args.port}: {exc}", file=sys.stderr, flush=True)
        print("[yaobi-server] another server may still be holding the port — stop it (or wait for it to exit) and retry, or pass a different --port.", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
    info = tao_info()
    print(f"YaoBi-Skill server on http://{args.host}:{args.port}  (frontend: {FRONTEND_DIR})", flush=True)
    print(f"Tao runtime: enabled={info['enabled']} backend={info['backend']} model={info['model_id']} quant={info['quantization']}", flush=True)
    if _should_preload(args.preload, info["backend"]) and TAO_ENABLED and info["backend"] != "disabled":
        _start_background_preload()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
