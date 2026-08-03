"""Run one deterministic model job through a configured remote model API."""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Provider:
    protocol: str
    base_url: str
    key_names: tuple[str, ...]


PROVIDERS = {
    "openai": Provider("openai-responses", "https://api.openai.com/v1", ("OPENAI_API_KEY",)),
    "anthropic": Provider("anthropic-messages", "https://api.anthropic.com/v1", ("ANTHROPIC_API_KEY",)),
    "glm": Provider("openai-chat", "https://open.bigmodel.cn/api/paas/v4", ("GLM_API_KEY", "ZHIPU_API_KEY")),
    "zhipu": Provider("openai-chat", "https://open.bigmodel.cn/api/paas/v4", ("GLM_API_KEY", "ZHIPU_API_KEY")),
    "minimax": Provider("openai-chat", "https://api.minimax.io/v1", ("MINIMAX_API_KEY",)),
    "openai-compatible": Provider("openai-chat", "", ()),
}
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
RESOURCE_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
RESOURCE_SUFFIXES = {".md", ".json"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _stage_value(stage: str, name: str) -> str:
    return (
        os.environ.get(f"{stage.upper()}_{name}", "").strip()
        if stage else ""
    ) or os.environ.get(name, "").strip()


def _configuration(stage: str = "") -> tuple[str, Provider, str, str, str]:
    name = _stage_value(stage, "MODEL_PROVIDER").lower()
    if name not in PROVIDERS:
        raise ValueError(
            "MODEL_PROVIDER must be openai, anthropic, openai-compatible, glm, or minimax"
        )
    provider = PROVIDERS[name]
    model = _stage_value(stage, "MODEL_NAME")
    if not model:
        raise ValueError("MODEL_NAME is required")
    base_url = _stage_value(stage, "MODEL_BASE_URL") or provider.base_url
    if not base_url:
        raise ValueError("MODEL_BASE_URL is required for openai-compatible providers")
    parsed = urllib.parse.urlparse(base_url)
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and local and os.environ.get("MODEL_ALLOW_INSECURE_HTTP") == "1"
    ):
        raise ValueError("MODEL_BASE_URL must use HTTPS; local HTTP requires MODEL_ALLOW_INSECURE_HTTP=1")
    key = _stage_value(stage, "MODEL_API_KEY")
    for key_name in provider.key_names:
        key = key or os.environ.get(key_name, "").strip()
    if not key and not (local and os.environ.get("MODEL_ALLOW_EMPTY_API_KEY") == "1"):
        raise ValueError("Set MODEL_API_KEY or the selected provider's API-key environment variable")
    protocol = _stage_value(stage, "MODEL_API_PROTOCOL") or provider.protocol
    if protocol not in {"openai-responses", "openai-chat", "anthropic-messages"}:
        raise ValueError("Unsupported MODEL_API_PROTOCOL")
    return name, provider, model, base_url.rstrip("/"), key


def _skill_paths(envelope: dict[str, Any]) -> list[Path]:
    raw: list[str] = []
    skills = envelope.get("skill_paths")
    job = envelope.get("job") or {}
    required = job.get("required_execution_order") or []
    if isinstance(skills, dict):
        if required:
            for name in required:
                if name not in skills:
                    raise ValueError(f"Missing SKILL.md path for required subskill: {name}")
                raw.append(str(skills[name]))
        else:
            raw.extend(str(value) for value in skills.values())
    if job.get("skill_path"):
        raw.append(str(job["skill_path"]))
    result: list[Path] = []
    for value in dict.fromkeys(raw):
        path = Path(value).expanduser().resolve()
        if path.name.lower() != "skill.md" or "skills" not in {part.lower() for part in path.parts}:
            raise ValueError(f"Refusing non-SKILL.md instruction path: {path}")
        if not path.is_file():
            raise FileNotFoundError(f"Referenced skill does not exist: {path}")
        result.append(path)
    if not result:
        raise ValueError("Model job does not reference any SKILL.md")
    return result


def _linked_resources(skill_path: Path) -> list[Path]:
    root = skill_path.parent.resolve()
    maximum_files = _env_int("MODEL_SKILL_MAX_FILES", 50)
    maximum_depth = _env_int("MODEL_SKILL_MAX_DEPTH", 2)
    if maximum_files < 1 or maximum_depth < 0:
        raise ValueError("MODEL_SKILL_MAX_FILES and MODEL_SKILL_MAX_DEPTH are invalid")
    discovered: list[Path] = []
    seen = {skill_path.resolve()}
    queue: list[tuple[Path, int]] = [(skill_path.resolve(), 0)]
    while queue:
        source, depth = queue.pop(0)
        text = source.read_text(encoding="utf-8")
        if depth >= maximum_depth:
            continue
        for match in RESOURCE_LINK.finditer(text):
            target_text = match.group(1).strip().split("#", 1)[0].split("?", 1)[0]
            if not target_text or urllib.parse.urlparse(target_text).scheme:
                continue
            target = (source.parent / urllib.parse.unquote(target_text)).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"Skill resource link escapes its skill directory: {source} -> {target_text}"
                ) from exc
            if target.suffix.lower() not in RESOURCE_SUFFIXES:
                continue
            if not target.is_file():
                raise FileNotFoundError(f"Referenced skill resource does not exist: {target}")
            if target in seen:
                continue
            seen.add(target)
            discovered.append(target)
            if len(discovered) > maximum_files:
                raise ValueError(
                    f"Referenced skill resources exceed MODEL_SKILL_MAX_FILES={maximum_files}"
                )
            queue.append((target, depth + 1))
    return discovered


