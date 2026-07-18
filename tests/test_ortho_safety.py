"""Orthopedic safety ontology tests (v0.10) — the review's P0 acceptance criteria.

1. High-energy trauma / open fracture / neurovascular deficit / compartment syndrome /
   PE / aortic emergency / cervical myelopathy: zero misses, all hard-halt (A0);
2. an aortic-emergency presentation produces NO TCM formula route and NO model call;
3. resolved historical fever does not trigger a current infection hard stop;
4. no syndrome candidate ⇒ primary_route is None (route gate);
5. after the emergency gate the LLM call count is exactly 0;
6. the scope router keeps non-lumbar complaints out of the lumbar-Bi chain;
7. action levels A0–A3 separate clinical urgency from medication review.
"""

from __future__ import annotations

import pytest

from backend.llm.dao_client import DaoClient, DaoGenerationConfig
from backend.skills.clinical_entity_skill import scan_term
from backend.skills.clinical_scope_router_skill import clinical_scope_router_skill
from backend.skills.formula_base_selector_skill import formula_base_selector_skill
from backend.skills.pipeline import run_case_pipeline

_EMERGENCY_CASES = [
    ("high_energy_fall", "患者男，38岁，从约三米高处坠落后腰痛，不能站立。", "major_trauma"),
    ("car_crash", "患者女，45岁，昨日车祸后腰背痛，活动受限。", "major_trauma"),
    ("open_fracture_pulseless", "患者男，30岁，摔伤后小腿明显畸形，伤口见骨，足背动脉摸不到。", "open_fracture_dislocation"),
    ("compartment_syndrome", "患者男，25岁，胫骨骨折固定后小腿进行性剧痛，被动牵伸痛明显，足趾麻木。", "compartment_syndrome"),
    ("suspected_pe", "患者女，60岁，骨折制动一周，今晨突发胸痛、气短、心慌，伴小腿肿痛。", "cardiopulmonary_emergency"),
    ("aortic_emergency", "患者女，72岁，突发腰背部撕裂样疼痛，伴腹部搏动感，出冷汗。", "vascular_emergency"),
    ("cervical_myelopathy", "患者男，55岁，近两月双手笨拙，走路有踩棉花感，逐渐加重。", "cervical_myelopathy"),
]


@pytest.mark.parametrize("name,text,category", _EMERGENCY_CASES, ids=[c[0] for c in _EMERGENCY_CASES])
def test_orthopedic_emergencies_hard_halt_with_zero_formula_output(name, text, category):
    result = run_case_pipeline(text)
    assert result["red_flag_gate"]["halted"] is True, f"{name}: emergency must hard-halt"
    assert result["safety"]["safety_status"] == "urgent"
    # Cervical myelopathy halts TCM reasoning but its action is same-day urgent
    # specialist referral (A1-with-halt), not resuscitation (P1-2 stratification).
    expected_level = "A1" if category == "cervical_myelopathy" else "A0"
    assert result["safety"]["action_level"] == expected_level
    assert result["syndrome_candidates"] == []
    assert result["formula_routes"] == [] and result["primary_route"] is None
    assert result["matched_modules"] == []
    categories = {f.get("category") for f in result["safety"]["confirmed_red_flags"]}
    assert category in categories, f"{name}: expected category {category}, got {categories}"


class _CountingDao(DaoClient):
    """Counts every generation entry point — the emergency invariant is count == 0."""

    def __init__(self):
        super().__init__(DaoGenerationConfig(backend="mock"))
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        return super().chat(*a, **k)

    def generate_report(self, *a, **k):
        self.calls += 1
        return super().generate_report(*a, **k)

    def generate_consultation(self, *a, **k):
        self.calls += 1
        return super().generate_consultation(*a, **k)

    def generate_reasoning(self, *a, **k):
        self.calls += 1
        return super().generate_reasoning(*a, **k)


def test_no_llm_calls_after_emergency_gate():
    client = _CountingDao()
    result = run_case_pipeline(
        "患者女，72岁，突发腰背部撕裂样疼痛，伴腹部搏动感，出冷汗。",
        use_llm=True, dao_client=client,
    )
    assert result["red_flag_gate"]["halted"] is True
    assert client.calls == 0, "the emergency gate must prevent every model call"


def test_resolved_historical_fever_does_not_hard_stop():
    result = run_case_pipeline("患者男，40岁，一周前感冒发热，现已痊愈，今天搬重物后腰痛。")
    assert result["red_flag_gate"]["halted"] is False
    assert result["safety"]["safety_status"] == "safe"
    historical = [f["term"] for f in result["safety"]["historical_red_flags"]]
    assert "发热" in historical  # recorded for the physician, not alarmed
    # An *unresolved* recent fever must still hard-stop (no over-downgrade).
    active = run_case_pipeline("患者男，52岁，腰痛2周，夜间痛明显，伴发热寒战，体温38.5度。")
    assert active["red_flag_gate"]["halted"] is True


def test_entity_temporality_resolved_marker():
    entity = scan_term("一周前感冒发热，现已痊愈。", "发热")
    assert entity["polarity"] == "affirmed"
    assert entity["temporality"] == "resolved"
    assert scan_term("今晨发热38.5度。", "发热")["temporality"] == "current"
    assert scan_term("既往有发热病史。", "发热")["temporality"] == "historical"


def test_low_energy_fragility_fall_keeps_clinician_review_analysis():
    # GC012 semantics: a simple fall on an osteoporotic background is urgent (A1
    # referral) but not an always-emergency category — retrospective analysis stays.
    result = run_case_pipeline(
        "患者女，71岁，三天前跌倒后腰痛加重，既往骨质疏松，平素腰膝酸软，舌淡，脉细。"
    )
    assert result["safety"]["safety_status"] == "urgent"
    assert result["safety"]["action_level"] == "A1"
    assert result["red_flag_gate"]["halted"] is False
    assert result["syndrome_candidates"]


