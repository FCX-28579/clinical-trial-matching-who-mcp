"""Stateful formal workflow entry point that prevents skipped analysis stages."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

from analysis_batch_manager import (
    collect_trials, combine_analysis_stages, create_deep_jobs, merge, status,
)
from analysis_contract import load_json, report_language
from full_pipeline import (
    _json_sha256, _load_portal_delta, data_freshness_assessment, finalize, prepare,
)
from model_batch_executor import execute_batches, execute_decision
from publication_prefetch import enrich_deep_jobs_file
from io_utils import write_json_atomic


STATE_NAME = "formal-run-state.json"
LOCK_NAME = "formal-execute.lock"


def _stage_models(fallback: str = "") -> dict[str, str]:
    return {
        stage: os.environ.get(f"{stage.upper()}_MODEL_NAME", "").strip() or fallback
        for stage in ("gater", "deep", "decision")
    }


@contextmanager
def _execution_lock(run_dir: Path):
    """Prevent two formal executors from charging for and writing the same run."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / LOCK_NAME
    handle = path.open("a+b")
    acquired = False
    try:
        try:
            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
        except OSError as exc:
            raise RuntimeError(f"Another formal executor is already using {run_dir}") from exc
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Another formal executor is already using {run_dir}") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError(f"Another formal executor is already using {run_dir}") from exc
        acquired = True
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _limited_text(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _compact_decision_trial(
    analysis: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Keep every candidate comparable without resending full deep narratives."""
    gating = analysis.get("gating") or {}
    risk = analysis.get("risk_annotation") or {}
    efficacy = analysis.get("efficacy_context") or {}
    snapshot = efficacy.get("efficacy_snapshot") or {}
    vs_soc = efficacy.get("vs_soc") or {}
    risks = []
    for item in (risk.get("risks") or [])[:1]:
        if isinstance(item, dict):
            risks.append({
                "key": item.get("key"),
                "mechanism": item.get("mechanism"),
                "risk_level": item.get("risk_level"),
                "applies_because": _limited_text(item.get("applies_because"), 200),
            })
    soc_options = []
    for item in (vs_soc.get("soc_options") or [])[:1]:
        if isinstance(item, dict):
            soc_options.append({
                "regimen": item.get("regimen"),
                "expected_orr": item.get("expected_orr"),
                "expected_pfs_months": item.get("expected_pfs_months"),
                "expected_os_months": item.get("expected_os_months"),
                "evidence": _limited_text(item.get("evidence"), 160),
                "patient_eligibility_note": _limited_text(
                    item.get("patient_eligibility_note"), 160
                ),
            })
    mechanism = source.get("mechanism_category") or {}
    feasibility = source.get("feasibility") or {}
    redundancy = efficacy.get("redundancy_with_existing_options") or {}
    return {
        "trial_id": analysis.get("trial_id"),
        "title": source.get("title") or source.get("scientific_title") or "",
        "phase": source.get("phases") or [],
        "overall_status": source.get("overall_status") or "",
        "sponsor": source.get("sponsor") or "",
        "interventions": (source.get("interventions") or [])[:6],
        "mechanism_category": {
            "category": mechanism.get("category"),
            "label": mechanism.get("label"),
            "label_en": mechanism.get("label_en"),
        },
        "patient_country_site_count": source.get("patient_country_site_count") or 0,
        "country_assessment": source.get("country_assessment") or {},
        "feasibility": {
            "composite": feasibility.get("composite", feasibility.get("score")),
            "sub_scores": feasibility.get("sub_scores") or {},
        },
        "gating": {
            "verdict": gating.get("verdict"),
            "confidence": gating.get("confidence"),
            "blockers_satisfied": (gating.get("blockers_satisfied") or [])[:3],
            "blockers_failed": (gating.get("blockers_failed") or [])[:3],
            "blockers_pending": (gating.get("blockers_pending") or [])[:3],
            "hard_rules_triggered": gating.get("hard_rules_triggered") or [],
            "rationale": _limited_text(gating.get("rationale"), 250),
        },
        "risk_summary": {
            "mechanisms": (risk.get("trial_mechanisms_identified") or [])[:6],
            "risks": risks,
        },
        "efficacy_summary": {
            "match_type": snapshot.get("match_type"),
            "metrics": snapshot.get("metrics"),
            "evidence_source": snapshot.get("evidence_source"),
            "applies_because": _limited_text(snapshot.get("applies_because"), 250),
            "vs_soc": {
                "available": bool(vs_soc.get("available")),
                "head_to_head_summary": _limited_text(
                    vs_soc.get("head_to_head_summary"), 250
                ),
                "soc_options": soc_options,
            },
            "redundancy_with_existing_options": {
                "is_trial_redundant_with_approved_combo": redundancy.get(
                    "is_trial_redundant_with_approved_combo"
                ),
                "explanation": _limited_text(redundancy.get("explanation"), 200),
            },
            "development_evidence_count": len(
                efficacy.get("development_evidence") or []
            ),
        },
    }


def _decision_input(
    gating_trials: list[dict[str, Any]],
    deep_trials: list[dict[str, Any]],
    deep_jobs: dict[str, Any],
) -> dict[str, Any]:
    combined = combine_analysis_stages(gating_trials, deep_trials)
    source_by_id = {
        str(trial.get("id") or ""): trial
        for batch in deep_jobs.get("batches") or []
        for trial in batch.get("trials") or []
    }
    inventory = {"match": 0, "conditional": 0, "exclude": 0}
    for item in gating_trials:
        verdict = str((item.get("gating") or {}).get("verdict") or "")
        if verdict in inventory:
            inventory[verdict] += 1
    candidates = [
        _compact_decision_trial(item, source_by_id.get(str(item.get("trial_id") or ""), {}))
        for item in combined
        if (item.get("gating") or {}).get("verdict") in {"match", "conditional"}
    ]
    return {
        "analysis_metadata": {
            "full_coverage": True,
            "candidate_count": len(candidates),
            "match_inventory_size": inventory,
            "note": "Full deep narratives remain in the validated analysis bundle.",
        },
        "analyzed_trials": candidates,
    }


def _state_path(run_dir: Path) -> Path:
    return run_dir / STATE_NAME


def _load_state(run_dir: Path) -> dict[str, Any]:
    path = _state_path(run_dir)
    if not path.exists():
        raise ValueError("Formal run is not prepared; run the prepare command first")
    return load_json(path)


def _save_state(run_dir: Path, state: dict[str, Any]) -> None:
    write_json_atomic(_state_path(run_dir), state)


def _deep_status(jobs_path: Path, batch_dir: Path) -> dict[str, Any]:
    jobs = load_json(jobs_path)
    expected = {
        str(trial.get("id") or "").strip()
        for batch in jobs.get("batches") or []
        for trial in batch.get("trials") or []
    }
    trials, errors = collect_trials(batch_dir, "deep-batch-*.json")
    actual = {str(item.get("trial_id") or "").strip() for item in trials}
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    return {
        "expected": len(expected),
        "completed": len(actual & expected),
        "missing_count": len(missing),
        "missing": missing[:20],
        "unexpected_count": len(unexpected),
        "unexpected": unexpected[:20],
        "errors": errors,
        "complete": actual == expected and not errors,
    }


def prepare_formal(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("Formal run directory must be new or empty; use a new directory for every run")
    portal_mode = getattr(args, "portal_delta_mode", "off")
    live_verification = not getattr(args, "skip_live_registry_verification", True)
    external_access = (
        os.environ.get("EXTERNAL_REGISTRY_ACCESS_AUTHORIZED", "").strip() == "1"
        or bool(getattr(args, "authorize_external_registry_access", False))
    )
    if (portal_mode == "auto" or live_verification) and not external_access:
        raise ValueError(
            "External registry access requires explicit operator authorization. "
            "Set EXTERNAL_REGISTRY_ACCESS_AUTHORIZED=1 or pass "
            "--authorize-external-registry-access. Portal auto sends patient-derived "
            "search terms to WHO ICTRP; live verification sends recalled trial IDs to "
            "allowlisted primary registries."
        )
    result = prepare(
        patient_path=Path(args.patient),
        plan_path=Path(args.plan) if args.plan else None,
        out_dir=run_dir,
        database=Path(args.db) if args.db else None,
        server_python=args.mcp_python or "",
        server_script=Path(args.mcp_server) if args.mcp_server else None,
        mcp_transport=args.mcp_transport,
        mcp_url=args.mcp_url or "",
        mcp_api_key=os.environ.get("WHO_MCP_API_KEY", ""),
        max_per_query=args.max_per_query,
        total_limit=args.total_limit,
        prefilter_limit=0,
        analysis_limit=0,
        batch_size=args.batch_size,
        portal_delta_path=Path(args.portal_delta) if args.portal_delta else None,
        portal_delta_mode=portal_mode,
        live_registry_verification=live_verification,
        report_language_override=getattr(args, "report_language", None),
        patient_country_override=getattr(args, "patient_country", None),
    )
    state = {
        "schema_version": "formal-pipeline-state-v1",
        "stage": "gater_pending",
        "run_dir": str(run_dir),
        "patient_path": result.get(
            "normalized_patient_path", str(Path(args.patient).resolve())
        ),
        "patient_input_path": str(Path(args.patient).resolve()),
        "search_plan_path": result.get(
            "normalized_search_plan_path",
            str(Path(args.plan).resolve()) if args.plan else "",
        ),
        "prepared_path": str((run_dir / "prepared.json").resolve()),
        "gater_jobs_path": str((run_dir / "analysis_jobs.json").resolve()),
        "gater_batch_dir": str((run_dir / "gater-batches").resolve()),
        "deep_jobs_path": str((run_dir / "deep_jobs.json").resolve()),
        "deep_batch_dir": str((run_dir / "deep-batches").resolve()),
        "analysis_bundle_path": str((run_dir / "analysis_bundle.json").resolve()),
        "final_dir": str((run_dir / "final").resolve()),
        "recall_count": len(result["all_verified_trials"]),
        "hard_excluded_count": len(result.get("hard_excluded_trials") or []),
        "gater_expected_count": len(result["analysis_candidate_ids"]),
        "next_action": "Complete every gater batch listed in analysis_jobs.json.",
    }
    _save_state(run_dir, state)
    return state


def formal_status(run_dir: Path) -> dict[str, Any]:
    state = _load_state(run_dir)
    jobs = Path(state["gater_jobs_path"])
    gater_dir = Path(state["gater_batch_dir"])
    batch_status = status(jobs, gater_dir)
    result = {**state, "batch_status": batch_status}
    if state["stage"] == "gater_pending":
        gater_complete = bool(
            (batch_status.get("stages") or {}).get("gater", {}).get(
                "complete", batch_status.get("complete")
            )
        )
        result["next_action"] = (
            "Complete all gater batches, then run deep-jobs."
            if not gater_complete
            else "Run deep-jobs."
        )
    elif state["stage"] in {"deep_pending", "formal_complete", "validation_failed"}:
        deep_status = _deep_status(
            Path(state["deep_jobs_path"]), Path(state["deep_batch_dir"])
        )
        if isinstance(batch_status.get("stages"), dict):
            batch_status["stages"]["deep"] = deep_status
            batch_status["complete"] = bool(
                batch_status["stages"].get("gater", {}).get("complete")
                and deep_status["complete"]
            )
        result["deep_status"] = deep_status
        if state["stage"] == "deep_pending":
            result["next_action"] = (
                "Complete all deep batches, then run merge."
                if not deep_status["complete"]
                else "Run merge with the decision-synthesizer output."
            )
        result["complete"] = bool(
            state["stage"] == "formal_complete"
            and batch_status.get("stages", {}).get("gater", {}).get("complete")
            and deep_status["complete"]
            and state.get("formal_report_ready")
        )
    return result


def create_formal_deep_jobs(run_dir: Path) -> dict[str, Any]:
    state = _load_state(run_dir)
    if state["stage"] != "gater_pending":
        raise ValueError(f"deep-jobs is not allowed from stage {state['stage']}")
    gater_status = status(Path(state["gater_jobs_path"]), Path(state["gater_batch_dir"]))
    gater_stage = (gater_status.get("stages") or {}).get("gater") or {}
    gater_complete = bool(gater_stage.get("complete", gater_status.get("complete")))
    if not gater_complete:
        raise ValueError(
            "Cannot create deep jobs: gater stage is incomplete; "
            f"missing={gater_stage.get('missing_count', gater_status.get('missing_count', 0))}, "
            f"unexpected={gater_stage.get('unexpected_count', gater_status.get('unexpected_count', 0))}, "
            f"errors={len(gater_stage.get('errors') or gater_status.get('errors') or [])}"
        )
    result = create_deep_jobs(
        Path(state["gater_jobs_path"]),
        Path(state["patient_path"]),
        Path(state["gater_batch_dir"]),
        Path(state["deep_jobs_path"]),
        batch_size=int(os.environ.get("MODEL_DEEP_BATCH_SIZE", "2")),
    )
    state["stage"] = "deep_pending"
    state["deep_expected_count"] = result["trials"]
    state["next_action"] = "Complete every deep batch listed in deep_jobs.json."
    _save_state(run_dir, state)
    return state


def merge_formal(
    run_dir: Path, decision: Path, model: str, output_language: str
) -> dict[str, Any]:
    state = _load_state(run_dir)
    if state["stage"] != "deep_pending":
        raise ValueError(f"merge is not allowed from stage {state['stage']}")
    deep_status = _deep_status(
        Path(state["deep_jobs_path"]), Path(state["deep_batch_dir"])
    )
    if not deep_status["complete"]:
        raise ValueError(
            f"Cannot merge: {deep_status['missing_count']} deep result(s) are missing"
        )
    result = merge(
        Path(state["gater_jobs_path"]),
        Path(state["patient_path"]),
        Path(state["gater_batch_dir"]),
        decision,
        Path(state["analysis_bundle_path"]),
        model,
        output_language,
        Path(state["deep_batch_dir"]),
    )
    state["stage"] = "analysis_merged"
    state["merged_trial_count"] = result["trials"]
    state["next_action"] = "Run finalize. Do not create patient-facing HTML manually."
    _save_state(run_dir, state)
    return state


def finalize_formal(run_dir: Path) -> dict[str, Any]:
    state = _load_state(run_dir)
    if state["stage"] != "analysis_merged":
        raise ValueError(f"finalize is not allowed from stage {state['stage']}")
    result = finalize(
        prepared_path=Path(state["prepared_path"]),
        analysis_path=Path(state["analysis_bundle_path"]),
        out_dir=Path(state["final_dir"]),
    )
    state["stage"] = "formal_complete" if result["formal_report_ready"] else "validation_failed"
    state["formal_report_ready"] = result["formal_report_ready"]
    state["report_mode"] = result["report_mode"]
    state["run_manifest"] = result["run_manifest"]
    state["next_action"] = (
        "Formal report complete."
        if result["formal_report_ready"]
        else "Inspect validation-report.html and complete the missing work; no formal report was generated."
    )
    _save_state(run_dir, state)
    return state


def refresh_formal_freshness(run_dir: Path, portal_delta_path: Path) -> dict[str, Any]:
    """Re-finalize an analyzed run only when a current delta adds no candidates."""
    state = _load_state(run_dir)
    if state["stage"] != "validation_failed":
        raise ValueError(
            f"refresh-freshness is not allowed from stage {state['stage']}"
        )
    prepared_path = Path(state["prepared_path"])
    prepared = load_json(prepared_path)
    trials, audit = _load_portal_delta(
        portal_delta_path,
        str(prepared.get("database_as_of") or ""),
        _json_sha256(prepared.get("search_plan") or {}),
    )
    if trials:
        raise ValueError(
            "Portal delta contains new trials; the existing analysis cannot be reused. "
            "Start a new formal run so every recalled trial receives a disposition."
        )
    prepared["portal_delta"] = audit
    prepared["data_freshness"] = data_freshness_assessment(prepared)
    write_json_atomic(prepared_path, prepared)
    state["stage"] = "analysis_merged"
    state["next_action"] = "Run finalize with the refreshed zero-result portal delta."
    _save_state(run_dir, state)
    return finalize_formal(run_dir)


def _execute_formal_unlocked(
    run_dir: Path, *, model: str, timeout: float, retries: int
) -> dict[str, Any]:
    state = _load_state(run_dir)
    stage_models = _stage_models(model)
    state["stage_models"] = stage_models
    _save_state(run_dir, state)
    if state["stage"] == "gater_pending":
        execute_batches(
            Path(state["gater_jobs_path"]), Path(state["gater_batch_dir"]),
            output_prefix="gater-batch", retries=retries, timeout=timeout,
        )
        create_formal_deep_jobs(run_dir)
        state = _load_state(run_dir)
    if state["stage"] == "deep_pending":
        state["publication_prefetch"] = enrich_deep_jobs_file(
            Path(state["deep_jobs_path"]), run_dir / "publication-cache"
        )
        _save_state(run_dir, state)
        execute_batches(
            Path(state["deep_jobs_path"]), Path(state["deep_batch_dir"]),
            output_prefix="deep-batch", retries=retries, timeout=timeout,
        )
        gater, _ = collect_trials(Path(state["gater_batch_dir"]), "gater-batch-*.json")
        deep, _ = collect_trials(Path(state["deep_batch_dir"]), "deep-batch-*.json")
        interim = run_dir / "decision-analysis-input.json"
        write_json_atomic(
            interim,
            _decision_input(gater, deep, load_json(Path(state["deep_jobs_path"]))),
        )
        decision = run_dir / "decision_report.json"
        execute_decision(
            patient_path=Path(state["patient_path"]), analysis_path=interim,
            skill_path=HERE.parents[3] / "decision-synthesizer" / "SKILL.md",
            output_path=decision, retries=retries, timeout=timeout,
        )
        patient = load_json(Path(state["patient_path"]))
        model_label = "; ".join(
            f"{stage}={name}" for stage, name in stage_models.items()
        )
        merge_formal(run_dir, decision, model_label, report_language(patient))
        state = _load_state(run_dir)
    if state["stage"] == "analysis_merged":
        return finalize_formal(run_dir)
    return state


def execute_formal(run_dir: Path, *, model: str, timeout: float, retries: int) -> dict[str, Any]:
    with _execution_lock(run_dir):
        return _execute_formal_unlocked(
            run_dir, model=model, timeout=timeout, retries=retries
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--patient", required=True)
    prepare_parser.add_argument(
        "--plan",
        help="Eight-dimensional search plan JSON. Omit to build an auditable baseline from patient data.",
    )
    prepare_parser.add_argument("--run-dir", required=True)
    prepare_parser.add_argument(
        "--report-language", choices=("zh-CN", "en"),
        help="Override the patient file's report language for this run.",
    )
    prepare_parser.add_argument(
        "--patient-country",
        help="Explicitly override the patient's current country for access scoring.",
    )
    prepare_parser.add_argument(
        "--mcp-transport",
        choices=("stdio", "streamable-http"),
        default=os.environ.get("WHO_MCP_TRANSPORT", "stdio"),
    )
    prepare_parser.add_argument("--db", default=os.environ.get("WHO_MCP_DB"))
    prepare_parser.add_argument("--mcp-python", default=os.environ.get("WHO_MCP_PYTHON"))
    prepare_parser.add_argument("--mcp-server", default=os.environ.get("WHO_MCP_SERVER"))
    prepare_parser.add_argument("--mcp-url", default=os.environ.get("WHO_MCP_URL"))
    prepare_parser.add_argument("--max-per-query", type=int, default=5000)
    prepare_parser.add_argument("--total-limit", type=int, default=20000)
    prepare_parser.add_argument(
        "--batch-size", type=int,
        default=int(os.environ.get("MODEL_GATER_BATCH_SIZE", "3")),
    )
    prepare_parser.add_argument("--portal-delta")
    prepare_parser.add_argument(
        "--portal-delta-mode", choices=("auto", "file", "off"), default="off",
        help=(
            "off performs no portal access and is the safe default; file validates "
            "--portal-delta; auto requires explicit authorization to query the WHO portal"
        ),
    )
    prepare_parser.add_argument(
        "--skip-live-registry-verification", action="store_true",
        help="Validation/debug only; formal freshness always fails when this is used.",
    )
    prepare_parser.add_argument(
        "--authorize-external-registry-access", action="store_true",
        help=(
            "Confirm this run may send patient-derived portal queries to WHO ICTRP "
            "and recalled trial IDs to allowlisted primary registries. For persistent "
            "operator configuration use EXTERNAL_REGISTRY_ACCESS_AUTHORIZED=1."
        ),
    )

    for command in ("status", "deep-jobs", "finalize"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--run-dir", required=True)
    freshness_parser = sub.add_parser("refresh-freshness")
    freshness_parser.add_argument("--run-dir", required=True)
    freshness_parser.add_argument("--portal-delta", required=True)

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--run-dir", required=True)
    merge_parser.add_argument("--decision", required=True)
    merge_parser.add_argument("--model", required=True)
    merge_parser.add_argument("--output-language", choices=("zh-CN", "en"), required=True)
    execute_parser = sub.add_parser("execute")
    execute_parser.add_argument("--run-dir", required=True)
    execute_parser.add_argument(
        "--model", default=(
            os.environ.get("MODEL_NAME")
            or os.environ.get("GATER_MODEL_NAME")
            or os.environ.get("DEEP_MODEL_NAME")
            or os.environ.get("DECISION_MODEL_NAME")
        ),
        help="Model identifier; defaults to MODEL_NAME for the API backend.",
    )
    execute_parser.add_argument("--timeout-seconds", type=float, default=1800)
    execute_parser.add_argument("--retries", type=int, default=2)

    args = parser.parse_args()
    run_dir = Path(getattr(args, "run_dir", ".")).resolve()
    if args.command == "prepare":
        result = prepare_formal(args)
    elif args.command == "status":
        result = formal_status(run_dir)
    elif args.command == "deep-jobs":
        result = create_formal_deep_jobs(run_dir)
    elif args.command == "merge":
        patient = load_json(Path(_load_state(run_dir)["patient_path"]))
        result = merge_formal(
            run_dir, Path(args.decision), args.model, args.output_language
        )
    elif args.command == "execute":
        if not args.model:
            raise ValueError("--model or MODEL_NAME is required")
        result = execute_formal(
            run_dir, model=args.model, timeout=args.timeout_seconds, retries=args.retries
        )
    elif args.command == "refresh-freshness":
        result = refresh_formal_freshness(run_dir, Path(args.portal_delta))
    else:
        result = finalize_formal(run_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
