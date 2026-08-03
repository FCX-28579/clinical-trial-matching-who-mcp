from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in (
    "scripts/pipeline", "scripts/presentation", "scripts/verification", "scripts/classification",
    "scripts/scoring", "scripts/retrieval", "scripts/render",
):
    sys.path.insert(0, str(ROOT / relative))

import full_pipeline as pipeline
from analysis_contract import AnalysisContractError, compact_trial_for_analysis, normalized_report_analysis, repair_mojibake, validate_analysis_bundle
from registry_presentation import assess_country_evidence, patient_facing_title, resolved_trial_url
from who_mcp_verifier import verify_batch


def valid_analysis(trial_id: str, cancer: str) -> dict:
    return {
        "trial_id": trial_id,
        "gating": {
            "verdict": "conditional", "confidence": 0.83,
            "inclusion_evaluation": [], "exclusion_evaluation": [],
            "hard_rules_triggered": ["R5"], "rationale": "Formal protocol review is required.",
            "blockers_satisfied": ["Disease and biomarker direction match"],
            "blockers_failed": [], "blockers_pending": ["Recent laboratory values"],
            "advisors_unknown": [],
        },
        "risk_annotation": {
            "trial_id": trial_id, "trial_mechanisms_identified": ["targeted therapy"],
            "patient_cancer_context": cancer, "risks": [], "risks_considered_but_omitted": [],
        },
        "efficacy_context": {
            "trial_id": trial_id,
            "development_evidence": [{
                "evidence_stage": "preclinical",
                "citation": "Test et al. Test Journal 2026",
                "url": "https://example.org/evidence",
                "findings": "The agent showed target engagement in a disease-relevant model.",
                "applicability": f"The model shares the {cancer} disease direction.",
                "limitations": "Preclinical findings do not establish patient benefit.",
            }],
            "evidence_search": {
                "status": "found",
                "searched_at": "2026-07-16T00:00:00+00:00",
                "queries": [f"{trial_id} publication"],
                "summary": "One relevant source was found.",
            },
            "efficacy_snapshot": {
                "match_type": "no_data", "metrics": None, "evidence_source": None,
                "applies_because": f"No directly applicable published data for this {cancer} cohort.", "caveats": [],
            },
            "vs_soc": {"available": False, "patient_line_context": cancer, "soc_options": [], "head_to_head_summary": ""},
            "redundancy_with_existing_options": {"is_trial_redundant_with_approved_combo": False, "explanation": ""},
        },
    }


