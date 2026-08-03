---
name: clinical-trial-matching-who-mcp
description: Use for generic multi-cancer clinical-trial matching through the WHO ICTRP MCP database while preserving the original model-executed gating, risk, efficacy and decision subskills.
---

# Generic WHO MCP clinical-trial matching

This skill changes the retrieval and registry-verification boundary. It does not replace the original clinical reasoning subskills with Python keyword rules.

## Non-negotiable architecture

`patient structure + original eight-dimensional plan` -> `real WHO MCP + optional authorized WHO registration-date delta` -> `one verifier/deduplicator` -> `direct-registry live status verification` -> `registry-status exclusion + generic structured hard-rule triage` -> `deterministic all-batch model executor` -> `trial-gater for every remaining trial` -> `risk/efficacy/evidence only for match or conditional` -> `decision-synthesizer` -> `mechanism classifier` -> `one patient-report renderer`.

A formal report has two executable stages separated by model work:

1. `full_pipeline.py prepare` calls the MCP server, reads its watermark, optionally validates an authorized portal delta, calls `get_trial`, deduplicates, attempts direct-registry status verification, applies conservative deterministic exclusions, and writes `analysis_jobs.json`. Portal crawling is off unless explicitly selected by the operator.
2. `run_formal_pipeline.py execute` enumerates every unfinished batch and invokes the selected API, agent-CLI, or custom command backend. It validates exact ID coverage, retries failures, resumes existing valid outputs, creates deep jobs only after complete gating, and runs the decision skill once after complete deep analysis.
3. `full_pipeline.py finalize` validates every subskill contract and renders only after complete coverage. It rejects heuristic/example analysis and missing trial outputs.

The canonical subskills are sibling directories, without `-who-mcp` forks:

- `../trial-gater/SKILL.md`
- `../trial-risk-annotator/SKILL.md`
- `../trial-efficacy-contextualizer/SKILL.md`
- `../decision-synthesizer/SKILL.md`

## Patient and search plan

Preserve the original structured patient fields, including cancer type, stage, biomarkers, molecular variants, treatment lines, current treatment, prior therapies/classes, performance status, organ function, comorbidities, country, city, travel willingness and affordability.

`--patient` accepts either the legacy flat patient JSON or a Cancer Buddy
`patients/<patient_code>/` directory. Directory inputs are normalized by
`patient_input.py` into `normalized-patient.json`. Structured diagnosis comes
from `patient_summary.json`; variants and biomarkers come from `molecular.json`.
Never infer country from locale, filenames, or hospitals. Cancer Buddy input
therefore requires an explicit `matching_context.json`.

The search plan must contain all original dimensions:

1. disease plus exact biomarker;
2. pan-tumor biomarker recall;
3. rational combination targets;
4. pathway and resistance strategies;
5. named approved/investigational agents;
6. cell and biologic therapy;
7. immune strategies;
8. patient-country and relevant regional registry terms.

Do not filter the first-pass recall by patient country.
Every formal query must include a biomarker, mechanism, intervention, drug, or
modality anchor. Before execution, the transport compiles `condition + term`
into one conjunctive MCP FTS query. Patient-disease-only queries are rejected
because they expand cost without demonstrating patient-specific relevance.

## MCP retrieval and verification

Use the real stdio MCP tools `database_metadata`, `execute_search_plan`, and `get_trial`. Persist `database_as_of`, MCP protocol/server metadata, query audit, pagination and truncation fields.

`who_mcp_verifier.py` is the only final deduplication authority. It uses
canonical registry IDs, WHO universal trial numbers, and normalized CTIS IDs
with transitive-set merging. Generic secondary protocol strings, shared titles,
interventions, and sponsors are never sufficient for automatic merging; such
lookalikes remain separate and receive an auditable possible-duplicate flag.

Keep named sites separate from country-only records. A national registry ID may be displayed under in-country access, but the card must state that a named center is unverified. A model may summarize location evidence; it may not invent a center.

The live verifier uses the ClinicalTrials.gov v2 API for NCT records and opens the
direct source-registry URL for other records. Only an explicit inactive registry
status is a deterministic exclusion. Network errors and unparseable pages remain
eligible for gater review and are disclosed as unverified.

## Model analysis contract

For every candidate that passes conservative structured hard rules, `trial-gater` must run. For every `match` or `conditional` result:

- `trial-gater` evaluates criterion by criterion and applies R1-R5.
- `trial-risk-annotator` grounds every risk in mechanism × patient cancer × patient state.
- The deterministic publication prefetch searches Europe PMC for every non-excluded trial. The trial-efficacy-contextualizer assesses candidate applicability and limitations. `evidence_grounding.py` rejects publications outside the prefetched candidate set and restores citation identity and URLs from source records.
- `decision-synthesizer` runs after all per-trial outputs and may not promote an excluded trial.