def test_immunosuppressed_night_pain_escalates_without_route():
    result = run_case_pipeline("患者女，58岁，长期使用生物制剂和激素，腰痛3月，夜间痛明显。")
    assert result["safety"]["safety_status"] == "urgent"
    categories = {f.get("category") for f in result["safety"]["confirmed_red_flags"]}
    assert "immunosuppressed_risk" in categories
    assert result["primary_route"] is None


# -- route gate ---------------------------------------------------------------------------

def test_formula_route_requires_syndrome_candidates():
    out = formula_base_selector_skill(["elderly"], [])
    assert out["primary_route"] is None and out["formula_routes"] == []
    assert out["route_gate"] == {"allowed": False, "reason": "no_syndrome_candidate",
                                 "note": out["route_gate"]["note"]}


def test_formula_route_blocked_on_low_confidence_top_candidate():
    out = formula_base_selector_skill(
        ["elderly", "chronic_yabi"],
        [{"name": "气血痹阻证", "score": 3, "confidence": "low", "evidence_tags": ["chronic_yabi"]}],
    )
    assert out["primary_route"] is None
    assert out["route_gate"]["reason"] == "low_confidence_syndrome"


def test_formula_route_allowed_with_solid_candidate():
    out = formula_base_selector_skill(
        ["elderly", "chronic_yabi", "osteoporosis", "lumbar_knee_soreness"],
        [{"name": "肝肾不足证", "score": 6, "confidence": "medium", "evidence_tags": ["elderly", "chronic_yabi"]}],
    )
    assert out["route_gate"]["allowed"] is True
    assert out["primary_route"] is not None


# -- scope router --------------------------------------------------------------------------

def test_scope_router_keeps_non_lumbar_complaints_out():
    result = run_case_pipeline("患者男，50岁，右膝关节肿痛三天，上下楼加重。")
    assert result["scope"]["in_scope"] is False
    assert result["scope"]["domain"] == "joint"
    assert result["syndrome_candidates"] == [] and result["primary_route"] is None
    assert "lumbar_bi_syndrome_support" not in result["scope"]["allowed_capabilities"]


def test_scope_router_unit_domains():
    assert clinical_scope_router_skill("腰痛三年，遇冷加重。")["in_scope"] is True
    assert clinical_scope_router_skill("肩关节疼痛半年。")["domain"] == "joint"
    assert clinical_scope_router_skill("右腕骨折术后复查。")["domain"] == "fracture_followup"
    assert clinical_scope_router_skill("最近心情不好。")["domain"] == "unknown"
    emergency = clinical_scope_router_skill("腰痛。", red_flag_categories=["vascular_emergency"])
    assert emergency["domain"] == "emergency" and emergency["in_scope"] is False


def test_scope_router_fracture_postop_outranks_lumbar_anchor():
    # "腰椎压缩性骨折术后复查" is a fracture-follow-up task, not lumbar-Bi formula work.
    result = clinical_scope_router_skill("患者68岁，腰椎压缩性骨折术后复查，腰膝酸软，腰痛反复。")
    assert result["in_scope"] is False
    assert result["domain"] == "spine_fracture_followup"
    assert "FRACTURE_POSTOPERATIVE_PRIORITY" in result["reason_codes"]
    assert "lumbar_bi_formula_route" in result["blocked_capabilities"]
    # A resolved dislocation ("已复位") no longer steers the domain nor halts.
    resolved = clinical_scope_router_skill("昨日肩关节脱位已复位，目前只有轻微疼痛。")
    assert resolved["domain"] in {"joint", "unknown"}


# -- action stratification -------------------------------------------------------------------

def test_action_levels_separate_urgency_from_medication_review():
    a0 = run_case_pipeline("患者男，45岁，腰痛伴会阴麻木，尿不出来。")["safety"]
    assert a0["action_level"] == "A0"
    a2 = run_case_pipeline("患者男，48岁，腰痛3月，阴雨天加重，热敷缓解，双腿沉重，苔白腻，脉缓，目前服用华法林。")["safety"]
    assert a2["action_level"] == "A2"
    assert a2["drivers"]["medication_review_required"] is True
    a3 = run_case_pipeline("患者男，48岁，腰痛3月，阴雨天加重，热敷缓解，双腿沉重，肢体困重，苔白腻，脉缓，胃纳可，二便调，夜寐可。")["safety"]
    assert a3["action_level"] in {"A2", "A3"}


def test_action_card_leads_with_level_and_blocked_items():
    halted = run_case_pipeline("患者男，38岁，从约三米高处坠落后腰痛，不能站立。")
    card = halted["action_card"]
    assert card["action_level"] == "A0"
    assert "方药路线" in card["blocked"]
    assert card["why"]
    normal = run_case_pipeline("患者男，48岁，腰痛3月，阴雨天加重，热敷缓解，双腿沉重，苔白腻，脉缓。")
    assert normal["action_card"]["action_level"] in {"A2", "A3"}


def test_all_golden_cases_pass_end_to_end():
    # Locks the full golden set (incl. the 9 orthopedic adversarial cases) as a CI gate.
    from backend.evaluation.benchmark import evaluate_case, load_golden_cases

    failures = [
        (case["id"], record["checks"])
        for case in load_golden_cases()
        for record in [evaluate_case(case)]
        if not record["passed"] and not case.get("known_gap")
    ]
    assert not failures, f"golden case regressions: {failures}"
