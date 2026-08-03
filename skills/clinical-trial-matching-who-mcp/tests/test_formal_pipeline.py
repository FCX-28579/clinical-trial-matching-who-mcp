from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for relative in ("scripts/pipeline", "scripts/render"):
    sys.path.insert(0, str(ROOT / relative))

import run_formal_pipeline as formal
from html_renderer import render_validation_html


class FormalPipelineStateTests(unittest.TestCase):
    def test_decision_input_keeps_all_non_excluded_candidates_in_compact_form(self):
        gating = [
            {
                "trial_id": "KEEP",
                "gating": {
                    "verdict": "conditional", "confidence": 0.8,
                    "blockers_satisfied": [], "blockers_failed": [],
                    "blockers_pending": ["lab"], "hard_rules_triggered": [],
                    "rationale": "candidate",
                },
            },
            {
                "trial_id": "DROP",
                "gating": {
                    "verdict": "exclude", "confidence": 0.9,
                    "blockers_satisfied": [], "blockers_failed": ["wrong cancer"],
                    "blockers_pending": [], "hard_rules_triggered": [],
                    "rationale": "excluded",
                },
            },
        ]
        deep = [{
            **gating[0],
            "risk_annotation": {
                "trial_mechanisms_identified": ["target"],
                "risks": [],
            },
            "efficacy_context": {
                "efficacy_snapshot": {
                    "match_type": "no_data", "applies_because": "early trial",
                },
                "vs_soc": {"available": False},
                "development_evidence": [],
            },
        }]
        jobs = {"batches": [{"trials": [{"id": "KEEP", "title": "Keep trial"}]}]}
        result = formal._decision_input(gating, deep, jobs)
        self.assertEqual(result["analysis_metadata"]["candidate_count"], 1)
        self.assertEqual(
            result["analysis_metadata"]["match_inventory_size"],
            {"match": 0, "conditional": 1, "exclude": 1},
        )
        self.assertEqual(
            [item["trial_id"] for item in result["analyzed_trials"]], ["KEEP"]
        )
        self.assertNotIn("risk_annotation", result["analyzed_trials"][0])

    def test_prepare_rejects_reused_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "stale.json").write_text("{}", encoding="utf-8")
            args = argparse.Namespace(run_dir=str(run_dir))
            with self.assertRaisesRegex(ValueError, "new or empty"):
                formal.prepare_formal(args)

    def test_prepare_forces_unlimited_analysis_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            patient = root / "patient.json"
            plan = root / "plan.json"
            patient.write_text("{}", encoding="utf-8")
            plan.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(
                run_dir=str(root / "run"),
                patient=str(patient),
                plan=str(plan),
                db=None,
                mcp_python=None,
                mcp_server=None,
                mcp_transport="streamable-http",
                mcp_url="http://127.0.0.1:8000/mcp",
                max_per_query=5000,
                total_limit=20000,
                batch_size=5,
                portal_delta=None,
            )
            prepared = {
                "all_verified_trials": [{"id": "T1"}, {"id": "T2"}],
                "hard_excluded_trials": [{"id": "T2"}],
                "analysis_candidate_ids": ["T1"],
            }
            with mock.patch.object(formal, "prepare", return_value=prepared) as called:
                state = formal.prepare_formal(args)
            self.assertEqual(called.call_args.kwargs["prefilter_limit"], 0)
            self.assertEqual(called.call_args.kwargs["analysis_limit"], 0)
            self.assertEqual(state["stage"], "gater_pending")
            self.assertEqual(state["recall_count"], 2)

    def test_prepare_requires_explicit_external_registry_authorization(self):
        with tempfile.TemporaryDirectory() as temp:
            args = argparse.Namespace(
                run_dir=str(Path(temp) / "run"),
                portal_delta_mode="auto",
                skip_live_registry_verification=False,
                authorize_external_registry_access=False,
            )
            with mock.patch.dict(
                os.environ, {"EXTERNAL_REGISTRY_ACCESS_AUTHORIZED": "0"}
            ):
                with self.assertRaisesRegex(ValueError, "explicit operator authorization"):
                    formal.prepare_formal(args)

    def test_finalize_cannot_skip_merge_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            formal._save_state(run_dir, {
                "schema_version": "formal-pipeline-state-v1",
                "stage": "gater_pending",
            })
            with self.assertRaisesRegex(ValueError, "not allowed"):
                formal.finalize_formal(run_dir)

    def test_refresh_freshness_rejects_delta_with_new_trials(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            prepared = run_dir / "prepared.json"
            prepared.write_text(json.dumps({
                "database_as_of": "2026-07-23T00:00:00+00:00",
            }), encoding="utf-8")
            formal._save_state(run_dir, {
                "schema_version": "formal-pipeline-state-v1",
                "stage": "validation_failed",
                "prepared_path": str(prepared),
            })
            with mock.patch.object(
                formal, "_load_portal_delta",
                return_value=([{"id": "NEW"}], {"status": "executed"}),
            ):
                with self.assertRaisesRegex(ValueError, "contains new trials"):
                    formal.refresh_formal_freshness(
                        run_dir, run_dir / "portal_delta.json"
                    )
            self.assertEqual(formal._load_state(run_dir)["stage"], "validation_failed")

    def test_deep_status_requires_exact_id_set(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            jobs = root / "deep_jobs.json"
            batches = root / "deep-batches"
            batches.mkdir()
            jobs.write_text(json.dumps({
                "batches": [{"trials": [{"id": "T1"}, {"id": "T2"}]}],
            }), encoding="utf-8")
            (batches / "deep-batch-001.json").write_text(json.dumps({
                "analyzed_trials": [{"trial_id": "T1"}],
            }), encoding="utf-8")
            audit = formal._deep_status(jobs, batches)
            self.assertFalse(audit["complete"])
            self.assertEqual(audit["missing"], ["T2"])

    def test_formal_complete_status_uses_the_real_deep_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            gater_dir = run_dir / "gater-batches"
            deep_dir = run_dir / "deep-batches"
            gater_dir.mkdir()
            deep_dir.mkdir()
            jobs = run_dir / "analysis_jobs.json"
            deep_jobs = run_dir / "deep_jobs.json"
            jobs.write_text(json.dumps({
                "schema_version": "clinical-analysis-jobs-v2",
                "batches": [{"trials": [{"id": "T1"}]}],
            }), encoding="utf-8")
            deep_jobs.write_text(json.dumps({
                "batches": [{"trials": [{"id": "T1"}]}],
            }), encoding="utf-8")
            (gater_dir / "gater-batch-001.json").write_text(json.dumps({
                "analyzed_trials": [{
                    "trial_id": "T1", "gating": {"verdict": "conditional"},
                }],
            }), encoding="utf-8")
            (deep_dir / "deep-batch-001.json").write_text(json.dumps({
                "analyzed_trials": [{"trial_id": "T1"}],
            }), encoding="utf-8")
            formal._save_state(run_dir, {
                "schema_version": "formal-pipeline-state-v1",
                "stage": "formal_complete",
                "gater_jobs_path": str(jobs),
                "gater_batch_dir": str(gater_dir),
                "deep_jobs_path": str(deep_jobs),
                "deep_batch_dir": str(deep_dir),
                "formal_report_ready": True,
            })
            result = formal.formal_status(run_dir)
            self.assertTrue(result["deep_status"]["complete"])
            self.assertTrue(result["batch_status"]["stages"]["deep"]["complete"])
            self.assertTrue(result["batch_status"]["complete"])
            self.assertTrue(result["complete"])

    def test_deep_jobs_requires_gater_stage_not_whole_workflow_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            state = {
                "schema_version": "formal-pipeline-state-v1",
                "stage": "gater_pending",
                "gater_jobs_path": str(run_dir / "analysis_jobs.json"),
                "gater_batch_dir": str(run_dir / "gater-batches"),
                "patient_path": str(run_dir / "patient.json"),
                "deep_jobs_path": str(run_dir / "deep_jobs.json"),
                "deep_batch_dir": str(run_dir / "deep-batches"),
            }
            formal._save_state(run_dir, state)
            staged_status = {
                "complete": False,
                "stages": {
                    "gater": {
                        "expected": 185,
                        "completed": 185,
                        "missing": [],
                        "unexpected": [],
                        "errors": [],
                        "complete": True,
                    },
                    "deep": {"expected": 104, "completed": 0, "complete": False},
                },
            }
            with (
                mock.patch.object(formal, "status", return_value=staged_status),
                mock.patch.object(formal, "create_deep_jobs", return_value={"trials": 104}) as create,
            ):
                result = formal.create_formal_deep_jobs(run_dir)
        self.assertEqual(result["stage"], "deep_pending")
        self.assertEqual(result["deep_expected_count"], 104)
        create.assert_called_once()


class ValidationRendererTests(unittest.TestCase):
    def test_validation_artifact_has_no_patient_trial_cards(self):
        payload = {
            "language": "en",
            "run_manifest": {
                "counts": {
                    "recall": 232,
                    "hard_excluded": 0,
                    "gater_completed": 24,
                    "deep_completed": 10,
                    "risk_completed": 10,
                    "efficacy_completed": 10,
                    "evidence_completed": 10,
                    "omitted": 208,
                },
                "quality_gates": {"complete_analysis": False},
                "coverage_audit": {"missing_disposition_ids": ["T25"]},
                "prepared_sha256": "a" * 64,
                "analysis_sha256": "b" * 64,
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "validation-report.html"
            render_validation_html(payload, output)
            html = output.read_text(encoding="utf-8")
        self.assertIn("Formal workflow validation failed", html)
        self.assertIn("Omitted", html)
        self.assertNotIn('class="trial', html)


if __name__ == "__main__":
    unittest.main()
