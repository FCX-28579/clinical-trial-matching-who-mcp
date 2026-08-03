"""Live recruitment verification against direct registry URLs before eligibility gating."""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlsplit
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any, Callable

ACTIVE = {"RECRUITING", "NOT_YET_RECRUITING", "ENROLLING_BY_INVITATION"}
INACTIVE = {
    "ACTIVE_NOT_RECRUITING", "COMPLETED", "TERMINATED", "WITHDRAWN",
    "SUSPENDED", "NO_LONGER_RECRUITING",
}
CTGOV = "https://clinicaltrials.gov/api/v2/studies"
WHO_DETAIL = "https://trialsearch.who.int/Trial2.aspx"
REGISTRY_HOST_SUFFIXES = (
    "clinicaltrials.gov", "trialsearch.who.int", "chictr.org.cn",
    "chictr.cn", "euclinicaltrials.eu", "ctri.nic.in", "isrctn.com",
    "drks.de", "umin.ac.jp",
    "cris.nih.go.kr", "irct.ir", "pactr.samrc.ac.za",
    "ensaiosclinicos.gov.br", "registroclinico.sld.cu", "slctr.lk",
    "clinicaltrials.in.th", "trialregister.nl",
)
# jRCT and ANZCTR are intentionally absent: their published terms restrict
# automated collection. Those records use the WHO detail fallback unless an
# authorized API adapter is configured in a deployment.
_CACHE: dict[str, tuple[str, str]] = {}
_CACHE_LOCK = threading.Lock()
_HOST_LIMITS: dict[str, threading.BoundedSemaphore] = {}
_HOST_LIMITS_LOCK = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _status(value: Any) -> str:
    normalized = re.sub(r"[^A-Z]+", "_", str(value or "").upper()).strip("_")
    aliases = {
        "ACTIVE_NOT_RECRUITING": "ACTIVE_NOT_RECRUITING",
        "NOT_YET_RECRUITING": "NOT_YET_RECRUITING",
        "ENROLLING_BY_INVITATION": "ENROLLING_BY_INVITATION",
        "NOT_RECRUITING": "NO_LONGER_RECRUITING",
    }
    return aliases.get(normalized, normalized)


