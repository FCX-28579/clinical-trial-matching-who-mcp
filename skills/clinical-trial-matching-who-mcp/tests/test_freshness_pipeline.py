from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for relative in ("scripts/pipeline", "scripts/retrieval", "scripts/verification"):
    sys.path.insert(0, str(ROOT / relative))

from direct_registry_verifier import _fetch, _webpage_status, verify_and_partition, verify_one
from model_batch_executor import execute_batches
from who_portal_delta import WhoPortalClient, build_delta


class _Portal:
    def __init__(self):
        self.searches = []

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return (["NCT00000001"], 1) if len(self.searches) == 1 else ([], 0)

    def trial(self, trial_id):
        return {"id": trial_id, "title": "New trial", "overall_status": "Recruiting"}


class FreshnessPipelineTests(unittest.TestCase):
    def test_portal_delta_runs_every_plan_variant_and_emits_audit(self):
        portal = _Portal()
        payload = build_delta(
            database_as_of="2026-07-23T00:00:00+00:00",
            plan={"keyword_groups": [{"label": "target", "queries": [
                {"condition": "colorectal cancer", "term": "KRAS G12C"}
            ]}]},
            client=portal,
        )
        self.assertEqual(payload["status"], "executed")
        self.assertEqual([trial["id"] for trial in payload["trials"]], ["NCT00000001"])
        self.assertEqual(len(payload["query_audit"]), 3)
        self.assertTrue(all(item["complete"] for item in payload["query_audit"]))

    def test_portal_application_error_page_is_not_a_successful_zero_result(self):
        client = WhoPortalClient(delay=0)
        responses = iter((
            '<input type="hidden" name="__VIEWSTATE" value="x">',
            "<html><title>Error Page</title><body>NoAccess.aspx</body></html>",
        ))
        client._open = lambda url, data=None: next(responses)
        with self.assertRaisesRegex(RuntimeError, "error page"):
            client.search(
                condition="cancer", date_start="01/01/2026", date_end="02/01/2026"
            )

    def test_nct_live_status_is_read_from_direct_api(self):
        body = json.dumps({"protocolSection": {
            "statusModule": {
                "overallStatus": "TERMINATED",
                "lastUpdatePostDateStruct": {"date": "2026-07-20"},
            },
            "contactsLocationsModule": {"locations": [{
                "facility": "Beijing Cancer Hospital", "status": "RECRUITING",
                "city": "Beijing", "country": "China",
            }]},
        }})
        result = verify_one(
            {"id": "NCT12345678"},
            fetcher=lambda url, timeout: (url, body),
        )
        self.assertEqual(result["status"], "inactive")
        self.assertEqual(result["method"], "clinicaltrials.gov_v2_api")
        self.assertEqual(result["sites"][0]["country"], "China")

        partitioned = verify_and_partition(
            [{"id": "NCT12345678"}], workers=1,
            verifier=lambda trial, timeout: {**result, "status": "active"},
            patient={"country": "中国"},
        )
        enriched = partitioned["active_or_unknown"][0]
        self.assertEqual(enriched["patient_country_site_count"], 1)
        self.assertEqual(enriched["patient_country_sites"][0]["city"], "Beijing")

    def test_only_explicit_inactive_status_is_removed(self):
        statuses = iter(("inactive", "error", "unknown", "active"))

        def verifier(trial, timeout):
            status = next(statuses)
            return {"status": status, "overall_status": "TERMINATED" if status == "inactive" else ""}

        result = verify_and_partition(
            [{"id": f"T{i}"} for i in range(4)], workers=1, verifier=verifier,
        )
        self.assertEqual([trial["id"] for trial in result["inactive"]], ["T0"])
        self.assertEqual(len(result["active_or_unknown"]), 3)

    def test_direct_webpage_status_parser_recognizes_non_recruiting(self):
        result = verify_one(
            {"id": "ChiCTR1", "source_url": "https://registry.example/trial"},
            fetcher=lambda url, timeout: (
                url, "<td>Recruitment status:</td><td>Active, not recruiting</td>"
            ),
        )
        self.assertEqual(result["status"], "inactive")

    def test_chinese_registry_statuses_are_parsed_without_substring_collision(self):
        self.assertEqual(_webpage_status("\u62db\u52df\u72b6\u6001\uff1a\u5df2\u7ec8\u6b62"), "TERMINATED")
        self.assertEqual(_webpage_status("\u62db\u52df\u72b6\u6001\uff1a\u62db\u52df\u4e2d"), "RECRUITING")

    def test_plain_not_recruiting_is_never_classified_as_active(self):
        self.assertEqual(
            _webpage_status("Overall status: Not Recruiting"),
            "NO_LONGER_RECRUITING",
        )
        self.assertEqual(_webpage_status("Overall status: Terminated"), "TERMINATED")

    def test_direct_registry_failure_falls_back_to_who(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            if "clinicaltrials.gov/api" in url:
                raise urllib.error.URLError("direct unavailable")
            return (
                url,
                "<td>Recruitment status:</td><td>Recruiting</td>",
            )

        result = verify_one({"id": "NCT12345678"}, fetcher=fetcher)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["method"], "who_ictrp_fallback")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(len(calls), 2)

    def test_registry_fetch_rejects_local_and_active_schemes(self):
        with self.assertRaisesRegex(ValueError, "HTTP"):
            _fetch(Path(__file__).resolve().as_uri(), 1)
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            _fetch("https://example.test/trial", 1)

    def test_batch_executor_runs_all_batches_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = root / "runner.py"
            runner.write_text(
                "import json,sys\n"
                "p=json.load(open(sys.argv[1],encoding='utf-8'))\n"
                "ids=p['required_output']['expected_trial_ids']\n"
                "json.dump({'analyzed_trials':[{'trial_id':i} for i in ids]},open(sys.argv[2],'w'))\n",
                encoding="utf-8",
            )
            jobs = root / "jobs.json"
            jobs.write_text(json.dumps({
                "skill_paths": {},
                "batches": [
                    {"batch_id": "clinical-gater-001", "trials": [{"id": "T1"}]},
                    {"batch_id": "clinical-gater-002", "trials": [{"id": "T2"}]},
                ],
            }), encoding="utf-8")
            command = json.dumps([sys.executable, str(runner), "{input}", "{output}"])
            with patch.dict(os.environ, {"MODEL_BATCH_RUNNER_JSON": command}):
                first = execute_batches(jobs, root / "out", output_prefix="gater-batch")
                second = execute_batches(jobs, root / "out", output_prefix="gater-batch")
            self.assertEqual(first["executed_batches"], 2)
            self.assertEqual(second["resumed_batches"], 2)


if __name__ == "__main__":
    unittest.main()
