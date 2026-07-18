"""Ambient execution context: the active AgentRun travels with the call stack.

The budget problem this solves (harness review v0.12, P0): planners charged
"1 tool call" per intent while the handler underneath executed 5–8 real tools,
and the model client had no idea which run it was serving. Charging must happen
at the *real execution points* — ``ToolRegistry.invoke`` and ``DaoClient``
generation — not be guessed at the orchestration layer.

``contextvars`` (stdlib) makes the active run visible to those execution points
without threading a parameter through every skill signature, and is safe under
the threaded HTTP server (each request handler thread gets its own context).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from backend.runtime.run_context import AgentRun, StopReason

_ACTIVE_RUN: ContextVar["AgentRun | None"] = ContextVar("yaobi_active_run", default=None)


def current_run() -> "AgentRun | None":
    return _ACTIVE_RUN.get()


@contextmanager
def use_run(run: "AgentRun") -> Iterator["AgentRun"]:
    """Bind ``run`` as the ambient run for the duration of the block."""

    token = _ACTIVE_RUN.set(run)
    try:
        yield run
    finally:
        _ACTIVE_RUN.reset(token)


def charge_active_run(kind: str, amount: int = 1) -> "StopReason | None":
    """Charge the ambient run's budget; no-op (None) when no run is bound."""

    run = _ACTIVE_RUN.get()
    if run is None:
        return None
    return run.budget.charge(kind, amount)
