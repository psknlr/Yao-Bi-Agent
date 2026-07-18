"""First-class approval objects for high-risk human-in-the-loop actions.

An emergency-referral override is the highest-risk action the system supports:
it clears confirmed red flags and resumes clinical questioning. Before this
module it was a bare ``review_action="override"`` string plus free-text reason.
Now it is a two-phase, attributable approval:

1. request phase — requires an authenticated clinician role, a ``reviewer_id``
   and a non-empty reason; creates a *pending* :class:`ApprovalRequest` and does
   NOT change clinical state;
2. confirm phase — the same reviewer re-submits with the approval id and the
   explicit confirmation flag; only then is the approval marked approved and the
   action executed.

Every phase is recorded in the audit log (which is hash-chained, see
``backend/audit/audit_log.py``), so who overrode which red flags, when and why
is reconstructible. Storage is in-memory per process — matching the interview
session store it protects; durable storage is a deployment concern documented
in docs/harness_review_response_2026-07.md.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any

from backend.audit import get_audit_log

# Risk tiers for reviewed actions (mirrors the review-requirement table in docs).
RISK_LEVELS = {"confirm_referral": "standard", "revise_referral": "standard", "override_emergency_referral": "critical"}


@dataclass
class ApprovalRequest:
    action_type: str
    session_id: str
    required_role: str = "clinician"
    reviewer_id: str = ""
    reason: str = ""
    risk_level: str = "critical"
    status: str = "pending"          # pending | approved | rejected | expired | invalidated_by_new_facts
    payload: dict[str, Any] = field(default_factory=dict)
    # Fact binding (v0.14): digest of the clinical facts the approval was requested
    # against (red flags + safety level + turn count). A confirm arriving after the
    # facts changed invalidates the approval instead of applying a stale decision.
    case_digest: str = ""
    approval_id: str = field(default_factory=lambda: f"apr-{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    decided_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditWriteError(RuntimeError):
    """High-risk commit refused: the audit record could not be written (fail closed)."""


def _audit_or_fail(event_type: str, payload: dict[str, Any]) -> None:
    """Audit is a PRECONDITION for high-risk approval actions: when audit is enabled
    but the write fails, the action is denied instead of committed unaccountably.
    (A deployment that explicitly disabled audit has made that decision itself.)"""

    log = get_audit_log()
    record = log.record(event_type, payload)
    if log.enabled and record is None:
        raise AuditWriteError(f"audit write failed for {event_type}; high-risk action denied")


class ApprovalManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: dict[str, ApprovalRequest] = {}

    @staticmethod
    def _store():
        from backend.runtime.event_store import get_event_store

        return get_event_store()

    def create(self, *, action_type: str, session_id: str, reviewer_id: str, reason: str,
               payload: dict[str, Any] | None = None, case_digest: str = "") -> ApprovalRequest:
        request = ApprovalRequest(
            action_type=action_type,
            session_id=session_id,
            reviewer_id=reviewer_id,
            reason=reason,
            risk_level=RISK_LEVELS.get(action_type, "critical"),
            payload=payload or {},
            case_digest=case_digest,
        )
        # Fail closed BEFORE registering: no unaudited pending approval may exist.
        _audit_or_fail("approval_requested", {
            "approval_id": request.approval_id, "action_type": action_type,
            "session_id": session_id, "reviewer_id": reviewer_id,
            "risk_level": request.risk_level, "reason": reason[:300],
            "payload": payload or {}, "case_digest": case_digest,
        })
        with self._lock:
            self._requests[request.approval_id] = request
        store = self._store()
        if store is not None:
            store.save_approval(request.to_dict())
            store.append_event("APPROVAL_REQUESTED", {"approval_id": request.approval_id,
                                                      "action_type": action_type, "reviewer_id": reviewer_id})
        return request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            request = self._requests.get(approval_id)
        if request is not None:
            return request
        # Restart recovery: rehydrate a persisted approval (e.g. the physician
        # confirms the next day after a redeploy).
        store = self._store()
        if store is not None:
            record = store.load_approval(approval_id)
            if record is not None:
                request = ApprovalRequest(**{k: v for k, v in record.items() if k in ApprovalRequest.__dataclass_fields__})
                with self._lock:
                    self._requests[approval_id] = request
                return request
        return None

    def decide(self, approval_id: str, *, decision: str, reviewer_id: str,
               current_case_digest: str | None = None) -> ApprovalRequest | None:
        """Approve/reject a pending request.

        Reviewer policy: by default the confirming reviewer must MATCH the requester
        (two-phase re-confirmation — honestly documented as re-confirmation, not
        independent review). With ``YAOBI_OVERRIDE_DISTINCT_REVIEWER=1``, critical
        approvals instead require a DIFFERENT authenticated reviewer (four-eyes).

        Fact binding: when the request carries a ``case_digest`` and the caller
        supplies the current digest, a mismatch marks the approval
        ``invalidated_by_new_facts`` — new patient facts void stale approvals.
        """

        request = self.get(approval_id)
        with self._lock:
            if request is None or request.status != "pending":
                return None
            distinct_required = request.risk_level == "critical" and \
                os.getenv("YAOBI_OVERRIDE_DISTINCT_REVIEWER", "").lower() in {"1", "true", "yes"}
            if distinct_required:
                if reviewer_id == request.reviewer_id:
                    # Four-eyes mode: the requester cannot confirm their own override.
                    return None
            elif reviewer_id != request.reviewer_id:
                # A different human cannot silently complete someone else's override.
                return None
            new_status = "approved" if decision == "approve" else "rejected"
        if request.case_digest and current_case_digest is not None \
                and current_case_digest != request.case_digest:
            # Facts changed between request and confirmation: the physician reviewed
            # a case that no longer exists. Invalidate instead of applying.
            _audit_or_fail("approval_invalidated_by_new_facts", {
                "approval_id": approval_id, "action_type": request.action_type,
                "session_id": request.session_id, "reviewer_id": reviewer_id,
                "requested_digest": request.case_digest, "current_digest": current_case_digest,
            })
            with self._lock:
                request.status = "invalidated_by_new_facts"
                request.decided_at = time.time()
            store = self._store()
            if store is not None:
                store.save_approval(request.to_dict())
                store.append_event("APPROVAL_INVALIDATED", {"approval_id": approval_id,
                                                            "reviewer_id": reviewer_id})
            return request
        # Fail closed: the decision is only committed if it can be audited.
        _audit_or_fail("approval_decided", {
            "approval_id": approval_id, "action_type": request.action_type,
            "session_id": request.session_id, "reviewer_id": reviewer_id,
            "decision": new_status,
        })
        with self._lock:
            request.status = new_status
            request.decided_at = time.time()
        store = self._store()
        if store is not None:
            store.save_approval(request.to_dict())
            store.append_event("APPROVAL_DECIDED", {"approval_id": approval_id, "decision": new_status,
                                                    "reviewer_id": reviewer_id})
        return request


_MANAGER: ApprovalManager | None = None


def get_approval_manager() -> ApprovalManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ApprovalManager()
    return _MANAGER
