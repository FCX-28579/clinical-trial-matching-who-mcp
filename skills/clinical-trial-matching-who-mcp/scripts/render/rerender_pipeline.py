"""Re-render an existing pipeline artifact with current deterministic presentation rules."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
for directory in (
    SCRIPT_ROOT / "classification",
    SCRIPT_ROOT / "pipeline",
    SCRIPT_ROOT / "presentation",
    SCRIPT_ROOT / "render",
):
    sys.path.insert(0, str(directory))

from analysis_contract import report_language
from decision_grounding import ground_decision_report
from html_renderer import render_html, render_validation_html
from mechanism_categories import classify_mechanism
from registry_presentation import (
    assess_country_evidence,
    patient_facing_title,
    resolved_trial_url,
)


def refresh_presentational_fields(
    payload: dict[str, Any], *, language_override: str | None = None,
) -> dict[str, Any]:
    """Refresh only deterministic fields; preserve clinical/model provenance."""
    refreshed = json.loads(json.dumps(payload, ensure_ascii=False))
    patient = refreshed.get("patient") or {}
    if language_override:
        if language_override not in {"zh-CN", "en"}:
            raise ValueError("language_override must be zh-CN or en")
        patient["report_language"] = language_override
    language = report_language(patient)
    refreshed["language"] = language

    counts: collections.Counter[str] = collections.Counter()
    geography: collections.Counter[str] = collections.Counter()
    for trial in refreshed.get("trials") or []:
        gating = trial.get("gating") or {}
        counts[gating.get("verdict") or "conditional"] += 1
        analysis = (
            {"efficacy_context": trial["efficacy_context_detail"]}
            if isinstance(trial.get("efficacy_context_detail"), dict)
            else None
        )
        trial["mechanism_category"] = classify_mechanism(
            trial, analysis=analysis, patient=patient
        )
        trial["country_assessment"] = assess_country_evidence(trial, patient)
        trial["resolved_source_url"] = resolved_trial_url(trial)
        trial["display_title"] = patient_facing_title(trial, language)
        geography[trial["country_assessment"]["class"]] += 1

    decision_sources = []
    for trial in refreshed.get("trials") or []:
        decision_sources.append({
            **trial,
            "trial_id": trial.get("id"),
            "title": trial.get("display_title") or trial.get("title"),
            "risk_summary": {"risks": trial.get("risks") or []},
            "efficacy_summary": trial.get("efficacy_context_detail") or {},
        })
    if isinstance(refreshed.get("decision_report"), dict):
        refreshed["decision_report"] = ground_decision_report(
            refreshed["decision_report"], decision_sources,
            patient=patient, language=language,
        )

    refreshed["counts"] = {
        name: counts.get(name, 0) for name in ("match", "conditional", "exclude")
    }
    refreshed["geography_audit"] = dict(geography)
    refreshed["presentation_provenance"] = {
        "mode": "deterministic_rerender",
        "completed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "clinical_analysis_preserved": True,
    }
    return refreshed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-render a pipeline artifact without rerunning clinical model analysis"
    )
    parser.add_argument("--pipeline", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--report-language", choices=("zh-CN", "en"))
    args = parser.parse_args()

    source = Path(args.pipeline)
    payload = refresh_presentational_fields(
        json.loads(source.read_text(encoding="utf-8-sig")),
        language_override=args.report_language,
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # Only full_pipeline.finalize may issue a formal report. A presentation-only
    # rerender cannot re-prove freshness or whole-run coverage.
    payload["formal_report_ready"] = False
    payload["report_mode"] = "validation"
    (out / "pipeline.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "report.html").unlink(missing_ok=True)
    render_validation_html(payload, out / "validation-report.html")
    print(json.dumps({
        "output": str(out),
        "formal_report_ready": bool(payload.get("formal_report_ready")),
        "recall": len(payload.get("trials") or []),
        "counts": payload["counts"],
        "geography_audit": payload["geography_audit"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
