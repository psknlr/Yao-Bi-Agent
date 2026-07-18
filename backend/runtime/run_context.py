"""Unified run lifecycle: one status enum, one stop-reason vocabulary, one budget.

Before this module, "is the run finished / halted / waiting for a human?" was
answered by ad-hoc field combinations (``halted`` + ``done`` + ``safety_level`` +
``review_action`` …), which admits illegal combinations as features grow
(``done=True`` with a pending review, ``halted=True`` in a normal stage). The
:class:`AgentRun` state machine makes the lifecycle explicit and validates every
transition; :class:`RunBudget` centralises the previously scattered limits
(max_steps / probe budgets / retry counts) and always reports *why* a run
stopped instead of a bare failure.

Stdlib only, deliberately small: this is the kernel other components attach to,
not a workflow framework.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_FOR_TOOL = "WAITING_FOR_TOOL"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    SUSPENDED = "SUSPENDED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SAFETY_HALTED = "SAFETY_HALTED"


class StopReason(str, Enum):
    GOAL_COMPLETED = "goal_completed"
    SAFETY_HALT = "safety_halt"
    HUMAN_INPUT_REQUIRED = "human_input_required"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TOOL_FAILURE = "tool_failure"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    POLICY_DENIED = "policy_denied"
    CANCELLED = "cancelled"


# Terminal statuses have no outgoing transitions.
_TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.SAFETY_HALTED}

_LEGAL_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_FOR_TOOL, RunStatus.WAITING_FOR_HUMAN, RunStatus.SUSPENDED,
        RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.SAFETY_HALTED,
    },
    RunStatus.WAITING_FOR_TOOL: {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED},
    RunStatus.WAITING_FOR_HUMAN: {RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.CANCELLED},
    RunStatus.SUSPENDED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    **{status: set() for status in _TERMINAL},
}

# Default status a stop reason lands in when the caller does not override it.
_STOP_STATUS: dict[StopReason, RunStatus] = {
    StopReason.GOAL_COMPLETED: RunStatus.COMPLETED,
    StopReason.SAFETY_HALT: RunStatus.SAFETY_HALTED,
    StopReason.HUMAN_INPUT_REQUIRED: RunStatus.WAITING_FOR_HUMAN,
    StopReason.BUDGET_EXHAUSTED: RunStatus.COMPLETED,   # partial answer is still delivered
    StopReason.TOOL_FAILURE: RunStatus.FAILED,
    StopReason.INSUFFICIENT_EVIDENCE: RunStatus.COMPLETED,  # honest abstention is a completion
    StopReason.POLICY_DENIED: RunStatus.COMPLETED,      # refusal answer is the deliverable
    StopReason.CANCELLED: RunStatus.CANCELLED,
}


class IllegalRunTransition(RuntimeError):
    pass


@dataclass
class RunBudget:
    """Central spend meter for one run. ``charge`` returns a StopReason when exhausted.

    tool_calls and model_calls are charged at the REAL execution points
    (``ToolRegistry.invoke`` / ``DaoClient._dispatch``) via the ambient execution
    context — never guessed by planners (one intent may execute 5–8 real tools).
    ``model_output_chars`` is the zero-dependency stand-in for token budgets:
    character counts are tokenizer-free, monotone with tokens, and enforce the
    same runaway-generation bound (true token/cost accounting is a production
    concern once a tokenizer/pricing model is pinned).
    """

    max_iterations: int = 8
    max_tool_calls: int = 64
    max_model_calls: int = 12
    max_model_output_chars: int = 120_000
    max_wall_time_seconds: float = 120.0
    iterations: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    model_output_chars: int = 0
    started_at: float = field(default_factory=time.time)

    def charge(self, kind: str, amount: int = 1) -> StopReason | None:
        if kind == "iteration":
            self.iterations += amount
            if self.iterations > self.max_iterations:
                return StopReason.BUDGET_EXHAUSTED
        elif kind == "tool_call":
            self.tool_calls += amount
            if self.tool_calls > self.max_tool_calls:
                return StopReason.BUDGET_EXHAUSTED
        elif kind == "model_call":
            self.model_calls += amount
            if self.model_calls > self.max_model_calls:
                return StopReason.BUDGET_EXHAUSTED
        elif kind == "model_output_chars":
            self.model_output_chars += amount
            if self.model_output_chars > self.max_model_output_chars:
                return StopReason.BUDGET_EXHAUSTED
        if time.time() - self.started_at > self.max_wall_time_seconds:
            return StopReason.BUDGET_EXHAUSTED
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "iterations": f"{self.iterations}/{self.max_iterations}",
            "tool_calls": f"{self.tool_calls}/{self.max_tool_calls}",
            "model_calls": f"{self.model_calls}/{self.max_model_calls}",
            "model_output_chars": f"{self.model_output_chars}/{self.max_model_output_chars}",
            "elapsed_seconds": round(time.time() - self.started_at, 2),
            "max_wall_time_seconds": self.max_wall_time_seconds,
        }


@dataclass
class AgentRun:
    """One agent run: identity, validated status machine, budget and event trace."""

    goal: str
    user_role: str = "clinician"
    session_id: str | None = None
    parent_run_id: str | None = None
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    status: RunStatus = RunStatus.CREATED
    stop_reason: StopReason | None = None
    budget: RunBudget = field(default_factory=RunBudget)
    events: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def record(self, event_type: str, **data: Any) -> None:
        self.events.append({"ts": round(time.time(), 3), "event": event_type, **data})

    def transition(self, new_status: RunStatus, note: str = "") -> None:
        if new_status == self.status:
            return
        allowed = _LEGAL_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise IllegalRunTransition(
                f"illegal run transition {self.status.value} → {new_status.value} (run {self.run_id})"
            )
        self.record("status_transition", from_status=self.status.value, to_status=new_status.value, note=note)
        self.status = new_status

    def start(self) -> None:
        self.transition(RunStatus.RUNNING)

    def finish(self, stop_reason: StopReason, note: str = "", status: RunStatus | None = None) -> None:
        """Terminal (or waiting) transition with an explicit machine-readable reason.

        A terminal run cannot be finished again (v0.14): the first stop reason is
        the truth — a later ``finish(GOAL_COMPLETED)`` must not relabel a
        budget-exhausted or safety-halted run as a normal completion. The transition
        runs BEFORE the stop_reason assignment so an illegal transition leaves the
        recorded reason untouched.
        """

        if self.terminal:
            raise IllegalRunTransition(
                f"run {self.run_id} is already terminal "
                f"({self.status.value}/{self.stop_reason.value if self.stop_reason else None}); "
                "a finished run cannot be finished again"
            )
        self.transition(status or _STOP_STATUS[stop_reason], note=note)
        self.stop_reason = stop_reason

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "parent_run_id": self.parent_run_id,
            "goal_digest_chars": len(self.goal or ""),
            "user_role": self.user_role,
            "status": self.status.value,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "budget": self.budget.snapshot(),
            "events": self.events[-20:],
            "created_at": self.created_at,
        }
