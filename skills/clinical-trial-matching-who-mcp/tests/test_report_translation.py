import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "render"))

from report_translation import (
    apply_units, collect_units, contains_narrative_language, needs_translation,
    mask_protected_facts, prepare_translation_work, protected_facts,
    maybe_translate_report, restore_protected_facts, translate_batch_resilient,
    translation_batches, translation_batch_limits,
)


class ReportTranslationTests(unittest.TestCase):
    def test_translation_preserves_identifiers_and_does_not_gate(self):
        payload = {
            "formal_report_ready": True, "language": "en",
            "trials": [{"id": "NCT06385925", "gating": {"rationale": "NCT06385925 matches KRAS G12D."}}],
        }
        units = collect_units(payload)
        result = apply_units(payload, units, {units[0]["id"]: "NCT06385925 与 KRAS G12D 匹配。"})
        self.assertTrue(result["formal_report_ready"])
        self.assertEqual(result["language"], "zh-CN")
        self.assertIn("NCT06385925", result["trials"][0]["gating"]["rationale"])

    def test_translation_that_drops_protected_fact_is_ignored(self):
        payload = {"trials": [{"gating": {"rationale": "NCT06385925 matches KRAS G12D."}}]}
        units = collect_units(payload)
        result = apply_units(payload, units, {units[0]["id"]: "这是合适的。"})
        self.assertEqual(result["trials"][0]["gating"]["rationale"], "NCT06385925 matches KRAS G12D.")


    def test_all_patient_facing_report_sections_are_collected(self):
        payload = {
            "decision_report": {
                "patient_summary": {"summary_text": "Patient summary."},
                "consistency_flags": [{"flag": "Labs are missing.", "evidence": "No CBC."}],
                "goals_of_care": {
                    "reasons": ["Rapid progression."],
                    "discussion_recommendation": "Discuss options.",
                },
                "decision_paths": [{
                    "trial_title": "A study of Drug-X.",
                    "rationale": "Relevant to the patient.",
                    "blockers_pending": ["Confirm liver function."],
                    "efficacy_snapshot": {"applies_because": "Same cancer type."},
                }],
            },
            "trials": [{
                "display_title": "Drug-X for solid tumors",
                "efficacy_context": "No direct efficacy data.",
                "risk_context": ["Hepatic risk."],
                "gating": {"inclusion_evaluation": [{
                    "criterion": "Adequate liver function.",
                    "reason": "Current values are missing.",
                }]},
                "development_evidence": [{
                    "findings": "Early activity.",
                    "applicability": "Same mutation.",
                    "limitations": "Small cohort.",
                }],
            }],
        }
        paths = {unit["path"] for unit in collect_units(payload)}
        expected = {
            "decision_report/patient_summary/summary_text",
            "decision_report/consistency_flags/0/flag",
            "decision_report/consistency_flags/0/evidence",
            "decision_report/goals_of_care/reasons/0",
            "decision_report/goals_of_care/discussion_recommendation",
            "decision_report/decision_paths/0/trial_title",
            "decision_report/decision_paths/0/rationale",
            "decision_report/decision_paths/0/blockers_pending/0",
            "decision_report/decision_paths/0/efficacy_snapshot/applies_because",
            "trials/0/display_title",
            "trials/0/efficacy_context",
            "trials/0/risk_context/0",
            "trials/0/gating/inclusion_evaluation/0/criterion",
            "trials/0/gating/inclusion_evaluation/0/reason",
            "trials/0/development_evidence/0/findings",
            "trials/0/development_evidence/0/applicability",
            "trials/0/development_evidence/0/limitations",
        }
        self.assertTrue(expected.issubset(paths))

    def test_chinese_text_is_not_sent_for_translation(self):
        self.assertFalse(needs_translation("患者为 KRAS G12D 突变胰腺癌。"))
        self.assertTrue(needs_translation("The patient has KRAS G12D pancreatic cancer."))

    def test_clinical_token_without_narrative_is_not_translated(self):
        self.assertFalse(contains_narrative_language("ECOG 1"))
        self.assertFalse(contains_narrative_language("KRAS G12D"))
        self.assertTrue(contains_narrative_language("Patient ECOG is 1"))

    def test_protected_facts_are_atomic_and_work_next_to_chinese(self):
        source = protected_facts("ECOG 0-1; 50-year-old; KRAS G12D; NCT06385925")
        translated = protected_facts("ECOG 0-1；50岁；KRAS G12D；NCT06385925试验")
        self.assertTrue(source.issubset(translated))
        self.assertNotIn("50-year-old", source)

    def test_protected_facts_round_trip_through_placeholders(self):
        source = "NCT06385925 tests KRAS G12D at 50 mg; see https://example.test/x."
        masked, replacements = mask_protected_facts(source)
        self.assertNotIn("NCT06385925", masked)
        restored = restore_protected_facts(masked, replacements)
        self.assertEqual(restored, source)

    def test_duplicate_sources_share_one_model_request_and_cached_translation(self):
        units = [
            {"id": "u0", "path": "0", "source": "Confirm liver function."},
            {"id": "u1", "path": "1", "source": "Confirm liver function."},
            {"id": "u2", "path": "2", "source": "Confirm ECOG status."},
        ]
        translated, pending, aliases = prepare_translation_work(
            units, {"u0": "确认肝功能。"}
        )
        self.assertEqual(translated["u1"], "确认肝功能。")
        self.assertEqual([unit["id"] for unit in pending], ["u2"])
        self.assertEqual(aliases, {"u2": ["u2"]})

    def test_translation_batches_are_not_selected_by_model_name(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(translation_batch_limits("model-a"), (200, 16000))
            self.assertEqual(translation_batch_limits("model-b"), (200, 16000))

    def test_only_china_patient_enters_translation(self):
        china = {"patient": {"country": "China"}, "language": "zh-CN"}
        overseas = {"patient": {"country": "United States"}, "language": "zh-CN"}
        translated = {**china, "translated": True}
        with mock.patch.dict(os.environ, {"MODEL_PROVIDER": "openai"}, clear=True), mock.patch(
            "report_translation.translate_payload", return_value=translated,
        ) as runner:
            self.assertTrue(maybe_translate_report(
                china, cache_path=Path("translation-cache.json")
            )["translated"])
            result = maybe_translate_report(
                overseas, cache_path=Path("translation-cache.json")
            )
        runner.assert_called_once()
        self.assertEqual(result["language"], "en")

    def test_required_translation_rejects_missing_api_configuration(self):
        payload = {"patient": {"country": "CN"}, "language": "zh-CN"}
        with mock.patch.dict(os.environ, {"TRANSLATION_MODE": "required"}, clear=True):
            with self.assertRaisesRegex(ValueError, "requires"):
                maybe_translate_report(payload, cache_path=Path("translation-cache.json"))

    def test_translation_batches_respect_character_budget(self):
        units = [
            {"id": f"u{index}", "path": str(index), "source": "x" * 60}
            for index in range(5)
        ]
        batches = translation_batches(units, max_units=3, max_characters=130)
        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])

    def test_truncated_translation_batch_is_split_without_losing_units(self):
        units = [
            {"id": f"u{index}", "path": str(index), "source": "source"}
            for index in range(4)
        ]
        calls = []

        def translate_once(batch):
            calls.append([unit["id"] for unit in batch])
            if len(batch) > 1:
                raise RuntimeError("Model response was truncated: length")
            return {batch[0]["id"]: "translation"}

        translated = translate_batch_resilient(units, translate_once)
        self.assertEqual(set(translated), {"u0", "u1", "u2", "u3"})
        self.assertGreater(len(calls), 1)

    def test_network_failure_is_not_multiplied_by_batch_splitting(self):
        units = [{"id": "u0", "path": "0", "source": "source"},
                 {"id": "u1", "path": "1", "source": "source"}]
        calls = []

        def translate_once(batch):
            calls.append(batch)
            raise RuntimeError("Model API connection failed after 5 attempts")

        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            translate_batch_resilient(units, translate_once)
        self.assertEqual(len(calls), 1)

    def test_single_omitted_translation_falls_back_to_source(self):
        unit = {"id": "u0", "path": "0", "source": "source"}
        translated = translate_batch_resilient([unit], lambda batch: {})
        self.assertEqual(translated, {})

    def test_malformed_json_batch_is_split(self):
        units = [{"id": "u0", "path": "0", "source": "first"},
                 {"id": "u1", "path": "1", "source": "second"}]

        def translate_once(batch):
            if len(batch) > 1:
                raise ValueError("Model API did not return one valid JSON object")
            return {batch[0]["id"]: "translated"}

        translated = translate_batch_resilient(units, translate_once)
        self.assertEqual(set(translated), {"u0", "u1"})


if __name__ == "__main__":
    unittest.main()
