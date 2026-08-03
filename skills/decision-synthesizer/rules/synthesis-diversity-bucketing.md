# Patient-fit ranking for Top-N decision paths

## Goal

Recommend the trials that best fit this patient. Diversity is useful only as a tie-breaker and must never displace a more suitable trial.

## Ranking

Default N=3 is a maximum, not a quota. Filter known molecular/cohort mismatches, failed blockers, inactive studies, and overlap with the patient's ongoing core therapy. Then rank every remaining trial together by exact disease/cohort/biomarker fit, gating verdict and unresolved blockers, treatment-line compatibility, recruiting access, evidence quality, and finally feasibility. Do not reserve geographic or mechanism slots. Return fewer than N paths if necessary.

Do not collapse clinical fit into a weighted composite score: a high site or feasibility score cannot compensate for the wrong cohort, biomarker, treatment line, or ongoing-drug overlap.

## Mechanism class tags (for diversity grouping)

Use the patient's mutation/biology to tag mechanisms:

- `kras_g12c_inhibitor`
- `kras_g12d_inhibitor`
- `pan_ras_inhibitor`
- `kras_inhibitor_combo_with_anti_egfr`
- `cell_therapy_car_t`
- `cell_therapy_til`
- `cell_therapy_tcr_t`
- `cell_therapy_cik_dc_cik`
- `bispecific_antibody`
- `adc` (antibody-drug conjugate)
- `chemo_combo` (chemotherapy backbone with novel partner)
- `immune_checkpoint_inhibitor`
- `radioconjugate`

Use these tags only to explain the ranking. Suppress an explicitly linked duplicate, the same core study agent, or a nearly identical intervention set; do not suppress distinct trials merely because they share a mechanism or target.

## Anti-pattern: forcing diversity at the cost of fit

**Critical bug from v1.7.x**: the diversity bucketing forced a "secondary" slot to be filled with NCT06895031 (JYP0015, KRAS G12D drug applied to a G12C patient) just because it was the next-best "different mechanism". The drug was wrong for the patient.

**Current rule**: every recommended candidate MUST pass these checks:

1. Trial mutation requirement matches patient mutation (or is mutation-agnostic)
2. Trial drug class is appropriate for patient mutation (G12C drugs for G12C patients; G12D drugs for G12D patients; pan-RAS / mutation-agnostic drugs OK for any RAS-mutant)
3. Patient has not already failed an equivalent regimen in the same drug class

If fewer than three non-duplicate candidates pass, return fewer than three recommendations.

## Path-vs-path comparison narrative

For each chosen path, emit `alternatives_comparison`: 1-2 nearest similar trials NOT chosen, with explicit reason. This serves the "为什么选这条 ≠ 选 X / Y" block in the report.

```json
"alternatives_comparison": [
  {
    "trial_id": "NCT05410145",
    "trial_title": "D3S-001 mono/combo in KRAS G12C solid tumors",
    "reason_not_chosen": "feasibility 0.881 < 0.961 of chosen path; only 16 China sites vs chosen path's 11 — wait, 16 > 11. Re-evaluate. Actually D3S-001's 16 China sites is MORE — the choice should explain why the chosen path was preferred (e.g. trial-specific evidence tier higher for MK-1084 due to recent ESMO readout; or alternative composition/safety profile)."
  }
]
```

Note: this is one of the v1.7.x bugs — alternatives_comparison was sometimes self-contradicting. v2 must verify the comparison narrative is consistent with the metrics.

## Empty slot handling

```json
"decision_paths": [
  { ...slot 1 path... },
  { ...slot 2 path... },
  null  // no third candidate meets the recommendation threshold
]
```

Or use length-2 array if N=3 was requested but only 2 qualifying. The HTML renderer should handle gracefully (don't show empty card).
