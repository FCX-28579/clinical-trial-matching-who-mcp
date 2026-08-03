from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PIPELINE = Path(__file__).resolve().parents[1] / "scripts" / "pipeline"
sys.path.insert(0, str(PIPELINE))

from publication_prefetch import _cache_name, _query_for_trial, enrich_deep_jobs_file


class PublicationPrefetchTests(unittest.TestCase):
    def test_europe_pmc_results_are_cached_and_injected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "deep_jobs.json"
            jobs.write_text(
                json.dumps({
                    "stage": "deep",
                    "batches": [{"trials": [{
                        "id": "NCT1", "interventions": ["Drug: Agent X"],
                        "disease_text": "CRC",
                    }]}],
                }),
                encoding="utf-8",
            )
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "resultList": {"result": [{
                    "pmid": "123", "source": "MED", "title": "Agent X study",
                    "authorString": "A et al.", "pubYear": "2025",
                }]}
            }).encode()
            with patch.dict(os.environ, {"PUBLICATION_SEARCH_MODE": "auto"}, clear=False), patch(
                "publication_prefetch.urllib.request.urlopen", return_value=response
            ) as opener:
                first = enrich_deep_jobs_file(jobs, root / "cache")
                second = enrich_deep_jobs_file(jobs, root / "cache")
            self.assertEqual(first["queried"], 1)
            self.assertEqual(second["cache_hits"], 1)
            self.assertEqual(opener.call_count, 1)
            trial = json.loads(jobs.read_text(encoding="utf-8"))["batches"][0]["trials"][0]
            self.assertEqual(
                trial["publication_prefetch"]["candidates"][0]["url"],
                "https://europepmc.org/article/MED/123",
            )
            self.assertTrue(trial["publication_prefetch"]["searched_at"])
            self.assertEqual(len(trial["publication_prefetch"]["queries"]), 1)

    def test_cache_key_changes_with_the_effective_query(self) -> None:
        first = _query_for_trial({
            "id": "NCT1", "interventions": ["Drug: Agent X"], "disease_text": "CRC",
        })
        second = _query_for_trial({
            "id": "NCT1", "interventions": ["Drug: Agent Y"], "disease_text": "CRC",
        })
        self.assertIn('"NCT1"', first)
        self.assertIn('("Agent X") AND "CRC"', first)
        self.assertNotEqual(_cache_name("NCT1", first), _cache_name("NCT1", second))

    def test_error_cache_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jobs = root / "deep_jobs.json"
            jobs.write_text(json.dumps({
                "stage": "deep",
                "batches": [{"trials": [{"id": "NCT1", "disease_text": "CRC"}]}],
            }), encoding="utf-8")
            cache = root / "cache"
            cache.mkdir()
            query = _query_for_trial({"id": "NCT1", "disease_text": "CRC"})
            (cache / _cache_name("NCT1", query)).write_text(json.dumps({
                "status": "error", "queries": [query], "candidates": [],
            }), encoding="utf-8")
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"resultList":{"result":[]}}'
            with patch.dict(os.environ, {"PUBLICATION_SEARCH_MODE": "auto"}), patch(
                "publication_prefetch.urllib.request.urlopen", return_value=response
            ) as opener:
                result = enrich_deep_jobs_file(jobs, cache)
            self.assertEqual(result["cache_hits"], 0)
            opener.assert_called_once()

    def test_off_mode_performs_no_network_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            jobs = Path(temporary) / "jobs.json"
            jobs.write_text('{"batches":[]}', encoding="utf-8")
            with patch.dict(os.environ, {"PUBLICATION_SEARCH_MODE": "off"}), patch(
                "publication_prefetch.urllib.request.urlopen"
            ) as opener:
                result = enrich_deep_jobs_file(jobs, Path(temporary) / "cache")
            self.assertEqual(result["mode"], "off")
            opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