The analysis bundle must use schema `clinical-subskills-analysis-v1` and provenance `mode=llm_subskills`. See `scripts/pipeline/analysis_contract.py`. A missing/invalid bundle is a hard stop, not a reason to fall back to deterministic clinical heuristics.

## Mechanism and feasibility

Mechanism classification is a report axis, independent of eligibility. Use the seven flat groups in `mechanism_categories.py`.

Feasibility remains operational and patient-relative. Geographic and financial dimensions may be computed for explanation but currently have zero composite weight. No feasibility score can override an exclusion verdict.

## Report contract

Use only `scripts/render/html_renderer.py`. The report follows the patient-triage layout, groups trials by mechanism and provides All / In-country access / Country record unverified / Overseas filters. Mechanism counts must update with the active filter.

Patients whose explicit current country is China receive a Simplified Chinese report; all other countries receive an English report. Formal finalize automatically post-translates patient-facing narratives through the provider-neutral `TRANSLATION_MODEL_*` API configuration (or inherited `MODEL_*`). Translation never changes retrieval or clinical decisions. Use `TRANSLATION_MODE=required` when a China report must fail rather than continue without a configured translation API.

## Commands

Formal patient runs must use `scripts/pipeline/run_formal_pipeline.py`. Do not
invoke the component commands as an alternative workflow, hand-write analysis
JSON, select a Top-N subset, or render patient-facing HTML directly.

```powershell
python scripts/pipeline/run_formal_pipeline.py prepare `
  --patient patient.json --plan search-plan.json --db trials.db `
  --mcp-python python --mcp-server server.py --run-dir run
```

Choose exactly one execution backend. For a remote API, select a provider and
model; the bundled runner embeds the referenced subskill instructions:

```powershell
$env:MODEL_EXECUTION_BACKEND="api"
$env:MODEL_PROVIDER="openai"
$env:MODEL_NAME="gpt-5"
$env:OPENAI_API_KEY=Read-Host "OpenAI API key"
python scripts/pipeline/run_formal_pipeline.py execute --run-dir run
```

Provider presets are `openai`, `anthropic`, `glm`, and `minimax`. Other
providers use `MODEL_PROVIDER=openai-compatible` with `MODEL_BASE_URL`,
`MODEL_API_KEY`, and any model identifier accepted by that endpoint. Local
agent CLIs use `MODEL_EXECUTION_BACKEND=cli` and
`MODEL_AGENT_COMMAND_JSON`. Custom runners use
`MODEL_EXECUTION_BACKEND=custom` and `MODEL_BATCH_RUNNER_JSON`.

The manual `deep-jobs`, `merge`, and `finalize` commands remain available for
diagnosis, but formal testing should use `execute` so the model cannot choose a
subset or stop between stages.

```powershell
python scripts/pipeline/run_formal_pipeline.py merge --run-dir run `
  --decision run/decision_report.json `
  --model MODEL_NAME --output-language zh-CN
```

After the canonical subskills produce `analysis_bundle.json`:

```powershell
python scripts/pipeline/run_formal_pipeline.py finalize --run-dir run
```

Run `run_formal_pipeline.py status --run-dir run` after each stage. The state
machine refuses deep-jobs before full gater coverage, merge before full deep
coverage, and finalize before a validated merged bundle.

## Quality gates

- Require all eight search dimensions when building a new plan.
- Disclose MCP truncation and retain per-query pagination/truncation audit.
- Reject formal reports without complete validated LLM subskill output.
- Reject risk output whose cancer context differs from the patient.
- Reject efficacy estimates without applicability reasoning and evidence source.
- Preserve database watermark and portal-delta limitation.
- Do not expose credentials in project files.
## Formal readiness semantics

A positive analysis-limit or prefilter-limit is validation-only. Both default to zero. Formal staged runs require every recalled trial to receive either an auditable deterministic hard exclusion or a model gater verdict, and every `match` or `conditional` verdict to receive validated risk, efficacy and development-evidence output.

Finalize uses one blocking gate and local warnings. It emits only
`validation-report.html` when validated analysis integrity is incomplete.
`formal_report_ready` depends on gate 1; gates 2 and 3 are visible warnings:

1. every recalled trial has a hard-rule or gater disposition, every non-excluded trial has complete deep analysis, and there are no budget omissions;
2. retrieval truncation warns that the report covers analyzed recall only;
3. freshness below level A/B warns that recruitment status and sites require
   re-verification. Level A has a current WHO portal delta and a
   complete direct-registry audit; level B permits a database snapshot within
   `WHO_MCP_DATABASE_MAX_AGE_HOURS` and still requires the complete direct-registry
   audit. A portal delta never substitutes for recruitment-status verification.

The clinical analysis language is not a gate. Chinese delivery is produced by
translating the grounded English patient-facing report while preserving trial
IDs, drug names, biomarkers, numbers, citations and URLs.
