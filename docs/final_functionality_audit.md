# Final Functionality Audit

## Verdict

The repository is **not a clinically perfect or production-complete CDSS**. It is a strong rule-first MVP that implements the core research, teaching, CaseGuide intake, clinician-facing CDSS draft, and physician sign-off boundaries. It should be described as **feature-complete for an MVP prototype**, not as a validated clinical product.

## Implemented MVP capabilities

| Capability | Status | Evidence |
|---|---|---|
| Rule-first pipeline | Implemented | Deterministic YAML rules are loaded and matched by `RuleEngine`, then orchestrated by `run_case_pipeline`. |
| Case extraction and tag normalization | Implemented with lightweight heuristics | Free text extraction and controlled tag normalization exist, but are not production-grade NLP. |
| Syndrome candidate scoring | Implemented | Syndrome rules score候选证型 and preserve evidence tags. |
| Formula-route and herb-module drafts | Implemented | Formula routes and herb modules are generated as non-final CDSS/research signals. |
| Red-flag screening | Implemented | CaseGuide starts with red flags and can stop into urgent referral mode. |
| Guided intake state machine | Implemented | `CaseGuideSession` stages consent, red flags, basic info, pain, neuro-ortho, TCM, Shen signals, comorbidities, repair, and reports. |
| Dynamic follow-up questions | Implemented | Adaptive planner prioritizes missing/high-value fields and patient burden. |
| Tao rule-constrained auto follow-up | Implemented (guarded, optional) | `tao_followup_probe_skill` lets Tao generate new clarifying probes within the current state theme; probes are advisory, capped, non-transitioning, and rejected on any diagnosis/prescription/dose leak. |
| Model+rule physician reasoning | Implemented (guarded, optional) | `physician_reasoning_skill` builds a deterministic syndrome→therapy→formula→safety chain and lets Tao articulate it; clinician-only, non-final. |
| Auto case-experience summary | Implemented (guarded, optional) | `case_experience_summary_skill` generates single-case 医案按语 and batch experience summaries from de-identified mined stats; research/teaching only. |
| xlsx de-identified rule mining | Implemented | `backend/mining/xlsx_case_miner.py` mines distributions, formula signatures, association rules (support/confidence/lift) and dose ranges into pending-review candidates. |
| Standard case and clinician handoff | Implemented | Case structuring and clinician handoff markdown outputs exist. |
| CDSS draft generation | Implemented | `cdss_recommendation_skill` returns clinician-facing candidate diagnoses and prescription strategy drafts. |
| Physician sign-off workflow | Implemented | `physician_review_skill` accepts physician-entered signed diagnosis/prescription records and rejects model-generated final orders. |
| Safety boundaries | Implemented in code and tests | Patient executable diagnosis/prescription/dose requests are blocked; CDSS drafts are not patient visible. |
| Dao1/Tao runtime integration | Implemented as optional guarded runtime | Supports disabled/mock/http/transformers backends, JSON repair, output guard and deterministic fallback; production still needs deployed model infrastructure and prompt regression. |
| Frontend UI | Implemented (zero-dependency static UI) | `frontend/` ships a same-origin static UI (chat, autonomous multi-step, interview, collaboration timeline, mining explorer) served by `backend/server.py`; it honestly labels LLM routing vs keyword fallback. |
| HTTP API service | Implemented as stdlib prototype | `backend/server.py` (stdlib `http.server`) serves `/api/chat`, `/api/autonomous`, `/api/followup_probe`, `/api/interview`, `/api/collaboration`, `/api/health` with bounded sessions and request-size limits; auth, persistence and audit-log storage are still missing for production. |
| Clinical validation | Not implemented | No expert-labeled dataset evaluation, prospective validation, or regulatory safety case is included. |

## Key safety conclusion

For a CDSS, the model may automatically generate **draft_for_clinician_review** diagnostic candidates and prescription strategy drafts. The model must not generate signed final diagnoses, final medication orders, administration instructions, or patient-executable doses. Final clinical content belongs in `physician_review_skill`, where it must be entered and signed by a licensed physician.

## Gaps to reach production readiness

1. **Validated data layer**: robust schemas, versioned case records, immutable audit logs, and reviewer identity management.
2. **API layer**: FastAPI routes, authentication/authorization, role-based patient vs clinician views, and persistence.
3. **Frontend**: staged intake UI, clinician review console, rule-evidence explorer, and signed-order release gate.
4. **LLM serving**: deploy Dao1/Tao via vLLM or Transformers with capacity planning, prompt regression tests, latency/timeout monitoring, and post-generation safety audits.
5. **Expert evaluation**: benchmark against curated沈钦荣腰痹 cases; measure top-k syndrome accuracy, route recall, safety recall, forbidden-output rate, and expert explanation scores.
6. **Safety engineering**: high-risk herb policy, interaction checks, contraindication checks, red-flag recall testing, and signed clinician-only release controls.
7. **Compliance review**: privacy, data retention, informed consent, deployment jurisdiction, CDSS regulatory classification, and non-commercial/non-medical model-license constraints.

## Bottom line

The project now implements the requested architecture and core workflows for a **research/CDSS MVP**. It is not “perfect” in the clinical-product sense and should not be represented as autonomous diagnosis or prescribing software. The correct product claim is:

> A rule-first Shen Qinrong腰痹 experience research and CDSS draft-generation prototype with guided case intake, evidence-traceable rule matching, clinician-facing draft recommendations, and a physician sign-off workflow.
