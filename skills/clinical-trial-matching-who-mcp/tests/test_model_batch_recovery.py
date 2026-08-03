from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PIPELINE = Path(__file__).resolve().parents[1] / "scripts" / "pipeline"
sys.path.insert(0, str(PIPELINE))

from model_batch_executor import (
    BatchContractError, _execute_one_batch, _retry_runner_failure,
    _validate_batch_output, _validate_decision_output, execute_batches,
    execute_decision,
)
from run_formal_pipeline import _execution_lock


RUNNER = r"""
import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--input", required=True)
p.add_argument("--output", required=True)
p.add_argument("--counter", required=True)
p.add_argument("--mode", required=True)
a = p.parse_args()
envelope = json.loads(Path(a.input).read_text(encoding="utf-8"))
expected = envelope.get("required_output", {}).get("expected_trial_ids", [])
def row(trial_id):
    return {
        "trial_id": trial_id,
        "gating": {
            "verdict": "exclude",
            "confidence": 0.9,
            "inclusion_evaluation": [],
            "exclusion_evaluation": [],
            "hard_rules_triggered": [],
            "blockers_satisfied": [],
            "blockers_failed": ["test"],
            "blockers_pending": [],
            "rationale": "test result"
        }
    }
counter = Path(a.counter)
attempt = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(attempt))
if a.mode == "invalid-once" and attempt == 1:
    result = {"analyzed_trials": [{"trial_id": expected[0]}]}
elif a.mode == "split" and len(expected) > 1:
    result = {"analyzed_trials": [{"trial_id": expected[0]}]}
elif a.mode == "decision-invalid-once" and attempt == 1:
    result = {"message": "not a decision"}
elif a.mode.startswith("decision"):
    result = {"decision_report": {"decision_paths": [], "goals_of_care": {"triggered": False}}}
else:
    result = {"analyzed_trials": [row(item) for item in expected]}
Path(a.output).parent.mkdir(parents=True, exist_ok=True)
Path(a.output).write_text(json.dumps(result), encoding="utf-8")
"""


