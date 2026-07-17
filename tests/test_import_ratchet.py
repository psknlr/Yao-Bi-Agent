"""Import ratchet: agents/server may not add NEW direct skill imports (CI gate).

Goal state (harness review v0.12): every business-skill execution goes through the
ToolRegistry (role check, schema validation, execution-point budget, audit spans).
The deterministic clinical chain already does; the entries below are the *documented
remaining exceptions* — each is either (a) a runtime-bound LLM overlay that owns the
DaoClient, (b) a pure helper/gate function that is not a tool invocation, or (c) the
interview engine (conversion tracked as roadmap). This test is a ratchet: removing an
entry is always fine; ADDING a direct import fails CI and forces the registry path.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (file, imported module) — the shrinking allowlist. Helpers/gates are marked.
ALLOWED_DIRECT_SKILL_IMPORTS = {
    # runtime-bound LLM overlays (own the DaoClient; guarded + provenance'd in-host)
    ("backend/agents/conversation.py", "backend.skills.tao_consultation_skill"),
    ("backend/agents/conversation.py", "backend.skills.physician_reasoning_skill"),
    ("backend/agents/conversation.py", "backend.skills.case_experience_summary_skill"),
    ("backend/agents/clinical_agents.py", "backend.skills.physician_reasoning_skill"),
    ("backend/agents/clinical_agents.py", "backend.skills.case_experience_summary_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.tao_consultation_skill"),
    # pure helper functions / constants, not tool invocations
    ("backend/agents/conversation.py", "backend.skills.clinical_scope_router_skill"),
    ("backend/agents/conversation.py", "backend.skills.mined_evidence_skill"),
    ("backend/agents/autonomous_agent.py", "backend.skills.clinical_scope_router_skill"),
    ("backend/agents/clinical_agents.py", "backend.skills.clinical_scope_router_skill"),
    ("backend/agents/skill_router.py", "backend.skills.patient_request_guard_skill"),
    ("backend/server.py", "backend.skills.clinical_scope_router_skill"),
    ("backend/server.py", "backend.skills.mined_evidence_skill"),
    # server bridge (extraction/enrichment before a session exists)
    ("backend/server.py", "backend.skills.case_extract_skill"),
    ("backend/server.py", "backend.skills.case_normalize_skill"),
    ("backend/server.py", "backend.skills.safety_guard_skill"),
    ("backend/server.py", "backend.skills.tao_followup_probe_skill"),
    # interview engine: registry conversion tracked as roadmap (FSM slots + probes)
    ("backend/agents/yaobi_interview.py", "backend.skills.active_questioning"),
    ("backend/agents/yaobi_interview.py", "backend.skills.case_extract_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.case_normalize_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.clinical_entity_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.conflict_checker_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.formula_base_selector_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.herb_module_composer_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.safety_guard_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.syndrome_router_skill"),
    ("backend/agents/yaobi_interview.py", "backend.skills.uncertainty_skill"),
}

_IMPORT_RE = re.compile(r"from (backend\.skills\.[a-z_]+) import", re.M)


def _scan(path: Path) -> set[tuple[str, str]]:
    rel = str(path.relative_to(ROOT))
    return {(rel, module) for module in _IMPORT_RE.findall(path.read_text(encoding="utf-8"))}


def test_no_new_direct_skill_imports_in_agents_or_server():
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "backend" / "agents").glob("*.py")):
        found |= _scan(path)
    for path in sorted((ROOT / "backend" / "runtime").glob("*.py")):
        found |= _scan(path)
    found |= _scan(ROOT / "backend" / "server.py")
    new_imports = found - ALLOWED_DIRECT_SKILL_IMPORTS
    assert not new_imports, (
        "new direct skill imports outside the ToolRegistry path — route them through "
        f"get_registry().call(...) instead:\n{sorted(new_imports)}"
    )