def _prompt_resources(
    skill_path: Path, envelope: dict[str, Any],
) -> list[Path]:
    resources = _linked_resources(skill_path)
    stage = str((envelope.get("job") or {}).get("stage") or "").strip()
    if stage != "deep" or os.environ.get("MODEL_COMPACT_DEEP_PROMPT", "1") == "0":
        return resources
    job_text = json.dumps(envelope.get("job") or {}, ensure_ascii=False).casefold()
    selected: list[Path] = []
    risk_rule_markers = {
        "risk-kras-g12c-by-cancer.md": ("g12c",),
        "risk-kras-g12d-class.md": ("g12d",),
        "risk-pan-ras-class.md": (
            "pan-ras", "pan ras", "ras(on)", "rmc-6236", "daraxonrasib",
        ),
        "risk-cell-therapy-solid-tumor.md": (
            "car-t", "cart", "tcr-t", "til therapy", "cell therapy",
            "cellular therapy",
        ),
        "risk-bispecific-mss-crc.md": (
            "bispecific", "双特异", "mss colorectal", "mss crc",
        ),
        "risk-phase-1-dose-escalation.md": (
            "phase 1", "phase1", "phase i", "early phase 1", "dose escalation",
        ),
    }
    for resource in resources:
        name = resource.name.casefold()
        if name.startswith("output-") and "schema" in resource.stem.casefold():
            selected.append(resource)
            continue
        markers = risk_rule_markers.get(name)
        if markers and any(marker in job_text for marker in markers):
            selected.append(resource)
    return selected


def build_prompt(envelope: dict[str, Any]) -> str:
    sections = []
    schema_sections = []
    for path in _skill_paths(envelope):
        resources = [path, *_prompt_resources(path, envelope)]
        for resource in resources:
            relative = resource.relative_to(path.parent)
            section = (
                f"--- BEGIN {path.parent.name}/{relative.as_posix()} ---\n"
                f"{resource.read_text(encoding='utf-8')}\n"
                f"--- END {path.parent.name}/{relative.as_posix()} ---"
            )
            target = (
                schema_sections
                if resource.name.startswith("output-") and "schema" in resource.stem
                else sections
            )
            target.append(section)
    prompt = (
        "Execute this clinical-analysis job exactly. Follow the embedded skill instructions. "
        "Do not omit, select, rename, or add trials. Return one strict JSON object only, with no "
        "Markdown fences or commentary.\n\n"
        + "\n\n".join(sections)
        + "\n\n--- JOB ENVELOPE ---\n"
        + json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    )
    if schema_sections:
        prompt += (
            "\n\n--- REQUIRED OUTPUT SCHEMAS (HIGHEST PRIORITY) ---\n"
            + "\n\n".join(schema_sections)
        )
    expected_ids = (envelope.get("required_output") or {}).get("expected_trial_ids")
    if expected_ids:
        stage = str((envelope.get("job") or {}).get("stage") or "").strip()
        prompt += (
            "\n\n--- FINAL OUTPUT CONTRACT (HIGHEST PRIORITY) ---\n"
            "Return exactly one object whose top-level key is analyzed_trials. "
            "analyzed_trials must be an array containing exactly one complete result for "
            "each of these trial IDs, with no omissions, duplicates, or extra IDs: "
            + json.dumps(expected_ids, ensure_ascii=False)
            + ". Do not return a single trial object directly."
        )
        if stage == "deep":
            prompt += (
                " For each trial, combine risk and efficacy into the same array item. "
                "Do not emit separate rows for the two subskills. Keep narratives concise, "
                "do not repeat gating or publication metadata, and preserve all required fields. "
                "Use at most 5 mechanisms, 3 applicable risks, 2 short narrative bullets per risk, "
                "3 omitted-risk audit items, 3 caveats, 3 standard-of-care options, and 3 "
                "development-evidence items. Prefer one or two sentences for explanatory strings. "
                "For development evidence, copy only the selected candidate URL and write findings, "
                "applicability, and limitations; the executor supplies citation and search metadata."
            )
    maximum = _env_int("MODEL_MAX_INPUT_CHARS", 500_000)
    if len(prompt) > maximum:
        raise ValueError(
            f"Model request contains {len(prompt)} characters, exceeding MODEL_MAX_INPUT_CHARS={maximum}; "
            "reduce the deterministic batch size"
        )
    return prompt