class ModelBatchRecoveryTests(unittest.TestCase):
    @staticmethod
    def _gating(verdict: str = "exclude") -> dict:
        return {
            "verdict": verdict,
            "confidence": 0.9,
            "inclusion_evaluation": [],
            "exclusion_evaluation": [],
            "hard_rules_triggered": [],
            "blockers_satisfied": [],
            "blockers_failed": ["test"] if verdict == "exclude" else [],
            "blockers_pending": [],
            "rationale": "test result",
        }

    def _jobs(self, root: Path, trial_ids: list[str]) -> Path:
        skill = root / "skills" / "trial-gater" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: trial-gater\ndescription: test\n---", encoding="utf-8")
        jobs = root / "jobs.json"
        jobs.write_text(
            json.dumps(
                {
                    "skill_paths": {"trial-gater": str(skill)},
                    "batches": [
                        {
                            "batch_id": "gater-0001",
                            "stage": "gater",
                            "trials": [{"id": trial_id} for trial_id in trial_ids],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return jobs

    def _environment(self, runner: Path, counter: Path, mode: str) -> dict[str, str]:
        command = [
            sys.executable, str(runner), "--input", "{input}", "--output", "{output}",
            "--counter", str(counter), "--mode", mode,
        ]
        return {
            "MODEL_EXECUTION_BACKEND": "custom",
            "MODEL_BATCH_RUNNER_JSON": json.dumps(command),
        }

    def test_contract_failure_is_retried_and_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            counter = root / "counter.txt"
            output = root / "outputs"
            with patch.dict(os.environ, self._environment(runner, counter, "invalid-once")):
                result = execute_batches(
                    self._jobs(root, ["NCT1"]), output,
                    output_prefix="gater-batch", retries=1, timeout=20,
                )
            self.assertTrue(result["complete"])
            self.assertEqual(counter.read_text(), "2")
            self.assertTrue(list((output / "invalid-responses").glob("*.json")))

    def test_native_flat_gater_schema_is_wrapped_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "gater.json"
            output.write_text(
                json.dumps(
                    {
                        "analyzed_trials": [
                            {"trial_id": "NCT1", **self._gating("exclude")}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = _validate_batch_output(
                {
                    "stage": "gater",
                    "patient": {},
                    "trials": [{"id": "NCT1"}],
                },
                output,
            )
            row = payload["analyzed_trials"][0]
            self.assertEqual(row["gating"]["verdict"], "exclude")
            self.assertNotIn("verdict", row)
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertIn("gating", persisted["analyzed_trials"][0])

    def test_unambiguous_hard_line_conflict_cannot_remain_conditional(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "gater.json"
            gating = {
                **self._gating("conditional"),
                "hard_rules_triggered": ["R2"],
                "blockers_failed": ["Prior systemic therapy"],
                "R2_detail": {"severity": "hard", "note": "First-line only"},
            }
            output.write_text(json.dumps({
                "analyzed_trials": [{"trial_id": "NCT1", **gating}],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "gater", "patient": {}, "trials": [{"id": "NCT1"}],
            }, output)
            self.assertEqual(payload["analyzed_trials"][0]["gating"]["verdict"], "exclude")

    def test_cohort_specific_hard_conflict_preserves_alternative_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "gater.json"
            gating = {
                **self._gating("conditional"),
                "hard_rules_triggered": ["R2"],
                "R2_detail": {
                    "severity": "hard",
                    "cohort_analysis": "First-line cohort fails; previously treated basket cohort may qualify.",
                },
            }
            output.write_text(json.dumps({
                "analyzed_trials": [{"trial_id": "NCT1", **gating}],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "gater", "patient": {}, "trials": [{"id": "NCT1"}],
            }, output)
            self.assertEqual(payload["analyzed_trials"][0]["gating"]["verdict"], "conditional")

    def test_existing_native_gater_output_resumes_without_calling_a_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = self._jobs(root, ["NCT1"])
            output_dir = root / "outputs"
            output_dir.mkdir()
            (output_dir / "gater-batch-0001.json").write_text(
                json.dumps(
                    {
                        "analyzed_trials": [
                            {"trial_id": "NCT1", **self._gating("exclude")}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                result = execute_batches(
                    jobs, output_dir, output_prefix="gater-batch",
                    retries=0, timeout=20,
                )
            self.assertEqual(result["resumed_batches"], 1)
            self.assertEqual(result["executed_batches"], 0)

    def test_native_flat_deep_schemas_are_wrapped_and_gating_comes_from_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            gating = self._gating("conditional")
            output.write_text(
                json.dumps(
                    {
                        "analyzed_trials": [
                            {
                                "trial_id": "NCT1",
                                "trial_mechanisms_identified": ["test"],
                                "patient_cancer_context": "CRC",
                                "risks": [],
                                "risks_considered_but_omitted": [],
                                "efficacy_snapshot": {
                                    "match_type": "no_data",
                                    "applies_because": "No applicable data",
                                },
                                "vs_soc": {"available": False},
                                "development_evidence": [],
                                "evidence_search": {
                                    "status": "no_relevant_publication",
                                    "searched_at": "2026-07-28T00:00:00Z",
                                    "queries": [],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            payload = _validate_batch_output(
                {
                    "stage": "deep",
                    "patient": {"cancer_type": "CRC"},
                    "trials": [{"id": "NCT1", "gating": gating}],
                },
                output,
            )
            row = payload["analyzed_trials"][0]
            self.assertEqual(row["gating"], gating)
            self.assertIn("risk_annotation", row)
            self.assertIn("efficacy_context", row)
            self.assertNotIn("risks", row)
            self.assertNotIn("efficacy_snapshot", row)

    def test_complementary_deep_rows_for_the_same_trial_are_coalesced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            output.write_text(json.dumps({
                "analyzed_trials": [
                    {
                        "trial_id": "NCT1",
                        "trial_mechanisms_identified": ["test"],
                        "patient_cancer_context": "CRC",
                        "risks": [],
                        "risks_considered_but_omitted": [],
                    },
                    {
                        "trial_id": "NCT1",
                        "efficacy_snapshot": {
                            "match_type": "no_data",
                            "applies_because": "No applicable data",
                        },
                        "vs_soc": {"available": False},
                        "development_evidence": [],
                        "evidence_search": {
                            "status": "no_relevant_publication",
                            "searched_at": "2026-07-29T00:00:00Z",
                            "queries": [],
                        },
                    },
                ]
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1",
                    "gating": self._gating("conditional"),
                }],
            }, output)
            self.assertEqual(len(payload["analyzed_trials"]), 1)
            self.assertIn("risk_annotation", payload["analyzed_trials"][0])
            self.assertIn("efficacy_context", payload["analyzed_trials"][0])

    def test_deep_audit_fields_come_from_authoritative_prefetch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            output.write_text(json.dumps({
                "trial_id": "NCT1",
                "gating": self._gating("exclude"),
                "trial_mechanisms_identified": ["test"],
                "patient_cancer_context": "NSCLC",
                "risks": [],
                "risks_considered_but_omitted": [],
                "efficacy_snapshot": {
                    "match_type": "no_data",
                    "applies_because": "No applicable data",
                },
                "vs_soc": {"available": False},
                "development_evidence": {},
                "evidence_search": {"status": "no_relevant_publication"},
            }), encoding="utf-8")
            prefetch = {
                "status": "no_results",
                "searched_at": "2026-07-29T00:00:00+00:00",
                "queries": ["NCT1 OR Agent X"],
                "candidates": [],
            }
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1", "gating": self._gating("conditional"),
                    "publication_prefetch": prefetch,
                }],
            }, output)
            search = payload["analyzed_trials"][0]["efficacy_context"]["evidence_search"]
            self.assertEqual(search["searched_at"], prefetch["searched_at"])
            self.assertEqual(search["queries"], prefetch["queries"])
            self.assertEqual(
                payload["analyzed_trials"][0]["gating"]["verdict"], "conditional"
            )
            self.assertEqual(
                payload["analyzed_trials"][0]["risk_annotation"]["patient_cancer_context"],
                "CRC",
            )
            self.assertEqual(
                payload["analyzed_trials"][0]["efficacy_context"]["development_evidence"], []
            )

    def test_incomplete_or_placeholder_development_evidence_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            output.write_text(json.dumps({
                "analyzed_trials": [{
                    "trial_id": "NCT1",
                    "trial_mechanisms_identified": ["test"],
                    "patient_cancer_context": "CRC",
                    "risks": [],
                    "risks_considered_but_omitted": [],
                    "efficacy_snapshot": {
                        "match_type": "no_data",
                        "applies_because": "No applicable data",
                    },
                    "vs_soc": {"available": False},
                    "development_evidence": [{
                        "evidence_stage": "no_relevant_publication",
                        "citation": None,
                        "url": None,
                        "findings": None,
                        "patient_applicability": "No paper was found",
                        "limitations": "No public data",
                    }],
                    "evidence_search": {"status": "found"},
                }],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1", "gating": self._gating("conditional"),
                    "publication_prefetch": {
                        "searched_at": "2026-07-29T00:00:00+00:00",
                        "queries": ["NCT1"], "candidates": [],
                    },
                }],
            }, output)
            efficacy = payload["analyzed_trials"][0]["efficacy_context"]
            self.assertEqual(efficacy["development_evidence"], [])
            self.assertEqual(
                efficacy["evidence_search"]["status"], "no_relevant_publication"
            )

    def test_null_placeholders_are_removed_from_deep_risk_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            output.write_text(json.dumps({
                "analyzed_trials": [{
                    "trial_id": "NCT1",
                    "risk_annotation": {
                        "patient_cancer_context": "CRC",
                        "risks": [
                            {
                                "key": "test",
                                "applies_because": "Patient-specific reason",
                            },
                            None,
                        ],
                    },
                    "efficacy_context": {
                        "efficacy_snapshot": {
                            "match_type": "no_data",
                            "applies_because": "No applicable data",
                        },
                        "vs_soc": {"available": False},
                        "development_evidence": [],
                        "evidence_search": {
                            "status": "no_relevant_publication",
                            "searched_at": "2026-07-29T00:00:00+00:00",
                            "queries": ["NCT1"],
                        },
                    },
                }],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1", "gating": self._gating("conditional"),
                }],
            }, output)
            self.assertEqual(
                len(payload["analyzed_trials"][0]["risk_annotation"]["risks"]), 1
            )

    def test_missing_risk_applicability_reuses_existing_narrative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            output.write_text(json.dumps({
                "analyzed_trials": [{
                    "trial_id": "NCT1",
                    "risk_annotation": {
                        "patient_cancer_context": "CRC",
                        "risks": [{
                            "key": "phase_1",
                            "narrative": ["Early dose escalation creates uncertain benefit."],
                        }],
                    },
                    "efficacy_context": {
                        "efficacy_snapshot": {
                            "match_type": "no_data",
                            "applies_because": "No applicable data",
                        },
                        "vs_soc": {"available": False},
                        "development_evidence": [],
                        "evidence_search": {
                            "status": "no_relevant_publication",
                            "searched_at": "2026-07-29T00:00:00+00:00",
                            "queries": ["NCT1"],
                        },
                    },
                }],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1", "gating": self._gating("conditional"),
                }],
            }, output)
            risk = payload["analyzed_trials"][0]["risk_annotation"]["risks"][0]
            self.assertEqual(
                risk["applies_because"],
                "Early dose escalation creates uncertain benefit.",
            )

    def test_evidence_urls_are_structurally_valid_or_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "deep.json"
            base = {
                "evidence_stage": "registry_support",
                "citation": "ClinicalTrials.gov record",
                "findings": "Early phase study",
                "applicability": "Same trial",
                "limitations": "No published outcome",
            }
            output.write_text(json.dumps({
                "analyzed_trials": [{
                    "trial_id": "NCT1",
                    "trial_mechanisms_identified": ["test"],
                    "patient_cancer_context": "CRC",
                    "risks": [],
                    "risks_considered_but_omitted": [],
                    "efficacy_snapshot": {
                        "match_type": "no_data",
                        "applies_because": "No applicable data",
                    },
                    "vs_soc": {"available": False},
                    "development_evidence": [
                        {**base, "url": "NCT12345678"},
                        {**base, "url": "N/A"},
                    ],
                    "evidence_search": {"status": "found"},
                }],
            }), encoding="utf-8")
            payload = _validate_batch_output({
                "stage": "deep",
                "patient": {"cancer_type": "CRC"},
                "trials": [{
                    "id": "NCT1", "gating": self._gating("conditional"),
                    "publication_prefetch": {
                        "searched_at": "2026-07-29T00:00:00+00:00",
                        "queries": ["NCT1"], "candidates": [],
                    },
                }],
            }, output)
            evidence = payload["analyzed_trials"][0]["efficacy_context"][
                "development_evidence"
            ]
            self.assertEqual(evidence, [])
            search = payload["analyzed_trials"][0]["efficacy_context"]["evidence_search"]
            self.assertEqual(search["status"], "no_relevant_publication")
            self.assertEqual(search["rejected_model_evidence_count"], 1)

    def test_new_adapter_can_restore_a_quarantined_response_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = self._jobs(root, ["NCT1"])
            output_dir = root / "outputs"
            invalid = output_dir / "invalid-responses"
            invalid.mkdir(parents=True)
            (invalid / "gater-batch-0001-old.json").write_text(json.dumps({
                "analyzed_trials": [{"trial_id": "NCT1", **self._gating("exclude")}],
            }), encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                result = execute_batches(
                    jobs, output_dir, output_prefix="gater-batch",
                    retries=0, timeout=20,
                )
            self.assertEqual(result["resumed_batches"], 1)
            self.assertTrue((output_dir / "gater-batch-0001.json").exists())

    def test_invalid_existing_output_is_replaced_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            counter = root / "counter.txt"
            output = root / "outputs"
            output.mkdir()
            (output / "gater-batch-0001.json").write_text('{"analyzed_trials":[]}', encoding="utf-8")
            with patch.dict(os.environ, self._environment(runner, counter, "valid")):
                result = execute_batches(
                    self._jobs(root, ["NCT1"]), output,
                    output_prefix="gater-batch", retries=0, timeout=20,
                )
            self.assertTrue(result["complete"])
            self.assertEqual(result["executed_batches"], 1)
            self.assertTrue(list((output / "invalid-responses").glob("*.json")))

    def test_failed_batch_falls_back_to_resumable_single_trials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            output = root / "outputs"
            with patch.dict(
                os.environ, self._environment(runner, root / "counter.txt", "split")
            ):
                execute_batches(
                    self._jobs(root, ["NCT1", "NCT2", "NCT3"]), output,
                    output_prefix="gater-batch", retries=0, timeout=20,
                )
            payload = json.loads((output / "gater-batch-0001.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {item["trial_id"] for item in payload["analyzed_trials"]},
                {"NCT1", "NCT2", "NCT3"},
            )
            self.assertEqual(len(list((output / "recovery" / "gater-batch-0001").glob("*.json"))), 3)

    def test_multi_trial_contract_failure_splits_without_repeating_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            counter = root / "counter.txt"
            with patch.dict(
                os.environ, self._environment(runner, counter, "split")
            ):
                execute_batches(
                    self._jobs(root, ["NCT1", "NCT2"]), root / "outputs",
                    output_prefix="gater-batch", retries=2, timeout=20,
                )
            self.assertEqual(int(counter.read_text(encoding="utf-8")), 3)

    def test_decision_contract_failure_retries_and_existing_valid_output_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "runner.py"
            runner.write_text(RUNNER, encoding="utf-8")
            patient = root / "patient.json"
            analysis = root / "analysis.json"
            skill = root / "skills" / "decision-synthesizer" / "SKILL.md"
            patient.write_text("{}", encoding="utf-8")
            analysis.write_text('{"analyzed_trials":[]}', encoding="utf-8")
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: decision-synthesizer\ndescription: test\n---", encoding="utf-8")
            output = root / "decision.json"
            environment = self._environment(runner, root / "counter.txt", "decision-invalid-once")
            with patch.dict(os.environ, environment):
                execute_decision(
                    patient_path=patient, analysis_path=analysis, skill_path=skill,
                    output_path=output, retries=1, timeout=20,
                )
                resumed = execute_decision(
                    patient_path=patient, analysis_path=analysis, skill_path=skill,
                    output_path=output, retries=1, timeout=20,
                )
            self.assertTrue(resumed["resumed"])
            self.assertEqual((root / "counter.txt").read_text(), "2")

    def test_decision_paths_drop_empty_slots_and_reject_unknown_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "decision.json"
            output.write_text(json.dumps({
                "decision_report": {
                    "decision_paths": [
                        {"rank": 1, "trial_id": None, "rationale": "empty slot"},
                        {"rank": 3, "trial_id": "NCT1", "rationale": "selected"},
                    ],
                    "goals_of_care": {},
                },
            }), encoding="utf-8")
            payload = _validate_decision_output(output, {"NCT1"})
            paths = payload["decision_report"]["decision_paths"]
            self.assertEqual(paths, [{
                "rank": 1, "trial_id": "NCT1", "rationale": "selected",
            }])
            output.write_text(json.dumps({
                "decision_report": {
                    "decision_paths": [{"rank": 1, "trial_id": "UNKNOWN"}],
                    "goals_of_care": {},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not a non-excluded candidate"):
                _validate_decision_output(output, {"NCT1"})

    def test_decision_paths_are_top_three_and_persist_grounded_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "decision.json"
            output.write_text(json.dumps({
                "decision_report": {
                    "decision_paths": [
                        {"trial_id": f"NCT{i}", "rationale": "资格待核实路径"}
                        for i in range(1, 6)
                    ],
                    "goals_of_care": {},
                },
            }), encoding="utf-8")
            analyzed = [{
                "trial_id": f"NCT{i}",
                "title": f"Trial {i}",
                "gating": {
                    "verdict": "conditional",
                    "rationale": f"Patient-specific reason {i}",
                    "blockers_pending": ["screening"],
                },
                "risk_summary": {"risks": []},
                "efficacy_summary": {"applies_because": "same cancer"},
            } for i in range(1, 6)]
            payload = _validate_decision_output(
                output, {f"NCT{i}" for i in range(1, 6)}, analyzed, "zh-CN"
            )
            paths = payload["decision_report"]["decision_paths"]
            self.assertEqual(len(paths), 3)
            self.assertEqual(paths[0]["trial_title"], "Trial 1")
            self.assertIn("Patient-specific reason 1", paths[0]["rationale"])
            persisted = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(persisted["decision_report"]["decision_paths"], paths)

    def test_decision_rejects_claimed_paths_when_all_slots_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "decision.json"
            output.write_text(json.dumps({
                "decision_report": {
                    "decision_paths": [
                        {"rank": 1, "trial_id": None, "rationale": "empty slot"},
                    ],
                    "goals_of_care": {},
                    "v2_summary": {"decision_paths_emitted": 1},
                },
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "none referenced a valid"):
                _validate_decision_output(output, {"NCT1"})

    def test_duplicate_batch_suffix_is_rejected_before_model_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = self._jobs(root, ["NCT1"])
            payload = json.loads(jobs.read_text(encoding="utf-8"))
            payload["batches"].append(
                {"batch_id": "deep-0001", "trials": [{"id": "NCT2"}]}
            )
            jobs.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "suffix collision"):
                execute_batches(jobs, root / "outputs", output_prefix="batch", retries=0)

    def test_same_run_directory_cannot_be_executed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with _execution_lock(run_dir):
                with self.assertRaisesRegex(RuntimeError, "already using"):
                    with _execution_lock(run_dir):
                        pass

    def test_batches_run_with_bounded_stage_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = self._jobs(root, ["NCT1"])
            payload = json.loads(jobs.read_text(encoding="utf-8"))
            payload["stage"] = "gater"
            payload["batches"] = [
                {
                    "batch_id": f"gater-{index:04d}",
                    "stage": "gater",
                    "patient": {},
                    "trials": [{"id": f"NCT{index}"}],
                }
                for index in range(1, 4)
            ]
            jobs.write_text(json.dumps(payload), encoding="utf-8")

            def fake_execute(*args, **kwargs):
                time.sleep(0.15)
                return "executed"

            started = time.monotonic()
            with patch.dict(os.environ, {"MODEL_GATER_CONCURRENCY": "3"}), patch(
                "model_batch_executor._execute_one_batch", side_effect=fake_execute
            ):
                result = execute_batches(
                    jobs, root / "outputs", output_prefix="gater-batch", retries=0
                )
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.35)
            self.assertEqual(result["concurrency"], 3)

    def test_consecutive_failures_open_circuit_before_all_batches_are_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = self._jobs(root, ["NCT1"])
            payload = json.loads(jobs.read_text(encoding="utf-8"))
            payload["stage"] = "deep"
            payload["batches"] = [
                {
                    "batch_id": f"deep-{index:04d}",
                    "stage": "deep",
                    "patient": {"cancer_type": "CRC"},
                    "trials": [{"id": f"NCT{index}", "gating": self._gating("conditional")}],
                }
                for index in range(1, 11)
            ]
            jobs.write_text(json.dumps(payload), encoding="utf-8")
            with patch.dict(os.environ, {
                "MODEL_DEEP_CONCURRENCY": "2",
                "MODEL_CIRCUIT_BREAKER_FAILURES": "4",
            }), patch(
                "model_batch_executor._execute_one_batch",
                side_effect=RuntimeError("invalid model contract"),
            ) as execute:
                with self.assertRaisesRegex(RuntimeError, "circuit breaker opened"):
                    execute_batches(
                        jobs, root / "outputs", output_prefix="deep-batch", retries=0
                    )
            self.assertEqual(execute.call_count, 4)

    def test_permanent_authentication_failure_does_not_split_batch(self) -> None:
        from model_batch_executor import _should_split_batch
        self.assertFalse(_should_split_batch(RuntimeError("Model API HTTP 401")))
        self.assertFalse(_should_split_batch(RuntimeError("Model API connection failed")))
        self.assertTrue(_should_split_batch(RuntimeError("gating confidence must be 0..1")))
        self.assertTrue(_should_split_batch(
            BatchContractError("NCT1: risk applies_because is required")
        ))

    def test_batch_contract_error_recovers_as_single_trials(self) -> None:
        batch = {
            "batch_id": "clinical-deep-031",
            "stage": "deep",
            "trials": [{"id": "NCT1"}, {"id": "NCT2"}],
        }
        error = BatchContractError("NCT1: risk applies_because is required")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "model_batch_executor._run_envelope", side_effect=error
            ), patch(
                "model_batch_executor._recover_as_single_trials"
            ) as recover:
                outcome = _execute_one_batch(
                    {"stage": "deep"}, batch, root / "outputs",
                    root / "inputs", "deep-batch", retries=0, timeout=20,
                )
        self.assertEqual(outcome, "recovered")
        recover.assert_called_once()

    def test_api_transport_exhaustion_is_not_retried_by_batch_layer(self) -> None:
        self.assertFalse(_retry_runner_failure(
            RuntimeError("Model API connection failed after 5 attempts")
        ))
        self.assertFalse(_retry_runner_failure(
            RuntimeError("Model API HTTP 503: unavailable")
        ))
        self.assertTrue(_retry_runner_failure(
            RuntimeError("Model API did not return one valid JSON object")
        ))


if __name__ == "__main__":
    unittest.main()
