"""Case-cohort evidence retrieval + engine↔cohort route concordance (G1, v0.15)."""

from __future__ import annotations

from backend.llm.output_guard import filter_patient_payload
from backend.skills.case_extract_skill import case_extract_skill
from backend.skills.case_normalize_skill import case_normalize_skill
from backend.skills.case_retrieval_skill import _route_core, _routes_match, case_retrieval_skill
from backend.skills.formula_base_selector_skill import formula_base_selector_skill
from backend.skills.pipeline import run_case_pipeline
from backend.skills.syndrome_router_skill import syndrome_router_skill


def _rank(text: str):
    normalized = case_normalize_skill(case_extract_skill(text))
    routed = syndrome_router_skill(normalized["normalized_tags"])
    formula = formula_base_selector_skill(normalized["normalized_tags"], routed["syndrome_candidates"])
    return normalized["normalized_tags"], routed["syndrome_candidates"], formula.get("primary_route")


def test_route_core_strips_cosmetic_suffixes():
    assert _route_core("独活寄生汤加减") == "独活寄生"
    assert _route_core("小柴胡汤类(和解少阳)") == "小柴胡"
    assert _routes_match("独活寄生汤加减", "独活寄生汤")
    assert not _routes_match("独活寄生汤", "当归四逆汤")


def test_retrieval_ranks_by_case_relevance_and_cites_provenance():
    tags, candidates, route = _rank("患者女，68岁，腰痛反复5年，畏寒，下肢麻木，舌暗苔白腻，既往骨质疏松。")
    result = case_retrieval_skill(tags, candidates, route)
    assert result["cohort_available"] is True
    hits = result["retrieved_cohort_evidence"]
    assert hits, "should retrieve at least one cohort route signal"
    # Ranked descending by relevance, and each hit carries de-identified provenance.
    relevances = [h["relevance"] for h in hits]
    assert relevances == sorted(relevances, reverse=True)
    assert all(h.get("evidence") for h in hits)
    assert result["clinician_only"] is True


def test_route_concordance_corroborates_matching_engine_route():
    tags, candidates, route = _rank("患者女，68岁，腰痛反复5年，畏寒，下肢麻木，舌暗苔白腻，既往骨质疏松。")
    result = case_retrieval_skill(tags, candidates, route)
    conc = result["route_concordance"]
    assert conc is not None
    # Engine picks 独活寄生汤加减; the cohort contains 独活寄生汤 for this syndrome.
    assert conc["verdict"] == "corroborated"
    assert "独活寄生" in _route_core(conc["engine_route"])


def test_concordance_is_none_without_an_engine_route():
    result = case_retrieval_skill(["stabbing_pain"], [{"name": "气滞血瘀证", "score": 4}], primary_route=None)
    assert result["route_concordance"] is None


def test_pipeline_attaches_cohort_evidence():
    result = run_case_pipeline("患者女，68岁，腰痛反复5年，畏寒，下肢麻木，舌暗苔白腻，既往骨质疏松。")
    assert "case_cohort_evidence" in result
    assert result["case_cohort_evidence"]["cohort_available"] is True


def test_cohort_evidence_never_reaches_patient_payload():
    """Clinician-only evidence must be dropped by the patient allowlist filter."""

    turn = {
        "answer": "这是安全科普内容。",
        "intent": "safety_inquiry",
        "case_cohort_evidence": {"route_concordance": {"verdict": "corroborated"}},
    }
    filtered = filter_patient_payload(turn)
    assert "case_cohort_evidence" not in filtered
