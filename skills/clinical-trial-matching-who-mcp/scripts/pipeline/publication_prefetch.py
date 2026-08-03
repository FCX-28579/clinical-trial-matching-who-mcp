"""Prefetch auditable publication candidates for deep-analysis jobs."""
from __future__ import annotations

import json
import os
import hashlib
import datetime as dt
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from analysis_contract import load_json
from io_utils import write_json_atomic

ENDPOINT = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _cache_name(trial_id: str, query: str = "") -> str:
    readable = "".join(
        character for character in trial_id
        if character.isalnum() or character in "-_"
    )[:60] or "trial"
    digest = hashlib.sha256(
        f"publication-query-v2\n{trial_id}\n{query}".encode("utf-8")
    ).hexdigest()[:10]
    return f"{readable}-{digest}.json"


def _query_for_trial(trial: dict[str, Any]) -> str:
    trial_id = str(trial.get("id") or "").strip()
    interventions = [
        str(value).split(":", 1)[-1].strip()
        for value in trial.get("interventions") or []
        if str(value).strip()
    ][:3]
    disease = str(trial.get("disease_text") or "").strip()
    exact = f'"{trial_id}"' if trial_id else ""
    contextual = " OR ".join(f'"{value}"' for value in interventions)
    if contextual and disease:
        contextual = f'({contextual}) AND "{disease}"'
    return " OR ".join(value for value in (exact, contextual) if value)


def _search(trial: dict[str, Any]) -> dict[str, Any]:
    searched_at = _utc_now()
    query = _query_for_trial(trial)
    params = urllib.parse.urlencode({
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": int(os.environ.get("PUBLICATION_MAX_RESULTS", "5")),
    })
    timeout = float(os.environ.get("PUBLICATION_TIMEOUT_SECONDS", "20"))
    request = urllib.request.Request(
        f"{ENDPOINT}?{params}",
        headers={"Accept": "application/json", "User-Agent": "clinical-trial-matching/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "status": "error", "queries": [query], "searched_at": searched_at,
            "error": str(exc)[:500], "candidates": [],
        }
    candidates = []
    for item in ((payload.get("resultList") or {}).get("result") or []):
        pmid = str(item.get("pmid") or "").strip()
        pmcid = str(item.get("pmcid") or "").strip()
        doi = str(item.get("doi") or "").strip()
        source = str(item.get("source") or "MED").strip()
        external_id = pmid or pmcid or str(item.get("id") or "").strip()
        url = (
            f"https://europepmc.org/article/{source}/{external_id}"
            if external_id else (f"https://doi.org/{doi}" if doi else "")
        )
        candidates.append({
            "title": item.get("title") or "",
            "authors": item.get("authorString") or "",
            "journal": item.get("journalTitle") or "",
            "year": item.get("pubYear") or "",
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "url": url,
            "abstract": item.get("abstractText") or "",
            "source": "Europe PMC",
        })
    return {
        "status": "found" if candidates else "no_results",
        "queries": [query],
        "searched_at": searched_at,
        "source": "Europe PMC REST API",
        "candidates": candidates,
    }


def enrich_deep_jobs_file(jobs_path: Path, cache_dir: Path) -> dict[str, Any]:
    mode = os.environ.get("PUBLICATION_SEARCH_MODE", "auto").strip().lower()
    if mode == "off":
        return {"mode": "off", "trials": 0, "cache_hits": 0, "queried": 0}
    if mode != "auto":
        raise ValueError("PUBLICATION_SEARCH_MODE must be auto or off")
    jobs = load_json(jobs_path)
    trials = [
        trial for batch in jobs.get("batches") or []
        for trial in batch.get("trials") or []
    ]
    results: dict[str, dict[str, Any]] = {}
    pending: dict[Any, tuple[str, Path]] = {}
    cache_hits = 0
    concurrency = max(1, min(16, int(os.environ.get("PUBLICATION_CONCURRENCY", "6"))))
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for trial in trials:
            trial_id = str(trial.get("id") or "").strip()
            query = _query_for_trial(trial)
            cache = cache_dir / _cache_name(trial_id, query)
            if cache.exists():
                cached = load_json(cache)
                if (cached.get("queries") or [""])[0] != query or cached.get("status") == "error":
                    pending[pool.submit(_search, trial)] = (trial_id, cache)
                    continue
                if not cached.get("searched_at"):
                    cached["searched_at"] = dt.datetime.fromtimestamp(
                        cache.stat().st_mtime, tz=dt.timezone.utc
                    ).isoformat()
                    write_json_atomic(cache, cached)
                results[trial_id] = cached
                cache_hits += 1
            else:
                pending[pool.submit(_search, trial)] = (trial_id, cache)
        for future in as_completed(pending):
            trial_id, cache = pending[future]
            result = future.result()
            write_json_atomic(cache, result)
            results[trial_id] = result
    for batch in jobs.get("batches") or []:
        for trial in batch.get("trials") or []:
            trial["publication_prefetch"] = results.get(str(trial.get("id") or ""), {})
    jobs["publication_prefetch"] = {
        "mode": "europe_pmc",
        "cache_hits": cache_hits,
        "queried": len(pending),
        "trials": len(trials),
    }
    write_json_atomic(jobs_path, jobs)
    return jobs["publication_prefetch"]
