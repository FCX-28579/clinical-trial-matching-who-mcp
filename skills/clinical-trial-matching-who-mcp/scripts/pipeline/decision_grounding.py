"""Reconcile decision paths with validated gater/deep fields."""
from __future__ import annotations

from typing import Any
import re


INACTIVE_STATUSES = {
    "COMPLETED", "TERMINATED", "WITHDRAWN", "SUSPENDED",
    "NOT_RECRUITING", "ACTIVE_NOT_RECRUITING",
}


def _normalized_terms(value: Any) -> set[str]:
    import re
    text = " ".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
    return {
        term.casefold() for term in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", text)
        if term.casefold() not in {
            "study", "trial", "therapy", "treatment", "phase", "agent",
            "drug", "biological", "combination", "product",
        }
    }


def _current_treatment_overlap(source: dict[str, Any], patient: dict[str, Any]) -> bool:
    if patient.get("current_therapy_ongoing") is not True:
        return False
    current = _normalized_terms(patient.get("current_therapy_status"))
    interventions = _normalized_terms(source.get("interventions") or [])
    return bool(current & interventions)


def _candidate_sort_key(source: dict[str, Any], patient: dict[str, Any]) -> tuple[Any, ...]:
    gating = source.get("gating") or {}
    efficacy = source.get("efficacy_summary") or {}
    evidence_source = efficacy.get("evidence_source") or {}
    status = str(source.get("overall_status") or "").upper().replace(" ", "_")
    evidence_weights = {
        "trial_specific": 4, "same_trial": 4, "mutation_class": 3,
        "drug_class": 2, "mechanism_background": 1, "no_data": 0,
    }
    evidence_tier = str(
        evidence_source.get("tier") if isinstance(evidence_source, dict) else ""
    ).casefold()
    feasibility = source.get("feasibility") or {}
    composite = feasibility.get("composite")
    try:
        feasibility_score = float(composite)
    except (TypeError, ValueError):
        feasibility_score = 0.0
    pending = len(gating.get("blockers_pending") or gating.get("pending") or [])
    satisfied = len(gating.get("blockers_satisfied") or gating.get("satisfied") or [])
    try:
        confidence = float(gating.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    verdict = str(gating.get("verdict") or "")
    return (
        0 if _current_treatment_overlap(source, patient) else 1,
        0 if status in INACTIVE_STATUSES else 1,
        1 if verdict == "match" else 0,
        confidence,
        satisfied,
        -pending,
        1 if int(source.get("patient_country_site_count") or 0) > 0 else 0,
        evidence_weights.get(evidence_tier, 0),
        feasibility_score,
        str(source.get("trial_id") or ""),
    )


def _rank_paths(
    model_paths: list[dict[str, Any]], sources: dict[str, dict[str, Any]],
    patient: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rank by patient fit; diversity never reserves a lower-quality slot."""
    supplied = {
        str(path.get("trial_id") or "").strip(): path
        for path in model_paths if isinstance(path, dict)
    }
    eligible = [
        source for source in sources.values()
        if (source.get("gating") or {}).get("verdict") in {"match", "conditional"}
        and not (source.get("gating") or {}).get("blockers_failed")
        and str(source.get("overall_status") or "").upper().replace(" ", "_")
        not in INACTIVE_STATUSES
        and not _current_treatment_overlap(source, patient)
    ]
    model_order = {
        trial_id: len(supplied) - index
        for index, trial_id in enumerate(supplied)
    }
    eligible.sort(
        key=lambda item: (
            _candidate_sort_key(item, patient)[:-1],
            model_order.get(str(item.get("trial_id") or "").strip(), 0),
            str(item.get("trial_id") or ""),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for source in eligible:
        if len(selected) >= 3:
            break
        if not any(_near_duplicate(source, chosen) for chosen in selected):
            selected.append(source)

    ranked = []
    for source in selected:
        trial_id = str(source.get("trial_id") or "").strip()
        ranked.append({
            **supplied.get(trial_id, {}),
            "trial_id": trial_id,
            "recommendation_bucket": "overall_best",
            "selection_basis": "global_patient_fit_with_near_duplicate_suppression",
        })
    return ranked


def _core_agent(source: dict[str, Any]) -> str:
    for raw in source.get("interventions") or []:
        value = str(raw.get("intervention_name_raw") if isinstance(raw, dict) else raw or "")
        value = re.sub(r"(?i)^(?:drug|biological|combination product)\s*:\s*", "", value)
        value = re.split(r"\s*(?:\+|\||,|\()", value, maxsplit=1)[0].strip().casefold()
        if value and value not in {"placebo", "standard of care", "chemotherapy"}:
            return value
    return ""


def _near_duplicate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_cluster = set(left.get("possible_duplicate_cluster_ids") or [])
    right_cluster = set(right.get("possible_duplicate_cluster_ids") or [])
    if left_cluster and right_cluster and left_cluster & right_cluster:
        return True
    left_agent, right_agent = _core_agent(left), _core_agent(right)
    if left_agent and left_agent == right_agent:
        return True
    left_terms = _normalized_terms(left.get("interventions") or [])
    right_terms = _normalized_terms(right.get("interventions") or [])
    union = left_terms | right_terms
    return bool(union) and len(left_terms & right_terms) / len(union) >= 0.75


def _meaningful_rationale(
    path: dict[str, Any], gating: dict[str, Any], efficacy: dict[str, Any]
) -> str:
    rationale = str(path.get("rationale") or "").strip()
    generic_markers = (
        "资格待核实路径",
        "不能视为治疗推荐",
        "eligibility-verification path",
        "not a treatment recommendation",
    )
    if not rationale or (
        len(rationale) < 120
        and any(marker.casefold() in rationale.casefold() for marker in generic_markers)
    ):
        rationale = str(
            gating.get("rationale") or efficacy.get("applies_because") or ""
        ).strip()
    return rationale


def ground_decision_path(
    path: dict[str, Any], source: dict[str, Any], *, language: str = "en"
) -> dict[str, Any]:
    gating = source.get("gating") or {}
    risk = source.get("risk_summary") or {}
    efficacy = source.get("efficacy_summary") or {}
    feasibility = source.get("feasibility") or {}
    verdict = str(gating.get("verdict") or "")
    grounded = {
        **path,
        "trial_title": source.get("title") or path.get("trial_title") or "",
        "sponsor": source.get("sponsor") or "",
        "phase": source.get("phase") or [],
        "feasibility_score": feasibility.get("composite"),
        "country_assessment": source.get("country_assessment") or {},
        "patient_country_site_count": source.get("patient_country_site_count") or 0,
        "eligibility_verdict": verdict,
        "requires_eligibility_confirmation": verdict == "conditional",
        "efficacy_snapshot": {
            key: efficacy.get(key)
            for key in ("match_type", "metrics", "evidence_source", "applies_because")
        },
        "vs_soc": efficacy.get("vs_soc") or {"available": False},
        "risks": risk.get("risks") or [],
        "blockers_satisfied": gating.get("blockers_satisfied") or [],
        "blockers_failed": gating.get("blockers_failed") or [],
        "blockers_pending": gating.get("blockers_pending") or [],
    }
    zh = language == "zh-CN"
    rationale = _meaningful_rationale(path, gating, efficacy)
    if verdict == "conditional":
        prefix = (
            "该试验仅为资格待核实路径，不能视为治疗推荐。"
            if zh
            else "This trial is an eligibility-verification path, not a treatment recommendation."
        )
        grounded["rationale"] = f"{prefix} {rationale}".strip()
    elif rationale:
        grounded["rationale"] = rationale
    grounded["consequences_of_skipping"] = (
        "不选择该试验并不等同于失去治疗机会；应与临床团队比较其他试验、标准治疗和支持治疗。"
        if zh
        else "Not pursuing this trial does not imply loss of treatment opportunity; compare other trials, standard care, and supportive care with the clinical team."
    )
    if isinstance(grounded.get("vs_soc"), dict):
        grounded["vs_soc"]["comparison_limitation"] = (
            "除非来源明确为头对头随机研究，否则仅可作为跨研究背景参考，不能据此声明优效。"
            if zh
            else "Unless the source is an explicit randomized head-to-head study, this is cross-study context and cannot establish superiority."
        )
    steps = (path.get("estimated_timeline") or {}).get("critical_path_steps") or []
    grounded["estimated_timeline"] = {
        "status": "site_confirmation_required",
        "screening_window": None,
        "earliest_first_dose": None,
        "critical_path_steps": steps,
        "limitation": (
            "在招募中心确认队列开放、筛选要求和排期之前，无法估算具体日期。"
            if zh
            else "Dates cannot be estimated until a recruiting site confirms cohort availability, screening requirements, and scheduling."
        ),
    }
    return grounded


def ground_decision_report(
    decision: dict[str, Any], analyzed_trials: list[dict[str, Any]], *,
    language: str = "en", patient: dict[str, Any] | None = None,
) -> dict[str, Any]:
    patient = patient or {}
    sources = {
        str(item.get("trial_id") or "").strip(): item
        for item in analyzed_trials
        if str(item.get("trial_id") or "").strip()
    }
    ranked_paths = _rank_paths(decision.get("decision_paths") or [], sources, patient)
    decision["decision_paths"] = [
        ground_decision_path(
            path, sources[str(path.get("trial_id") or "").strip()], language=language
        )
        for path in ranked_paths
        if str(path.get("trial_id") or "").strip() in sources
    ]
    for index, path in enumerate(decision["decision_paths"], 1):
        path["rank"] = index
    decision["clinical_field_policy"] = (
        "Eligibility, efficacy, risk, blockers, and timeline limitations are "
        "deterministically grounded to validated upstream outputs."
    )
    return decision
