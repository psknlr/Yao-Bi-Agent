"""Single-version-fact and manifest-drift lints (CI gate).

The 2026-07 harness review found three drifted config surfaces:
pyproject 0.8.0 vs hermes_agent.yaml 0.1.0, a hand-maintained hermes_tools.json
whose schemas no longer matched the real function signatures, and a
default_sequence that still ran safety *after* clinical reasoning. These tests
make each drift a test failure instead of a review finding.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import yaml

from backend.provenance import APP_VERSION
from backend.tools import get_registry

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    match = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def _agent_manifest() -> dict:
    with open(ROOT / "config" / "hermes_agent.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_single_version_fact():
    manifest = _agent_manifest()
    assert _pyproject_version() == APP_VERSION == manifest["version"] == manifest["case_guide_agent"]["version"], (
        f"version drift: pyproject={_pyproject_version()} provenance={APP_VERSION} "
        f"manifest={manifest['version']} case_guide={manifest['case_guide_agent']['version']}"
    )


def test_hermes_tools_json_is_generated_from_registry():
    generated = get_registry().export_schemas()
    on_disk = json.loads((ROOT / "config" / "hermes_tools.json").read_text(encoding="utf-8"))
    assert on_disk == generated, (
        "config/hermes_tools.json is stale — regenerate it from the registry:\n"
        "python -c \"import json; from backend.tools import get_registry; "
        "open('config/hermes_tools.json','w').write(json.dumps(get_registry().export_schemas(), ensure_ascii=False, indent=2)+'\\n')\""
    )


def test_tool_schemas_match_real_signatures():
    # The anti-drift property itself: every schema property is a real parameter and
    # every required property is a real required parameter.
    for spec in get_registry().specs():
        if spec.handler is None:
            continue
        params = inspect.signature(spec.handler).parameters
        schema_props = set(spec.parameters.get("properties") or {})
        assert schema_props <= set(params), f"{spec.name}: schema has unknown params {schema_props - set(params)}"
        required = {name for name, p in params.items()
                    if p.default is p.empty and name not in ("dao_client",)
                    and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)}
        assert set(spec.parameters.get("required") or []) == required, (
            f"{spec.name}: required drift (schema {sorted(spec.parameters.get('required') or [])} vs real {sorted(required)})"
        )


def test_manifest_safety_gate_precedes_clinical_reasoning():
    sequence = _agent_manifest()["default_sequence"]
    assert sequence.index("safety_guard_skill") < sequence.index("syndrome_router_skill"), (
        "hermes_agent.yaml default_sequence must run the red-flag gate before clinical reasoning"
    )
    # Full-pool re-scan after herb modules must also be present.
    assert sequence.count("safety_guard_skill") >= 2


def test_manifest_sequence_tools_exist_in_registry():
    names = set(get_registry().names())
    for tool in _agent_manifest()["default_sequence"]:
        assert tool in names, f"default_sequence references unregistered tool {tool}"


def test_manifest_halt_categories_match_code():
    from backend.skills.safety_guard_skill import EMERGENCY_HALT_CATEGORIES

    manifest_categories = set(_agent_manifest()["dynamic_logic"]["red_flag_gate"]["halt_categories"])
    assert manifest_categories == set(EMERGENCY_HALT_CATEGORIES)
