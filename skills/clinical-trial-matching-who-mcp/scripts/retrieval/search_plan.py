"""Search-plan structure and eight-branch recall coverage contract."""
from __future__ import annotations

from copy import deepcopy
import os
import re
from typing import Any

REQUIRED_DIMENSIONS = (
    "disease_biomarker", "pan_tumor", "combination_targets", "pathway_resistance",
    "named_drug", "cell_therapy", "immune", "chinese_registry_terms",
)
DIMENSION_HINTS = {
    "disease_biomarker": ("疾病", "突变特异", "disease", "exact"),
    "pan_tumor": ("泛化", "泛实体瘤", "pan-tumor", "pan tumor", "all comers"),
    "combination_targets": ("联合靶点", "combination", "egfr/shp2", "egfr", "shp2"),
    "pathway_resistance": ("通路", "耐药", "pathway", "resistance", "pan-kras", "ras-on"),
    "named_drug": ("具体药物", "药物名", "named drug", "drug names"),
    "cell_therapy": ("细胞治疗", "cell therapy", "car-t", "tcr-t", "til"),
    "immune": ("免疫", "immune", "checkpoint", "pd-1"),
    "chinese_registry_terms": ("中文", "chictr", "中国注册"),
}


def _dimension(group: dict[str, Any]) -> str:
    explicit = str(group.get("dimension") or "").strip()
    if explicit in REQUIRED_DIMENSIONS:
        return explicit
    label = str(group.get("label") or "").casefold()
    for dimension, hints in DIMENSION_HINTS.items():
        if any(hint.casefold() in label for hint in hints):
            return dimension
    return ""


def search_plan_coverage(plan: dict[str, Any]) -> dict[str, Any]:
    present = list(dict.fromkeys(_dimension(group) for group in plan.get("keyword_groups") or [] if _dimension(group)))
    return {
        "required": list(REQUIRED_DIMENSIONS),
        "present": present,
        "missing": [dimension for dimension in REQUIRED_DIMENSIONS if dimension not in present],
        "unclassified_groups": [
            str(group.get("label") or "") for group in plan.get("keyword_groups") or [] if not _dimension(group)
        ],
    }


def validate_search_plan(plan: dict[str, Any], *, require_full_coverage: bool = True) -> list[str]:
    errors: list[str] = []
    groups = plan.get("keyword_groups") or []
    if not groups:
        return ["keyword_groups is required"]
    for index, group in enumerate(groups):
        if not group.get("label"):
            errors.append(f"keyword_groups[{index}].label is required")
        queries = group.get("queries") or []
        if not queries:
            errors.append(f"keyword_groups[{index}].queries is empty")
        for query_index, query in enumerate(queries):
            condition = str(query.get("condition") or "").strip()
            term = str(query.get("term") or "").strip()
            if not condition and not term:
                errors.append(f"keyword_groups[{index}].queries[{query_index}] has no condition or term")
            elif condition and not term:
                errors.append(
                    f"keyword_groups[{index}].queries[{query_index}] is condition-only; "
                    "formal recall requires a biomarker, mechanism, intervention, or modality anchor"
                )
            elif condition.casefold() == term.casefold():
                errors.append(
                    f"keyword_groups[{index}].queries[{query_index}] repeats the same "
                    "disease-only text in condition and term"
                )
    if require_full_coverage:
        coverage = search_plan_coverage(plan)
        if coverage["missing"]:
            errors.append("missing required recall dimensions: " + ", ".join(coverage["missing"]))
    return errors


def validate_search_plan_for_patient(
    plan: dict[str, Any], patient: dict[str, Any]
) -> list[str]:
    """Reject patient-disease-only queries that create unbounded formal recall."""
    cancer = re.sub(
        r"\s+", " ", str(patient.get("cancer_type") or "").strip().casefold()
    )
    if not cancer:
        return []
    errors: list[str] = []
    for group_index, group in enumerate(plan.get("keyword_groups") or []):
        for query_index, query in enumerate(group.get("queries") or []):
            condition = re.sub(
                r"\s+", " ", str(query.get("condition") or "").strip().casefold()
            )
            term = re.sub(
                r"\s+", " ", str(query.get("term") or "").strip().casefold()
            )
            if term == cancer and not condition:
                errors.append(
                    f"keyword_groups[{group_index}].queries[{query_index}] is a "
                    "patient-disease-only query"
                )
    return errors


def compile_search_plan_for_mcp(plan: dict[str, Any]) -> dict[str, Any]:
    """Compile each disease + concept pair into one conjunctive FTS query."""
    compiled = deepcopy(plan)
    transformed = 0
    for group in compiled.get("keyword_groups") or []:
        for query in group.get("queries") or []:
            condition = str(query.get("condition") or "").strip()
            term = str(query.get("term") or "").strip()
            if condition and term:
                query["original_condition"] = condition
                query["original_term"] = term
                query["condition"] = None
                query["term"] = f"{condition} {term}"
                query["query_semantics"] = "compiled_conjunctive_anchor"
                transformed += 1
    compiled["execution_audit"] = {
        "compiler": "conjunctive-search-plan-v1",
        "transformed_query_count": transformed,
        "purpose": (
            "Prevent condition-only OR recall by requiring disease and concept "
            "anchors in the same MCP FTS query."
        ),
    }
    return compiled


