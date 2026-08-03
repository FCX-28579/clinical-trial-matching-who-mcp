# Repository Guidance

- During design, review, optimization, and generation work, do not add excessive or redundant gates unless explicitly required.

- Treat `clinical-trial-matching-skill-who-mcp` as a parallel project. Never modify the original sibling project from this repository.
- Preserve patient location fields and keep retrieval global; apply country only when labeling domestic/international access.
- Always call MCP metadata before search and carry `database_as_of` through the report.
- Keep eligibility verdict, mechanism category, and feasibility score independent.
- Use MCP `get_trial` details before eligibility gating.
- Never store SSH passwords or clinical credentials in project files.
- Run unit tests and skill validation after changes.

## Ownership Boundaries

- Put clinical interpretation, eligibility reasoning, risk context, efficacy
  interpretation, and decision synthesis in the corresponding sibling Skill
  and its `rules/` resources.
- Put deterministic transport, schema validation, ID coverage, provenance,
  security, freshness, and rendering behavior in Python.
- Do not turn free-text eligibility language into a deterministic hard
  exclusion. Hard exclusions require explicit structured facts; ambiguity
  belongs in `trial-gater`.
- Keep JSON Schema or a linked rule document as the output-contract source of
  truth. Python may validate and normalize that contract but must not define a
  contradictory model-facing shape.
- New cancer types and aliases belong in `data/clinical_ontology.json`, not in
  parallel Python dictionaries.
- Do not add patient-specific trial IDs, drugs, dates, response rates, or
  report prose to generic runtime code. Evidence numbers must be grounded in
  the publication candidates attached to that trial.
- Only `run_formal_pipeline.py` may orchestrate a formal run, and only
  `full_pipeline.py finalize` may promote a result to `report.html`.
