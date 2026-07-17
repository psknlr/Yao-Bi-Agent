from pathlib import Path

import yaml

from backend.engine.conflict_resolver import check_conflicts, check_interactions
from backend.skills.conflict_checker_skill import conflict_checker_skill

RULES_PATH = Path(__file__).resolve().parents[1] / "rules" / "06_conflict_rules.yaml"


def _items(*herbs: str) -> list[dict]:
    return [{"herbs": list(herbs)}]


def _by_id(alerts: list[dict], alert_id: str) -> dict:
    matches = [a for a in alerts if a["id"] == alert_id]
    assert matches, f"expected alert {alert_id}, got {[a['id'] for a in alerts]}"
    return matches[0]


def test_anticoagulant_x_huoxue_fires_interruptive():
    alerts = check_interactions(_items("丹参", "川芎"), medications=["华法林"])
    alert = _by_id(alerts, "anticoagulant_x_huoxue_herbs")
    assert alert["type"] == "herb_drug"
    assert alert["alert_level"] == "interruptive"
    assert set(alert["herbs_involved"]) == {"丹参", "川芎"}
    assert alert["matched_drugs"] == ["华法林"]
    assert "需医师审核" in alert["description"]


def test_anticoagulant_x_insect_herbs_fires_interruptive():
    alerts = check_interactions(_items("蜈蚣", "地龙"), medications=["氯吡格雷"])
    alert = _by_id(alerts, "anticoagulant_x_insect_herbs")
    assert alert["alert_level"] == "interruptive"
    assert set(alert["herbs_involved"]) == {"蜈蚣", "地龙"}


def test_fupian_x_banxia_shibafan_fires_interruptive():
    conflicts = check_conflicts(_items("附片", "姜半夏"))
    conflict = _by_id(conflicts, "shibafan_wutou_banxia")
    assert conflict["type"] == "route_conflict"
    assert conflict["alert_level"] == "interruptive"
    # Plain 半夏 must trip the same rule as its processed form.
    assert any(c["id"] == "shibafan_wutou_banxia" for c in check_conflicts(_items("附片", "半夏")))


def test_pregnancy_contraindication_fires_interruptive():
    alerts = check_interactions(_items("桃仁", "红花"), conditions=["妊娠"])
    alert = _by_id(alerts, "pregnancy_contraindicated_herbs")
    assert alert["type"] == "comorbidity_contraindication"
    assert alert["alert_level"] == "interruptive"
    assert set(alert["herbs_involved"]) == {"桃仁", "红花"}
    assert alert["matched_conditions"] == ["妊娠"]


def test_drug_matching_is_substring_tolerant():
    alerts = check_interactions(_items("红花"), medications=["阿司匹林肠溶片"])
    alert = _by_id(alerts, "anticoagulant_x_huoxue_herbs")
    assert alert["matched_drugs"] == ["阿司匹林肠溶片"]


def test_condition_matching_is_substring_tolerant_both_directions():
    # Reported term contains the rule term.
    alerts = check_interactions(_items("麻黄"), conditions=["高血压病"])
    alert = _by_id(alerts, "mahuang_x_hypertension_cardiac")
    assert alert["alert_level"] == "interruptive"
    assert alert["matched_conditions"] == ["高血压病"]
    # Rule term contains the reported term.
    alerts = check_interactions(_items("丹参"), conditions=["溃疡"])
    alert = _by_id(alerts, "huoxue_herbs_x_peptic_ulcer")
    assert alert["alert_level"] == "advisory"
    assert alert["matched_conditions"] == ["溃疡"]


def test_empty_strings_never_match():
    assert check_interactions(_items("丹参", "甘草"), medications=[""], conditions=["", "  "]) == []


def test_toxic_herbs_x_hepatorenal_and_cardiac():
    alerts = check_interactions(_items("附片", "细辛", "全蝎"), conditions=["肾功能不全", "心律失常"])
    assert _by_id(alerts, "toxic_herbs_x_hepatorenal_insufficiency")["alert_level"] == "interruptive"
    assert _by_id(alerts, "fupian_xixin_x_cardiac")["alert_level"] == "interruptive"


def test_skill_without_meds_or_conditions_keeps_legacy_shape():
    modules = _items("独活", "桑寄生")
    route = {"core_module": ["知母", "麻黄"]}
    result = conflict_checker_skill(modules, route)
    assert [c["id"] for c in result["conflicts"]] == ["duhuo_vs_guizhi_zhimu"]
    assert result["interaction_alerts"] == []
    assert result["alert_summary"] == {
        "interruptive": 0,
        "advisory": 1,
        "requires_dual_signoff": False,
    }
    # Single-argument legacy call shape still works too.
    assert conflict_checker_skill([])["conflicts"] == []


def test_skill_flattens_formula_route_into_interaction_pool():
    result = conflict_checker_skill(
        [],
        formula_route={"core_module": ["麻黄", "桂枝"]},
        conditions=["高血压"],
    )
    alert = _by_id(result["interaction_alerts"], "mahuang_x_hypertension_cardiac")
    assert alert["herbs_involved"] == ["麻黄"]
    assert result["alert_summary"]["requires_dual_signoff"] is True


def test_alert_summary_counts_across_tiers():
    result = conflict_checker_skill(
        _items("丹参", "甘草"),
        medications=["华法林", "布洛芬"],
    )
    ids = {a["id"] for a in result["interaction_alerts"]}
    assert ids == {"anticoagulant_x_huoxue_herbs", "nsaid_x_gancao", "nsaid_x_huoxue_herbs"}
    assert result["conflicts"] == []
    assert result["alert_summary"] == {
        "interruptive": 1,
        "advisory": 2,
        "requires_dual_signoff": True,
    }


def test_advisory_only_does_not_require_dual_signoff():
    result = conflict_checker_skill(_items("甘草"), medications=["布洛芬"])
    assert result["alert_summary"]["interruptive"] == 0
    assert result["alert_summary"]["advisory"] == 1
    assert result["alert_summary"]["requires_dual_signoff"] is False


def test_existing_three_conflicts_still_fire_with_alert_level():
    conflicts = check_conflicts(_items("独活", "麻黄", "桃仁", "白术", "麸炒白术"))
    ids = {c["id"] for c in conflicts}
    assert {"duhuo_vs_guizhi_zhimu", "heavy_stasis_vs_duhuo", "baizhu_vs_fried_baizhu"} <= ids
    for conflict in conflicts:
        if conflict["id"] != "shibafan_wutou_banxia":
            assert conflict["alert_level"] == "advisory"


def test_rule_file_schema_and_review_phrasing():
    config = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    for rule in config["herb_drug_interactions"]:
        assert rule["id"] and rule["herbs"] and rule["drugs"] and rule["action"]
        assert rule["alert_level"] in {"interruptive", "advisory"}
        assert "需医师审核" in rule["meaning"]
    for rule in config["comorbidity_contraindications"]:
        assert rule["id"] and rule["herbs"] and rule["conditions"] and rule["action"]
        assert rule["alert_level"] in {"interruptive", "advisory"}
        assert "需医师审核" in rule["meaning"]
    for rule in config["conflicts"]:
        assert rule["alert_level"] in {"interruptive", "advisory"}