class GenericPipelineContractTests(unittest.TestCase):
    def test_prepare_reads_mcp_configuration_from_environment(self):
        fake_result = {
            "transport": "stdio_mcp_jsonrpc",
            "all_verified_trials": [],
            "analysis_candidates": [],
            "database_as_of": "2026-07-16",
            "analysis_scope": {"mode": "all"},
        }
        env = {
            "WHO_MCP_PYTHON": "/configured/python",
            "WHO_MCP_SERVER": "/configured/server.py",
            "WHO_MCP_DB": "/configured/trials.db",
        }
        argv = ["full_pipeline.py", "prepare", "--patient", "patient.json", "--plan", "plan.json", "--out", "run"]
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "argv", argv), mock.patch.object(
            pipeline, "prepare", return_value=fake_result
        ) as prepare_mock, mock.patch("builtins.print"):
            pipeline.main()
        called = prepare_mock.call_args.kwargs
        self.assertEqual(called["server_python"], env["WHO_MCP_PYTHON"])
        self.assertEqual(called["server_script"], Path(env["WHO_MCP_SERVER"]))
        self.assertEqual(called["database"], Path(env["WHO_MCP_DB"]))

    def test_prepare_reads_remote_mcp_configuration_from_environment(self):
        fake_result = {
            "transport": "streamable_http_mcp_jsonrpc",
            "all_verified_trials": [],
            "analysis_candidates": [],
            "database_as_of": "2026-07-16",
            "analysis_scope": {"mode": "all"},
        }
        env = {
            "WHO_MCP_TRANSPORT": "streamable-http",
            "WHO_MCP_URL": "https://mcp.example.org/mcp",
            "WHO_MCP_API_KEY": "test-only-secret",
        }
        argv = ["full_pipeline.py", "prepare", "--patient", "patient.json", "--plan", "plan.json", "--out", "run"]
        with mock.patch.dict(os.environ, env, clear=False), mock.patch.object(sys, "argv", argv), mock.patch.object(
            pipeline, "prepare", return_value=fake_result
        ) as prepare_mock, mock.patch("builtins.print"):
            pipeline.main()
        called = prepare_mock.call_args.kwargs
        self.assertEqual(called["mcp_transport"], "streamable-http")
        self.assertEqual(called["mcp_url"], env["WHO_MCP_URL"])
        self.assertEqual(called["mcp_api_key"], env["WHO_MCP_API_KEY"])
        self.assertIsNone(called["database"])
        self.assertIsNone(called["server_script"])

    def test_pipeline_contains_no_cancer_specific_clinical_engine(self):
        source = inspect.getsource(pipeline)
        self.assertNotIn("def gate(", source)
        self.assertNotIn("def risks(", source)
        self.assertNotIn("def efficacy(", source)
        self.assertNotIn("KRAS G12C", source)
        self.assertNotIn("colorectal", source.casefold())

    def test_analysis_contract_rejects_heuristic_or_incomplete_output(self):
        patient = {"cancer_type": "NSCLC"}
        item = valid_analysis("NCT1", "NSCLC")
        with self.assertRaises(AnalysisContractError):
            validate_analysis_bundle({
                "schema_version": "clinical-subskills-analysis-v1",
                "analysis_provenance": {"mode": "deterministic_heuristic", "model": "none", "completed_at": "now", "output_language": "en"},
                "analyzed_trials": [item], "decision_report": {"decision_paths": [], "goals_of_care": {}},
            }, patient, ["NCT1"])

    def test_analysis_language_mismatch_is_disclosed_not_rejected(self):
        patient = {"cancer_type": "NSCLC", "report_language": "zh-CN"}
        item = valid_analysis("NCT-LANG", "NSCLC")
        bundle = {
            "schema_version": "clinical-subskills-analysis-v1",
            "analysis_provenance": {
                "mode": "llm_subskills", "model": "test-model",
                "completed_at": "now", "output_language": "en",
            },
            "analyzed_trials": [item],
            "decision_report": {"decision_paths": [], "goals_of_care": {}},
        }
        by_id = validate_analysis_bundle(bundle, patient, ["NCT-LANG"])
        self.assertIn("NCT-LANG", by_id)
        del item["efficacy_context"]
        with self.assertRaises(AnalysisContractError):
            validate_analysis_bundle({
                "schema_version": "clinical-subskills-analysis-v1",
                "analysis_provenance": {"mode": "llm_subskills", "model": "test-model", "completed_at": "now", "output_language": "en"},
                "analyzed_trials": [item], "decision_report": {"decision_paths": [], "goals_of_care": {}},
            }, patient, ["NCT1"])

    def test_non_excluded_trial_requires_auditable_development_evidence_search(self):
        patient = {"cancer_type": "NSCLC"}
        item = valid_analysis("NCT-EVIDENCE", "NSCLC")
        del item["efficacy_context"]["development_evidence"]
        del item["efficacy_context"]["evidence_search"]
        bundle = {
            "schema_version": "clinical-subskills-analysis-v1",
            "analysis_provenance": {
                "mode": "llm_subskills", "model": "test-model",
                "completed_at": "now", "output_language": "en",
            },
            "analyzed_trials": [item],
            "decision_report": {"decision_paths": [], "goals_of_care": {}},
        }
        with self.assertRaisesRegex(AnalysisContractError, "development_evidence"):
            validate_analysis_bundle(bundle, patient, ["NCT-EVIDENCE"])
    def test_same_contract_accepts_multiple_cancer_types(self):
        for cancer, context in (
            ("NSCLC", "NSCLC"),
            ("HER2-positive breast cancer", "HER2-positive breast cancer"),
            ("pancreatic adenocarcinoma", "pancreatic adenocarcinoma"),
            ("gastric cancer", "胃癌"),
            ("cholangiocarcinoma", "胆管癌"),
        ):
            item = valid_analysis("TRIAL-" + cancer[:3], context)
            bundle = {
                "schema_version": "clinical-subskills-analysis-v1",
                "analysis_provenance": {"mode": "llm_subskills", "model": "gpt", "completed_at": "2026-07-16", "output_language": "en"},
                "analyzed_trials": [item],
                "decision_report": {"decision_paths": [], "goals_of_care": {"triggered": False}},
            }
            by_id = validate_analysis_bundle(bundle, {"cancer_type": cancer}, [item["trial_id"]])
            normalized = normalized_report_analysis(by_id[item["trial_id"]])
            self.assertEqual(normalized["gating"]["verdict"], "conditional")

    def test_portal_delta_requires_matching_database_watermark(self):
        watermark = "2026-07-09T00:45:35+00:00"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "delta.json"
            path.write_text(json.dumps({
                "schema_version": "who-portal-delta-v1",
                "generator": "who_portal_delta.py",
                "search_plan_sha256": "0" * 64,
                "status": "executed",
                "database_as_of": "2026-07-08T00:00:00+00:00",
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "trials": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "watermark"):
                pipeline._load_portal_delta(path, watermark)

    def test_portal_delta_rejects_stale_execution(self):
        watermark = "2026-07-09T00:45:35+00:00"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "delta.json"
            stale = dt.datetime.now().astimezone() - dt.timedelta(hours=25)
            path.write_text(json.dumps({
                "schema_version": "who-portal-delta-v1",
                "generator": "who_portal_delta.py",
                "search_plan_sha256": "0" * 64,
                "status": "executed",
                "database_as_of": watermark,
                "executed_at": stale.isoformat(),
                "trials": [],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not current"):
                pipeline._load_portal_delta(path, watermark)

    def test_portal_delta_rejects_future_execution_beyond_clock_skew(self):
        watermark = "2026-07-09T00:45:35+00:00"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "delta.json"
            future = dt.datetime.now().astimezone() + dt.timedelta(minutes=10)
            path.write_text(json.dumps({
                "schema_version": "who-portal-delta-v1",
                "generator": "who_portal_delta.py",
                "search_plan_sha256": "0" * 64,
                "status": "executed",
                "database_as_of": watermark,
                "executed_at": future.isoformat(),
                "trials": [],
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"WHO_PORTAL_CLOCK_SKEW_MINUTES": "5"}):
                with self.assertRaisesRegex(ValueError, "not current"):
                    pipeline._load_portal_delta(path, watermark)

    def test_portal_clock_skew_configuration_is_bounded(self):
        with mock.patch.dict(os.environ, {"WHO_PORTAL_CLOCK_SKEW_MINUTES": "61"}):
            with self.assertRaisesRegex(ValueError, "between 0 and 60"):
                pipeline._portal_clock_skew_minutes()

    def test_portal_delta_contract_accepts_auditable_trials(self):
        watermark = "2026-07-09T00:45:35+00:00"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "delta.json"
            path.write_text(json.dumps({
                "schema_version": "who-portal-delta-v1",
                "generator": "who_portal_delta.py",
                "search_plan_sha256": "0" * 64,
                "status": "executed",
                "database_as_of": watermark,
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "source": "WHO ICTRP portal",
                "query_audit": [{
                    "label": "control", "returned": 1, "portal_total": 1,
                    "complete": True,
                }],
                "control_query": {"query_variants": 1, "complete": True},
                "trials": [{"id": "NCT99999999", "title": "A new basket trial"}],
            }), encoding="utf-8")
            trials, audit = pipeline._load_portal_delta(path, watermark)
        self.assertEqual([trial["id"] for trial in trials], ["NCT99999999"])
        self.assertEqual(audit["status"], "executed")
        self.assertEqual(audit["returned"], 1)
    def test_formal_readiness_requires_all_three_quality_gates(self):
        prepared = {
            "all_verified_trials": [{"id": "T1"}, {"id": "T2"}],
            "analysis_scope": "validation_subset",
            "retrieval_complete": False,
            "search_stats": {"query_truncation_count": 1},
            "portal_delta": {"status": "not_executed"},
        }
        gates = pipeline.report_quality_gates(prepared, 1)
        self.assertEqual(gates, {
            "complete_analysis": False,
            "complete_retrieval": False,
            "current_data_snapshot": False,
        })
        prepared.update({
            "analysis_scope": "complete",
            "retrieval_complete": True,
            "portal_delta": {
                "status": "executed",
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "query_audit": [{"returned": 0, "portal_total": 0, "complete": True}],
                "control_query": {"query_variants": 1, "complete": True},
            },
            "live_registry_audit": {
                "attempted": 2, "complete": True, "unknown": 0, "errors": 0,
            },
        })
        self.assertTrue(all(pipeline.report_quality_gates(prepared, 2).values()))

    def test_empty_or_unidentified_recall_cannot_be_formal(self):
        common = {
            "analysis_scope": "complete", "retrieval_complete": True,
            "portal_delta": {
                "status": "executed",
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "query_audit": [{"returned": 0, "portal_total": 0, "complete": True}],
                "control_query": {"query_variants": 1, "complete": True},
            },
            "live_registry_audit": {
                "attempted": 0, "complete": True, "unknown": 0, "errors": 0,
            },
        }
        self.assertFalse(pipeline.report_quality_gates({
            **common, "all_verified_trials": [],
        }, 0)["complete_analysis"])
        self.assertFalse(pipeline.report_quality_gates({
            **common, "all_verified_trials": [{"id": ""}],
        }, 1)["complete_analysis"])

    def test_all_unknown_live_registry_results_fail_freshness(self):
        prepared = {
            "all_verified_trials": [{"id": "T1"}, {"id": "T2"}],
            "database_as_of": dt.datetime.now().astimezone().isoformat(),
            "portal_delta": {"status": "not_executed"},
            "live_registry_audit": {
                "attempted": 2, "complete": True, "unknown": 2, "errors": 0,
            },
        }
        self.assertFalse(pipeline.data_freshness_assessment(prepared)["formal_freshness_ready"])

    def test_staged_quality_gate_uses_audited_workload_counts(self):
        prepared = {
            "analysis_workflow": "gater_then_deep_analysis",
            "all_verified_trials": [{"id": f"T{index}"} for index in range(6)],
            "analysis_candidate_ids": ["T1", "T2"],
            "hard_excluded_trials": [{"id": "T3"}],
            "analysis_scope": "staged_complete",
            "preanalysis_filter": {"budget_omitted_count": 0},
            "retrieval_complete": True,
            "portal_delta": {
                "status": "executed",
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "query_audit": [{"returned": 0, "portal_total": 0, "complete": True}],
                "control_query": {"query_variants": 1, "complete": True},
            },
            "live_registry_audit": {
                "attempted": 6, "complete": True, "unknown": 0, "errors": 0,
            },
        }
        self.assertTrue(all(pipeline.report_quality_gates(prepared, 3).values()))
        prepared["analysis_scope"] = "validation_subset"
        self.assertFalse(pipeline.report_quality_gates(prepared, 3)["complete_analysis"])

    def test_coverage_audit_rejects_top_n_gater_subset(self):
        prepared = {
            "all_verified_trials": [{"id": "T1"}, {"id": "T2"}, {"id": "T3"}],
            "hard_excluded_trials": [{"id": "T3"}],
        }
        by_id = {
            "T1": {
                "gating": {"verdict": "match"},
                "risk_annotation": {},
                "efficacy_context": {
                    "evidence_search": {},
                    "development_evidence": [],
                },
            },
        }
        audit = pipeline.analysis_coverage_audit(prepared, by_id)
        self.assertFalse(audit["disposition_equation_valid"])
        self.assertEqual(audit["missing_disposition_ids"], ["T2"])
        self.assertEqual(audit["omitted_count"], 1)

    def test_coverage_audit_requires_equal_deep_analysis_sets(self):
        prepared = {
            "all_verified_trials": [{"id": "T1"}, {"id": "T2"}],
            "hard_excluded_trials": [],
        }
        by_id = {
            "T1": {
                "gating": {"verdict": "match"},
                "risk_annotation": {},
                "efficacy_context": {
                    "evidence_search": {},
                    "development_evidence": [],
                },
            },
            "T2": {
                "gating": {"verdict": "conditional"},
                "risk_annotation": {},
            },
        }
        audit = pipeline.analysis_coverage_audit(prepared, by_id)
        self.assertTrue(audit["disposition_equation_valid"])
        self.assertFalse(audit["deep_analysis_equations_valid"])
        self.assertEqual(audit["missing_efficacy_ids"], ["T2"])
        self.assertEqual(audit["missing_evidence_ids"], ["T2"])

    def test_candidate_selection_is_mechanism_diverse_not_disease_coded(self):
        trials = []
        for index, category in enumerate(("targeted_therapy", "immune_combination", "cell_and_biologic", "other")):
            trials.append({
                "id": f"T{index}", "search_rank": index + 1,
                "mechanism_category": {"category": category},
                "feasibility": {"composite": 0.8}, "matched_by": ["branch"],
            })
        selected = pipeline.select_analysis_candidates(trials, 3)
        self.assertEqual(len({trial["mechanism_category"]["category"] for trial in selected}), 3)

    def test_model_job_payload_drops_bulky_site_collections(self):
        compact = compact_trial_for_analysis({
            "id": "T1",
            "title": "Targeted trial",
            "parsed_criteria": {"inclusion": ["advanced cancer"]},
            "sites": [{"site_name": "A"}] * 100,
            "country_records": [{"country": "China"}] * 100,
            "patient_country_sites": [{"site_name": "A", "country": "China"}],
        })
        self.assertEqual(compact["id"], "T1")
        self.assertIn("parsed_criteria", compact)
        self.assertIn("patient_country_sites", compact)
        self.assertNotIn("sites", compact)
        self.assertNotIn("country_records", compact)
        self.assertEqual(compact["identity_context"]["canonical_trial_id"], "T1")
        self.assertEqual(
            compact["identity_context"]["official_registry_title"],
            "Targeted trial",
        )
    def test_report_language_is_derived_from_explicit_country(self):
        patient = {"country": "China", "report_language": "en"}
        self.assertEqual(pipeline._language(patient), "zh-CN")
        self.assertEqual(
            pipeline._language({"country": "United States", "report_language": "zh-CN"}),
            "en",
        )

    def test_prefilter_limits_active_trials_and_remains_auditable(self):
        trials = []
        for index in range(8):
            trials.append({
                "id": f"T{index}",
                "overall_status": "RECRUITING" if index < 6 else "COMPLETED",
                "search_rank": index,
                "mechanism_category": {"category": "targeted_therapy" if index % 2 else "other"},
                "feasibility": {"composite": 0.8},
                "matched_by": ["branch"],
            })
        selected, audit = pipeline.deterministic_prefilter(trials, 4)
        self.assertEqual(len(selected), 4)
        self.assertTrue(all(trial["overall_status"] == "RECRUITING" for trial in selected))
        self.assertEqual(audit["inactive_or_unknown_omitted_count"], 0)
        self.assertEqual(audit["budget_omitted_count"], 4)
        prepared = {
            "all_verified_trials": trials,
            "prefiltered_trials": selected,
            "analysis_scope": "prefilter_complete",
            "retrieval_complete": True,
            "portal_delta": {
                "status": "executed",
                "executed_at": dt.datetime.now().astimezone().isoformat(),
                "query_audit": [{"returned": 0, "portal_total": 0, "complete": True}],
                "control_query": {"query_variants": 1, "complete": True},
            },
            "live_registry_audit": {
                "attempted": 8, "complete": True, "unknown": 0, "errors": 0,
            },
            "preanalysis_filter": {"budget_omitted_count": 0},
        }
        self.assertTrue(all(pipeline.report_quality_gates(prepared, 4).values()))
        prepared["preanalysis_filter"]["budget_omitted_count"] = 2
        self.assertFalse(pipeline.report_quality_gates(prepared, 4)["complete_analysis"])

    def test_portal_freshness_is_rechecked_at_finalize(self):
        audit = {
            "status": "executed",
            "executed_at": "2020-01-01T00:00:00+00:00",
            "freshness_validated_at_prepare": True,
            "max_age_hours": 24,
        }
        self.assertFalse(pipeline._portal_audit_is_current(audit))

class TextNormalizationTests(unittest.TestCase):
    def test_repairs_strict_utf8_as_latin1_mojibake(self):
        broken = "CRC\u00ef\u00bc\u0088KRAS G12C\u00e3\u0080\u0081MSS\u00ef\u00bc\u0089"
        expected = "CRC\uFF08KRAS G12C\u3001MSS\uFF09"
        self.assertEqual(repair_mojibake(broken), expected)
        self.assertEqual(repair_mojibake("\u6b63\u5e38\u4e2d\u6587\u4e0d\u5e94\u6539\u53d8"), "\u6b63\u5e38\u4e2d\u6587\u4e0d\u5e94\u6539\u53d8")
        self.assertEqual(repair_mojibake("ordinary English"), "ordinary English")


class RegistryBoundaryTests(unittest.TestCase):
    def test_protocol_and_ctis_keys_deduplicate_only_in_verifier(self):
        patient = {"country": "China"}
        trials = [
            {"trial_uid": "a", "id": "CTRI/1", "registry_ids": [{"registry_id": "61186372COR3002 original"}]},
            {"trial_uid": "b", "id": "CTIS-X", "registry_ids": [{"registry_id": "61186372COR3002 CPMS"}]},
        ]
        details = [
            {"found": True, "trial_uid": "a", "primary_registry_id": "CTRI/1", "title": "T", "registry_ids": trials[0]["registry_ids"], "sites": [], "parsed_criteria": {"raw": "x"}},
            {"found": True, "trial_uid": "b", "primary_registry_id": "CTIS-X", "title": "T", "registry_ids": trials[1]["registry_ids"], "sites": [], "parsed_criteria": {"raw": "x"}},
        ]
        self.assertEqual(len(verify_batch(trials, details, patient)), 2)

    def test_explicit_registry_id_bridge_deduplicates_transitively(self):
        patient = {"country": "China"}
        trials = [
            {"id": "A", "registry_ids": ["CTIS2024-123456-11-00"]},
            {"id": "C", "registry_ids": ["NCT00000003"]},
            {"id": "B", "registry_ids": ["CTIS2024-123456-11", "NCT00000003"]},
        ]
        self.assertEqual(len(verify_batch(trials, [], patient)), 1)

    def test_registry_titles_country_evidence_and_links_are_presentation_only(self):
        ctis = {
            "id": "CTIS2024-513853-66-00", "title": "Amivantamab + FOLFIRI Versus Cetuximab/Bevacizumab + FOLFIRI",
            "interventions": ["Product Name: VEGZELMA 25 mg/mL concentrate for solution for infusion, Product Code:X, Product Name: Erbitux 5 mg/mL solution for infusion, Product Code:Y, Product Name: JNJ-61186372, Product Code:Z"],
            "mechanism_category": {"label_zh": "靶点治疗", "label_en": "Targeted therapy"},
            "patient_country_site_count": 0, "patient_country_location_record_count": 1,
        }
        title = patient_facing_title(ctis, "en")
        self.assertTrue(title.startswith("Amivantamab (multi-arm)"))
        self.assertNotIn("Product Code", title)
        self.assertLessEqual(len(title), 125)
        noisy = {
            "id": "NCT-NOISY",
            "title": "Study of Osimertinib in EGFR-mutated NSCLC",
            "scientific_title": "Osimertinib for EGFR-mutated Non-small Cell Lung Cancer",
            "interventions": [
                "Osimertinib", "Echocardiography Test", "Multigated Acquisition Scan",
                "Biospecimen Collection", "Computed Tomography",
                "LGX818 will be supplied as capsules for oral use of 100 mg",
                {"intervention_name_raw": "Cardiac Monitoring", "intervention_type": "Procedure"},
                {"intervention_name_raw": "Tumor Assessment", "intervention_type": "Diagnostic Test"},
            ],
            "mechanism_category": {"label_zh": "靶点治疗", "label_en": "Targeted therapy"},
        }
        noisy_title = patient_facing_title(noisy, "en")
        self.assertIn("Osimertinib", noisy_title)
        self.assertNotIn("Echocardiography", noisy_title)
        self.assertNotIn("Biospecimen", noisy_title)
        self.assertNotIn("will be supplied", noisy_title)
        self.assertNotIn("Cardiac Monitoring", noisy_title)
        self.assertNotIn("Tumor Assessment", noisy_title)
        dose_title = patient_facing_title({
            "id": "NCT-DOSE", "title": "Tumor vaccine study",
            "interventions": ["Low Dose MSH2 tumor cell vaccine", "Medium Dose MSH2 tumor cell vaccine", "High Dose MSH2 tumor cell vaccine"],
            "mechanism_category": {"label_en": "Cell therapy"},
        }, "en")
        self.assertIn("MSH2 tumor cell vaccine", dose_title)
        self.assertNotIn(" + ", dose_title)
        regimen_title = patient_facing_title({
            "id": "NCT-REGIMEN",
            "title": "A Study of OBI-833 and Erlotinib in NSCLC",
            "interventions": ["30 \u03bcg OBI-833/100 \u03bcg OBI-821", "Erlotinib (150 mg daily)"],
            "mechanism_category": {"label_en": "Cell and biologic therapy"},
        }, "en")
        self.assertIn("OBI-833", regimen_title)
        self.assertNotIn("OBI-833/OBI-821 +", regimen_title)
        self.assertNotIn("150 mg", regimen_title)
        escalation_title = patient_facing_title({
            "id": "NCT-ESCALATION",
            "scientific_title": "A Study of KY-0301 in Advanced Solid Tumors",
            "interventions": [
                "Group A:0.3mg/kg For the accelerated escalation group, one patient was enrolled",
                "Group B:0.6mg/kg For the accelerated escalation group, one patient was enrolled",
            ],
            "mechanism_category": {"label_en": "Other"},
        }, "en")
        self.assertIn("KY-0301", escalation_title)
        self.assertNotIn("mg/kg", escalation_title)
        long_title = patient_facing_title({
            "id": "NCT-LONG", "scientific_title": "A Phase I Clinical Study Evaluating the Safety and Efficacy of a New Treatment in Patients With Advanced Solid Tumors Across Multiple Cohorts",
            "interventions": [], "mechanism_category": {"label_en": "Other"},
        }, "en")
        self.assertNotIn("Clinical Study", long_title)
        self.assertLessEqual(len(long_title), 180)
        self.assertTrue(long_title.endswith("Other"))
        for generic in (
            "will continue until disease progression", "Cell", "Biopsy", "Phase 1a",
            "Each Cycle D1-5 + Part 2: dose expansion",
        ):
            cleaned = patient_facing_title({
                "id": "NCT-GENERIC",
                "scientific_title": "A Study of RMC-6236 in Advanced Solid Tumors",
                "interventions": [generic],
                "mechanism_category": {"label_en": "Targeted therapy"},
            }, "en")
            self.assertIn("RMC-6236", cleaned)
            self.assertNotIn(generic, cleaned)
        self.assertEqual(assess_country_evidence(ctis, {"country": "China"})["class"], "country_unverified")
        self.assertIn("trialsearch.who.int/Trial2.aspx", resolved_trial_url(ctis))
        chictr = {"id": "ChiCTR2400082391", "patient_country_site_count": 0, "patient_country_location_record_count": 1}
        self.assertEqual(assess_country_evidence(chictr, {"country": "China"})["class"], "domestic_registry")

    def test_multicohort_title_never_flattens_global_interventions(self):
        title = patient_facing_title({
            "id": "NCT-MULTI",
            "title": "A Multi-cohort Study of Setidegrasib in Solid Tumors",
            "interventions": [
                "Setidegrasib", "Cetuximab", "Leucovorin", "Oxaliplatin",
            ],
            "mechanism_category": {"label_en": "Targeted therapy"},
        }, "en")
        self.assertIn("Setidegrasib (multi-arm)", title)
        self.assertNotIn("Setidegrasib + Cetuximab", title)

    def test_title_uses_official_core_drug_code_and_recruitment_state(self):
        title = patient_facing_title({
            "id": "NCT1",
            "title": "A Study of GDC-7035 as a Single Agent and in Combination",
            "interventions": ["Drug: Phase I Arm A", "Drug: Phase I Arm B"],
            "overall_status": "COMPLETED",
            "gating": {"verdict": "exclude"},
            "mechanism_category": {"label_zh": "靶点治疗"},
        }, "zh-CN")
        self.assertEqual(title, "GDC-7035 · 靶点治疗 · 已关闭招募")


if __name__ == "__main__":
    unittest.main()
