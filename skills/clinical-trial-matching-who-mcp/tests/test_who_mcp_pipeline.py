from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("scripts/retrieval", "scripts/verification", "scripts/scoring", "scripts/classification", "scripts/render"):
    sys.path.insert(0, str(ROOT / relative))

from feasibility import WEIGHTS, compute_feasibility
from html_renderer import render_html
from mechanism_categories import classify_mechanism
from search_plan import (
    build_baseline_search_plan, compile_search_plan_for_mcp,
    search_plan_coverage, validate_search_plan,
    validate_search_plan_for_patient,
)
from who_mcp_adapter import build_mcp_requests, build_portal_delta_contract, merge_sources
from who_mcp_verifier import verify_batch


class WhoMcpPipelineTests(unittest.TestCase):
    def test_baseline_plan_covers_all_eight_dimensions(self):
        plan = build_baseline_search_plan({
            "patient_id": "PT-1", "cancer_type": "pancreatic cancer",
            "stage": "IV", "mutations": ["KRAS G12D"],
            "search_terms": {
                "named_agents": ["ASP3082"],
                "combination_targets": ["SHP2"],
                "pathway_terms": ["RAS(ON)"],
                "chinese_terms": ["胰腺癌 KRAS G12D"],
            },
        })
        self.assertEqual(validate_search_plan(plan), [])
        self.assertEqual(search_plan_coverage(plan)["missing"], [])
        self.assertFalse(plan["generation_audit"]["requires_human_review"])

    def test_baseline_registry_branch_never_uses_a_disease_only_query(self):
        patient = {
            "patient_id": "PT-1",
            "cancer_type": "pancreatic ductal adenocarcinoma",
            "mutations": ["KRAS G12D"],
        }
        plan = build_baseline_search_plan(patient)
        registry_group = next(
            group for group in plan["keyword_groups"]
            if group["dimension"] == "chinese_registry_terms"
        )
        self.assertNotIn(
            patient["cancer_type"],
            [query.get("term") for query in registry_group["queries"]],
        )

    def test_search_expansion_is_bounded_per_dimension(self):
        patient = {
            "patient_id": "PT-1",
            "cancer_type": "solid tumor",
            "mutations": [f"MARKER-{index}" for index in range(12)],
            "search_terms": {
                "named_agents": [f"AGENT-{index}" for index in range(12)],
            },
        }
        with mock.patch.dict("os.environ", {"SEARCH_MAX_TERMS_PER_DIMENSION": "3"}):
            plan = build_baseline_search_plan(patient)
        self.assertTrue(all(
            len(group["queries"]) <= 3 for group in plan["keyword_groups"]
        ))
        self.assertEqual(plan["generation_audit"]["max_terms_per_dimension"], 3)
        self.assertEqual(
            plan["generation_audit"]["query_count"],
            sum(len(group["queries"]) for group in plan["keyword_groups"]),
        )

    def test_mcp_plan_compiler_joins_disease_and_concept_anchors(self):
        plan = {
            "keyword_groups": [{
                "dimension": "disease_biomarker",
                "label": "Disease and marker",
                "queries": [{
                    "condition": "pancreatic ductal adenocarcinoma",
                    "term": "KRAS G12D",
                }],
            }],
        }
        compiled = compile_search_plan_for_mcp(plan)
        query = compiled["keyword_groups"][0]["queries"][0]
        self.assertIsNone(query["condition"])
        self.assertEqual(
            query["term"],
            "pancreatic ductal adenocarcinoma KRAS G12D",
        )
        self.assertEqual(query["query_semantics"], "compiled_conjunctive_anchor")
        self.assertEqual(plan["keyword_groups"][0]["queries"][0]["term"], "KRAS G12D")

    def test_formal_plan_rejects_condition_or_patient_disease_only_queries(self):
        condition_only = {
            "keyword_groups": [{
                "dimension": "disease_biomarker",
                "label": "Disease only",
                "queries": [{"condition": "gastric cancer", "term": ""}],
            }],
        }
        self.assertTrue(validate_search_plan(condition_only, require_full_coverage=False))
        disease_term = {
            "keyword_groups": [{
                "dimension": "disease_biomarker",
                "label": "Disease only",
                "queries": [{"condition": None, "term": "gastric cancer"}],
            }],
        }
        self.assertTrue(validate_search_plan_for_patient(
            disease_term, {"cancer_type": "gastric cancer"}
        ))
    def setUp(self):
        self.patient = {
            "patient_id": "P-001", "country": "China", "city": "Beijing",
            "patient_location": "北京市", "cancer_type": "colorectal cancer", "stage": "IV",
            "mutations": ["KRAS G12C"], "treatment_lines_completed": 2,
            "willing_to_travel_domestic": True, "willing_to_travel_internationally": False,
            "affordability_tier": "medium", "treatment_history": [],
        }
        self.plan = {
            "treatment_lines": 2,
            "keyword_groups": [{"label": "KRAS G12C", "source": "both", "queries": [{"condition": "colorectal cancer", "term": "KRAS G12C"}]}],
            "hard_exclude": {"first_line_only": True, "molecular_mismatch": ["RAS wild-type only"]},
        }
        self.local = [{
            "trial_uid": "NCT00000001", "primary_registry_id": "NCT00000001", "primary_source": "ClinicalTrials.gov",
            "title": "KRAS G12C inhibitor in colorectal cancer", "phase_normalized": "Phase 2",
            "recruitment_status_normalized": "recruiting", "last_update_date": "2026-07-01",
            "matched_by": ["KRAS G12C"],
        }]
        self.delta = [{
            "id": "NCT00000001", "title": "KRAS G12C inhibitor in colorectal cancer",
            "last_update_date": "2026-07-10", "source": "WHO portal",
        }]
        self.detail = [{
            "found": True, "trial_uid": "NCT00000001", "primary_registry_id": "NCT00000001",
            "title": "KRAS G12C inhibitor in colorectal cancer", "recruitment_status_normalized": "recruiting",
            "last_update_date": "2026-07-10", "brief_summary": "Targeted therapy",
            "registry_ids": [{"registry_id": "NCT00000001", "registry_source": "ClinicalTrials.gov"}],
            "sites": [
                {"country": "China", "city": "Beijing", "site_name": "Hospital A"},
                {"country": "United States", "city": "Boston", "site_name": "Hospital B"},
            ],
            "interventions": [{"intervention_name_raw": "adagrasib"}],
            "parsed_criteria": {"inclusion": ["KRAS G12C"], "exclusion": [], "unknown": [], "raw": "Inclusion Criteria\nKRAS G12C"},
        }]

    def test_request_preserves_full_plan_and_global_recall(self):
        self.assertEqual(validate_search_plan(self.plan, require_full_coverage=False), [])
        request = build_mcp_requests(self.plan, self.patient)
        executed = request["local_search"]["arguments"]["search_plan"]
        self.assertEqual(
            executed["execution_audit"]["compiler"],
            "conjunctive-search-plan-v1",
        )
        self.assertEqual(request["local_search"]["arguments"]["country"], "")
        self.assertEqual(request["metadata"]["tool"], "database_metadata")


    def test_original_example_covers_all_eight_recall_dimensions(self):
        example = json.loads((ROOT / "examples" / "SYNTHETIC-CN-CRC-KRAS-G12C-search-plan.json").read_text(encoding="utf-8-sig"))
        coverage = search_plan_coverage(example)
        self.assertEqual(coverage["missing"], [])
        self.assertEqual(validate_search_plan(example), [])
    def test_merge_deduplicates_across_branches_and_keeps_watermark(self):
        merged = merge_sources(self.local, self.delta, patient=self.patient, database_as_of="2026-07-09T00:45:35+00:00")
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]["retrieval_provenance"]), {"who_mcp_database", "who_portal_delta"})
        self.assertEqual(merged[0]["database_as_of"], "2026-07-09T00:45:35+00:00")
        self.assertEqual(build_portal_delta_contract("2026-07-09")["boundary_type"], "registration_date_proxy")

    def test_get_trial_enrichment_preserves_patient_location_and_criteria(self):
        merged = merge_sources(self.local, [], patient=self.patient, database_as_of="2026-07-09")
        verified = verify_batch(merged, self.detail, self.patient)[0]
        self.assertEqual(verified["verification"]["status"], "verified")
        self.assertEqual(verified["patient_country_site_count"], 1)
        self.assertEqual(verified["geography_class"], "domestic")
        self.assertIn("KRAS G12C", verified["parsed_criteria"]["inclusion"])

    def test_country_only_location_is_not_domestic_accessible(self):
        trial = {"trial_uid": "who:CTIS1", "id": "CTIS1", "primary_registry_id": "CTIS1"}
        detail = [{
            "found": True, "trial_uid": "who:CTIS1", "primary_registry_id": "CTIS1",
            "title": "Trial", "recruitment_status_normalized": "recruiting",
            "sites": [{"country": "China", "city": None, "site_name": None}],
            "registry_ids": [{"registry_id": "CTIS1"}], "parsed_criteria": {"raw": "criteria"},
        }]
        verified = verify_batch([trial], detail, self.patient)[0]
        self.assertEqual(verified["patient_country_site_count"], 0)
        self.assertEqual(verified["patient_country_location_record_count"], 1)
        self.assertFalse(verified["domestic_accessible"])
        self.assertEqual(verified["geography_class"], "domestic_unverified")
        feasibility = compute_feasibility(verified, self.patient)
        self.assertEqual(feasibility.sub_scores["geographic_access"], 0.55)
        self.assertEqual(feasibility.sub_scores["financial_cost"], 0.65)
        self.assertNotIn("Domestic cross-city travel is likely", feasibility.flags)

    def test_countries_field_is_country_only_fallback(self):
        trial = {"trial_uid": "who:NCT1", "id": "NCT1", "primary_registry_id": "NCT1"}
        detail = [{
            "found": True, "trial_uid": "who:NCT1", "primary_registry_id": "NCT1",
            "title": "Trial", "countries": "China | United States | Canada",
            "sites": [], "country_records": [], "parsed_criteria": {"raw": "criteria"},
        }]
        verified = verify_batch([trial], detail, {"country": "United States"})[0]
        self.assertEqual(verified["patient_country_site_count"], 0)
        self.assertEqual(verified["patient_country_location_record_count"], 1)
        self.assertEqual(verified["verification"]["location_evidence"], "country_only")
        self.assertEqual(verified["geography_class"], "domestic_unverified")

    def test_post_detail_dedup_uses_exact_record_then_secondary_ids(self):
        trials = [
            {"trial_uid": "who:NCT", "id": "NCT00000001", "primary_registry_id": "NCT00000001", "matched_by": ["a"]},
            {"trial_uid": "who:CHI", "id": "ChiCTR2400000001", "primary_registry_id": "ChiCTR2400000001", "matched_by": ["b"]},
        ]
        details = [
            {"found": True, "trial_uid": "who:NCT", "primary_registry_id": "NCT00000001", "title": "Same",
             "registry_ids": [{"registry_id": "NCT00000001"}, {"registry_id": "ChiCTR2400000001"}], "sites": [], "parsed_criteria": {"raw": "x"}},
            {"found": True, "trial_uid": "who:CHI", "primary_registry_id": "ChiCTR2400000001", "title": "Same",
             "registry_ids": [{"registry_id": "ChiCTR2400000001"}, {"registry_id": "NCT00000001"}], "sites": [], "parsed_criteria": {"raw": "x"}},
        ]
        verified = verify_batch(trials, details, self.patient)
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["id"], "NCT00000001")
        self.assertEqual(set(verified[0]["matched_by"]), {"a", "b"})
        self.assertEqual(len(verified[0]["duplicate_registry_records"]), 1)

    def test_generic_secondary_protocol_collision_does_not_merge_trials(self):
        trials = [
            {
                "id": "NCT00000001",
                "primary_registry_id": "NCT00000001",
                "registry_ids": [{"registry_id": "123456AB1", "id_type": "secondary_id"}],
            },
            {
                "id": "NCT00000002",
                "primary_registry_id": "NCT00000002",
                "registry_ids": [{"registry_id": "123456AB1", "id_type": "secondary_id"}],
            },
        ]
        self.assertEqual(len(verify_batch(trials, [], self.patient)), 2)

    def test_identical_title_and_interventions_are_flagged_not_merged(self):
        shared = {
            "scientific_title": (
                "A sufficiently long identical scientific title for two independent protocols"
            ),
            "interventions": ["Drug A", "Drug B"],
        }
        trials = [
            {"id": "NCT00000001", **shared},
            {"id": "NCT00000002", **shared},
        ]
        verified = verify_batch(trials, [], self.patient)
        self.assertEqual(len(verified), 2)
        self.assertEqual(
            verified[0]["possible_duplicate_cluster_ids"],
            ["NCT00000001", "NCT00000002"],
        )
    def test_feasibility_is_relative_to_patient_country(self):
        trial = verify_batch(self.local, self.detail, self.patient)[0]
        china = compute_feasibility(trial, self.patient)
        uk_patient = {**self.patient, "country": "United Kingdom", "city": "London"}
        uk_trial = verify_batch(self.local, self.detail, uk_patient)[0]
        uk = compute_feasibility(uk_trial, uk_patient)
        self.assertGreater(china.sub_scores["geographic_access"], uk.sub_scores["geographic_access"])
        self.assertTrue(uk.promote_to_decision_report)
        self.assertEqual(china.composite, uk.composite)
        self.assertEqual(WEIGHTS["geographic_access"], 0.0)
        self.assertEqual(WEIGHTS["financial_cost"], 0.0)

    def test_mechanism_category_is_separate_from_verdict(self):
        category = classify_mechanism({"title": "Adagrasib KRAS G12C study"})
        self.assertEqual(category["category"], "targeted_therapy")
        self.assertNotIn("verdict", category)

    def test_patient_report_contains_watermark_and_location_grouping(self):
        trial = verify_batch(self.local, self.detail, self.patient)[0]
        trial.update({
            "feasibility": {"composite": 0.8, "sub_scores": {}, "flags": []},
            "mechanism_category": classify_mechanism(trial),
            "country_assessment": {"class": "domestic_named"},
            "resolved_source_url": "https://clinicaltrials.gov/study/NCT00000001",
            "display_title": "Adagrasib · 靶点治疗",
            "gating": {"verdict": "conditional", "satisfied": ["方向一致"], "pending": ["正式筛选"], "exclusion_reasons": []},
            "risk_context": ["风险待中心复核"], "efficacy_context": "不从注册信息推断疗效",
        })
        payload = {
            "language": "zh-CN", "patient": self.patient, "trials": [trial],
            "counts": {"match": 0, "conditional": 1, "exclude": 0},
            "geography_audit": {"domestic_named": 1},
            "database_as_of": "2026-07-09T00:45:35+00:00",
            "database_metadata": {"schema_version": "3"},
            "portal_delta": {"status": "not_executed"},
            "decision_report": {
                "decision_paths": [{
                    "rank": 1, "trial_id": "NCT00000001",
                    "trial_title": "Adagrasib", "rationale": "优先核实。",
                }],
                "goals_of_care": {},
            },
            "report_warnings": [{
                "code": "DATA_SNAPSHOT_STALE",
                "message_zh": "招募状态须重新核实。",
                "message_en": "Recruitment status requires re-verification.",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            render_html(payload, output)
            html = output.read_text(encoding="utf-8")
            self.assertIn("为您筛选的全球临床试验", html)
            self.assertIn("2026-07-09T00:45:35+00:00", html)
            self.assertIn("国内", html)
            self.assertIn("靶点治疗", html)
            self.assertIn("招募状态须重新核实", html)
            self.assertIn('href="#trial-NCT00000001"', html)
            self.assertIn('id="trial-NCT00000001"', html)
            self.assertEqual(html.count('<details class="trial'), 1)

    def test_html_displays_duplicate_id_once_and_labels_closed_recruitment(self):
        trial = {
            "id": "NCT-CLOSED", "phases": [], "overall_status": "COMPLETED",
            "mechanism_category": {
                "category": "targeted_therapy", "label_zh": "靶点治疗",
                "label_en": "Targeted therapy",
            },
            "country_assessment": {"class": "overseas"},
            "resolved_source_url": "https://clinicaltrials.gov/study/NCT-CLOSED",
            "display_title": "Agent X · 靶点治疗 · 已关闭招募",
            "gating": {"verdict": "exclude", "satisfied": [], "pending": [], "exclusion_reasons": []},
            "risk_context": [], "efficacy_context": "",
        }
        payload = {
            "language": "zh-CN", "patient": self.patient,
            "trials": [trial, dict(trial)],
            "counts": {"match": 0, "conditional": 0, "exclude": 1},
            "geography_audit": {"overseas": 1},
            "database_as_of": "2026-07-09T00:45:35+00:00",
            "database_metadata": {}, "portal_delta": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            render_html(payload, output)
            html = output.read_text(encoding="utf-8")
        self.assertEqual(html.count('<details class="trial'), 1)
        self.assertIn("已关闭招募", html)

if __name__ == "__main__":
    unittest.main()
