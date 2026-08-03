"""Translate patient-facing narratives without changing clinical facts or readiness."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from io_utils import write_json_atomic  # noqa: E402
from analysis_contract import is_china_patient  # noqa: E402
from model_api_runner import (  # noqa: E402
    _assert_complete_response, _configuration, _json_payload, _post,
    _request, _response_text, _stage_value,
)

TRANSLATABLE_KEYS = {
    "summary_text", "country_evidence", "patient_location",
    "current_therapy_status", "organ_function_note", "ecog_source",
    "travel_note", "missing_critical_information", "outcome",
    "rationale", "satisfied", "pending", "exclusion_reasons", "notes",
    "summary", "finding", "findings", "applicability", "limitations",
    "risk_context", "efficacy_context", "why_considered", "next_steps",
    "blockers", "blockers_pending", "blockers_failed", "criterion", "reason",
    "applies_because", "patient_eligibility_note", "head_to_head_summary",
    "comparison_limitation", "mechanism", "flag", "evidence", "trial_title",
    "display_title", "selection_basis", "reasons", "discussion_recommendation",
    "consequences_of_skipping",
}
PROTECTED = re.compile(
    r"(?:https?://\S+|(?i:NCT)\d{8}|(?i:ChiCTR)\w+|"
    r"[A-Za-z]{1,15}-?\d[A-Za-z0-9.-]*|[A-Z]{2,10}|\d+(?:\.\d+)?%?)"
)
CJK = re.compile(r"[\u3400-\u9fff]")


def needs_translation(value: str) -> bool:
    ascii_letters = sum(character.isascii() and character.isalpha() for character in value)
    cjk_characters = len(CJK.findall(value))
    return ascii_letters >= 4 and ascii_letters > cjk_characters


def contains_narrative_language(value: str) -> bool:
    """Keep clinical identifiers unchanged when no prose remains around them."""
    remainder = PROTECTED.sub(" ", value)
    return sum(character.isascii() and character.isalpha() for character in remainder) >= 4


def protected_facts(value: str) -> set[str]:
    return {match.rstrip(".,;:)") for match in PROTECTED.findall(value)}


def mask_protected_facts(value: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        placeholder = f"[[FACT_{len(replacements)}]]"
        replacements[placeholder] = match.group(0)
        return placeholder

    return PROTECTED.sub(replace, value), replacements


def restore_protected_facts(value: str, replacements: dict[str, str]) -> str:
    restored = value
    for placeholder, fact in replacements.items():
        restored = restored.replace(placeholder, fact)
    return restored


def translation_batches(
    units: list[dict[str, str]], *, max_units: int = 40, max_characters: int = 3_000,
) -> list[list[dict[str, str]]]:
    batches: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    characters = 0
    for unit in units:
        size = len(unit["source"])
        if current and (len(current) >= max_units or characters + size > max_characters):
            batches.append(current)
            current = []
            characters = 0
        current.append(unit)
        characters += size
    if current:
        batches.append(current)
    return batches


def translate_batch_resilient(
    batch: list[dict[str, str]], translate_once: Any,
) -> dict[str, str]:
    """Retry an incomplete translation as smaller independent batches."""
    try:
        translated = translate_once(batch)
        expected = {unit["id"] for unit in batch}
        if expected.issubset(translated):
            return translated
        raise RuntimeError("Translation response omitted one or more units")
    except (RuntimeError, ValueError) as exc:
        recoverable = (
            "Model response was truncated" in str(exc)
            or "Translation response omitted" in str(exc)
            or "did not return one valid JSON object" in str(exc)
        )
        if not recoverable:
            raise
        if len(batch) == 1:
            return {}
        midpoint = len(batch) // 2
        return {
            **translate_batch_resilient(batch[:midpoint], translate_once),
            **translate_batch_resilient(batch[midpoint:], translate_once),
        }


def prepare_translation_work(
    units: list[dict[str, str]], cached: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[dict[str, str]], dict[str, list[str]]]:
    """Reuse identical prose and return one representative per unique source."""
    cached = dict(cached or {})
    translated = {}
    for unit in units:
        value = cached.get(unit["id"])
        if (
            isinstance(value, str) and value.strip()
            and protected_facts(unit["source"]).issubset(protected_facts(value))
        ):
            translated[unit["id"]] = value
    source_translation: dict[str, str] = {}
    for unit in units:
        if unit["id"] in translated:
            source_translation.setdefault(unit["source"].strip(), translated[unit["id"]])

    representatives: dict[str, dict[str, str]] = {}
    aliases: dict[str, list[str]] = {}
    for unit in units:
        source = unit["source"].strip()
        if not contains_narrative_language(source):
            continue
        if source in source_translation:
            translated[unit["id"]] = source_translation[source]
            continue
        representative = representatives.setdefault(source, unit)
        aliases.setdefault(representative["id"], []).append(unit["id"])
    return translated, list(representatives.values()), aliases


def translation_batch_limits(_model: str = "") -> tuple[int, int]:
    """Return provider-neutral limits; operators may tune them per API."""
    default_units = "200"
    default_characters = "16000"
    return (
        max(1, int(os.environ.get("TRANSLATION_BATCH_MAX_UNITS", default_units))),
        max(200, int(os.environ.get(
            "TRANSLATION_BATCH_MAX_CHARACTERS", default_characters,
        ))),
    )


def collect_units(payload: dict[str, Any]) -> list[dict[str, str]]:
    units: list[dict[str, str]] = []

    def walk(value: Any, path: list[str], enabled: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, path + [str(key)], str(key) in TRANSLATABLE_KEYS)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path + [str(index)], enabled)
        elif enabled and isinstance(value, str) and value.strip() and needs_translation(value):
            units.append({"id": f"u{len(units)}", "path": "/".join(path), "source": value})

    walk(payload, [])
    return units


def apply_units(payload: dict[str, Any], units: list[dict[str, str]], translations: dict[str, str]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    applied_units = 0
    for unit in units:
        translated = translations.get(unit["id"])
        if not isinstance(translated, str) or not translated.strip():
            continue
        source_tokens = protected_facts(unit["source"])
        if not source_tokens.issubset(protected_facts(translated)):
            continue
        cursor: Any = result
        parts = unit["path"].split("/")
        for part in parts[:-1]:
            cursor = cursor[int(part)] if isinstance(cursor, list) else cursor[part]
        leaf = parts[-1]
        if isinstance(cursor, list):
            cursor[int(leaf)] = translated.strip()
        else:
            cursor[leaf] = translated.strip()
        applied_units += 1
    result["language"] = "zh-CN"
    result["translation_provenance"] = {
        "source_language": "en", "target_language": "zh-CN",
        "mode": "post_analysis_translation",
        "requested_units": len(units),
        "applied_units": applied_units,
    }
    return result


def translate_with_configured_model(
    units: list[dict[str, str]], *, cached: dict[str, str] | None = None,
    checkpoint: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, str]:
    provider_name, provider, model, base_url, key = _configuration("translation")
    protocol = _stage_value("translation", "MODEL_API_PROTOCOL") or provider.protocol
    def translate_once(batch: list[dict[str, str]]) -> dict[str, str]:
        masked: dict[str, tuple[str, dict[str, str]]] = {
            unit["id"]: mask_protected_facts(unit["source"]) for unit in batch
        }
        prompt = (
            "Translate each source into concise, natural Simplified Chinese. Preserve every trial ID, "
            "drug/biomarker name, number, URL and citation exactly. Preserve every [[FACT_n]] placeholder "
            "exactly; do not translate, remove, duplicate or reorder it. Do not add facts. Return exactly "
            "{\"translations\":[{\"id\":\"u0\",\"text\":\"...\"}]}.\nINPUT:\n"
            + json.dumps(
                [{"id": u["id"], "source": masked[u["id"]][0]} for u in batch],
                ensure_ascii=False,
            )
        )
        url, headers, body = _request(
            provider_name, protocol, base_url, key, model, prompt, stage="translation"
        )
        response = _post(url, headers, body)
        _assert_complete_response(protocol, response)
        parsed = _json_payload(_response_text(protocol, response))
        batch_translations: dict[str, str] = {}
        for row in parsed.get("translations") or []:
            if isinstance(row, dict) and row.get("id") and isinstance(row.get("text"), str):
                unit_id = str(row["id"])
                if unit_id in masked:
                    batch_translations[unit_id] = restore_protected_facts(
                        row["text"], masked[unit_id][1]
                    )
        return batch_translations

    translated, pending, aliases = prepare_translation_work(units, cached)
    max_units, max_characters = translation_batch_limits(model)
    batches = translation_batches(
        pending,
        max_units=max_units,
        max_characters=max_characters,
    )
    concurrency = max(1, min(int(os.environ.get("TRANSLATION_MODEL_CONCURRENCY", "3")), 8))
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(translate_batch_resilient, batch, translate_once)
            for batch in batches
        ]
        for future in concurrent.futures.as_completed(futures):
            batch_translations = future.result()
            for unit_id, text in batch_translations.items():
                for alias in aliases.get(unit_id, [unit_id]):
                    translated[alias] = text
            if checkpoint:
                checkpoint(translated)
    return translated


def _translation_api_configured() -> bool:
    return bool(_stage_value("translation", "MODEL_PROVIDER"))


def translate_payload(
    payload: dict[str, Any], *, cache_path: Path,
) -> dict[str, Any]:
    units = collect_units(payload)
    cached = (
        json.loads(cache_path.read_text(encoding="utf-8"))
        if cache_path.is_file() else {}
    )
    if not isinstance(cached, dict):
        cached = {}
    translations = translate_with_configured_model(
        units,
        cached={str(key): value for key, value in cached.items() if isinstance(value, str)},
        checkpoint=lambda current: write_json_atomic(cache_path, current),
    )
    return apply_units(payload, units, translations)


def maybe_translate_report(
    payload: dict[str, Any], *, cache_path: Path,
) -> dict[str, Any]:
    if not is_china_patient(payload.get("patient") or {}):
        payload["language"] = "en"
        return payload
    mode = os.environ.get("TRANSLATION_MODE", "auto").strip().casefold()
    if mode not in {"auto", "required", "off"}:
        raise ValueError("TRANSLATION_MODE must be auto, required, or off")
    if mode == "off":
        payload["translation_provenance"] = {
            "target_language": "zh-CN", "mode": "disabled", "applied_units": 0,
        }
        return payload
    if not _translation_api_configured():
        if mode == "required":
            raise ValueError(
                "China report translation requires TRANSLATION_MODEL_PROVIDER or MODEL_PROVIDER"
            )
        payload["translation_provenance"] = {
            "target_language": "zh-CN", "mode": "skipped_no_model_api", "applied_units": 0,
        }
        return payload
    return translate_payload(payload, cache_path=cache_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="English pipeline.json")
    parser.add_argument("--output", required=True, help="Translated pipeline.json")
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    output = Path(args.output)
    cache_path = output.with_suffix(output.suffix + ".translations.json")
    write_json_atomic(output, translate_payload(payload, cache_path=cache_path))


if __name__ == "__main__":
    main()