def _request(
    provider_name: str, protocol: str, base_url: str, key: str, model: str, prompt: str,
    *, stage: str = "",
) -> tuple[str, dict, dict]:
    max_tokens_raw = _stage_value(stage, "MODEL_MAX_OUTPUT_TOKENS") or "16384"
    try:
        max_tokens = int(max_tokens_raw)
    except ValueError as exc:
        raise ValueError("MODEL_MAX_OUTPUT_TOKENS must be an integer") from exc
    temperature_raw = _stage_value(stage, "MODEL_TEMPERATURE")
    try:
        temperature = float(temperature_raw) if temperature_raw else None
    except ValueError as exc:
        raise ValueError("MODEL_TEMPERATURE must be numeric") from exc
    if protocol == "openai-responses":
        body = {"model": model, "input": prompt, "max_output_tokens": max_tokens, "store": False}
        if temperature is not None:
            body["temperature"] = temperature
        return f"{base_url}/responses", {"Authorization": f"Bearer {key}"}, body
    if protocol == "anthropic-messages":
        body = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if temperature is not None:
            body["temperature"] = temperature
        return (
            f"{base_url}/messages",
            {"x-api-key": key, "anthropic-version": os.environ.get("ANTHROPIC_VERSION", "2023-06-01")},
            body,
        )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return exactly one valid JSON object."},
            {"role": "user", "content": prompt},
        ],
    }
    token_parameter = _stage_value(stage, "MODEL_TOKEN_PARAMETER")
    if not token_parameter:
        token_parameter = "max_completion_tokens" if provider_name == "minimax" else "max_tokens"
    if token_parameter not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("MODEL_TOKEN_PARAMETER must be max_tokens or max_completion_tokens")
    body[token_parameter] = max_tokens
    if temperature is not None:
        body["temperature"] = temperature
    if _stage_value(stage, "MODEL_ENABLE_JSON_MODE") == "1":
        body["response_format"] = {"type": "json_object"}
    return f"{base_url}/chat/completions", {"Authorization": f"Bearer {key}"}, body


def _post(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    timeout = _env_float("MODEL_API_TIMEOUT_SECONDS", 600)
    retries = _env_int("MODEL_API_RETRIES", 4)
    base = _env_float("MODEL_API_RETRY_BASE_SECONDS", 1)
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Model API response must be a JSON object")
                return payload
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            if exc.code not in RETRYABLE or attempt == retries:
                raise RuntimeError(f"Model API HTTP {exc.code}: {detail}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else base * (2 ** attempt)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Model API connection failed after {retries + 1} attempts: {exc.reason}") from exc
            delay = base * (2 ** attempt)
        time.sleep(min(delay, 60))
    raise AssertionError("unreachable")


def _response_text(protocol: str, payload: dict[str, Any]) -> str:
    if protocol == "openai-responses":
        texts = [
            str(content.get("text") or "")
            for item in payload.get("output") or []
            for content in item.get("content") or []
            if content.get("type") == "output_text"
        ]
        return "\n".join(filter(None, texts))
    if protocol == "anthropic-messages":
        return "\n".join(
            str(item.get("text") or "") for item in payload.get("content") or []
            if item.get("type") == "text"
        )
    content = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    if isinstance(content, list):
        return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    return str(content)


def _assert_complete_response(protocol: str, payload: dict[str, Any]) -> None:
    if protocol == "openai-responses":
        if payload.get("status") == "incomplete" or payload.get("incomplete_details"):
            raise RuntimeError(
                f"Model response was incomplete: {payload.get('incomplete_details') or 'unknown reason'}"
            )
        return
    if protocol == "anthropic-messages":
        if payload.get("stop_reason") == "max_tokens":
            raise RuntimeError("Model response was truncated at max_tokens")
        return
    choice = (payload.get("choices") or [{}])[0]
    if choice.get("finish_reason") in {"length", "max_tokens"}:
        raise RuntimeError(f"Model response was truncated: {choice.get('finish_reason')}")


def _json_payload(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    candidates.extend(match.group(1).strip() for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.I))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            payload, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("Model API did not return one valid JSON object")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    envelope = json.loads(Path(args.input).read_text(encoding="utf-8"))
    stage = str((envelope.get("job") or {}).get("stage") or "").strip()
    provider_name, provider, model, base_url, key = _configuration(stage)
    protocol = _stage_value(stage, "MODEL_API_PROTOCOL") or provider.protocol
    url, headers, body = _request(
        provider_name, protocol, base_url, key, model, build_prompt(envelope), stage=stage
    )
    response = _post(url, headers, body)
    _assert_complete_response(protocol, response)
    payload = _json_payload(_response_text(protocol, response))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(output)


if __name__ == "__main__":
    main()
