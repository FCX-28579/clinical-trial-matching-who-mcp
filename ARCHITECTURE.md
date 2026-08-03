# Architecture

The formal workflow has one stateful entry point:

```text
Cancer Buddy archive or legacy patient JSON
-> patient_input.py
-> normalized-patient.json
-> WHO MCP retrieval + WHO portal delta
-> WHO verifier/deduplicator
-> direct registry verification
-> generic structured hard rules
-> trial-gater
-> publication prefetch
-> risk + efficacy deep analysis
-> evidence_grounding.py
-> decision-synthesizer
-> decision_grounding.py
-> full_pipeline.py finalize
-> country-based report language routing
-> provider-neutral patient-facing translation (China only)
-> formal report + run manifest
```

## Module ownership

| Area | Owner | Responsibility |
|---|---|---|
| Patient input | `pipeline/patient_input.py` | Detect legacy JSON or Cancer Buddy archive, preserve unknown/disputed facts, emit one normalized patient snapshot |
| Search plan | `retrieval/search_plan.py` | Validate a supplied eight-dimensional plan or generate an auditable baseline with explicit recall limitations |
| Retrieval | `retrieval/` | Execute the eight-dimensional plan through MCP and collect the WHO registration-date delta |
| Registry truth | `verification/` | Enrich, deduplicate, separate country records from named sites, and attempt direct live-status checks |
| Deterministic triage | `pipeline/generic_hard_rules.py` | Exclude only explicit structured contradictions |
| Clinical contracts | `pipeline/analysis_contract.py` | Define stage payloads and validate gater/deep outputs |
| Model transport | `pipeline/model_api_runner.py`, `cli_model_runner.py` | Call one configured model backend |
| Batch reliability | `pipeline/model_batch_executor.py` | Exact ID coverage, retry, quarantine, split recovery, circuit breaking |
| Publication retrieval | `pipeline/publication_prefetch.py` | Fetch and cache auditable Europe PMC candidates |
| Evidence authority | `pipeline/evidence_grounding.py` | Reject model-added publications and restore publication identity from prefetch records |
| Decision authority | `pipeline/decision_grounding.py` | Restore eligibility, efficacy, risk, blockers, and timeline limits from validated upstream data |
| State machine | `pipeline/run_formal_pipeline.py` | Enforce stage order and resume a formal run |
| Final quality gate | `pipeline/full_pipeline.py` | Enforce coverage, retrieval, freshness, and produce the only formal report |
| Report translation | `render/report_translation.py` | Translate only China-patient narratives through configured model APIs while preserving identifiers and clinical decisions |
| Presentation | `presentation/`, `render/` | Titles, links, geography grouping, mechanism sections, HTML |

The retrieval boundary compiles every disease condition and its
biomarker/mechanism/modality term into one conjunctive FTS query. Formal plans
reject patient-disease-only queries. Post-detail deduplication merges only
authoritative registry-ID bridges; title/intervention lookalikes remain
separate with an audit flag.

## Formal invariants

1. Every recalled ID is either deterministically excluded or receives one gater result.
2. Every `match` or `conditional` ID receives risk, efficacy, and publication-search output.
3. Every displayed publication resolves to a deterministic prefetch candidate.
4. Every recalled trial receives a direct-registry verification attempt.
5. A current WHO portal delta does not substitute for direct status verification.
6. Only `full_pipeline.py finalize` may produce the formal report template.
7. Unknown or disputed patient facts remain unknown and are never inferred from language, filenames, or treatment ordering.
