from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "pipeline"))

from patient_input import load_patient_input


class PatientInputTests(unittest.TestCase):
    def _write(self, root: Path, name: str, value: dict) -> None:
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    def _archive(self, root: Path, *, with_location: bool = True) -> None:
        code = "PT-A1B2C3"
        self._write(root, "profile.json", {
            "schema": "cancer_buddy_profile_v3", "patient_code": code,
            "locale": "zh", "summary": {}, "latest_status": {},
        })
        self._write(root, "patient_summary.json", {
            "patient_code": code, "schema_version": "2",
            "demographics": {"sex_normalized": "female", "age": 55, "ecog": 1},
            "diagnosis": {
                "primary": "colorectal cancer", "histology": "adenocarcinoma",
                "stage": "IV", "metastasis_sites": ["liver"],
            },
            "current_status": {"regimen": None, "ecog": 1},
        })
        self._write(root, "molecular.json", {
            "patient_code": code, "schema_version": "2", "reports": [],
            "variants": [
                {"gene": "KRAS", "variant": "G12C", "verification_status": "clinician_verified"},
                {"gene": "TP53", "variant": "R175H", "verification_status": "disputed"},
            ],
            "ihc": [], "msi_results": [
                {"label": "MSI", "value": "MSS", "verification_status": "clinician_verified"}
            ], "mmr_results": [],
        })
        self._write(root, "treatment_lines.json", {
            "patient_code": code, "schema_version": "2",
            "episodes": [{
                "episode_id": "T1", "sequence_index": 0,
                "documented_line_label": None, "regimen": "FOLFOX",
                "ended_at": "2025-01-01", "verification_status": "clinician_verified",
                "source_refs": ["source"],
            }],
        })
        self._write(root, "labs.json", {
            "patient_code": code, "schema_version": "2", "panels": [],
        })
        self._write(root, "comorbidities.json", {
            "patient_code": code, "schema_version": "2",
            "conditions": [], "medications": [], "allergies": [],
        })
        if with_location:
            self._write(root, "matching_context.json", {
                "country": "China", "city": "Shanghai",
            })

    def test_unresolved_readiness_flags_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._archive(root)
            self._write(root, "readiness.json", {
                "patient_code": "PT-A1B2C3",
                "schema_version": "2",
                "documentation_coverage": {"pathology_documents": "present"},
                "review_flags": [{
                    "id": "RF-1", "category": "cross_source_conflict",
                    "affected_field": "diagnosis.stage",
                    "current_source_values": [],
                    "issue": "Conflicting stage", "resolution_status": "unresolved",
                }],
            })
            patient, audit = load_patient_input(root)
        self.assertIn(
            "Disputed source field: diagnosis.stage",
            patient["missing_critical_information"],
        )
        self.assertEqual(audit["unresolved_review_flag_count"], 1)

    def test_cancer_buddy_archive_is_normalized_without_line_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._archive(root)
            patient, audit = load_patient_input(root)
        self.assertEqual(patient["schema_version"], "clinical-trial-matching-patient-v1")
        self.assertEqual(patient["patient_id"], "PT-A1B2C3")
        self.assertEqual(patient["country"], "China")
        self.assertEqual(patient["report_language"], "zh-CN")
        self.assertEqual(patient["mutations"], ["KRAS G12C"])
        self.assertIsNone(patient["treatment_lines_completed"])
        self.assertEqual(audit["input_type"], "cancer_buddy_archive")

    def test_country_is_never_inferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._archive(root, with_location=False)
            with self.assertRaisesRegex(ValueError, "no explicit patient country"):
                load_patient_input(root)

    def test_regimen_presence_does_not_imply_ongoing_treatment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._archive(root)
            summary = json.loads((root / "patient_summary.json").read_text())
            summary["current_status"] = {
                "regimen": "RMC-6236", "therapy_ongoing": False, "ecog": 1,
            }
            self._write(root, "patient_summary.json", summary)
            patient, _ = load_patient_input(root)
        self.assertEqual(patient["current_therapy_status"], "RMC-6236")
        self.assertIs(patient["current_therapy_ongoing"], False)
