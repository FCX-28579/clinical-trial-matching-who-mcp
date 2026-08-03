from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for relative in ("scripts/retrieval", "scripts/verification", "scripts/scoring", "scripts/classification", "scripts/render"):
    sys.path.insert(0, str(ROOT / relative))

from feasibility import compute_feasibility, score_geographic_access
from html_renderer import classify_treatment_mechanism, render_html
from who_mcp_adapter import apply_hard_filters, merge_sources
from who_mcp_verifier import verify_batch


class RetrievalEdgeCaseTests(unittest.TestCase):
    def test_sparse_newer_delta_does_not_erase_local_fields(self):
        local = [{
            "trial_uid": "uid-1", "primary_registry_id": "NCT1", "title": "Complete title",
            "brief_summary": "Complete summary", "phase_normalized": "Phase 2",
            "intervention_summary": "Drug A", "last_update_date": "2026-07-01",
        }]
        delta = [{"id": "NCT1", "last_update_date": "2026-07-10", "title": ""}]
        result = merge_sources(local, delta, patient={"country": "China"}, database_as_of="2026-07-09")[0]
        self.assertEqual(result["title"], "Complete title")
        self.assertEqual(result["brief_summary"], "Complete summary")
        self.assertEqual(result["phases"], ["PHASE_2"])
        self.assertEqual(result["interventions"], ["Drug A"])
        self.assertEqual(result["last_update_date"], "2026-07-10")

    def test_merge_rejects_malformed_collections(self):
        with self.assertRaises(TypeError):
            merge_sources({"id": "NCT1"}, [], patient={}, database_as_of="2026-07-09")
        with self.assertRaises(TypeError):
            merge_sources(["NCT1"], [], patient={}, database_as_of="2026-07-09")

    def test_molecular_negation_is_not_string_hard_excluded(self):
        trial = {
            "id": "NCT1", "title": "RAS mutant study",
            "parsed_criteria": {"inclusion": ["RAS mutant disease"], "exclusion": ["RAS wild-type patients are excluded"]},
        }
        plan = {"treatment_lines": 2, "hard_exclude": {"molecular_mismatch": ["RAS wild-type only"]}}
        result = apply_hard_filters([trial], plan)
        self.assertEqual(len(result["included_trials"]), 1)
        self.assertEqual(len(result["excluded_trials"]), 0)
        self.assertTrue(result["included_trials"][0]["hard_filter_warnings"])

    def test_explicit_first_line_inclusion_is_filtered(self):
        trial = {"id": "NCT1", "parsed_criteria": {"inclusion": ["First-line only treatment-naive patients"]}}
        plan = {"treatment_lines": 2, "hard_exclude": {"first_line_only": True}}
        result = apply_hard_filters([trial], plan)
        self.assertEqual(len(result["excluded_trials"]), 1)


class VerificationAndScoringEdgeCaseTests(unittest.TestCase):
    def test_status_with_comma_is_normalized(self):
        trial = {"id": "NCT1"}
        detail = [{
            "found": True, "primary_registry_id": "NCT1", "title": "T",
            "recruitment_status_normalized": "Active, not recruiting", "phase_normalized": "Phase 1; Phase 2",
            "sites": [], "interventions": [], "parsed_criteria": {"raw": "criteria"},
        }]
        result = verify_batch([trial], detail, {"country": "China"})[0]
        self.assertEqual(result["overall_status"], "ACTIVE_NOT_RECRUITING")
        self.assertEqual(result["phases"], ["PHASE_1", "PHASE_2"])

    def test_missing_country_is_unknown_not_international(self):
        trial = {"overall_status": "RECRUITING", "last_update_date": "2026-07-01", "sites": []}
        score, flags = score_geographic_access(trial, {})
        self.assertEqual(score, 0.5)
        self.assertIn("missing", flags[0].lower())
        feasibility = compute_feasibility(trial, {"affordability_tier": "medium"})
        self.assertEqual(feasibility.sub_scores["financial_cost"], 0.5)

    def test_blank_site_city_is_not_treated_as_same_city(self):
        trial = {"sites": [{"country": "United States", "city": ""}], "patient_country_site_count": 1}
        patient = {"country": "United States", "city": "Boston", "affordability_tier": "medium"}
        feasibility = compute_feasibility(trial, patient)
        self.assertIn("Domestic cross-city travel is likely", feasibility.flags)


