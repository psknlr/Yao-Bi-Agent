import pytest

openpyxl = pytest.importorskip("openpyxl")

from backend.mining.xlsx_case_miner import (
    build_rule_candidates,
    load_cases_from_xlsx,
    mine_cases,
    parse_herbs,
    parse_zheng,
    write_outputs,
)
from backend.skills.mined_evidence_skill import mined_evidence_skill

HEADER = [
    "就诊序号", "医师工号", "医师姓名", "科室代码", "科室名称", "姓名", "性别", "年龄", "病案号",
    "就诊日期", "地址", "主诉", "现病史", "既往史", "过敏史", "手术史", "中医四诊", "辅助检查",
    "中医诊断", "西医诊断", "治疗方法", "西药", "中药", "治疗",
]

HERB_BLOCK = (
    "1/独活*1克/10克/用法：无/贴数:7\r\n,2/*当归*1克/10克/用法：无/贴数:7\r\n,"
    "3/桂枝*1克/15克/用法：无/贴数:7\r\n,4/麸白芍*1克/15克/用法：无/贴数:7\r\n,"
    "5/细辛*1克/3克/用法：无/贴数:7\r\n,6/通草*1克/6克/用法：无/贴数:7\r\n,"
    "7/大枣*1克/30克/用法：无/贴数:7\r\n,8/炙甘草*1克/6克/用法：无/贴数:7\r\n,"
    "9/全蝎*1克/3克/用法：无/贴数:7\r\n,10/盐补骨脂 *1克/15克/用法：无/贴数:7\r\n"
)


PLAIN_HERB_BLOCK = (
    "1/白术*1克/30克/用法：无/贴数:7\r\n,2/茯苓*1克/15克/用法：无/贴数:7\r\n,"
    "3/陈皮*1克/6克/用法：无/贴数:7\r\n,4/党参*1克/15克/用法：无/贴数:7\r\n"
)


def make_xlsx(tmp_path, n_rows=12):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADER)
    for i in range(n_rows):
        numb = i % 2 == 0  # 偶数行：麻木 + 当归四逆汤；奇数行：脾虚 + 健脾方
        ws.append([
            f"1000{i}", "007", "某医师", "1166", "脊柱关节病门诊", f"患者{i}", "女" if i % 2 else "男",
            f"{60 + i}岁", f"5051{i:04d}", "2026-04-22", "某市某区某街道",
            "腰痛伴双下肢麻木1月" if numb else "腰痛1月",
            "腰痛反复，双下肢麻木，遇冷加重，夜寐差，口苦" if numb else "腰痛反复，胃口差，乏力",
            "[骨质疏松史]", "[否认药物过敏史]", "否认手术史", "望诊：模板", "检查：无",
            "中医诊断:腰痹/证型：气血痹阻证" if numb else "中医诊断:腰痹/证型：脾虚不运证",
            "西医诊断:腰椎间盘突出,骨质疏松",
            "注意休息", None, HERB_BLOCK if numb else PLAIN_HERB_BLOCK, None,
        ])
    path = tmp_path / "cases.xlsx"
    wb.save(path)
    return path


def test_parse_herbs_handles_star_prefix_and_spaces():
    herbs = dict(parse_herbs(HERB_BLOCK))
    assert herbs["当归"] == 10.0
    assert herbs["细辛"] == 3.0
    assert herbs["盐补骨脂"] == 15.0
    assert parse_zheng("中医诊断:腰痹/证型：气血痹阻证") == "气血痹阻证"


def test_loader_deidentifies_rows(tmp_path):
    cases = load_cases_from_xlsx(make_xlsx(tmp_path))
    assert len(cases) == 12
    dumped = str(cases)
    assert "患者0" not in dumped  # 姓名
    assert "50510000" not in dumped  # 病案号
    assert "某街道" not in dumped  # 地址
    case = cases[0]
    assert case["sex"] == "男"
    assert case["age_band"] == "60s"
    assert "lower_limb_numbness" in case["symptom_tags"]
    assert "elderly" in case["symptom_tags"]
    assert case["zheng"] == "气血痹阻证"
    assert case["osteoporosis_history"] is True
    assert case["has_prescription"] is True


def test_miner_finds_formula_signatures_and_associations(tmp_path):
    cases = load_cases_from_xlsx(make_xlsx(tmp_path))
    mined = mine_cases(cases)
    formulas = {hit["formula"] for hit in mined["formula_signature_hits"]}
    assert "当归四逆汤" in formulas  # 当归桂枝芍药细辛通草大枣炙甘草 7/7 签名命中
    assert mined["dose_table"]["细辛"]["mode_g"] == 3.0
    assert mined["dose_table"]["细辛"]["clinician_review_required"] is True
    assert mined["data_quality"]["tongue_pulse_usable"] is False
    assocs = {(a["antecedent"], a["consequent"]) for a in mined["associations"]}
    assert any(c == "formula::当归四逆汤" for _, c in assocs)
    for a in mined["associations"]:
        assert a["support"] > 0 and a["confidence"] > 0 and a["lift"] > 0


def test_rule_candidates_are_pending_review_and_clinician_only(tmp_path):
    cases = load_cases_from_xlsx(make_xlsx(tmp_path))
    mined = mine_cases(cases)
    candidates = build_rule_candidates(mined)
    assert candidates
    for rule in candidates:
        assert rule["status"] == "pending_expert_review"
        assert rule["clinician_only"] is True
        assert rule["rule_id"].startswith("MINED-")
    dose_rules = [r for r in candidates if r["rule_type"] == "dose_convention"]
    assert dose_rules
    assert "不向患者输出" in dose_rules[0]["evidence"]


def test_write_outputs_contain_no_pii_and_load_back(tmp_path):
    cases = load_cases_from_xlsx(make_xlsx(tmp_path))
    mined = mine_cases(cases)
    candidates = build_rule_candidates(mined)
    yaml_path = tmp_path / "candidates.yaml"
    js_path = tmp_path / "mined_rules.js"
    write_outputs(mined, candidates, yaml_path=yaml_path, frontend_path=js_path)
    for path in (yaml_path, js_path):
        text = path.read_text(encoding="utf-8")
        assert "患者0" not in text and "某街道" not in text and "50510000" not in text
    assert js_path.read_text(encoding="utf-8").startswith("// Auto-generated")
    import yaml as yaml_lib

    payload = yaml_lib.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert payload["meta"]["review_policy"].startswith("all candidates pending_expert_review")
    assert payload["rule_candidates"]


def test_mined_evidence_skill_matches_repo_rules_by_tag():
    result = mined_evidence_skill(["lower_limb_numbness", "elderly"], [{"name": "气血痹阻证"}])
    assert result["mined_rules_available"] is True
    assert result["mined_evidence"], "repo 内置挖掘规则应能被下肢麻木/高龄标签命中"
    for rule in result["mined_evidence"]:
        assert rule["status"] == "pending_expert_review"
        assert rule["clinician_only"] is True
    assert "不构成诊断或处方依据" in result["disclaimer"]


def test_final_report_includes_mined_evidence():
    from tests.test_caseguide import build_complete_session

    final = build_complete_session().final_report()
    assert "mined_evidence" in final
    assert "不构成诊断或处方依据" in final["mined_evidence_disclaimer"]
