---
name: decision-synthesizer
description: Use when synthesizing per-trial gating + risk + efficacy outputs into a Top-N decision report with diversity bucketing, Goals-of-Care trigger, and vs-SoC head-to-head. Triggers as the final synthesis step in clinical-trial-matching after trial-gater + trial-risk-annotator + trial-efficacy-contextualizer have run. Replaces v1.7.x synthesis/decision_paths.py + goals_of_care.py + consistency_check.py.
---

# decision-synthesizer

You produce the final `decision_report.json` that drives the patient-facing HTML report. You read the aggregated per-trial analysis (gating + risk + efficacy) plus the patient profile, and emit a structured report with Top-N decision paths, Goals-of-Care section (when triggered), and patient consistency flags.

The executor is authoritative for eligibility verdicts, efficacy snapshots, risks,
blockers, and publication identity. Do not alter those fields. A `conditional`
trial is a verification path, not a treatment recommendation. Do not describe
cross-trial comparisons as superiority unless the supplied evidence is a direct
head-to-head comparison. Do not invent screening dates, first-dose dates, site
slots, travel time, or manufacturing time.

## The bugs this subskill exists to fix

v1.7.x had three Python modules that all had quality issues:

1. **`decision_paths.py`** — produced empty `patient_summary={}`, `feasibility_score=None` for paths despite the synthesizer printing correct values to stdout. JSON serialization broke. Also picked a KRAS G12D drug class trial as #3 path for a KRAS G12C patient because the diversity bucket forced "alternative mechanism" without checking class compatibility.
2. **`goals_of_care.py`** — used `treatment_lines_completed` (which excludes ongoing line) to compare against ontology thresholds. A patient on L3 with `treatment_lines_completed=2` never triggered GoC even when ontology said CRC L3+ is GoC-relevant.
3. **`consistency_check.py`** — emitted empty consistency_flags for patients with severe comorbidity loads, conflicting treatment timelines, or unusual responses (e.g. PR after multiple progressions).

## Inputs

```json
{
  "patient": { /* full patient.json */ },
  "analyzed_trials": [
    {
      "trial_id": "NCT...",
      "title": "...",
      "phases": [...],
      "sponsor": "...",
      "interventions": [...],
      "patient_country_sites": [...],
      "patient_country_site_count": 0,
      "country_assessment": {...},
      "feasibility": {"composite": 0.961, "sub_scores": {...}},
      "gating": { ...trial-gater output },
      "risks": [ ...trial-risk-annotator output ],
      "efficacy_context": { ...trial-efficacy-contextualizer output }
    }
  ]
}
```

## Output

```json
{
  "report_version": "v2.0.0",
  "generated_at": "2026-05-07T12:34:56Z",

  "patient_summary": {
    "summary_text": "string",
    "cancer_type": "CRC",
    "mutations": ["KRAS G12C"],
    "treatment_lines_completed": 2,
    "current_therapy_ongoing": true,
    "key_comorbidities": [...]
  },

  "consistency_flags": [
    {
      "flag": "string description",
      "severity": "info | warn | alert",
      "evidence": "what in the patient profile triggered this"
    }
  ],

  "goals_of_care": {
    "triggered": true,
    "reasons": [
      "CRC L3 progression on prior FOLFOX/KELOX+IO+Apatinib; entering L4",
      "Median OS at L3+ mCRC per ontology: ~6 months",
      "Multiple serious comorbidities constrain future regimen tolerability"
    ],
    "discussion_recommendation": "Recommend formal Goals-of-Care discussion before initiating next line. Frame trial enrollment as one option among several (palliative care, hospice, comfort-focused care, continuation of cytotoxic SoC). Patient and family should hear realistic prognosis at L4 mCRC (median OS ~6mo) before committing to trial logistics (cross-city travel, screening burden, possible Phase 1 sub-therapeutic dosing)."
  },

  "decision_paths": [
    {
      "rank": 1,
      "role": "primary | secondary | secondary_overseas | secondary_cell_therapy | alternative_mechanism",
      "trial_id": "NCT...",
      "trial_title": "...",
      "sponsor": "...",
      "phase": "PHASE2",
      "patient_country_site_count": 11,
      "country_assessment": {...},
      "feasibility": {"composite": 0.961, "sub_scores": {...}},
      "rationale": "string",
      "efficacy_snapshot": {...from contextualizer},
      "vs_soc": {...from contextualizer},
      "risks": [...from annotator],
      "blockers_satisfied": [...],
      "blockers_pending": [...],
      "alternatives_comparison": [
        {"trial_id": "NCT...", "reason_not_chosen": "..."}
      ],
      "consequences_of_skipping": "string — what happens if patient doesn't pursue this path",
      "estimated_timeline": {
        "screening_window": null,
        "earliest_first_dose": null,
        "critical_path_steps": ["string", ...]
      }
    }
  ],

  "soc_benchmarks": [
    {
      "regimen": "string",
      "expected_orr": 0.26,
      "expected_pfs_months": 5.6,
      "evidence": "trial name"
    }
  ],

  "match_inventory_size": {"match": 30, "conditional": 15, "exclude": 49},

  "v2_summary": {
    "total_trials_analyzed": 45,
    "verified_real_nct_ids": 45,
    "decision_paths_emitted": 3,
    "goc_triggered": true,
    "consistency_flags_count": 2,
    "redundancy_flags_count": 0
  }
}
```