def _validate_registry_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme.casefold() not in {"http", "https"} or not host:
        raise ValueError("Registry URL must use HTTP(S)")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in REGISTRY_HOST_SUFFIXES):
        raise ValueError(f"Registry host is not allowlisted: {host}")
    try:
        addresses = {
            item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ValueError(f"Registry host could not be resolved: {host}") from exc
    if any(
        (ip := ipaddress.ip_address(address)).is_private
        or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
        for address in addresses
    ):
        raise ValueError("Registry URL resolves to a non-public address")


def _fetch(url: str, timeout: float) -> tuple[str, str]:
    with _CACHE_LOCK:
        if url in _CACHE:
            return _CACHE[url]
    current = url
    opener = urllib.request.build_opener(_NoRedirect())
    for redirect_count in range(4):
        _validate_registry_url(current)
        host = (urlsplit(current).hostname or "").casefold()
        with _HOST_LIMITS_LOCK:
            limiter = _HOST_LIMITS.setdefault(host, threading.BoundedSemaphore(2))
        request = urllib.request.Request(
            current, headers={"User-Agent": "CancerDAO-live-registry-verifier/1.1"}
        )
        with limiter:
            for attempt in range(3):
                try:
                    with opener.open(request, timeout=timeout) as response:
                        resolved = response.geturl()
                        _validate_registry_url(resolved)
                        result = (
                            resolved,
                            response.read().decode("utf-8", errors="replace"),
                        )
                        with _CACHE_LOCK:
                            _CACHE[url] = result
                        return result
                except urllib.error.HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        location = exc.headers.get("Location")
                        if not location:
                            raise
                        current = urljoin(current, location)
                        _validate_registry_url(current)
                        break
                    if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                        raise
                    retry_after = exc.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 0.5 * (2 ** attempt)
                    time.sleep(min(delay, 10))
            else:
                continue
        if redirect_count == 3:
            break
    raise urllib.error.URLError("Registry redirect limit exceeded")


def _nct_id(trial: dict[str, Any]) -> str:
    values = [trial.get("id"), trial.get("primary_registry_id")]
    values.extend(
        item.get("registry_id") if isinstance(item, dict) else item
        for item in trial.get("registry_ids") or []
    )
    return next((str(value).upper() for value in values if re.fullmatch(r"NCT\d{8}", str(value or ""), re.I)), "")


def _who_fallback_url(trial: dict[str, Any]) -> str:
    trial_id = str(
        trial.get("primary_registry_id") or trial.get("id") or ""
    ).strip()
    return f"{WHO_DETAIL}?TrialID={trial_id}" if trial_id else ""


def _ctgov_sites(payload: dict[str, Any]) -> list[dict[str, Any]]:
    module = (payload.get("protocolSection") or {}).get("contactsLocationsModule") or {}
    sites = []
    for location in module.get("locations") or []:
        if not isinstance(location, dict):
            continue
        site = {
            "site_name": location.get("facility") or "",
            "facility": location.get("facility") or "",
            "status": _status(location.get("status")),
            "city": location.get("city") or "",
            "province": location.get("state") or "",
            "country": location.get("country") or "",
            "postal_code": location.get("zip") or "",
        }
        if any(str(value or "").strip() for value in site.values()):
            sites.append(site)
    return sites


def _country_key(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    aliases = {
        "中国": "china", "mainland china": "china", "pr china": "china",
        "people's republic of china": "china", "usa": "united states",
        "us": "united states", "u.s.": "united states",
    }
    return aliases.get(raw, raw)


def _webpage_status(body: str) -> str:
    plain = " ".join(re.sub(r"<[^>]+>", " ", body).split())
    chinese = re.search(r"(?:招募状态|募集状态|研究状态)\s*[:：]?\s*([^\s,，。；;]{1,20})", plain)
    if chinese:
        value = chinese.group(1)
        if any(term in value for term in ("已终止", "已完成", "暂停", "不再招募", "停止招募")):
            return "TERMINATED" if "终止" in value else "NO_LONGER_RECRUITING"
        if any(term in value for term in ("招募中", "正在招募", "尚未招募")):
            return "NOT_YET_RECRUITING" if "尚未" in value else "RECRUITING"
    label = re.search(
        r"(?:recruitment|recruiting|trial|overall)\s+status\s*[:\-]?\s*(.{0,100})",
        plain, re.I,
    )
    status_window = label.group(1) if label else ""
    normalized_window = re.sub(
        r"[^A-Z]+", "_", status_window.upper()
    ).strip("_")
    # Keep negative and compound states ahead of their RECRUITING substring.
    ordered_patterns = (
        ("ACTIVE_NOT_RECRUITING", "ACTIVE_NOT_RECRUITING"),
        ("NOT_RECRUITING", "NO_LONGER_RECRUITING"),
        ("NO_LONGER_RECRUITING", "NO_LONGER_RECRUITING"),
        ("NOT_YET_RECRUITING", "NOT_YET_RECRUITING"),
        ("ENROLLING_BY_INVITATION", "ENROLLING_BY_INVITATION"),
        ("TERMINATED", "TERMINATED"),
        ("WITHDRAWN", "WITHDRAWN"),
        ("SUSPENDED", "SUSPENDED"),
        ("COMPLETED", "COMPLETED"),
        ("RECRUITING", "RECRUITING"),
    )
    return next(
        (status for marker, status in ordered_patterns if marker in normalized_window),
        "",
    )


def verify_one(
    trial: dict[str, Any], *, timeout: float = 20,
    fetcher: Callable[[str, float], tuple[str, str]] = _fetch,
) -> dict[str, Any]:
    checked_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    nct = _nct_id(trial)
    direct_url = f"{CTGOV}/{nct}" if nct else str(trial.get("source_url") or trial.get("resolved_source_url") or "")
    if not direct_url:
        return {"status": "unavailable", "checked_at": checked_at, "reason": "No direct registry URL"}
    try:
        resolved_url, body = fetcher(direct_url, timeout)
        if nct:
            payload = json.loads(body)
            module = (payload.get("protocolSection") or {}).get("statusModule") or {}
            live_status = _status(module.get("overallStatus"))
            updated = (module.get("lastUpdatePostDateStruct") or {}).get("date", "")
            sites = _ctgov_sites(payload)
            source_url = f"https://clinicaltrials.gov/study/{nct}"
            method = "clinicaltrials.gov_v2_api"
        else:
            live_status = _webpage_status(body)
            updated = ""
            sites = []
            source_url, method = resolved_url, "direct_registry_webpage"
        if live_status in ACTIVE:
            result_status = "active"
        elif live_status in INACTIVE:
            result_status = "inactive"
        else:
            result_status = "unknown"
        return {
            "status": result_status,
            "overall_status": live_status,
            "last_update_date": updated,
            "source_url": source_url,
            "method": method,
            "checked_at": checked_at,
            "http_reachable": True,
            "sites": sites,
            "countries": sorted({site["country"] for site in sites if site.get("country")}),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        fallback_url = _who_fallback_url(trial)
        if fallback_url and fallback_url != direct_url:
            try:
                resolved_url, body = fetcher(fallback_url, timeout)
                live_status = _webpage_status(body)
                result_status = (
                    "active" if live_status in ACTIVE
                    else "inactive" if live_status in INACTIVE
                    else "unknown"
                )
                return {
                    "status": result_status,
                    "overall_status": live_status,
                    "last_update_date": "",
                    "source_url": resolved_url,
                    "method": "who_ictrp_fallback",
                    "checked_at": checked_at,
                    "http_reachable": True,
                    "fallback_used": True,
                    "direct_error": str(exc),
                }
            except (
                urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError, json.JSONDecodeError,
            ) as fallback_exc:
                return {
                    "status": "error", "checked_at": checked_at,
                    "source_url": direct_url, "fallback_url": fallback_url,
                    "http_reachable": False,
                    "error": str(exc), "fallback_error": str(fallback_exc),
                }
        return {
            "status": "error", "checked_at": checked_at, "source_url": direct_url,
            "http_reachable": False, "error": str(exc),
        }


def verify_and_partition(
    trials: list[dict[str, Any]], *, workers: int = 4, timeout: float = 20,
    verifier: Callable[..., dict[str, Any]] = verify_one,
    patient: dict[str, Any] | None = None,
) -> dict[str, Any]:
    copies = [deepcopy(trial) for trial in trials]
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 32))) as pool:
        results = list(pool.map(lambda trial: verifier(trial, timeout=timeout), copies))
    active_or_unknown: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    for trial, result in zip(copies, results):
        trial["live_registry_verification"] = result
        if result.get("overall_status"):
            trial["overall_status"] = result["overall_status"]
        if result.get("sites"):
            trial["sites"] = result["sites"]
            trial["countries"] = result.get("countries") or trial.get("countries") or []
            if patient:
                country = _country_key(patient.get("country"))
                patient_sites = [
                    site for site in result["sites"]
                    if _country_key(site.get("country")) == country
                ]
                trial["patient_country_sites"] = patient_sites
                trial["patient_country_site_count"] = len(patient_sites)
                trial["patient_country_location_records"] = [
                    {"country": site.get("country"), "evidence_type": "direct_registry_site"}
                    for site in patient_sites
                ]
                trial["patient_country_location_record_count"] = len(patient_sites)
        (inactive if result.get("status") == "inactive" else active_or_unknown).append(trial)
    return {
        "active_or_unknown": active_or_unknown,
        "inactive": inactive,
        "audit": {
            "attempted": len(copies),
            "active": sum(result.get("status") == "active" for result in results),
            "inactive": len(inactive),
            "unknown": sum(result.get("status") == "unknown" for result in results),
            "errors": sum(result.get("status") in {"error", "unavailable"} for result in results),
            "reachable": sum(result.get("status") not in {"error", "unavailable"} for result in results),
            "complete": len(results) == len(copies),
            "policy": "Only an explicit non-enrolling status from a direct registry is excluded.",
        },
    }