class ReportSafetyTests(unittest.TestCase):
    def test_renderer_uses_shared_mechanism_categories(self):
        self.assertEqual(classify_treatment_mechanism({"title": "Pan-KRAS RAS(ON) study"}, "conditional")[2], "ras_mapk_pathway")
        self.assertEqual(classify_treatment_mechanism({"title": "CAR-T cell therapy"}, "conditional")[2], "cell_and_biologic")
        self.assertEqual(classify_treatment_mechanism({"title": "CEA-positive CAR-T cell therapy"}, "conditional")[2], "cell_and_biologic")
        self.assertEqual(classify_treatment_mechanism({"title": "KRAS-mutated tumor RAS inhibitor"}, "conditional")[2], "ras_mapk_pathway")
        self.assertEqual(classify_treatment_mechanism({"title": "MSH2 tumor cell vaccine"}, "conditional")[2], "cell_and_biologic")
        targeted = {"title": "Calderasib plus pembrolizumab", "interventions": ["Calderasib", "Pembrolizumab"]}
        noisy_analysis = {"gating": {"exclusion_evaluation": [{"criterion": "prior cell therapy"}]}}
        self.assertEqual(classify_treatment_mechanism(targeted, "conditional", noisy_analysis)[2], "targeted_therapy")
        comparator_noise = {
            "efficacy_context": {"caveats": ["CAR-T may be considered as an alternative"]},
            "risk_annotation": {"trial_mechanisms_identified": ["KRAS inhibitor"]},
        }
        self.assertEqual(
            classify_treatment_mechanism(targeted, "conditional", comparator_noise)[2],
            "targeted_therapy",
        )

    def test_report_escapes_html_and_uses_registry_source_url(self):
        patient = {"patient_id": "<script>alert(1)</script>", "country": "China", "mutations": [], "treatment_history": []}
        trial = {
            "id": "NL-TEST", "title": "<b>unsafe</b>", "resolved_source_url": "https://example.org/trial/NL-TEST",
            "patient_country_site_count": 0, "phases": [], "feasibility": {"composite": 0.7},
            "overall_status": "RECRUITING", "display_title": "<b>unsafe</b> · 其他治疗与研究方向",
            "mechanism_category": {"category": "other", "label_zh": "其他治疗与研究方向", "label_en": "Other"},
            "country_assessment": {"class": "overseas"},
            "gating": {"verdict": "conditional", "satisfied": [], "pending": ["Formal review"], "exclusion_reasons": []},
            "risk_context": ["No specific risk"], "efficacy_context": "No efficacy inference",
        }
        payload = {
            "language": "zh-CN", "patient": patient, "trials": [trial],
            "counts": {"match": 0, "conditional": 1, "exclude": 0},
            "geography_audit": {"overseas": 1}, "portal_delta": {"status": "not_executed"},
            "database_as_of": "2026-07-09", "database_metadata": {"schema_version": "3"},
            "run_manifest": {"prepared_sha256": "abc", "analysis_sha256": "def"},
            "decision_report": {
                "patient_summary": {"summary_text": "患者摘要正文"},
                "consistency_flags": [{"flag": "需要核实肝功能"}],
                "decision_paths": [],
                "goals_of_care": {
                    "triggered": True,
                    "reasons": ["疾病进展较快"],
                    "discussion_recommendation": "讨论标准治疗与临床试验。",
                },
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            render_html(payload, output)
            html = output.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("https://example.org/trial/NL-TEST", html)
        self.assertNotIn(">0.7<", html)
        self.assertIn('data-filter="all"', html)
        self.assertIn('data-filter="domestic"', html)
        self.assertIn('data-filter="country_unverified"', html)
        self.assertIn('data-filter="overseas"', html)
        self.assertNotIn('data-filter="actionable"', html)
        self.assertNotIn('data-filter="excluded"', html)
        self.assertIn("患者摘要", html)
        self.assertIn("患者摘要正文", html)
        self.assertIn("需要核实肝功能", html)
        self.assertIn("讨论标准治疗与临床试验", html)
        self.assertIn("检索数据 SHA-256", html)
        self.assertIn("分析结果 SHA-256", html)

    def test_native_registry_is_displayed_in_domestic_filter(self):
        patient = {"patient_id": "P1", "country": "China", "mutations": []}
        trial = {
            "id": "ChiCTR1", "resolved_source_url": "https://example.org/ChiCTR1",
            "display_title": "Trial", "phases": [],
            "mechanism_category": {"category": "other", "label_zh": "其他", "label_en": "Other"},
            "country_assessment": {"class": "domestic_registry"},
            "gating": {"verdict": "conditional", "satisfied": [], "pending": [], "exclusion_reasons": []},
            "risk_context": [], "efficacy_context": "",
        }
        payload = {
            "language": "zh-CN", "patient": patient, "trials": [trial],
            "counts": {"match": 0, "conditional": 1, "exclude": 0},
            "geography_audit": {"domestic_registry": 1}, "portal_delta": {},
            "database_as_of": "2026-07-09", "database_metadata": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            render_html(payload, output)
            html = output.read_text(encoding="utf-8")
        self.assertIn('data-access="domestic"', html)
        self.assertIn("<b>1</b><span>国内可及</span>", html)

    def test_renderer_rejects_untrusted_language_and_active_url_schemes(self):
        patient = {"patient_id": "P1", "country": "China", "mutations": []}
        trial = {
            "id": "T1", "resolved_source_url": "javascript:alert(1)",
            "display_title": "Trial", "phases": [],
            "mechanism_category": {"category": "other", "label_zh": "其他", "label_en": "Other"},
            "country_assessment": {"class": "overseas"},
            "gating": {
                "verdict": "conditional", "satisfied": [], "pending": [],
                "exclusion_reasons": [],
            },
            "risk_context": [], "efficacy_context": "",
        }
        payload = {
            "language": "en", "patient": patient, "trials": [trial],
            "counts": {"match": 0, "conditional": 1, "exclude": 0},
            "geography_audit": {"overseas": 1}, "portal_delta": {},
            "database_as_of": "2026-07-09", "database_metadata": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.html"
            render_html(payload, output)
            self.assertNotIn("javascript:", output.read_text(encoding="utf-8"))
            payload["language"] = 'en\"><script>alert(1)</script>'
            with self.assertRaisesRegex(ValueError, "language"):
                render_html(payload, output)

if __name__ == "__main__":
    unittest.main()