def generate_search_plan_prompt(patient_text: str) -> str:
    return f"""Create a strict JSON clinical-trial search plan for this oncology patient.
Preserve all eight original recall branches. Every keyword group must include a `dimension`
using exactly one of these values:
1. disease_biomarker — disease plus exact biomarker;
2. pan_tumor — pan-solid-tumor plus biomarker;
3. combination_targets — rational combinations such as EGFR/SHP2/SOS1;
4. pathway_resistance — pathway and resistance strategies;
5. named_drug — exhaustive approved and investigational drug names and aliases;
6. cell_therapy — cell and biologic therapies independent of the mutation;
7. immune — immune strategies appropriate to MSI/MSS and prior checkpoint exposure;
8. chinese_registry_terms — Chinese registry terms, including single-token alternatives.
Each group must contain a label and queries with condition and term. Include treatment_lines
and hard_exclude.first_line_only plus explicit molecular mismatch rules. Do not filter by country;
country is applied later for domestic/international reporting.

Patient:
{patient_text}

Return JSON only."""


def build_baseline_search_plan(patient: dict[str, Any]) -> dict[str, Any]:
    """Build a cancer-agnostic eight-branch baseline when no plan is supplied."""
    cancer = str(patient.get("cancer_type") or "").strip()
    stage = str(patient.get("disease_stage") or patient.get("stage") or "").strip()
    mutations = [
        str(value).strip() for value in patient.get("mutations") or []
        if str(value).strip()
    ]
    search_terms = patient.get("search_terms") or {}
    named_agents = [
        str(value).strip() for value in search_terms.get("named_agents") or []
        if str(value).strip()
    ]
    combinations = [
        str(value).strip() for value in search_terms.get("combination_targets") or []
        if str(value).strip()
    ]
    pathways = [
        str(value).strip() for value in search_terms.get("pathway_terms") or []
        if str(value).strip()
    ]
    chinese_terms = [
        str(value).strip() for value in search_terms.get("chinese_terms") or []
        if str(value).strip()
    ]
    biomarker_terms = mutations or [
        str(key).strip() for key, value in (patient.get("biomarkers_known") or {}).items()
        if value not in (None, "", "unknown")
    ]
    anchor = biomarker_terms[0] if biomarker_terms else "precision oncology"

    try:
        max_terms = max(1, min(20, int(os.environ.get(
            "SEARCH_MAX_TERMS_PER_DIMENSION", "6"
        ))))
    except ValueError as exc:
        raise ValueError("SEARCH_MAX_TERMS_PER_DIMENSION must be an integer") from exc

    def queries(terms: list[str], condition: str | None = cancer) -> list[dict[str, Any]]:
        unique: list[str] = []
        seen: set[str] = set()
        for value in terms:
            term = re.sub(r"\s+", " ", str(value or "")).strip()
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                unique.append(term)
        return [
            {"condition": condition or None, "term": term}
            for term in unique[:max_terms]
        ]

    groups = [
        {
            "dimension": "disease_biomarker", "label": "1. Disease + exact biomarker",
            "source": "both",
            "queries": queries(biomarker_terms or ["precision oncology"]),
        },
        {
            "dimension": "pan_tumor", "label": "2. Pan-tumor biomarker recall",
            "source": "both",
            "queries": queries(biomarker_terms or ["precision oncology"], "solid tumor"),
        },
        {
            "dimension": "combination_targets", "label": "3. Rational combination targets",
            "source": "both",
            "queries": queries(combinations or [f"{anchor} combination therapy"]),
        },
        {
            "dimension": "pathway_resistance", "label": "4. Pathway and resistance",
            "source": "both",
            "queries": queries(pathways or [f"{anchor} pathway resistance"]),
        },
        {
            "dimension": "named_drug", "label": "5. Named agents and aliases",
            "source": "both",
            "queries": queries(named_agents or [f"{anchor} inhibitor"]),
        },
        {
            "dimension": "cell_therapy", "label": "6. Cell and biologic therapy",
            "source": "both",
            "queries": queries(["CAR-T", "TCR-T", "TIL", "cancer vaccine"]),
        },
        {
            "dimension": "immune", "label": "7. Immune strategies",
            "source": "both",
            "queries": queries(["immunotherapy", "checkpoint inhibitor", "bispecific"]),
        },
        {
            "dimension": "chinese_registry_terms", "label": "8. Chinese registry terms",
            "source": "chictr",
            "queries": queries(
                chinese_terms or [f"{cancer} {anchor}"], None
            ),
        },
    ]
    return {
        "schema_version": "clinical-search-plan-v1",
        "patient_id": patient.get("patient_id"),
        "patient_summary": "Deterministic baseline generated from normalized patient facts.",
        "treatment_lines": patient.get("treatment_lines_completed"),
        "disease_stage_filter": stage,
        "keyword_groups": groups,
        "hard_exclude": {
            "first_line_only": False,
            "molecular_mismatch": [],
        },
        "generation_audit": {
            "mode": "deterministic_baseline",
            "max_terms_per_dimension": max_terms,
            "query_count": sum(len(group["queries"]) for group in groups),
            "requires_human_review": not bool(named_agents and combinations and pathways),
            "limitations": (
                "Named-agent, rational-combination, pathway, and Chinese synonym "
                "recall is only exhaustive when matching_context.search_terms supplies them."
            ),
        },
    }
