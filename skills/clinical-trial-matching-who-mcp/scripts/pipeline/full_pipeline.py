"""Generic WHO-MCP clinical-trial workflow with model-subskill analysis contracts.

`prepare` performs only deterministic retrieval, verification, feasibility and job
construction. `finalize` refuses to render unless all canonical LLM subskill
outputs pass the analysis contract. No cancer- or biomarker-specific gating lives
in this module.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
SKILL = HERE.parents[2]
SKILLS_ROOT = SKILL.parent
sys.path[:0] = [
    str(SKILL / "scripts" / "retrieval"),
    str(SKILL / "scripts" / "verification"),
    str(SKILL / "scripts" / "scoring"),
    str(SKILL / "scripts" / "classification"),
    str(SKILL / "scripts" / "presentation"),
    str(SKILL / "scripts" / "render"),
]

from analysis_contract import (
    build_analysis_jobs, load_json, normalized_report_analysis, report_language,
    validate_analysis_bundle,
)
from io_utils import write_json_atomic
from patient_input import load_patient_input
from generic_hard_rules import apply_generic_hard_rules
from feasibility import WEIGHTS, compute_feasibility
from html_renderer import render_html, render_validation_html
from report_translation import maybe_translate_report
from mcp_http_client import run_remote_who_workflow
from mcp_stdio_client import run_who_workflow
from mechanism_categories import CATEGORY_ORDER, classify_mechanism
from registry_presentation import assess_country_evidence, patient_facing_title, resolved_trial_url
from search_plan import (
    build_baseline_search_plan, validate_search_plan,
    validate_search_plan_for_patient,
)
from who_mcp_adapter import merge_sources
from who_portal_delta import build_delta
from direct_registry_verifier import verify_and_partition
from who_mcp_verifier import verify_batch


def _language(patient: dict[str, Any]) -> str:
    return report_language(patient)


def _portal_max_age_hours() -> float:
    raw = os.environ.get("WHO_PORTAL_DELTA_MAX_AGE_HOURS", "24")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("WHO_PORTAL_DELTA_MAX_AGE_HOURS must be numeric") from exc
    if value <= 0:
        raise ValueError("WHO_PORTAL_DELTA_MAX_AGE_HOURS must be greater than zero")
    return value


def _portal_clock_skew_minutes() -> float:
    raw = os.environ.get("WHO_PORTAL_CLOCK_SKEW_MINUTES", "5")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("WHO_PORTAL_CLOCK_SKEW_MINUTES must be numeric") from exc
    if value < 0 or value > 60:
        raise ValueError("WHO_PORTAL_CLOCK_SKEW_MINUTES must be between 0 and 60")
    return value


def _load_portal_delta(
    path: Path | None, database_as_of: str, expected_plan_sha256: str = ""
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load an externally captured WHO portal delta with an auditable boundary."""
    if path is None:
        return [], {
            "status": "not_executed",
            "boundary_type": "registration_date_proxy",
            "database_as_of": database_as_of,
            "limitation": "WHO portal registration date is not a reliable record-last-update filter.",
        }
    payload = load_json(path)
    if payload.get("schema_version") != "who-portal-delta-v1":
        raise ValueError("Portal delta artifact has an unsupported schema_version")
    if payload.get("generator") != "who_portal_delta.py":
        raise ValueError("Portal delta artifact has unrecognized generator provenance")
    plan_sha256 = str(payload.get("search_plan_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", plan_sha256):
        raise ValueError("Portal delta artifact must include search_plan_sha256")
    if expected_plan_sha256 and plan_sha256 != expected_plan_sha256:
        raise ValueError("Portal delta search plan does not match the formal run")
    if payload.get("status") != "executed":
        raise ValueError("Portal delta artifact must have status='executed'")
    if payload.get("database_as_of") != database_as_of:
        raise ValueError("Portal delta database_as_of does not match the MCP database watermark")
    if not payload.get("executed_at"):
        raise ValueError("Portal delta artifact must include executed_at")
    try:
        executed_at = dt.datetime.fromisoformat(str(payload["executed_at"]).replace("Z", "+00:00"))
        watermark = dt.datetime.fromisoformat(database_as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Portal delta timestamps must be ISO-8601") from exc
    now = dt.datetime.now().astimezone()
    if executed_at.tzinfo is None or watermark.tzinfo is None:
        raise ValueError("Portal delta timestamps must include timezone offsets")
    if executed_at < watermark:
        raise ValueError("Portal delta execution predates the MCP database watermark")
    age_hours = (now - executed_at.astimezone(now.tzinfo)).total_seconds() / 3600
    max_age_hours = _portal_max_age_hours()
    clock_skew_minutes = _portal_clock_skew_minutes()
    if age_hours < -(clock_skew_minutes / 60) or age_hours > max_age_hours:
        raise ValueError(f"Portal delta artifact is not current; age_hours={age_hours:.1f}")
    trials = payload.get("trials")
    if not isinstance(trials, list):
        raise ValueError("Portal delta artifact trials must be a list")
    query_audit = payload.get("query_audit")
    control_query = payload.get("control_query")
    if not isinstance(query_audit, list) or not query_audit:
        raise ValueError("Portal delta artifact must include a non-empty query_audit")
    if any(
        not isinstance(item, dict)
        or item.get("complete") is not True
        or int(item.get("returned") or 0) != int(item.get("portal_total") or 0)
        for item in query_audit
    ):
        raise ValueError("Portal delta query audit is incomplete or truncated")
    if not isinstance(control_query, dict) or control_query.get("complete") is not True:
        raise ValueError("Portal delta artifact must include a successful control_query")
    if int(control_query.get("query_variants") or 0) != len(query_audit):
        raise ValueError("Portal delta control_query does not match query_audit")
    audit = {
        "status": "executed",
        "boundary_type": "registration_date_proxy",
        "database_as_of": database_as_of,
        "executed_at": payload["executed_at"],
        "returned": len(trials),
        "source": payload.get("source") or "WHO ICTRP portal",
        "generator": payload["generator"],
        "search_plan_sha256": plan_sha256,
        "date_start": payload.get("date_start"),
        "date_end": payload.get("date_end"),
        "query_audit": query_audit,
        "control_query": control_query,
        "limitation": payload.get("limitation") or "Registration-date filtering may miss modified older records.",
        "freshness_validated_at_prepare": True,
        "max_age_hours": max_age_hours,
        "clock_skew_tolerance_minutes": clock_skew_minutes,
    }
    return trials, audit

def _candidate_rank(trial: dict[str, Any]) -> tuple[Any, ...]:
    return (
        int(trial.get("search_rank") or 10**9),
        -len(trial.get("matched_by") or []),
        -float((trial.get("feasibility") or {}).get("composite") or 0),
        str(trial.get("id") or ""),
    )


def _portal_audit_is_current(portal: dict[str, Any]) -> bool:
    if portal.get("status") != "executed" or not portal.get("executed_at"):
        return False
    query_audit = portal.get("query_audit")
    control_query = portal.get("control_query")
    if (
        not isinstance(query_audit, list)
        or not query_audit
        or any(
            not isinstance(item, dict)
            or item.get("complete") is not True
            or int(item.get("returned") or 0) != int(item.get("portal_total") or 0)
            for item in query_audit
        )
        or not isinstance(control_query, dict)
        or control_query.get("complete") is not True
        or int(control_query.get("query_variants") or 0) != len(query_audit)
    ):
        return False
    try:
        executed_at = dt.datetime.fromisoformat(str(portal["executed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if executed_at.tzinfo is None:
        return False
    now = dt.datetime.now().astimezone()
    age_hours = (now - executed_at.astimezone(now.tzinfo)).total_seconds() / 3600
    skew_minutes = float(
        portal.get("clock_skew_tolerance_minutes")
        if portal.get("clock_skew_tolerance_minutes") is not None
        else _portal_clock_skew_minutes()
    )
    return -(skew_minutes / 60) <= age_hours <= float(
        portal.get("max_age_hours") or _portal_max_age_hours()
    )


def _database_snapshot_is_current(prepared: dict[str, Any]) -> bool:
    raw = str(prepared.get("database_as_of") or "")
    if not raw:
        return False
    try:
        watermark = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        max_age = float(os.environ.get("WHO_MCP_DATABASE_MAX_AGE_HOURS", "168"))
    except ValueError:
        return False
    if watermark.tzinfo is None or max_age <= 0:
        return False
    age = (dt.datetime.now().astimezone() - watermark.astimezone()).total_seconds() / 3600
    live = prepared.get("live_registry_audit") or {}
    expected = len(prepared.get("all_verified_trials") or [])
    attempted = int(live.get("attempted") or 0)
    errors = int(live.get("errors") or 0)
    unknown = int(live.get("unknown") or 0)
    try:
        max_error_rate = float(os.environ.get("LIVE_REGISTRY_MAX_ERROR_RATE", "0.25"))
    except ValueError:
        return False
    if not 0 <= max_error_rate <= 1:
        return False
    error_rate = (errors + unknown) / attempted if attempted else 1.0
    return (
        -1 <= age <= max_age
        and live.get("complete") is True
        and attempted == expected
        and error_rate <= max_error_rate
    )


def data_freshness_assessment(prepared: dict[str, Any]) -> dict[str, Any]:
    portal_current = _portal_audit_is_current(prepared.get("portal_delta") or {})
    snapshot_current = _database_snapshot_is_current(prepared)
    live = prepared.get("live_registry_audit") or {}
    expected = len(prepared.get("all_verified_trials") or [])
    attempted = int(live.get("attempted") or 0)
    errors = int(live.get("errors") or 0)
    unknown = int(live.get("unknown") or 0)
    try:
        max_error_rate = float(os.environ.get("LIVE_REGISTRY_MAX_ERROR_RATE", "0.25"))
    except ValueError:
        max_error_rate = -1
    error_rate = (errors + unknown) / attempted if attempted else 1.0
    live_complete = bool(
        live.get("complete")
        and attempted == expected
        and 0 <= max_error_rate <= 1
        and error_rate <= max_error_rate
    )
    if portal_current and live_complete:
        level = "A"
        rationale = (
            "Current WHO portal delta was merged and every recalled trial received "
            "a direct-registry verification attempt."
        )
    elif snapshot_current and live_complete:
        level = "B"
        rationale = (
            "MCP database is within the configured age and every recalled trial "
            "received a direct-registry verification attempt."
        )
    else:
        level = "C"
        missing = []
        if not (portal_current or snapshot_current):
            missing.append("current portal delta or database snapshot")
        if not live_complete:
            missing.append(
                f"complete direct-registry audit ({int(live.get('attempted') or 0)}/{expected})"
            )
        rationale = "Data freshness is insufficient: missing " + " and ".join(missing) + "."
    return {
        "level": level,
        "formal_freshness_ready": live_complete and (portal_current or snapshot_current),
        "portal_delta_current": portal_current,
        "database_snapshot_current": snapshot_current,
        "live_registry_complete": live_complete,
        "live_registry_error_rate": error_rate,
        "live_registry_max_error_rate": max_error_rate,
        "live_registry_audit": live,
        "rationale": rationale,
    }

def _trial_ids(trials: list[dict[str, Any]]) -> set[str]:
    return {str(trial.get("id") or "").strip() for trial in trials if str(trial.get("id") or "").strip()}


def analysis_coverage_audit(
    prepared: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Prove complete disposition and deep-analysis coverage with explicit ID sets."""
    recall_ids = _trial_ids(prepared.get("all_verified_trials") or [])
    hard_excluded_ids = _trial_ids(prepared.get("hard_excluded_trials") or [])
    gater_ids = set(by_id)
    dispositions_disjoint = not (hard_excluded_ids & gater_ids)
    disposition_ids = hard_excluded_ids | gater_ids

    non_excluded_ids = {
        trial_id for trial_id, item in by_id.items()
        if (item.get("gating") or {}).get("verdict") in {"match", "conditional"}
    }
    risk_ids = {
        trial_id for trial_id, item in by_id.items()
        if isinstance(item.get("risk_annotation"), dict)
    }
    efficacy_ids = {
        trial_id for trial_id, item in by_id.items()
        if isinstance(item.get("efficacy_context"), dict)
    }
    evidence_ids = {
        trial_id for trial_id, item in by_id.items()
        if isinstance((item.get("efficacy_context") or {}).get("evidence_search"), dict)
        and isinstance((item.get("efficacy_context") or {}).get("development_evidence"), list)
    }
    disposition_complete = dispositions_disjoint and disposition_ids == recall_ids
    deep_complete = (
        risk_ids == non_excluded_ids
        and efficacy_ids == non_excluded_ids
        and evidence_ids == non_excluded_ids
    )
    return {
        "recall_count": len(recall_ids),
        "hard_excluded_count": len(hard_excluded_ids),
        "gater_completed_count": len(gater_ids),
        "non_excluded_count": len(non_excluded_ids),
        "risk_completed_count": len(risk_ids),
        "efficacy_completed_count": len(efficacy_ids),
        "evidence_completed_count": len(evidence_ids),
        "omitted_count": len(recall_ids - disposition_ids),
        "unexpected_disposition_count": len(disposition_ids - recall_ids),
        "disposition_overlap_count": len(hard_excluded_ids & gater_ids),
        "missing_disposition_ids": sorted(recall_ids - disposition_ids),
        "unexpected_disposition_ids": sorted(disposition_ids - recall_ids),
        "missing_risk_ids": sorted(non_excluded_ids - risk_ids),
        "missing_efficacy_ids": sorted(non_excluded_ids - efficacy_ids),
        "missing_evidence_ids": sorted(non_excluded_ids - evidence_ids),
        "extra_deep_analysis_ids": sorted(
            (risk_ids | efficacy_ids | evidence_ids) - non_excluded_ids
        ),
        "disposition_equation_valid": disposition_complete,
        "deep_analysis_equations_valid": deep_complete,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def report_quality_gates(
    prepared: dict[str, Any],
    analyzed_count: int,
    coverage_audit: dict[str, Any] | None = None,
) -> dict[str, bool]:
    staged = prepared.get("analysis_workflow") == "gater_then_deep_analysis"
    if staged:
        expected = len(prepared.get("analysis_candidate_ids") or []) + len(
            prepared.get("hard_excluded_trials") or []
        )
    else:
        expected = len(prepared.get("prefiltered_trials") or prepared.get("all_verified_trials") or [])
    scope = prepared.get("analysis_scope") or (
        "prefilter_complete" if analyzed_count == expected else "validation_subset"
    )
    prefilter_audit = prepared.get("preanalysis_filter") or {}
    no_budget_omissions = int(prefilter_audit.get("budget_omitted_count") or 0) == 0
    recall_ids = _trial_ids(prepared.get("all_verified_trials") or [])
    valid_recall = bool(recall_ids) and len(recall_ids) == len(
        prepared.get("all_verified_trials") or []
    )
    return {
        "complete_analysis": (
            valid_recall
            and
            scope in {"complete", "complete_recall", "prefilter_complete", "staged_complete"}
            and analyzed_count == expected
            and no_budget_omissions
            and (
                coverage_audit is None
                or (
                    coverage_audit.get("disposition_equation_valid") is True
                    and coverage_audit.get("deep_analysis_equations_valid") is True
                )
            )
        ),
        "complete_retrieval": bool(prepared.get(
            "retrieval_complete", not (prepared.get("search_stats") or {}).get("query_truncation_count")
        )),
        "current_data_snapshot": data_freshness_assessment(prepared)["formal_freshness_ready"],
    }

def select_analysis_candidates(trials: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Select without clinical judgment while preserving mechanism/search diversity."""
    ordered = sorted(trials, key=_candidate_rank)
    if limit <= 0 or limit >= len(ordered):
        return ordered
    buckets: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORY_ORDER}
    for trial in ordered:
        buckets[trial["mechanism_category"]["category"]].append(trial)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit and any(buckets.values()):
        for category in CATEGORY_ORDER:
            if buckets[category] and len(selected) < limit:
                selected.append(buckets[category].pop(0))
    return sorted(selected, key=_candidate_rank)


ACTIVE_ANALYSIS_STATUSES = {
    "RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION", "ACTIVE_NOT_RECRUITING",
}


def deterministic_prefilter(
    trials: list[dict[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a broad, auditable model workload without making eligibility judgments."""
    active = [
        trial for trial in trials
        if str(trial.get("overall_status") or "").strip().upper().replace(" ", "_")
        in ACTIVE_ANALYSIS_STATUSES
    ]
    # Recruitment disposition is handled by direct-registry verification and
    # explicit hard exclusions. A workload selector must not silently remove
    # records from the formal disposition equation.
    eligible = list(trials)
    selected = select_analysis_candidates(eligible, limit)
    audit = {
        "version": "deterministic-prefilter-v1",
        "input_recall_count": len(trials),
        "active_status_count": len(active),
        "inactive_or_unknown_omitted_count": 0,
        "prefilter_limit": limit,
        "budget_omitted_count": len(eligible) - len(selected),
        "selected_count": len(selected),
        "selection_basis": [
            "search rank", "matched search-branch count",
            "operational feasibility", "mechanism diversity",
        ],
        "clinical_eligibility_applied": False,
    }
    return selected, audit


def prepare(
    *, patient_path: Path, plan_path: Path | None, out_dir: Path,
    database: Path | None = None, server_python: str = "", server_script: Path | None = None,
    mcp_transport: str = "stdio", mcp_url: str = "", mcp_api_key: str = "",
    max_per_query: int = 5000, total_limit: int = 20000,
    prefilter_limit: int = 0, analysis_limit: int = 0, batch_size: int = 5,
    portal_delta_path: Path | None = None,
    portal_delta_mode: str = "off", live_registry_verification: bool = False,
    report_language_override: str | None = None,
    patient_country_override: str | None = None,
) -> dict[str, Any]:
    patient, patient_input_audit = load_patient_input(patient_path)
    if patient_country_override:
        patient["country"] = str(patient_country_override).strip()
        patient_input_audit["patient_country_override"] = patient["country"]
    if report_language_override:
        if report_language_override not in {"zh-CN", "en"}:
            raise ValueError("report_language_override must be zh-CN or en")
        patient["report_language"] = report_language_override
        patient_input_audit["report_language_override"] = report_language_override
    plan = load_json(plan_path) if plan_path else build_baseline_search_plan(patient)
    errors = validate_search_plan(plan, require_full_coverage=True)
    errors.extend(validate_search_plan_for_patient(plan, patient))
    if errors:
        raise ValueError("Invalid full search plan: " + "; ".join(errors))
    transport = mcp_transport.strip().casefold()
    if transport == "stdio":
        if not (database and server_python and server_script):
            raise ValueError("stdio MCP requires database, server_python, and server_script")
        mcp_payload = run_who_workflow(
            server_python=server_python,
            server_script=server_script,
            database=database,
            search_plan=plan,
            max_per_query=max_per_query,
            total_limit=total_limit,
        )
    elif transport in {"http", "streamable-http"}:
        if not (mcp_url and mcp_api_key):
            raise ValueError("Streamable HTTP MCP requires WHO_MCP_URL and WHO_MCP_API_KEY")
        mcp_payload = run_remote_who_workflow(
            url=mcp_url,
            api_key=mcp_api_key,
            search_plan=plan,
            max_per_query=max_per_query,
            total_limit=total_limit,
        )
    else:
        raise ValueError("mcp_transport must be stdio or streamable-http")
    search = mcp_payload["search"]
    stats = search.get("search_stats") or {}
    if stats.get("global_truncated"):
        raise RuntimeError(f"MCP search globally truncated: {stats}")
    database_as_of = mcp_payload["metadata"].get("database_as_of") or ""
    if portal_delta_path is not None and portal_delta_mode == "auto":
        portal_delta_mode = "file"
    if portal_delta_mode == "auto":
        generated = build_delta(database_as_of=database_as_of, plan=plan)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "portal_delta.json").write_text(
            json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        portal_trials = generated["trials"]
        portal_audit = {
            **generated,
            "returned": len(portal_trials),
            "freshness_validated_at_prepare": True,
            "max_age_hours": _portal_max_age_hours(),
            "clock_skew_tolerance_minutes": _portal_clock_skew_minutes(),
        }
        portal_audit.pop("trials", None)
    elif portal_delta_mode == "file" or portal_delta_path:
        portal_trials, portal_audit = _load_portal_delta(
            portal_delta_path, database_as_of, _json_sha256(plan)
        )
    elif portal_delta_mode == "off":
        portal_trials, portal_audit = _load_portal_delta(None, database_as_of)
    else:
        raise ValueError("portal_delta_mode must be auto, file, or off")
    merged = merge_sources(
        search.get("results") or [], portal_trials, patient=patient, database_as_of=database_as_of,
    )
    verified = verify_batch(merged, mcp_payload.get("details") or [], patient)
    duplicate_clusters = {
        tuple(trial.get("possible_duplicate_cluster_ids") or [])
        for trial in verified
        if trial.get("possible_duplicate_cluster_ids")
    }
    deduplication_audit = {
        "input_count": len(merged),
        "output_count": len(verified),
        "authoritative_id_merges": len(merged) - len(verified),
        "possible_duplicate_cluster_count": len(duplicate_clusters),
        "possible_duplicate_clusters": [
            list(cluster) for cluster in sorted(duplicate_clusters)
        ],
        "policy": (
            "Automatic merges require an authoritative registry-ID bridge. "
            "Title/intervention lookalikes remain separate and are flagged for review."
        ),
    }
    for trial in verified:
        trial["resolved_source_url"] = resolved_trial_url(trial)
    if live_registry_verification:
        live = verify_and_partition(
            verified,
            workers=int(os.environ.get("LIVE_REGISTRY_CONCURRENCY", "8")),
            timeout=float(os.environ.get("LIVE_REGISTRY_TIMEOUT_SECONDS", "20")),
            patient=patient,
        )
        verified = live["active_or_unknown"] + live["inactive"]
        live_inactive_ids = {
            str(trial.get("id") or "") for trial in live["inactive"]
        }
        live_audit = live["audit"]
    else:
        live_inactive_ids = set()
        live_audit = {
            "attempted": 0, "active": 0, "inactive": 0, "unknown": 0,
            "errors": 0, "complete": False, "policy": "Not executed",
        }
    for trial in verified:
        trial["feasibility"] = compute_feasibility(trial, patient).__dict__
        trial["mechanism_category"] = classify_mechanism(trial, patient=patient)
        trial["country_assessment"] = assess_country_evidence(trial, patient)
        trial["resolved_source_url"] = resolved_trial_url(trial)
    triage = apply_generic_hard_rules(patient, verified)
    registry_inactive = []
    generic_hard_excluded = []
    for trial in triage["hard_exclude"]:
        generic_hard_excluded.append(trial)
    gater_pool = []
    for trial in triage["pass_to_gater"]:
        if str(trial.get("id") or "") in live_inactive_ids:
            trial["hard_excluded"] = True
            trial["exclude_reason"] = "Direct registry explicitly reports a non-enrolling status."
            trial["registry_status_rule"] = {
                "rule_id": "REGISTRY-INACTIVE",
                "source": trial.get("live_registry_verification"),
            }
            registry_inactive.append(trial)
        else:
            gater_pool.append(trial)
    hard_excluded = generic_hard_excluded + registry_inactive
    prefiltered, prefilter_audit = deterministic_prefilter(gater_pool, prefilter_limit)
    candidates = select_analysis_candidates(prefiltered, analysis_limit)
    query_audit = search.get("query_audit") or []
    retrieval_complete = bool(query_audit) and not stats.get("global_truncated") and not (
        stats.get("query_truncation_count")
        or any(
            item.get("truncated") is True
            or item.get("has_more") is True
            or item.get("complete") is False
            for item in query_audit
            if isinstance(item, dict)
        )
    )
    no_budget_omissions = len(candidates) == len(gater_pool)
    analysis_scope = "staged_complete" if no_budget_omissions else "validation_subset"
    jobs = build_analysis_jobs(patient, candidates, SKILLS_ROOT, batch_size=batch_size)
    payload = {
        "schema_version": "generic-who-mcp-prepared-v1",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "transport": mcp_payload["transport"],
        "server_tools": mcp_payload["server_tools"],
        "patient": patient,
        "patient_input_audit": patient_input_audit,
        "search_plan": plan,
        "executed_search_plan": mcp_payload.get("executed_search_plan") or plan,
        "search_plan_audit": plan.get("generation_audit") or {
            "mode": "provided",
            "requires_human_review": False,
        },
        "database_metadata": mcp_payload["metadata"],
        "database_as_of": mcp_payload["metadata"].get("database_as_of"),
        "search_stats": stats,
        "query_audit": query_audit,
        "deduplication_audit": deduplication_audit,
        "portal_delta": portal_audit,
        "live_registry_audit": live_audit,
        "all_verified_trials": verified,
        "analysis_workflow": "gater_then_deep_analysis",
        "hard_rule_triage": {
            "ruleset_version": triage["ruleset_version"],
            "input_count": len(verified),
            "hard_excluded_count": len(hard_excluded),
            "pass_to_gater_count": len(gater_pool),
            "registry_inactive_count": len(registry_inactive),
            "audit": triage["audit"],
        },
        "hard_excluded_trials": hard_excluded,
        "prefiltered_trials": prefiltered,
        "preanalysis_filter": prefilter_audit,
        "analysis_candidates": candidates,
        "analysis_candidate_ids": [trial["id"] for trial in candidates],
        "analysis_limit": analysis_limit,
        "analysis_scope": analysis_scope,
        "retrieval_complete": retrieval_complete,
        "data_freshness": {},
        "formal_report_ready": False,
        "guardrail": "Run canonical LLM subskills and finalize with a validated analysis bundle.",
    }
    payload["data_freshness"] = data_freshness_assessment(payload)
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized_patient_path = out_dir / "normalized-patient.json"
    write_json_atomic(normalized_patient_path, patient)
    normalized_plan_path = out_dir / "normalized-search-plan.json"
    write_json_atomic(normalized_plan_path, plan)
    payload["normalized_patient_path"] = str(normalized_patient_path.resolve())
    payload["normalized_search_plan_path"] = str(normalized_plan_path.resolve())
    write_json_atomic(out_dir / "prepared.json", payload)
    write_json_atomic(out_dir / "analysis_jobs.json", jobs)
    return payload


def finalize(*, prepared_path: Path, analysis_path: Path, out_dir: Path) -> dict[str, Any]:
    prepared = load_json(prepared_path)
    analysis_bundle = load_json(analysis_path)
    patient = prepared["patient"]
    candidates = prepared["analysis_candidates"]
    by_id = validate_analysis_bundle(analysis_bundle, patient, prepared["analysis_candidate_ids"])
    coverage_audit = analysis_coverage_audit(prepared, by_id)
    language = _language(patient)
    trials: list[dict[str, Any]] = []
    for source in candidates:
        trial = dict(source)
        normalized = normalized_report_analysis(by_id[trial["id"]])
        trial["gating"] = normalized["gating"]
        trial["risks"] = normalized["risks"]
        trial["risk_context"] = [
            note for risk in normalized["risks"] for note in risk.get("notes") or []
        ] or (["No patient-specific risk narrative was emitted by the risk subskill."] if language != "zh-CN" else ["\u98ce\u9669\u5b50\u6280\u80fd\u672a\u8f93\u51fa\u9002\u7528\u4e8e\u8be5\u60a3\u8005\u7684\u7279\u5f02\u6027\u98ce\u9669\u3002"])
        trial["efficacy_context_detail"] = normalized["efficacy_context"]
        trial["efficacy_context"] = normalized["efficacy_context"]["summary"]
        trial["development_evidence"] = normalized["efficacy_context"]["development_evidence"]
        trial["evidence_search"] = normalized["efficacy_context"]["evidence_search"]
        trial["mechanism_category"] = classify_mechanism(trial, analysis=by_id[trial["id"]], patient=patient)
        trial["country_assessment"] = assess_country_evidence(trial, patient)
        trial["resolved_source_url"] = resolved_trial_url(trial)
        trial["display_title"] = patient_facing_title(trial, language)
        trials.append(trial)
    for source in prepared.get("hard_excluded_trials") or []:
        trial = dict(source)
        hard = trial.get("generic_hard_rules") or {}
        reasons = [str(item.get("reason") or item.get("rule_id") or "") for item in hard.get("triggered_rules") or []]
        if trial.get("exclude_reason"):
            reasons.append(str(trial["exclude_reason"]))
        trial["gating"] = {
            "verdict": "exclude", "confidence": 1.0, "satisfied": [], "pending": [],
            "exclusion_reasons": reasons,
            "rationale": "; ".join(reasons) or "Explicit structured eligibility conflict.",
            "hard_rules_triggered": [item.get("rule_id") for item in hard.get("triggered_rules") or []],
            "inclusion_evaluation": [], "exclusion_evaluation": [],
        }
        trial["risks"] = []
        trial["risk_context"] = []
        trial["efficacy_context_detail"] = {}
        trial["efficacy_context"] = ""
        trial["development_evidence"] = []
        trial["evidence_search"] = {}
        trial["country_assessment"] = assess_country_evidence(trial, patient)
        trial["resolved_source_url"] = resolved_trial_url(trial)
        trial["display_title"] = patient_facing_title(trial, language)
        trials.append(trial)
    verdict_order = {"match": 0, "conditional": 1, "exclude": 2}
    trials.sort(key=lambda trial: (
        verdict_order[trial["gating"]["verdict"]],
        -float((trial.get("feasibility") or {}).get("composite") or 0),
        str(trial.get("id") or ""),
    ))
    counts = dict(Counter(trial["gating"]["verdict"] for trial in trials))
    counts = {key: counts.get(key, 0) for key in ("match", "conditional", "exclude")}
    geography = dict(Counter(trial["country_assessment"]["class"] for trial in trials))
    quality_gates = report_quality_gates(prepared, len(trials), coverage_audit)
    # Only analysis integrity blocks patient-card rendering. Retrieval
    # completeness, freshness and language consistency remain prominent,
    # auditable warnings because they are local limitations, not corrupt data.
    formal_ready = quality_gates["complete_analysis"]
    report_warnings: list[dict[str, str]] = []
    if not quality_gates["complete_retrieval"]:
        report_warnings.append({
            "code": "RETRIEVAL_INCOMPLETE",
            "message_zh": "检索结果可能因分页或数量上限而不完整；报告仅覆盖已召回并完成分析的试验。",
            "message_en": "Retrieval may be incomplete due to pagination or result limits; the report covers recalled and analyzed trials only.",
        })
    if not quality_gates["current_data_snapshot"]:
        report_warnings.append({
            "code": "DATA_SNAPSHOT_STALE",
            "message_zh": "数据快照未达到当前新鲜度目标；招募状态和中心信息须在联系研究中心前重新核实。",
            "message_en": "The data snapshot does not meet the current freshness target; recruitment status and sites require re-verification before contact.",
        })
    run_manifest = {
        "schema_version": "formal-run-manifest-v1",
        "prepared_sha256": _sha256(prepared_path),
        "analysis_sha256": _sha256(analysis_path),
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "counts": {
            "recall": coverage_audit["recall_count"],
            "hard_excluded": coverage_audit["hard_excluded_count"],
            "gater_completed": coverage_audit["gater_completed_count"],
            "deep_completed": coverage_audit["non_excluded_count"],
            "risk_completed": coverage_audit["risk_completed_count"],
            "efficacy_completed": coverage_audit["efficacy_completed_count"],
            "evidence_completed": coverage_audit["evidence_completed_count"],
            "omitted": coverage_audit["omitted_count"],
        },
        "coverage_audit": coverage_audit,
        "quality_gates": quality_gates,
        "gate_policy": {
            "complete_analysis": "blocking",
            "complete_retrieval": "warning",
            "current_data_snapshot": "warning",
        },
        "warnings": report_warnings,
        "formal_report_ready": formal_ready,
    }
    payload = {
        "schema_version": "generic-who-mcp-final-v1",
        "analysis_schema_version": analysis_bundle["schema_version"],
        "analysis_provenance": analysis_bundle["analysis_provenance"],
        "formal_report_ready": formal_ready,
        "report_mode": (
            "formal_with_warnings" if formal_ready and report_warnings
            else "formal" if formal_ready else "validation"
        ),
        "quality_gates": quality_gates,
        "gate_policy": run_manifest["gate_policy"],
        "report_warnings": report_warnings,
        "run_manifest": run_manifest,
        "stages": [
            "patient_structuring", "8-dimension_MCP_recall", "canonical_registry_deduplication",
            "get_trial_verification", "original_trial_gater", "feasibility_scoring",
            "original_risk_annotator", "original_efficacy_contextualizer",
            "original_decision_synthesizer", "mechanism_classification", "patient_report",
        ],
        "patient": patient,
        "language": language,
        "database_metadata": prepared["database_metadata"],
        "database_as_of": prepared["database_as_of"],
        "portal_delta": prepared["portal_delta"],
        "live_registry_audit": prepared.get("live_registry_audit") or {},
        "data_freshness": data_freshness_assessment(prepared),
        "search_stats": prepared["search_stats"],
        "query_audit": prepared["query_audit"],
        "recall_count": len(prepared["all_verified_trials"]),
        "analyzed_count": len(trials),
        "gater_analyzed_count": len(candidates),
        "deep_analyzed_count": sum(1 for trial in trials if trial["gating"]["verdict"] in {"match", "conditional"}),
        "hard_excluded_count": len(prepared.get("hard_excluded_trials") or []),
        "counts": counts,
        "geography_audit": geography,
        "feasibility_weights": WEIGHTS,
        "decision_report": analysis_bundle["decision_report"],
        "trials": trials,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if formal_ready:
        payload = maybe_translate_report(
            payload, cache_path=out_dir / "report-translations.json"
        )
    write_json_atomic(out_dir / "pipeline.json", payload)
    (out_dir / "run-manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if formal_ready:
        (out_dir / "validation-report.html").unlink(missing_ok=True)
        render_html(payload, out_dir / "report.html")
    else:
        (out_dir / "report.html").unlink(missing_ok=True)
        render_validation_html(payload, out_dir / "validation-report.html")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic clinical-trial matching over WHO MCP")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--patient", required=True)
    prepare_parser.add_argument(
        "--plan",
        help="Eight-dimensional plan JSON; omit for deterministic baseline generation.",
    )
    prepare_parser.add_argument(
        "--mcp-transport", choices=("stdio", "streamable-http"),
        default=os.environ.get("WHO_MCP_TRANSPORT", "stdio"),
    )
    prepare_parser.add_argument("--db", default=os.environ.get("WHO_MCP_DB"), help="WHO trial database path (or WHO_MCP_DB)")
    prepare_parser.add_argument(
        "--mcp-python", default=os.environ.get("WHO_MCP_PYTHON"),
        help="Python executable for the stdio MCP server (or WHO_MCP_PYTHON)",
    )
    prepare_parser.add_argument(
        "--mcp-server", default=os.environ.get("WHO_MCP_SERVER"),
        help="WHO MCP stdio server script (or WHO_MCP_SERVER)",
    )
    prepare_parser.add_argument("--mcp-url", default=os.environ.get("WHO_MCP_URL"), help="Streamable HTTP MCP URL (or WHO_MCP_URL)")
    prepare_parser.add_argument("--out", required=True)
    prepare_parser.add_argument("--max-per-query", type=int, default=5000)
    prepare_parser.add_argument("--total-limit", type=int, default=20000)
    prepare_parser.add_argument("--prefilter-limit", type=int, default=0)
    prepare_parser.add_argument("--analysis-limit", type=int, default=0)
    prepare_parser.add_argument("--batch-size", type=int, default=5)
    prepare_parser.add_argument("--portal-delta")
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--prepared", required=True)
    finalize_parser.add_argument("--analysis", required=True)
    finalize_parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        if args.mcp_transport == "stdio" and not (args.db and args.mcp_python and args.mcp_server):
            prepare_parser.error("stdio transport requires --db, --mcp-python and --mcp-server, or matching environment variables")
        mcp_api_key = os.environ.get("WHO_MCP_API_KEY", "")
        if args.mcp_transport == "streamable-http" and not (args.mcp_url and mcp_api_key):
            prepare_parser.error("streamable-http requires WHO_MCP_URL/--mcp-url and WHO_MCP_API_KEY")
        result = prepare(
            patient_path=Path(args.patient),
            plan_path=Path(args.plan) if args.plan else None,
            database=Path(args.db) if args.db else None,
            server_python=args.mcp_python or "", server_script=Path(args.mcp_server) if args.mcp_server else None,
            mcp_transport=args.mcp_transport, mcp_url=args.mcp_url or "", mcp_api_key=mcp_api_key,
            out_dir=Path(args.out),
            max_per_query=args.max_per_query, total_limit=args.total_limit,
            prefilter_limit=args.prefilter_limit, analysis_limit=args.analysis_limit,
            batch_size=args.batch_size,
            portal_delta_path=Path(args.portal_delta) if args.portal_delta else None,
        )
        summary = {
            "prepared": args.out, "transport": result["transport"],
            "recall": len(result["all_verified_trials"]), "analysis_candidates": len(result["analysis_candidates"]),
            "database_as_of": result["database_as_of"], "analysis_scope": result["analysis_scope"], "formal_report_ready": False,
        }
    else:
        result = finalize(prepared_path=Path(args.prepared), analysis_path=Path(args.analysis), out_dir=Path(args.out))
        summary = {
            "output": args.out, "counts": result["counts"], "recall": result["recall_count"],
            "analyzed": result["analyzed_count"], "formal_report_ready": result["formal_report_ready"], "report_mode": result["report_mode"],
        }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
