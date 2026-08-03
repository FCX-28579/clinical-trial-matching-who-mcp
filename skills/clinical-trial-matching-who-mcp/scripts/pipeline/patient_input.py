"""Normalize legacy patient JSON or a Cancer Buddy archive into one contract."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from io_utils import load_json

NORMALIZED_SCHEMA = "clinical-trial-matching-patient-v1"
CB_REQUIRED = (
    "profile.json",
    "patient_summary.json",
    "molecular.json",
    "treatment_lines.json",
    "labs.json",
    "comorbidities.json",
)
CB_OPTIONAL = ("readiness.json", "missing_items.json")


def _settled(item: dict[str, Any]) -> bool:
    return str(item.get("verification_status") or "") != "disputed"


def _simple_results(items: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        if _settled(item) and str(item.get("label") or "").strip():
            result[str(item["label"]).strip()] = item.get("value")
    return result


def _location(profile: dict[str, Any], root: Path) -> tuple[dict[str, Any], str]:
    context_path = root / "matching_context.json"
    context = load_json(context_path) if context_path.exists() else {}
    country = str(
        context.get("country")
        or profile.get("country")
        or profile.get("patient_location_hint")
        or ""
    ).strip()
    if not country:
        raise ValueError(
            "Cancer Buddy input has no explicit patient country. Add "
            "matching_context.json with at least {\"country\": \"...\"}; "
            "country is never inferred from language, filenames, or hospitals."
        )
    return {
        "country": country,
        "city": str(context.get("city") or "unknown"),
        "patient_location": str(context.get("patient_location") or country),
        "willing_to_travel_domestic": context.get(
            "willing_to_travel_domestic", "unknown"
        ),
        "willing_to_travel_internationally": context.get(
            "willing_to_travel_internationally", "unknown"
        ),
        "travel_note": context.get("travel_note") or "",
        "affordability_tier": context.get("affordability_tier") or "unknown",
        "search_terms": context.get("search_terms") or {},
    }, str(context_path) if context_path.exists() else "profile.json"


def _validate_cancer_buddy(root: Path, documents: dict[str, dict[str, Any]]) -> str:
    missing = [name for name in CB_REQUIRED if name not in documents]
    if missing:
        raise ValueError(f"Incomplete Cancer Buddy archive; missing: {', '.join(missing)}")
    profile = documents["profile.json"]
    if profile.get("schema") != "cancer_buddy_profile_v3":
        raise ValueError("profile.json must use schema cancer_buddy_profile_v3")
    codes = {
        str(document.get("patient_code") or "").strip()
        for document in documents.values()
        if document.get("patient_code")
    }
    if len(codes) != 1:
        raise ValueError(f"Cancer Buddy patient_code mismatch in {root}: {sorted(codes)}")
    for name in CB_REQUIRED[1:]:
        if str(documents[name].get("schema_version") or "") != "2":
            raise ValueError(f"{name} must use schema_version 2")
    return next(iter(codes))


def normalize_cancer_buddy(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    documents = {
        name: load_json(root / name)
        for name in CB_REQUIRED + CB_OPTIONAL
        if (root / name).exists()
    }
    patient_code = _validate_cancer_buddy(root, documents)
    profile = documents["profile.json"]
    summary = documents["patient_summary.json"]
    molecular = documents["molecular.json"]
    treatments = documents["treatment_lines.json"]
    comorbid = documents["comorbidities.json"]
    labs = documents["labs.json"]
    readiness = documents.get("readiness.json") or {}
    demographics = summary.get("demographics") or {}
    diagnosis = summary.get("diagnosis") or {}
    current = summary.get("current_status") or {}
    location, location_source = _location(profile, root)

    variants = [
        " ".join(
            part for part in (
                str(item.get("gene") or "").strip(),
                str(item.get("variant") or "").strip(),
            ) if part
        )
        for item in molecular.get("variants") or []
        if _settled(item) and item.get("gene")
    ]
    biomarkers: dict[str, Any] = {}
    for key in ("ihc", "msi_results", "mmr_results", "tmb_results"):
        biomarkers.update(_simple_results(molecular.get(key) or []))

    episodes = treatments.get("episodes") or []
    history = [
        {
            "episode_id": item.get("episode_id"),
            "sequence_index": item.get("sequence_index"),
            "documented_line_label": item.get("documented_line_label"),
            "regimen": item.get("regimen"),
            "started_at": item.get("started_at"),
            "ended_at": item.get("ended_at"),
            "outcome": item.get("clinician_reported_response"),
            "reason_for_change": item.get("reason_for_change_source"),
            "verification_status": item.get("verification_status"),
            "source_refs": item.get("source_refs") or [],
        }
        for item in episodes
        if _settled(item)
    ]
    documented_lines = {
        str(item.get("documented_line_label")).strip()
        for item in episodes
        if _settled(item) and item.get("documented_line_label") and item.get("ended_at")
    }
    ongoing_raw = current.get("therapy_ongoing")
    if ongoing_raw is None:
        ongoing_raw = current.get("ongoing")
    if isinstance(ongoing_raw, bool):
        current_therapy_ongoing = ongoing_raw
    elif str(ongoing_raw or "").strip().casefold() in {
        "true", "yes", "ongoing", "active", "currently_receiving",
    }:
        current_therapy_ongoing = True
    elif str(ongoing_raw or "").strip().casefold() in {
        "false", "no", "ended", "stopped", "completed", "discontinued",
    }:
        current_therapy_ongoing = False
    else:
        current_therapy_ongoing = None
    lab_values: dict[str, Any] = {}
    for panel in labs.get("panels") or []:
        values = [
            value for value in panel.get("values") or []
            if _settled(value)
        ]
        if values:
            latest = sorted(values, key=lambda value: str(value.get("date") or ""))[-1]
            lab_values[str(panel.get("normalized_analyte") or panel.get("analyte"))] = {
                key: latest.get(key)
                for key in ("date", "value", "raw_value", "unit", "reference_range")
            }

    disputed = []
    for source_name, rows in (
        ("molecular.variants", molecular.get("variants") or []),
        ("comorbidities.conditions", comorbid.get("conditions") or []),
        ("comorbidities.medications", comorbid.get("medications") or []),
        ("comorbidities.allergies", comorbid.get("allergies") or []),
    ):
        if any(not _settled(item) for item in rows):
            disputed.append(source_name)
    unresolved_review_flags = [
        flag for flag in readiness.get("review_flags") or []
        if flag.get("resolution_status") == "unresolved"
    ]
    disputed.extend(
        str(flag.get("affected_field") or flag.get("category") or "unknown")
        for flag in unresolved_review_flags
    )
    disputed = list(dict.fromkeys(disputed))

    patient = {
        "schema_version": NORMALIZED_SCHEMA,
        "patient_id": patient_code,
        "report_language": (
            "zh-CN" if str(profile.get("locale") or "").lower().startswith("zh") else "en"
        ),
        **location,
        "sex": demographics.get("sex_normalized") or demographics.get("sex"),
        "age": demographics.get("age"),
        "height_cm": demographics.get("height_cm"),
        "weight_kg": demographics.get("weight_kg"),
        "ecog": current.get("ecog") if current.get("ecog") is not None else demographics.get("ecog"),
        "cancer_type": diagnosis.get("primary"),
        "histology": diagnosis.get("histology"),
        "stage": diagnosis.get("stage"),
        "disease_stage": diagnosis.get("stage"),
        "metastasis_sites": diagnosis.get("metastasis_sites") or [],
        "mutations": variants,
        "biomarkers_known": biomarkers,
        "treatment_lines_completed": len(documented_lines) if documented_lines else None,
        "prior_therapies": [item.get("regimen") for item in history if item.get("regimen")],
        "treatment_history": history,
        "current_therapy_status": current.get("regimen"),
        # Regimen presence is not proof that treatment is still ongoing.
        "current_therapy_ongoing": current_therapy_ongoing,
        "organ_function": "source_values_available" if lab_values else "unknown",
        "laboratory_values": lab_values,
        "comorbidities": [
            item.get("name") for item in comorbid.get("conditions") or []
            if _settled(item) and item.get("name")
        ],
        "current_medications": [
            item.get("normalized_name") or item.get("name")
            for item in comorbid.get("medications") or []
            if _settled(item)
            and item.get("use_status") in {"active_confirmed", "active_reported"}
        ],
        "allergies": [
            item.get("allergen") for item in comorbid.get("allergies") or []
            if _settled(item) and item.get("allergen")
        ],
        "missing_critical_information": [
            f"Disputed source field: {name}" for name in disputed
        ],
        "source_review_flags": unresolved_review_flags,
        "documentation_coverage": readiness.get("documentation_coverage") or {},
        "source_contract": {
            "schema": "cancer_buddy_profile_v3",
            "schema_version": "2",
            "archive": str(root.resolve()),
        },
    }
    required = ("patient_id", "country", "cancer_type")
    missing_required = [key for key in required if not patient.get(key)]
    if missing_required:
        raise ValueError(
            "Cancer Buddy archive lacks matching-critical settled fields: "
            + ", ".join(missing_required)
        )
    audit = {
        "input_type": "cancer_buddy_archive",
        "source_path": str(root.resolve()),
        "patient_code": patient_code,
        "location_source": location_source,
        "disputed_sections": disputed,
        "unresolved_review_flag_count": len(unresolved_review_flags),
        "treatment_line_policy": (
            "Count only completed episodes with an explicit documented_line_label."
        ),
    }
    return patient, audit


def load_patient_input(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if path.is_dir():
        return normalize_cancer_buddy(path)
    patient = load_json(path)
    return patient, {
        "input_type": "legacy_flat_json",
        "source_path": str(path.resolve()),
        "schema_version": patient.get("schema_version") or "legacy-unversioned",
    }
