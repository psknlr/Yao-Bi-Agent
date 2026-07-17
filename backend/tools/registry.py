"""Runtime tool registry: the single source of truth for tool schemas and execution.

Previously ``config/hermes_tools.json`` was a hand-maintained description file while
the code called skill functions directly — the two had already drifted apart
(``conflict_checker_skill`` gained ``medications``/``conditions`` the JSON never knew;
``consent_privacy_skill`` / ``chief_complaint_skill`` schemas described parameters the
real functions do not even have). This module inverts the relationship:

* every tool is registered as a :class:`ToolSpec` whose JSON schema is **derived from
  the real function signature** (``schema_from_callable``), so schema drift is
  structurally impossible;
* ``config/hermes_tools.json`` is *generated* from the registry
  (``ToolRegistry.export_schemas``) and a CI test asserts the file matches;
* execution goes through one entry point that enforces, in order:
  existence → role authorization → input validation → execute (timed) →
  output validation → audit span.

Error taxonomy (harness decides what to do with each):
``ToolNotFound`` / ``ToolPolicyDenied`` / ``ToolInputError`` (re-plan arguments) /
``ToolOutputValidationError`` (discard result) / ``ToolExecutionError`` (retry or
fall back to the deterministic template).

Stdlib only. Timeouts are *soft* by design: the deterministic skills run in-process
and Python threads cannot be safely killed, so the registry measures duration and
attaches a ``timeout_exceeded`` warning instead of pretending to hard-cancel — the
model-call path (DaoClient) has its own genuine HTTP timeouts and retries.
"""

from __future__ import annotations

import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    error_type = "tool_error"
    retryable = False


class ToolNotFound(ToolError):
    error_type = "tool_not_found"


class ToolPolicyDenied(ToolError):
    error_type = "tool_policy_denied"


class ToolInputError(ToolError):
    error_type = "tool_input_error"


class ToolOutputValidationError(ToolError):
    error_type = "tool_output_validation_error"


class ToolExecutionError(ToolError):
    error_type = "tool_execution_error"
    retryable = True


# --------------------------------------------------------------- schema derivation
_BASE_TYPES = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def _annotation_to_schema(annotation: str) -> dict[str, Any]:
    """Map a (stringified) Python annotation to a JSON-schema-lite property."""

    text = str(annotation).strip().strip("'\"")
    nullable = False
    if text.endswith("| None"):
        nullable = True
        text = text[: text.rfind("|")].strip()
    schema: dict[str, Any] = {}
    if text.startswith("dict"):
        schema["type"] = "object"
    elif text.startswith(("list", "tuple")):
        schema["type"] = "array"
        inner = text[text.find("[") + 1: text.rfind("]")] if "[" in text else ""
        inner = inner.split(",")[0].strip()
        if inner in _BASE_TYPES:
            schema["items"] = {"type": _BASE_TYPES[inner]}
        elif inner.startswith("dict"):
            schema["items"] = {"type": "object"}
    elif text in _BASE_TYPES:
        schema["type"] = _BASE_TYPES[text]
    # anything else ("Any", custom classes) stays untyped — accepts any JSON value
    if nullable and "type" in schema:
        schema["type"] = [schema["type"], "null"]
    return schema


