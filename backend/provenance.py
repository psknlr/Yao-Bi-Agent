"""Decision provenance: version fingerprints for rules, app and model runtime.

Top-tier CDSS practice requires every recommendation to be reproducible and
attributable: which rule content, which application version and which model
configuration produced it. This module computes a stable fingerprint over the
YAML rule library plus the app/model versions, so reports, audit records and
/api/health can all carry the same provenance block.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RULES_DIR = ROOT / "rules"

# Bump when the recommendation-relevant behaviour changes (kept in sync with pyproject.toml).
APP_VERSION = "0.14.0"

_FINGERPRINT_CACHE: dict[str, Any] | None = None
_FINGERPRINT_STAT_KEY: tuple | None = None


def _short_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _rules_stat_key() -> tuple:
    """Cheap change detector: (name, mtime_ns, size) of every rule file."""

    return tuple(
        (path.name, path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(RULES_DIR.glob("*.yaml"))
    )


def rules_fingerprint(refresh: bool = False) -> dict[str, Any]:
    """Content hash of every rules/*.yaml file plus a combined library hash.

    The rule engine re-reads YAML per request, so a live rule edit changes behaviour
    immediately — the fingerprint must not go stale or reports/audit would attribute
    decisions to the wrong rule version. The cache is keyed on file mtimes/sizes and
    recomputed whenever any rule file changes.
    """

    global _FINGERPRINT_CACHE, _FINGERPRINT_STAT_KEY
    stat_key = _rules_stat_key()
    if _FINGERPRINT_CACHE is not None and not refresh and stat_key == _FINGERPRINT_STAT_KEY:
        return _FINGERPRINT_CACHE

    files: dict[str, str] = {}
    combined = hashlib.sha256()
    for path in sorted(RULES_DIR.glob("*.yaml")):
        content = path.read_bytes()
        files[path.name] = _short_sha256(content)
        combined.update(path.name.encode("utf-8"))
        combined.update(content)
    _FINGERPRINT_CACHE = {
        "rules_version": combined.hexdigest()[:12],
        "rule_files": files,
    }
    _FINGERPRINT_STAT_KEY = stat_key
    return _FINGERPRINT_CACHE


def _file_fingerprint(*paths: Path) -> str | None:
    """Combined short hash of one or more files; None when none exist."""

    combined = hashlib.sha256()
    found = False
    for path in paths:
        try:
            combined.update(path.name.encode("utf-8"))
            combined.update(path.read_bytes())
            found = True
        except OSError:
            continue
    return combined.hexdigest()[:12] if found else None


def _git_commit() -> str | None:
    """Current git commit SHA via .git plumbing (no subprocess, best-effort)."""

    try:
        head = (ROOT / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            return (ROOT / ".git" / ref).read_text(encoding="utf-8").strip()[:12]
        return head[:12]
    except OSError:
        return None


_RUNTIME_FINGERPRINT: dict[str, Any] | None = None


def runtime_fingerprint(refresh: bool = False) -> dict[str, Any]:
    """Fingerprints of every behaviour-bearing artifact beyond the rule YAMLs.

    "Why did the same case produce a different answer today?" must be answerable
    from the provenance block alone — so prompts, guards, tool schemas, the policy
    manifest, the case schema and the code commit are all hashed, not just rules.
    """

    global _RUNTIME_FINGERPRINT
    if _RUNTIME_FINGERPRINT is not None and not refresh:
        return _RUNTIME_FINGERPRINT

    backend_dir = ROOT / "backend"
    config_dir = ROOT / "config"
    try:
        import json

        from backend.tools import get_registry

        tool_registry_hash = _short_sha256(
            json.dumps(get_registry().export_schemas(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        )
    except Exception:
        tool_registry_hash = None
    _RUNTIME_FINGERPRINT = {
        "prompt_bundle_hash": _file_fingerprint(backend_dir / "llm" / "prompt_templates.py"),
        "guard_version": _file_fingerprint(backend_dir / "llm" / "output_guard.py"),
        "policy_bundle_hash": _file_fingerprint(config_dir / "safety_config.yaml", config_dir / "hermes_agent.yaml"),
        "case_schema_hash": _file_fingerprint(config_dir / "case_schema.json"),
        "tool_registry_hash": tool_registry_hash,
        "git_commit": _git_commit(),
    }
    return _RUNTIME_FINGERPRINT


def get_provenance(dao_config: Any | None = None) -> dict[str, Any]:
    """Provenance block attached to reports, audit records and /api/health.

    ``dao_config`` is an optional ``DaoGenerationConfig``; when given, the model
    runtime configuration is fingerprinted alongside the rule library so an
    LLM-overlaid report can be traced to the exact backend/model that wrote it.
    """

    block: dict[str, Any] = {
        "app_version": APP_VERSION,
        **rules_fingerprint(),
        **runtime_fingerprint(),
        "decision_basis": "deterministic_rules_first",
    }
    if dao_config is not None:
        block["model_runtime"] = {
            "model_id": getattr(dao_config, "model_id", None),
            # Model revision pin — None means "latest", acceptable for the research
            # prototype but must be pinned (with audited remote code) in production.
            "model_revision": getattr(dao_config, "model_revision", None),
            "backend": getattr(dao_config, "backend", None),
            "torch_dtype": getattr(dao_config, "torch_dtype", None),
            "quantization": "4bit" if getattr(dao_config, "load_in_4bit", False)
            else "8bit" if getattr(dao_config, "load_in_8bit", False) else "none",
        }
    return block