## Process

### Step 1 — Patient summary

Echo the patient summary from `patient.json` plus 1-paragraph synthesis.

### Step 2 — Consistency check

Look for internal inconsistencies in the patient profile:

- Treatment timeline gaps (e.g. "L1 ended 2023-01, L2 started 2023-08" — gap of 7 months unexplained?)
- Response classification mismatches (e.g. "L1 PR" but baseline cancer was metastatic at diagnosis — was it really PR or just stable?)
- Comorbidity vs treatment compatibility (e.g. severe CV disease + recent anti-angiogenic without mention of cardiac monitoring)
- Lab value anomalies vs treatment context (e.g. TSH up + on PD-1 drug — irAE? was it managed?)
- Missing standard workup (e.g. CRC patient without recent CEA, or without HER2/MMR results)

Emit `consistency_flags` as an array. Severity: `info` (note for completeness), `warn` (clarify before proceeding), `alert` (must resolve before any treatment decision).

### Step 3 — Goals-of-Care trigger

Trigger GoC discussion if ANY of:

1. Patient is on or progressed past Nth line where ontology median OS < threshold months. Use `clinical-trial-matching/data/clinical_ontology.json:cancers.{X}.median_os_months_at_line` for thresholds; default threshold 6 months.
   - **Critical fix from v1.7.x**: count current ongoing line in this calculation. If `patient.current_therapy_ongoing == true`, evaluate as if patient is at `treatment_lines_completed + 1` for ontology lookup.
2. Patient has rapid progression (≤4 cycles) on most recent regimen
3. Patient ECOG ≥2 OR KPS ≤60
4. Patient has significant cumulative comorbidity burden (≥3 serious comorbidities, or any single life-limiting one)
5. Patient is in Phase 1 dose-escalation only options with no Phase 2/3 alternatives

If any trigger fires, emit `goals_of_care.triggered = true` with `reasons` and a `discussion_recommendation` paragraph.

### Step 4 — Top-N decision paths (global patient-fit ranking)

Select up to N paths (default N=3) by patient fit. Three is a maximum, not a quota:

```
Step 4a — Filter
  - Drop verdict=exclude
  - Keep verdict=match and verdict=conditional
  - Drop known biomarker/cohort mismatches and trials with failed blockers
  - Drop inactive studies from the recommendation list
  - Drop trials whose core study drug overlaps the patient's ongoing regimen;
    they may remain in the audit trail as verification options

Step 4b — Rank lexicographically
  1. exact disease, cohort and biomarker fit
  2. match before conditional; fewer unresolved eligibility blockers
  3. compatibility with the current treatment line and prior therapies
  4. recruiting status and accessible patient-country sites
  5. trial-specific evidence quality
  6. operational feasibility

Step 4c — Near-duplicate suppression
  Select the globally highest-ranked candidates without reserving geographic or
  mechanism slots. Skip a later candidate when it is an explicitly flagged
  duplicate, uses the same core study agent, or has a nearly identical
  intervention set to an already selected path. Do not suppress distinct
  modalities merely because they share a disease or molecular target.

Step 4d — Per-path narrative
  For each chosen path:
  - role
  - rationale
  - efficacy_snapshot (from contextualizer)
  - vs_soc (from contextualizer)
  - risks (from annotator) — REQUIRED to be cancer-context-grounded
  - blockers_satisfied / blockers_pending (from gater)
  - alternatives_comparison: 1-2 similar trials with reason for non-selection
  - consequences_of_skipping: 1-2 sentence narrative
  - critical screening steps only; dates remain null until a site confirms them
```

### Step 5 — SoC benchmarks (cross-cutting, not per-path)

Pull the patient's relevant SoC options from any of the path's vs_soc data, dedupe, list as a top-level `soc_benchmarks` array. This drives the report's "标准治疗对照" section.

### Step 6 — Cross-validation guardrails

Before emitting, verify:

1. Every path's risks pass the cancer-context grounding check (no PDAC narrative on CRC paths, etc.)
2. Every path's efficacy_snapshot.applies_because actually applies to this patient's mutation
3. No path with R2-hard, R3-hard, or hard exclusion criteria slipped through
4. If `goc_triggered=true` and Top 3 paths are all Phase 1 dose escalation, add a flag: "All recommended paths are early-phase; review GoC discussion carefully"
5. If `redundancy_flags_count > 0`, ensure each redundant trial's path narrative mentions the off-trial alternative (e.g. "Sotorasib + panitumumab is FDA-approved for this indication and may be accessible without trial enrollment")

## Output schema

See [`rules/output-decision-report-schema.md`](rules/output-decision-report-schema.md) for full JSON contract.

## Reference

- [Diversity bucketing logic](rules/synthesis-diversity-bucketing.md)
- [Goals of Care trigger](rules/synthesis-goals-of-care-trigger.md)
- [Output schema](rules/output-decision-report-schema.md)