def schema_from_callable(
    fn: Callable[..., Any],
    exclude: tuple[str, ...] = ("dao_client",),
    param_enums: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    """JSON schema for a tool derived from its real signature — the anti-drift source.

    ``additionalProperties: false`` by default: unknown arguments ("ignore_safety": true)
    are a schema violation, not a Python TypeError deep inside the handler.
    ``param_enums`` narrows string parameters to closed vocabularies (e.g. user_role).
    """

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in inspect.signature(fn).parameters.items():
        if name in exclude or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        prop = _annotation_to_schema(param.annotation)
        if param_enums and name in param_enums:
            prop["enum"] = list(param_enums[name])
        properties[name] = prop
        if param.default is param.empty:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


# --------------------------------------------------------------- schema validation
_JSON_TYPES = {
    "object": dict, "array": list, "string": str, "integer": int,
    "number": (int, float), "boolean": bool, "null": type(None),
}


def _validate(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    problems: list[str] = []
    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        py_types = tuple(t for name in types for t in (
            _JSON_TYPES[name] if isinstance(_JSON_TYPES[name], tuple) else (_JSON_TYPES[name],)
        ))
        if not isinstance(value, py_types) or (isinstance(value, bool) and "boolean" not in types):
            problems.append(f"{path}: expected {expected}, got {type(value).__name__}")
            return problems
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for key, sub in properties.items():
            if key in value:
                problems.extend(_validate(value[key], sub, f"{path}.{key}"))
        for key in schema.get("required") or []:
            if key not in value:
                problems.append(f"{path}: missing required property '{key}'")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                problems.append(f"{path}: unknown properties {unknown} (additionalProperties: false)")
    if isinstance(value, list) and schema.get("items"):
        for i, item in enumerate(value):
            problems.extend(_validate(item, schema["items"], f"{path}[{i}]"))
    return problems


def validate_against_schema(value: Any, schema: dict[str, Any]) -> list[str]:
    return _validate(value, schema, "$")


# --------------------------------------------------------------------- registry
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Any] | None
    parameters: dict[str, Any]
    allowed_roles: frozenset[str]
    risk_level: str = "read"                 # read | clinical_draft | high_risk
    idempotent: bool = True
    timeout_seconds: float = 5.0
    execution: str = "registry"              # registry | direct (runtime-bound: needs dao_client)
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool name: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolNotFound(f"unknown tool: {name}")
        return spec

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[name] for name in self.names()]

    def export_schemas(self) -> list[dict[str, Any]]:
        """The generated content of config/hermes_tools.json (single source of truth)."""

        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
                "x_governance": {
                    "allowed_roles": sorted(spec.allowed_roles),
                    "risk_level": spec.risk_level,
                    "idempotent": spec.idempotent,
                    "execution": spec.execution,
                },
            }
            for spec in self.specs()
        ]

    # -- execution --------------------------------------------------------------
    def invoke(self, name: str, arguments: dict[str, Any] | None = None, *, role: str = "system",
               context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Guarded execution returning a standardized ToolResult envelope (never raises)."""

        span_id = f"span-{uuid.uuid4().hex[:12]}"
        arguments = arguments or {}
        started = time.time()
        warnings: list[str] = []

        def envelope(status: str, output: Any = None, error: ToolError | None = None) -> dict[str, Any]:
            record = {
                "status": status,
                "tool": name,
                "span_id": span_id,
                "output": output,
                "error": str(error) if error else None,
                "error_type": error.error_type if error else None,
                "retryable": bool(error.retryable) if error else False,
                "warnings": warnings,
                "duration_ms": int((time.time() - started) * 1000),
                "role": role,
            }
            self._record_span(record, context)
            return record

        try:
            spec = self.get(name)
        except ToolNotFound as exc:
            return envelope("error", error=exc)
        if role not in spec.allowed_roles:
            return envelope("error", error=ToolPolicyDenied(
                f"role '{role}' is not authorized for tool '{name}' (allowed: {sorted(spec.allowed_roles)})"
            ))
        # Budget is charged HERE — the real execution point — via the ambient run
        # context, so nested tools inside one intent are all counted (never guessed
        # at the planner layer). No ambient run ⇒ no charge (library use).
        from backend.runtime.execution_context import charge_active_run

        exhausted = charge_active_run("tool_call")
        if exhausted is not None:
            return envelope("error", error=ToolPolicyDenied(
                f"run budget exhausted before tool '{name}' ({exhausted.value})"
            ))
        if spec.handler is None or spec.execution != "registry":
            return envelope("error", error=ToolPolicyDenied(
                f"tool '{name}' is runtime-bound (execution={spec.execution}) and must be invoked by its host component"
            ))
        problems = validate_against_schema(arguments, spec.parameters)
        if problems:
            return envelope("error", error=ToolInputError("; ".join(problems[:6])))
        try:
            output = spec.handler(**arguments)
        except ToolError as exc:
            return envelope("error", error=exc)
        except Exception as exc:  # noqa: BLE001 — classified, not swallowed
            return envelope("error", error=ToolExecutionError(f"{type(exc).__name__}: {exc}"))
        duration = time.time() - started
        if duration > spec.timeout_seconds:
            warnings.append(f"timeout_exceeded: {duration:.2f}s > {spec.timeout_seconds}s (soft timeout)")
        out_problems = validate_against_schema(output, spec.output_schema)
        if out_problems:
            return envelope("error", error=ToolOutputValidationError("; ".join(out_problems[:6])))
        return envelope("success", output=output)

    def call(self, name: str, *, role: str = "system", context: dict[str, Any] | None = None,
             **arguments: Any) -> Any:
        """Raising variant for deterministic in-process pipelines: returns the tool's
        raw output on success, raises the classified ToolError on failure."""

        result = self.invoke(name, arguments, role=role, context=context)
        if result["status"] == "success":
            return result["output"]
        error_cls = {
            "tool_not_found": ToolNotFound,
            "tool_policy_denied": ToolPolicyDenied,
            "tool_input_error": ToolInputError,
            "tool_output_validation_error": ToolOutputValidationError,
        }.get(result["error_type"], ToolExecutionError)
        raise error_cls(result["error"] or "tool call failed")

    @staticmethod
    def _record_span(record: dict[str, Any], context: dict[str, Any] | None) -> None:
        try:
            from backend.audit import get_audit_log

            get_audit_log().record("tool_span", {
                "tool": record["tool"], "span_id": record["span_id"], "status": record["status"],
                "error_type": record["error_type"], "duration_ms": record["duration_ms"],
                "role": record["role"], "run_id": (context or {}).get("run_id"),
                "warnings": record["warnings"],
            })
        except Exception:  # audit must never break a tool call
            pass
