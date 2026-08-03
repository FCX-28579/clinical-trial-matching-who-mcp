"""Registry-specific presentation helpers with no clinical eligibility logic."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit

PRODUCT_ALIASES = {
    "vegzelma": "Bevacizumab",
    "avastin": "Bevacizumab",
    "erbitux": "Cetuximab",
    "jnj-61186372": "Amivantamab",
}
GENERIC_INTERVENTIONS = {
    "product", "drug", "treatment", "none", "solution for infusion",
    "solution for injection", "placebo", "best supportive care", "n/a", "na",
    "not applicable", "not available", "standard of care", "soc therapy",
    "for", "clinical trial", "see study design above", "cell", "biopsy",
    "phase 1", "phase 1a", "phase i", "phase ia",
}
NON_THERAPEUTIC_TERMS = {
    "biopsy procedure", "biospecimen", "blood draw", "computed tomography", "ct scan",
    "diagnostic test", "echocardiography", "electrocardiogram", "imaging",
    "laboratory test", "magnetic resonance imaging", "mri", "muga",
    "multigated acquisition", "pharmacokinetic sampling", "pet scan",
    "digital photography", "questionnaire", "radiologic assessment", "sample collection",
    "specimen collection", "ultrasound", "bone scan", "cardiac monitoring",
    "electrocardiography", "molecular analysis", "pathology review",
    "physical examination", "screening test", "tumor assessment",
    "quality of life survey", "patient status engine", "wearable device",
    "ctdna test", "genomic tumor advisory board review", "oura ring",
    "withings", "daily weights", "patient monitoring device",
}
DOSING_SENTENCE_TERMS = {
    "will be supplied", "will be administered", "participants will receive",
    "subjects will receive", "dose escalation", "according to the dose",
    "is administered", "orally administered", "once daily", "twice daily", "apply to",
    "continuous administration", "accelerated escalation group", "study design above",
    "patients can be enrolled", "patient was enrolled", "once every",
    "on the premise of confirming", "will be maintained", "will continue until",
    "until disease progression", "dose expansion", "each cycle",
}
COUNTRY_ALIASES = {
    "中国": "china", "mainland china": "china", "pr china": "china",
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom",
    "south korea": "republic of korea", "korea": "republic of korea",
}
NATIVE_REGISTRY_PREFIXES = {
    "china": ("CHICTR", "ITMCTR"),
    "india": ("CTRI/", "CTRI"),
    "japan": ("JPRN", "JRCT"),
    "netherlands": ("NL-", "NTR"),
    "australia": ("ACTRN",),
    "new zealand": ("ACTRN",),
    "germany": ("DRKS",),
    "iran": ("IRCT",),
    "republic of korea": ("KCT", "KCT000"),
    "brazil": ("RBR-",),
    "thailand": ("TCTR",),
    "sri lanka": ("SLCTR",),
}


def safe_external_url(value: Any, *, fallback: str = "#") -> str:
    """Return a browser-safe HTTP(S) URL without accepting active schemes."""
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return fallback
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return fallback
    return raw


def country_key(value: Any) -> str:
    raw = str(value or "").strip().casefold()
    return COUNTRY_ALIASES.get(raw, raw)


def registry_ids(trial: dict[str, Any]) -> list[str]:
    values = [trial.get("id"), trial.get("primary_registry_id"), trial.get("trial_uid")]
    values.extend(
        item.get("registry_id") if isinstance(item, dict) else item
        for item in trial.get("registry_ids") or []
    )
    return list(dict.fromkeys(str(value).strip() for value in values if str(value or "").strip()))


def _clean_agent_name(value: str) -> str:
    name = re.sub(r"(?i)^(?:none|drug|experimental|intervention|biological|procedure|device|combination product)\s*:\s*", "", str(value or "")).strip()
    for _ in range(2):
        name = re.sub(r"(?i)^(?:(?:experimental|treatment|intervention|control)(?:\s+group)?|single drug research|combined administration|intervention\s*\d+)\s*:?\s*", "", name)
    name = re.sub(r"(?i)^biological\s+", "", name)
    name = re.sub(r"(?i)^combination of\s+", "", name)
    name = re.sub(r"(?i)^(?:low|medium|high)(?:est)?(?:[- ]dose)?\s+", "", name)
    name = re.sub(r"(?i)^(?:dose level|cohort|group|arm)\s*[A-Z0-9.-]*\s*:\s*", "", name)
    name = re.sub(r"(?i)^(.+?)\s*:\s*\d+(?:\.\d+)?(?:\s*x\s*10\^?\d+)?\s*(?:mg|mcg|g|ml|cells?)?\b.*$", r"\1", name)
    name = re.sub(r"(?i)^([A-Z]{2,}[-]?\d+)[- ](?:\d+(?:\.\d+)?\s*mg|recommended dose.*)$", r"\1", name)
    name = re.sub(
        r"(?i)(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:mg|mcg|ug|\u03bcg|g)"
        r"(?:\s*/\s*(?:kg|day|d|ml))?\s*",
        "",
        name,
    )
    name = re.sub(r"(?i)\s*\(\s*\d+(?:\.\d+)?\s*(?:mg|mcg|ug|\u03bcg|g)\b.*$", "", name)
    name = re.sub(r"(?i)\s*\(\s*(?:daily|orally|intravenous|for body weight)\b.*$", "", name)
    name = name.split(",", 1)[0].strip(" .:+")
    name = re.sub(r"(?i)\b([a-z][a-z0-9-]+)\s+\1\b", r"\1", name)
    name = re.sub(r"(?i)\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml)(?:/ml)?\b.*$", "", name).strip()
    name = re.sub(r"(?i)\s+(?:concentrate|solution|tablet|capsule|injection)\b.*$", "", name).strip()
    return PRODUCT_ALIASES.get(name.casefold(), name)


def _usable_agent_name(name: str) -> bool:
    folded = name.casefold()
    if not name or folded in GENERIC_INTERVENTIONS:
        return False
    if folded in {"autologous", "experimental", "experimental drug", "control", "control rx"}:
        return False
    if re.fullmatch(r"(?i)phase\s*[0-4ivx]+[a-z]?(?:\s+arm\s+[a-z0-9]+)?", name):
        return False
    if any(term in folded for term in NON_THERAPEUTIC_TERMS | DOSING_SENTENCE_TERMS):
        return False
    if re.fullmatch(r"(?i)[\d.\s/%-]*(?:mg|mcg|ug|\u03bcg|g|ml|q\d+w?)[\d.\s/%-]*", name):
        return False
    if len(name) > 80 or len(name.split()) > 10:
        return False
    if name.endswith((".", ";")) and len(name.split()) > 5:
        return False
    return True


def intervention_names(trial: dict[str, Any]) -> list[str]:
    output: list[str] = []
    for item in trial.get("interventions") or []:
        raw = (item.get("intervention_name_raw") if isinstance(item, dict) else str(item)) or ""
        intervention_type = str(item.get("intervention_type") or item.get("type") or "") if isinstance(item, dict) else ""
        if intervention_type.casefold() in {"diagnostic test", "procedure"} and any(
            term in raw.casefold() for term in NON_THERAPEUTIC_TERMS
        ):
            continue
        if "product name:" in raw.casefold():
            values = re.findall(r"(?i)Product Name:\s*([^,]*)", raw)
        elif raw.casefold().startswith("hifu system "):
            values = []
        else:
            values = re.split(r"\s*\+\s*", raw)
        for value in values:
            name = _clean_agent_name(value)
            if _usable_agent_name(name):
                output.append(name)
    return list(dict.fromkeys(output))

def _clean_registry_title(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip(" .")
    title = re.sub(
        r"(?i)^(?:a |an )?(?:phase\s+[0-4ivx?/ -]+\s+)?"
        r"(?:open-label\s+|multicenter\s+|randomized\s+|single-center\s+)*"
        r"(?:(?:clinical\s+)?(?:study|trial)\s+(?:of|to evaluate|evaluating)\s+|the study of )",
        "",
        title,
    ).strip(" .")
    title = re.sub(r"(?i)^(?:the )?(?:safety|efficacy)(?:,? safety)? and (?:efficacy|safety) of\s+", "", title)
    title = re.sub(r"(?i)^study on the (?:safety|efficacy|safety and efficacy|safety and tolerability) of\s+", "", title)
    return title


def _title_agent_codes(*titles: str) -> list[str]:
    """Extract study-drug codes without treating common biomarkers as drugs."""
    excluded = {
        "NCT", "KRAS", "NRAS", "HRAS", "BRAF", "EGFR", "MSI", "MMR",
        "PDAC", "NSCLC", "CRC", "MCRC", "MPDAC", "RECIST", "RP2D",
    }
    output: list[str] = []
    for title in titles:
        for value in re.findall(r"\b[A-Z]{1,8}(?:-[A-Z0-9]+|\d[A-Z0-9-]*)\b", title or ""):
            prefix = re.match(r"[A-Z]+", value)
            if not prefix or prefix.group(0) in excluded:
                continue
            if re.fullmatch(r"[A-Z]\d+[A-Z]", value):
                continue
            output.append(value)
    return list(dict.fromkeys(output))


def registry_is_inactive(trial: dict[str, Any]) -> bool:
    status = str(trial.get("overall_status") or "").strip().upper().replace(" ", "_")
    return status in {
        "ACTIVE_NOT_RECRUITING", "COMPLETED", "NO_LONGER_RECRUITING",
        "NOT_RECRUITING", "SUSPENDED", "TERMINATED", "WITHDRAWN",
    } or (trial.get("registry_status_rule") or {}).get("rule_id") == "REGISTRY-INACTIVE"


def patient_facing_title(trial: dict[str, Any], language: str) -> str:
    names = intervention_names(trial)
    public_title = _clean_registry_title(trial.get("title"))
    scientific_title = _clean_registry_title(trial.get("scientific_title"))
    title_codes = _title_agent_codes(public_title, scientific_title)
    title_text = f"{public_title} {scientific_title}".casefold()
    title_mentions = [
        (title_text.find(name.casefold()), name)
        for name in names if name.casefold() in title_text
    ]
    title_named_agent = min(title_mentions, default=(-1, ""))[1]
    core = title_codes[0] if title_codes else title_named_agent or (names[0] if names else "")
    # This is a browsing label, not a synthesized regimen. A global
    # intervention list can span mutually exclusive study arms.
    base = core or public_title or scientific_title
    if not base:
        base = str(trial.get("id") or "Untitled trial")
    if core and len(names) > 1:
        base += "（多臂）" if language == "zh-CN" else " (multi-arm)"

    mechanism = trial.get("mechanism_category") or {}
    label = (
        mechanism.get("label_zh" if language == "zh-CN" else "label_en")
        or mechanism.get("label")
        or ("其他" if language == "zh-CN" else "Other")
    )
    verdict = str((trial.get("gating") or {}).get("verdict") or "")
    if registry_is_inactive(trial):
        qualifier = "已关闭招募" if language == "zh-CN" else "Recruitment closed"
    elif verdict == "exclude":
        qualifier = "不符合关键条件" if language == "zh-CN" else "Key eligibility conflict"
    elif verdict == "conditional":
        qualifier = "需确认入组条件" if language == "zh-CN" else "Eligibility to confirm"
    else:
        qualifier = ""
    max_base = max(48, 110 - len(label) - len(qualifier) - 6)
    if len(base) > max_base:
        shortened = base[:max_base].rsplit(" ", 1)[0].rstrip(" +,;/.-")
        shortened = re.sub(r"(?i)\s+(?:with|for|of|in|and|or|the|a|an|to)$", "", shortened).rstrip(" +,;/.-")
        base = shortened + "..."
    parts = [base or "Untitled trial", label]
    if qualifier:
        parts.append(qualifier)
    return " · ".join(parts)

def assess_country_evidence(trial: dict[str, Any], patient: dict[str, Any]) -> dict[str, Any]:
    country = str(patient.get("country") or "").strip()
    key = country_key(country)
    if int(trial.get("patient_country_site_count") or 0) > 0:
        return {
            "class": "domestic_named", "decision": "confirmed_in_country", "confidence": "high",
            "basis": ["named_site"], "review_method": "structured_evidence",
        }
    prefixes = NATIVE_REGISTRY_PREFIXES.get(key, ())
    ids = registry_ids(trial)
    if prefixes and any(value.upper().startswith(tuple(prefix.upper() for prefix in prefixes)) for value in ids):
        return {
            "class": "domestic_registry", "decision": "country_native_registry", "confidence": "medium",
            "basis": ["registry_jurisdiction"], "review_method": "deterministic_registry_rule",
            "rationale": "Registration in the patient's national registry does not prove a named open centre.",
        }
    if int(trial.get("patient_country_location_record_count") or 0) > 0:
        return {
            "class": "country_unverified", "decision": "country_mentioned_but_site_unverified", "confidence": "low",
            "basis": ["registry_country_list_only"], "review_method": "structured_conservative_review",
            "rationale": "A country record does not establish a named open patient-accessible centre.",
        }
    return {
        "class": "overseas", "decision": "no_patient_country_evidence", "confidence": "medium",
        "basis": [], "review_method": "structured_evidence",
    }


def resolved_trial_url(trial: dict[str, Any]) -> str:
    trial_id = str(trial.get("id") or trial.get("primary_registry_id") or "").strip()
    upper = trial_id.upper()
    if upper.startswith(("CTIS", "CTRI/", "CTRI")):
        return "https://trialsearch.who.int/Trial2.aspx?TrialID=" + quote(trial_id, safe="")
    if upper.startswith("NCT"):
        return "https://clinicaltrials.gov/study/" + upper
    url = str(trial.get("source_url") or "").strip()
    if "chictr.org.cn" in url:
        url = url.replace("http://", "https://")
    fallback = "https://trialsearch.who.int/Trial2.aspx?TrialID=" + quote(trial_id, safe="")
    return safe_external_url(url, fallback=fallback)
