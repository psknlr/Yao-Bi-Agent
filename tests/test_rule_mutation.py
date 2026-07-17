"""Rule-mutation tests (evaluation level L2).

A green golden-case suite only proves the *current* rules behave; it says nothing about
whether the suite would notice a harmful rule edit. These tests inject harmful mutations
into a copy of the rule base and assert the golden cases CATCH them (at least one case
fails). If a mutation survives with the whole suite green, the evaluation set has a
blind spot and this test fails.

Mutations covered:
* stripping every `contra` list (counter-evidence ignored → cold/heat confusion);
* stripping every `at_least` threshold (single weak tag over-triggers syndromes).
"""

from __future__ import annotations

import shutil

import yaml

import backend.engine.conformal as conformal
import backend.engine.rule_engine as rule_engine
from backend.evaluation.benchmark import evaluate_case, load_golden_cases

# Discriminative golden cases: GC031 is the purpose-built counter-evidence sentinel
# (mixed cold/heat where the contra penalty decides top-1); GC009/GC010/GC019/GC029
# are the single-tag over-trigger sentinels for threshold stripping.
_SENTINEL_CASES = ("GC009", "GC010", "GC015", "GC016", "GC019", "GC029", "GC031")


def _mutated_rules(tmp_path, mutate):
    """Copy rules/ to tmp, apply `mutate` to the 02 syndrome rule list, return the dir."""

    target = tmp_path / "rules"
    shutil.copytree(rule_engine.RULES_DIR, target)
    path = target / "02_syndrome_rules.yaml"
    rules = yaml.safe_load(path.read_text(encoding="utf-8"))
    for rule in rules:
        mutate(rule)
    path.write_text(yaml.safe_dump(rules, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target


def _run_sentinels(monkeypatch, rules_dir) -> list[str]:
    monkeypatch.setattr(rule_engine, "RULES_DIR", rules_dir)
    # The conformal calibration cache must not be computed under (or poisoned by) a
    # mutated rule base — clear before and after.
    monkeypatch.setattr(conformal, "_CALIBRATION_CACHE", None)
    cases = {c["id"]: c for c in load_golden_cases()}
    failed = [cid for cid in _SENTINEL_CASES if not evaluate_case(cases[cid])["passed"]]
    monkeypatch.setattr(conformal, "_CALIBRATION_CACHE", None)
    return failed


def test_baseline_sentinels_pass(monkeypatch):
    failed = _run_sentinels(monkeypatch, rule_engine.RULES_DIR)
    assert not failed, f"sentinel cases must pass on the unmutated rule base: {failed}"


def test_removing_counter_evidence_is_caught(tmp_path, monkeypatch):
    mutated = _mutated_rules(tmp_path, lambda rule: rule.pop("contra", None))
    failed = _run_sentinels(monkeypatch, mutated)
    assert failed, "stripping every contra list survived the golden set — evaluation blind spot"


def test_removing_at_least_thresholds_is_caught(tmp_path, monkeypatch):
    def drop_threshold(rule):
        (rule.get("trigger") or {}).pop("at_least", None)

    mutated = _mutated_rules(tmp_path, drop_threshold)
    failed = _run_sentinels(monkeypatch, mutated)
    assert failed, "stripping every at_least threshold survived the golden set — evaluation blind spot"
