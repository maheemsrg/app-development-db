"""Design Studio — a Cursor-style chat workspace for Databricks.

Serves a single-page UI plus two JSON endpoints:
  GET  /api/models  -> chat-capable serving endpoints available to the user
  POST /api/chat    -> proxies a chat completion to the model the user picked

Auth: when the app is deployed with user authorization, each request carries
the signed-in user's token in the `x-forwarded-access-token` header. We use
that so the app sees exactly the models the user can access and calls them as
the user. When that header is absent (e.g. local dev), we fall back to the
app's own service-principal credentials resolved by the SDK Config().
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from databricks.sdk.core import Config

app = FastAPI(title="Design Studio")
logger = logging.getLogger("design-studio")

STATIC_DIR = Path(__file__).parent / "static"

# Shown only when the live lookup is unavailable, so the app is always usable.
FALLBACK_MODELS = [
    "databricks-claude-opus-4-8",
    "databricks-claude-sonnet-4-6",
    "databricks-claude-haiku-4-5",
    "databricks-gpt-5",
    "databricks-meta-llama-3-3-70b-instruct",
]
BUILDER_SAFE_MODEL_PATTERNS = (
    r"claude-sonnet-4-6",
    r"claude-opus-4-8",
    r"gpt-5-5",
    r"gpt-5",
)

_cfg = Config()
_SERVING_ENDPOINTS_PATH = "/api/2.0/serving-endpoints"
_CURRENT_USER_PATH = "/api/2.0/preview/scim/v2/Me"
_WORKSPACE_MKDIRS_PATH = "/api/2.0/workspace/mkdirs"
_WORKSPACE_DELETE_PATH = "/api/2.0/workspace/delete"

MAX_GENERATED_FILES = 30
MAX_TOTAL_FILE_BYTES = 512_000
MAX_SINGLE_FILE_BYTES = 180_000
GENERATION_PARSE_MAX_ATTEMPTS = 2
MAX_GENERATION_HTTP_ATTEMPTS = 6
MAX_GENERATION_REGEN_ATTEMPTS = 3
GENERATION_REQUEST_TIMEOUT_SECONDS = int(os.getenv("GENERATION_REQUEST_TIMEOUT_SECONDS", "240"))
GENERATION_TIMEOUT_MAX_RETRIES = int(os.getenv("GENERATION_TIMEOUT_MAX_RETRIES", "2"))
GENERATION_TIMEOUT_BACKOFF_BASE_SECONDS = float(os.getenv("GENERATION_TIMEOUT_BACKOFF_BASE_SECONDS", "1.0"))
GENERATION_TIMEOUT_BACKOFF_JITTER_SECONDS = float(
    os.getenv("GENERATION_TIMEOUT_BACKOFF_JITTER_SECONDS", "0.35")
)
SAFE_ROOT_FILES = {"app.py", "app.yaml", "requirements.txt", "README.md"}
SAFE_STATIC_EXTENSIONS = {".html", ".js", ".css", ".json", ".ico", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".ttf"}
SAFE_STATIC_FILES = {"static/index.html", "static/app.js", "static/styles.css"}
REQUIRED_GENERATED_FILES = {"app.py", "app.yaml", "requirements.txt"}
EXTERNAL_DEPLOYMENT_TARGETS = (
    "netlify",
    "vercel",
    "github pages",
    "cloudflare pages",
    "firebase hosting",
    "render.com",
    "railway.app",
    "heroku",
)
BUILD_INTENT_KEYWORDS = (
    "build and deploy",
    "build & deploy",
    "deploy this",
    "deploy app",
    "create app",
    "build app",
    "generate app",
    "ship this app",
)
GENERATION_MODEL_PREFERENCE_PATTERNS = (
    r"claude-opus-4-8",
    r"claude-sonnet-4-6",
    r"gpt-5-5",
    r"gpt-5",
    r"claude",
    r"gpt",
)
LOW_RELIABILITY_GENERATION_PATTERNS = (
    r"claude-sonnet-4-7",
    r"sonnet-4\.7",
)
AI_DEV_KIT_BOOTSTRAP = (
    'python -c "import shutil,pathlib;'
    "src=pathlib.Path.home()/'.ai-dev-kit'/'repo';"
    "pairs=[(src/'databricks-skills',pathlib.Path.home()/'.claude'/'skills'),"
    "(src/'databricks-skills',pathlib.Path.home()/'.agent'/'skills')];"
    "\\nfor s,d in pairs:\\n"
    " d.mkdir(parents=True,exist_ok=True)\\n"
    " if s.exists():\\n"
    "  [shutil.copytree(p, d/p.name, dirs_exist_ok=True) for p in s.iterdir() if p.is_dir()]"
    '"'
)

DESIGN_STUDIO_SYSTEM_PROMPT = """You are Design Studio, an expert AI assistant for designing and building Databricks applications.

You help users:
- Design app architecture for Databricks (Apps, Lakebase, Unity Catalog, serving endpoints)
- Write clean, production-ready Python/FastAPI backends and vanilla JS frontends
- Plan features, data models, and API contracts before writing any code
- Review and improve existing Databricks application designs

When a user describes what they want to build:
1. First ask 2-3 clarifying questions (who uses it, does it need to save data, rough scale)
2. Propose an architecture in plain language before writing code
3. Write idiomatic, minimal code — no unnecessary abstractions
4. Explain Databricks-specific choices (why Lakebase vs external DB, why a serving endpoint vs direct SDK call)

Keep responses concise. Use code blocks for all code snippets. Never recommend clicking the Databricks UI — always show the programmatic path."""

_jobs_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}

_SKILL_DIRS = [
    Path.home() / ".ai-dev-kit" / "repo" / "databricks-skills" / "databricks-app-python",
    Path.home() / ".claude" / "skills" / "databricks-app-python",
    Path.home() / ".agent" / "skills" / "databricks-app-python",
]
_MAX_SKILL_BYTES = 40_000


def _load_generation_skills() -> str:
    for skill_dir in _SKILL_DIRS:
        if not skill_dir.exists():
            continue
        parts: list[str] = []
        total = 0
        files = sorted(
            skill_dir.glob("*.md"),
            key=lambda p: (0 if p.name == "SKILL.md" else int(p.name.split("-")[0]) if p.name[0].isdigit() else 99),
        )
        for f in files:
            try:
                content = f.read_text(encoding="utf-8")
                if total + len(content) > _MAX_SKILL_BYTES:
                    break
                parts.append(f"### {f.name}\n{content}")
                total += len(content)
            except Exception:
                continue
        if parts:
            return "\n\n".join(parts)
    return ""


_GENERATION_SKILLS = _load_generation_skills()
if _GENERATION_SKILLS:
    logger.info("Loaded databricks-app-python skills (%d bytes) into generation context", len(_GENERATION_SKILLS))
else:
    logger.warning("databricks-app-python skills not found — generation will use base prompt only")


def _host() -> str:
    return _cfg.host.rstrip("/")


def _app_auth_headers() -> dict:
    """
    Authenticate as the app's service principal.

    Config.authenticate() returns Authorization headers in Databricks Apps.
    """
    headers = _cfg.authenticate()
    if not isinstance(headers, dict):
        raise TypeError("Config.authenticate() did not return headers dict")
    return headers


def _user_auth_headers(request: Request) -> Optional[dict]:
    """Authenticate as the signed-in user when forwarded token exists."""
    user_token = request.headers.get("x-forwarded-access-token")
    if user_token:
        return {"Authorization": f"Bearer {user_token}"}
    return None


def _normalize_task(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _is_chat_task(task: str) -> bool:
    normalized = _normalize_task(task)
    if not normalized:
        return False
    if "embed" in normalized:
        return False
    if "chat" in normalized:
        return True
    return normalized in {"llm/v1/completions", "completions", "text-generation", "generation"}


def _extract_task_candidates(payload: Any) -> set[str]:
    """Recursively collect known task metadata fields from endpoint payload."""
    candidates: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = key.lower()
            if lowered in {"task", "endpoint_task"}:
                task_value = _normalize_task(value)
                if task_value:
                    candidates.add(task_value)
            if isinstance(value, (dict, list)):
                candidates.update(_extract_task_candidates(value))
    elif isinstance(payload, list):
        for item in payload:
            candidates.update(_extract_task_candidates(item))
    return candidates


def _infer_chat_capability(endpoint: dict) -> tuple[bool, Optional[str]]:
    """Infer whether endpoint can accept chat payloads."""
    tasks = sorted(_extract_task_candidates(endpoint))
    if not tasks:
        # Keep unknown endpoints visible but selectable for chat by default.
        return True, None

    for task in tasks:
        if _is_chat_task(task):
            return True, task
    return False, tasks[0]


def _to_model_metadata(endpoint_name: str, endpoint_payload: Optional[dict] = None) -> dict:
    payload = endpoint_payload or {}
    chat_capable, task = _infer_chat_capability(payload)
    return {
        "id": endpoint_name,
        "name": endpoint_name,
        "chat": chat_capable,
        "task": task,
    }


def _is_builder_safe_model_name(model_name: str) -> bool:
    lowered = (model_name or "").strip().lower()
    if not lowered:
        return False
    return any(re.search(pattern, lowered) for pattern in BUILDER_SAFE_MODEL_PATTERNS)


def _filter_builder_safe_chat_models(models: list[dict]) -> list[dict]:
    curated: list[dict] = []
    seen: set[str] = set()
    for model in models:
        model_id = str(model.get("id", "")).strip()
        if not model_id or model_id in seen:
            continue
        if not bool(model.get("chat", True)):
            continue
        if not _is_builder_safe_model_name(model_id):
            continue
        seen.add(model_id)
        curated.append(model)
    preferred_order = _sort_models_by_preference([m["id"] for m in curated])
    rank = {model_id: idx for idx, model_id in enumerate(preferred_order)}
    return sorted(curated, key=lambda m: (rank.get(m["id"], 999), str(m["id"]).lower()))


def _preferred_builder_safe_fallback_model(model_ids: list[str]) -> Optional[str]:
    ordered = _sort_models_by_preference([m for m in model_ids if _is_builder_safe_model_name(m)])
    return ordered[0] if ordered else None


def _resolve_allowed_model(
    requested_model: str,
    available_models: list[dict],
) -> tuple[Optional[str], dict[str, Any]]:
    curated = _filter_builder_safe_chat_models(available_models)
    curated_ids = [m.get("id", "") for m in curated if m.get("id")]
    fallback = _preferred_builder_safe_fallback_model(curated_ids)
    requested = (requested_model or "").strip()
    selected = requested if requested and requested in curated_ids else fallback
    diagnostics = {
        "requested_model": requested,
        "selected_model": selected,
        "fallback_applied": bool(selected and requested and selected != requested),
        "fallback_reason": "outside_allowlist_or_unavailable"
        if selected and requested and selected != requested
        else None,
        "allowlisted_count": len(curated_ids),
    }
    return selected, diagnostics


def _list_serving_endpoints(headers: dict) -> list[dict]:
    """List all serving endpoints with basic metadata, following pagination."""
    models_by_name: dict[str, dict] = {}
    page_token: Optional[str] = None

    while True:
        params = {"page_token": page_token} if page_token else None
        resp = requests.get(
            f"{_host()}{_SERVING_ENDPOINTS_PATH}",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        endpoints = payload.get("endpoints", [])
        for endpoint in endpoints:
            name = endpoint.get("name", "").strip()
            if name:
                models_by_name[name] = _to_model_metadata(name, endpoint)

        page_token = payload.get("next_page_token")
        if not page_token:
            break

    return [models_by_name[name] for name in sorted(models_by_name)]


def _safe_error_metadata(exc: Exception) -> dict:
    """
    Build user-safe, concise error metadata.

    Avoid returning raw response bodies so secrets/tokens are never exposed.
    """
    status_code: Optional[int] = None
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status_code = exc.response.status_code
        message = f"HTTP {status_code} while listing serving endpoints"
    elif isinstance(exc, requests.Timeout):
        message = "Timed out while listing serving endpoints"
    elif isinstance(exc, requests.RequestException):
        message = "Request failed while listing serving endpoints"
    else:
        message = "Unexpected error while listing serving endpoints"

    return {
        "http_status": status_code,
        "error_type": type(exc).__name__,
        "error_message": message,
        "exception_detail": str(exc)[:240],
    }


def _fallback_model_metadata() -> list[dict]:
    return [
        {"id": name, "name": name, "chat": True, "task": "chat/completions"}
        for name in FALLBACK_MODELS
        if _is_builder_safe_model_name(name)
    ]


def _fetch_accessible_models_with_diagnostics(request: Request) -> tuple[list[dict], str, dict]:
    """
    List serving endpoint names visible to the caller.

    Prefer user-token visibility when available. If user-token lookup fails
    (commonly due missing user API scopes), retry with app credentials.
    """
    diagnostics = {
        "lookup_attempts": [],
        "http_status": None,
        "error_type": None,
        "error_message": None,
        "host": _host(),
        "endpoint_path": _SERVING_ENDPOINTS_PATH,
        "fallback_used": False,
    }

    user_headers = _user_auth_headers(request)
    if user_headers:
        try:
            models = _filter_builder_safe_chat_models(_list_serving_endpoints(user_headers))
            diagnostics["lookup_attempts"].append({"mode": "user", "success": True})
            return models, "live-user", diagnostics
        except Exception as exc:
            err = _safe_error_metadata(exc)
            diagnostics["lookup_attempts"].append({"mode": "user", "success": False, **err})
            diagnostics.update(err)

    try:
        models = _filter_builder_safe_chat_models(_list_serving_endpoints(_app_auth_headers()))
        diagnostics["lookup_attempts"].append({"mode": "app", "success": True})
        return models, "live-app", diagnostics
    except Exception as exc:
        err = _safe_error_metadata(exc)
        diagnostics["lookup_attempts"].append({"mode": "app", "success": False, **err})
        diagnostics.update(err)
        diagnostics["fallback_used"] = True
        return _fallback_model_metadata(), "fallback", diagnostics


def _fetch_accessible_models(request: Request) -> tuple[list[dict], str]:
    models, source, _ = _fetch_accessible_models_with_diagnostics(request)
    return models, source


def _slugify(value: str, default: str = "generated-app") -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:40] or default


def _safe_json_loads(content: str) -> tuple[dict[str, Any], str]:
    candidates: list[tuple[str, str]] = [("direct", content)]
    stripped = content.strip()

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        candidates.append(("extracted", fence_match.group(1)))

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(("extracted", stripped[start : end + 1]))

    seen: set[str] = set()
    last_exc: Optional[json.JSONDecodeError] = None
    for mode, candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed, mode
            raise ValueError("Model output was not a JSON object.")
        except json.JSONDecodeError as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise ValueError("Unable to parse model output as JSON object.")


def _strip_markdown_fences_and_prose(content: str) -> str:
    stripped = (content or "").strip().replace("\x00", "")
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
    if fenced:
        for block in fenced:
            if "{" in block and "}" in block:
                return block.strip()
        return fenced[0].strip()
    return stripped


def _extract_likely_json_bounds(content: str) -> str:
    text = (content or "").strip()
    start = text.find("{")
    if start < 0:
        return text
    # Keep the broadest plausible JSON object body when trailing prose is present.
    end = text.rfind("}")
    if end > start:
        return text[start : end + 1]
    return text[start:]


def _escape_newlines_inside_strings(candidate: str) -> str:
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in candidate:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch == "\n":
            out.append("\\n")
            continue
        if in_string and ch == "\r":
            out.append("\\r")
            continue
        out.append(ch)
    return "".join(out)


def _trim_trailing_invalid_json_tail(candidate: str) -> str:
    text = candidate.strip()
    while text:
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            text = text[:-1].rstrip()
    return candidate.strip()


def _balance_json_tail(candidate: str) -> str:
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in candidate:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    if not stack:
        return candidate
    closer = {"{": "}", "[": "]"}
    return f"{candidate}{''.join(closer[ch] for ch in reversed(stack))}"


def _parse_generation_output_with_salvage(content: str) -> tuple[dict[str, Any], str]:
    try:
        parsed, mode = _safe_json_loads(content)
        return _validate_generation_contract(parsed), mode
    except Exception:
        pass

    base = _strip_markdown_fences_and_prose(content)
    extracted = _extract_likely_json_bounds(base)
    candidates: list[tuple[str, str]] = [
        ("salvage_extracted", extracted),
        ("salvage_balanced", _balance_json_tail(extracted)),
    ]
    escaped = _escape_newlines_inside_strings(candidates[-1][1])
    candidates.append(("salvage_escaped_newlines", escaped))
    candidates.append(("salvage_trimmed_tail", _trim_trailing_invalid_json_tail(escaped)))

    last_error: Optional[Exception] = None
    seen: set[str] = set()
    for stage, candidate in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
            return _validate_generation_contract(parsed), stage
        except (json.JSONDecodeError, ValueError, GenerationFormatError) as exc:
            last_error = exc
            continue

    detail = type(last_error).__name__ if last_error else "UnknownParseError"
    raise GenerationFormatError(f"Unable to parse generated JSON after local salvage ({detail}).")


class GenerationFormatError(RuntimeError):
    """Raised when generation output cannot be validated as project JSON."""

    def __init__(self, message: str, attempts: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.attempts = attempts or []


class GenerationInvocationError(RuntimeError):
    """Raised when generation invocation cannot obtain a successful response."""

    def __init__(
        self,
        message: str,
        attempts: list[dict[str, Any]],
        diagnostics: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.attempts = attempts
        self.diagnostics = diagnostics or {}


def _validate_generation_contract(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GenerationFormatError("Model output was not a JSON object.")
    if "files" not in data:
        raise GenerationFormatError("Missing required key: files.")
    if not isinstance(data["files"], dict):
        raise GenerationFormatError("The files field must be a JSON object map.")
    if "project_name" in data and not isinstance(data["project_name"], str):
        raise GenerationFormatError("project_name must be a string when provided.")
    if "summary" in data and not isinstance(data["summary"], str):
        raise GenerationFormatError("summary must be a string when provided.")
    return data


def _extract_response_content(body: dict) -> str:
    try:
        content = body["choices"][0]["message"]["content"]
    except Exception as exc:
        raise RuntimeError("Model returned unexpected generation payload.") from exc
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Model returned empty generation content.")
    return content


def _invoke_generation_with_auth_fallback(
    model: str,
    payload: dict[str, Any],
    user_headers: Optional[dict],
    timeout_seconds: int = 120,
) -> requests.Response:
    headers = user_headers or _app_auth_headers()
    resp = _invoke_chat_model(model, payload, headers, timeout_seconds=timeout_seconds)
    if resp.status_code >= 400 and user_headers:
        resp = _invoke_chat_model(model, payload, _app_auth_headers(), timeout_seconds=timeout_seconds)
    return resp


def _response_format_unsupported(resp: requests.Response) -> bool:
    if resp.status_code not in (400, 404, 422):
        return False
    body = (resp.text or "").lower()
    return (
        "response_format" in body
        or "unknown field" in body
        or "additional properties" in body
        or "not allowed" in body
    )


def _classify_generation_400_reason(status_code: int, body_text: str) -> str:
    if status_code != 400:
        return "non_400"
    text = (body_text or "").lower()
    if (
        "response_format" in text
        or "json_object" in text
        or "additional properties" in text
        or "unknown field" in text
    ):
        return "unsupported_response_format"
    if (
        "invalid schema" in text
        or "schema" in text and "invalid" in text
        or "json schema" in text
    ):
        return "invalid_schema"
    if (
        "chat" in text and "support" in text
        or "messages" in text and "unsupported" in text
        or "not a chat model" in text
        or "completions api" in text
    ):
        return "non_chat_model"
    if (
        "unknown parameter" in text
        or "unsupported parameter" in text
        or "unexpected field" in text
        or "not allowed" in text
    ):
        return "unsupported_params"
    return "generic_http_400"


def _short_response_reason(body_text: str) -> str:
    compact = re.sub(r"\s+", " ", (body_text or "")).strip()
    return compact[:180] if compact else "no response body"


def _available_chat_model_ids_app_auth() -> list[str]:
    try:
        endpoints = _list_serving_endpoints(_app_auth_headers())
    except Exception as exc:
        logger.warning("Model discovery failed for generation fallback: %s", type(exc).__name__)
        return []
    return [
        m.get("id", "").strip()
        for m in endpoints
        if m.get("id") and m.get("chat", True) and _is_builder_safe_model_name(m.get("id", ""))
    ]


def _sort_models_by_preference(model_ids: list[str]) -> list[str]:
    if not model_ids:
        return []
    scored: list[tuple[int, str, str]] = []
    for model_id in model_ids:
        lowered = model_id.lower()
        rank = len(GENERATION_MODEL_PREFERENCE_PATTERNS) + 1
        for idx, pattern in enumerate(GENERATION_MODEL_PREFERENCE_PATTERNS):
            if re.search(pattern, lowered):
                rank = idx
                break
        scored.append((rank, lowered, model_id))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in scored]


def _is_lower_reliability_generation_model(model_id: str) -> bool:
    lowered = (model_id or "").lower()
    return any(re.search(pattern, lowered) for pattern in LOW_RELIABILITY_GENERATION_PATTERNS)


def _generation_model_candidates(preferred_model: str) -> list[str]:
    discovered = _sort_models_by_preference(_available_chat_model_ids_app_auth())
    discovered = [m for m in discovered if m != preferred_model]
    fallback_ranked = _sort_models_by_preference([m for m in FALLBACK_MODELS if m != preferred_model])
    stable_head = [model_id for model_id in fallback_ranked if re.search(r"claude-opus-4-8", model_id.lower())]
    stable_tail = [model_id for model_id in fallback_ranked if model_id not in stable_head]
    # Keep the requested model first so user intent is respected, then prefer stable builder models.
    candidates = [preferred_model, *stable_head, *discovered, *stable_tail]
    seen: set[str] = set()
    ordered: list[str] = []
    for model_id in candidates:
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        ordered.append(model_id)
    return ordered


def _is_transient_generation_error(exc: Exception) -> bool:
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.RequestException):
        lowered = str(exc).lower()
        return "timed out" in lowered or "timeout" in lowered or "connection" in lowered
    return False


def _invoke_generation_request_with_timeout_retries(
    candidate: str,
    payload: dict[str, Any],
    payload_mode: str,
    user_headers: Optional[dict],
) -> tuple[requests.Response, int, Optional[str]]:
    timeout_retries = 0
    last_error_class: Optional[str] = None
    max_retries = max(0, int(GENERATION_TIMEOUT_MAX_RETRIES))

    for retry_index in range(max_retries + 1):
        try:
            resp = _invoke_generation_with_auth_fallback(
                candidate,
                payload,
                user_headers,
                timeout_seconds=GENERATION_REQUEST_TIMEOUT_SECONDS,
            )
            return resp, timeout_retries, last_error_class
        except Exception as exc:
            if not _is_transient_generation_error(exc):
                raise
            last_error_class = type(exc).__name__
            timeout_retries += 1
            if retry_index >= max_retries:
                raise
            sleep_base = GENERATION_TIMEOUT_BACKOFF_BASE_SECONDS * (2**retry_index)
            sleep_jitter = random.uniform(0, max(0.0, GENERATION_TIMEOUT_BACKOFF_JITTER_SECONDS))
            sleep_for = sleep_base + sleep_jitter
            logger.warning(
                "Transient generation error model=%s mode=%s retry=%s/%s error_class=%s sleep=%.2fs",
                candidate,
                payload_mode,
                retry_index + 1,
                max_retries,
                last_error_class,
                sleep_for,
            )
            time.sleep(sleep_for)

    raise RuntimeError("Generation request retry loop exited unexpectedly.")


def _invoke_generation_with_retries(
    preferred_model: str,
    strict_payload: dict[str, Any],
    fallback_payload: dict[str, Any],
    user_headers: Optional[dict],
) -> tuple[requests.Response, str, list[dict[str, Any]], dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    candidates = _generation_model_candidates(preferred_model)
    attempt_number = 0
    timeout_retry_count = 0
    models_tried: list[str] = []
    last_error_class: Optional[str] = None

    for candidate in candidates:
        if candidate not in models_tried:
            models_tried.append(candidate)
        for payload_mode, payload in (("structured_json", strict_payload), ("minimal_chat", fallback_payload)):
            if attempt_number >= MAX_GENERATION_HTTP_ATTEMPTS:
                break
            attempt_number += 1
            try:
                resp, timeout_retries_for_attempt, timeout_error_class = _invoke_generation_request_with_timeout_retries(
                    candidate, payload, payload_mode, user_headers
                )
                timeout_retry_count += timeout_retries_for_attempt
                if timeout_error_class:
                    last_error_class = timeout_error_class
            except Exception as exc:
                if not _is_transient_generation_error(exc):
                    raise
                timeout_retry_count += max(1, GENERATION_TIMEOUT_MAX_RETRIES + 1)
                last_error_class = type(exc).__name__
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "model": candidate,
                        "payload_mode": payload_mode,
                        "status_code": None,
                        "reason": "transient_timeout_exhausted",
                        "short_reason": f"{type(exc).__name__}: {_short_response_reason(str(exc))}",
                    }
                )
                logger.warning(
                    "Generation timeout budget exhausted model=%s mode=%s error_class=%s",
                    candidate,
                    payload_mode,
                    last_error_class,
                )
                # After timeout retries are exhausted for one model, move to next model.
                break
            status_code = int(resp.status_code)
            reason_class = _classify_generation_400_reason(status_code, resp.text)
            attempt_meta = {
                "attempt": attempt_number,
                "model": candidate,
                "payload_mode": payload_mode,
                "status_code": status_code,
                "reason": reason_class,
                "short_reason": _short_response_reason(resp.text),
            }
            attempts.append(attempt_meta)
            logger.info(
                "Generation attempt=%s model=%s mode=%s status=%s reason=%s",
                attempt_number,
                candidate,
                payload_mode,
                status_code,
                reason_class,
            )
            if status_code < 400:
                return resp, candidate, attempts, {
                    "timeout_retries": timeout_retry_count,
                    "models_tried": models_tried,
                    "last_error_class": last_error_class,
                }

            if status_code == 400:
                if reason_class in {"unsupported_response_format", "unsupported_params", "invalid_schema"}:
                    if payload_mode == "structured_json":
                        continue
                if reason_class == "non_chat_model":
                    break
                if payload_mode == "minimal_chat":
                    break
                continue

            if payload_mode == "minimal_chat":
                break
            if status_code in (401, 403, 404, 422, 429):
                break
        if attempt_number >= MAX_GENERATION_HTTP_ATTEMPTS:
            break

    final = attempts[-1] if attempts else {}
    short_reason = final.get("reason", "unknown")
    short_status = final.get("status_code", "unknown")
    message = (
        "Model generation timed out after "
        f"{max(1, timeout_retry_count)} attempts across {len(models_tried)} models; "
        "please retry or pick a smaller scope."
        if timeout_retry_count
        else f"Model generation failed after {len(attempts)} attempts (last_status={short_status}, reason={short_reason})."
    )
    raise GenerationInvocationError(
        message,
        attempts,
        diagnostics={
            "timeout_retries": timeout_retry_count,
            "models_tried": models_tried,
            "last_error_class": last_error_class,
            "last_status": short_status,
            "last_reason": short_reason,
        },
    )


def _repair_generation_json(model: str, raw_content: str, user_headers: Optional[dict]) -> str:
    repair_payload = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Repair malformed JSON. Return ONLY valid JSON object with keys: "
                    "project_name, summary, files. files must be an object mapping file path to content."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Repair this into valid JSON only. Do not add markdown or prose.\n\n"
                    f"{raw_content}"
                ),
            },
        ],
        "response_format": {"type": "json_object"},
    }
    resp = _invoke_generation_with_auth_fallback(model, repair_payload, user_headers)
    if resp.status_code >= 400:
        raise GenerationFormatError(f"JSON repair failed (HTTP {resp.status_code}).")
    return _extract_response_content(resp.json())


def _latest_user_message(messages: list["ChatMessage"]) -> str:
    for msg in reversed(messages):
        if msg.role.strip().lower() == "user":
            return msg.content.strip()
    return ""


def _contains_external_target(text: str) -> bool:
    lowered = text.lower()
    return any(target in lowered for target in EXTERNAL_DEPLOYMENT_TARGETS)


def _is_build_intent(message: str) -> bool:
    lowered = message.lower()
    if any(keyword in lowered for keyword in BUILD_INTENT_KEYWORDS):
        return True
    if "deploy" in lowered and "app" in lowered:
        return True
    return False


def _framework_deployable_without_static(files: dict[str, str]) -> bool:
    app_yaml = files.get("app.yaml", "").lower()
    app_py = files.get("app.py", "").lower()
    framework_markers = (
        "streamlit",
        "gradio",
        "dash",
        "uvicorn",
        "gunicorn",
        "fastapi",
        "flask",
        "reflex",
    )
    return any(marker in app_yaml or marker in app_py for marker in framework_markers)


def _ensure_required_generated_artifacts(files: dict[str, str]) -> None:
    missing = sorted(path for path in REQUIRED_GENERATED_FILES if path not in files)
    if missing:
        raise ValueError(f"Missing required generated files: {', '.join(missing)}")
    has_static = any(path.startswith("static/") for path in files)
    if not has_static and not _framework_deployable_without_static(files):
        raise ValueError("Generated app must include static/* assets or a known framework entrypoint.")


_SAFE_UVICORN_COMMAND = ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
_SAFE_STREAMLIT_COMMAND = ["streamlit", "run", "app.py", "--server.port", "8000", "--server.address", "0.0.0.0"]


def _repair_app_yaml(app_yaml: str, app_py: str = "") -> str:
    """Parse app.yaml and replace it if the command is missing or malformed.

    Preserves any valid `resources:` block from the LLM output. Detects
    Streamlit apps from app.py content and sets the right default command.
    """
    import yaml as _yaml  # pyyaml — only imported here to avoid top-level dep at startup

    parsed: dict = {}
    try:
        parsed = _yaml.safe_load(app_yaml) or {}
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception:
        parsed = {}

    command = parsed.get("command")
    command_ok = (
        isinstance(command, list)
        and len(command) >= 1
        and all(isinstance(c, str) for c in command)
    )
    if command_ok:
        return app_yaml  # already valid

    # Command is missing or invalid — rebuild with a safe default.
    is_streamlit = "streamlit" in (app_py or "").lower()
    default_cmd = _SAFE_STREAMLIT_COMMAND if is_streamlit else _SAFE_UVICORN_COMMAND

    rebuilt: dict = {"command": default_cmd}
    # Preserve resources / env sections if the LLM generated them correctly.
    for key in ("resources", "env"):
        if key in parsed and isinstance(parsed[key], list):
            rebuilt[key] = parsed[key]

    return _yaml.dump(rebuilt, default_flow_style=False, sort_keys=False)


def _inject_ai_dev_kit_bootstrap_into_app_yaml(app_yaml: str) -> str:
    if AI_DEV_KIT_BOOTSTRAP in app_yaml:
        return app_yaml
    import yaml as _yaml
    try:
        parsed = _yaml.safe_load(app_yaml) or {}
        if not isinstance(parsed, dict):
            parsed = {}
        command = parsed.get("command", [])
        if isinstance(command, list) and command:
            shell_cmd = shlex.join(str(c) for c in command)
        else:
            shell_cmd = "python app.py"
        parsed["command"] = ["sh", "-lc", f"{AI_DEV_KIT_BOOTSTRAP} && exec {shell_cmd}"]
        return _yaml.dump(parsed, default_flow_style=False, sort_keys=False)
    except Exception:
        trimmed = app_yaml.rstrip()
        separator = "\n" if trimmed else ""
        return f'{trimmed}{separator}command: ["sh", "-lc", "{AI_DEV_KIT_BOOTSTRAP} && exec python app.py"]\n'


def _validate_generated_files(files: dict[str, str]) -> tuple[dict[str, str], int]:
    if not isinstance(files, dict) or not files:
        raise ValueError("Model output did not include any files.")
    if len(files) > MAX_GENERATED_FILES:
        raise ValueError(f"Generated too many files ({len(files)} > {MAX_GENERATED_FILES}).")

    sanitized: dict[str, str] = {}
    total_bytes = 0
    for raw_path, raw_content in files.items():
        if not isinstance(raw_path, str) or not isinstance(raw_content, str):
            raise ValueError("Generated files must be a map of string path to string content.")
        path = raw_path.strip().replace("\\", "/")
        if not path:
            raise ValueError("Generated file path is empty.")
        posix = PurePosixPath(path)
        if posix.is_absolute() or ".." in posix.parts:
            raise ValueError(f"Illegal file path: {path}")
        normalized = str(posix)
        if normalized.startswith(".git") or normalized.startswith(".venv"):
            raise ValueError(f"Blocked file path: {path}")
        in_static = normalized.startswith("static/") and PurePosixPath(normalized).suffix in SAFE_STATIC_EXTENSIONS
        if normalized not in SAFE_ROOT_FILES and not in_static:
            raise ValueError(f"File path is not in allowlist: {path}")
        size = len(raw_content.encode("utf-8"))
        if size > MAX_SINGLE_FILE_BYTES:
            raise ValueError(f"File too large: {path}")
        total_bytes += size
        sanitized[normalized] = raw_content

    if total_bytes > MAX_TOTAL_FILE_BYTES:
        raise ValueError("Generated project exceeds total file size cap.")
    _ensure_required_generated_artifacts(sanitized)
    if "app.yaml" in sanitized:
        sanitized["app.yaml"] = _repair_app_yaml(sanitized["app.yaml"], sanitized.get("app.py", ""))
    return sanitized, total_bytes


def _run_cmd(args: list[str], retries: int = 1, timeout: int = 120, extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
    import os
    run_env = {**os.environ, **(extra_env or {})}
    # If a PAT token is being injected, remove OAuth credentials to avoid
    # "more than one authorization method" error from the Databricks CLI.
    if extra_env and "DATABRICKS_TOKEN" in extra_env:
        run_env.pop("DATABRICKS_CLIENT_ID", None)
        run_env.pop("DATABRICKS_CLIENT_SECRET", None)
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=run_env,
            )
            if proc.returncode == 0:
                return proc
            message = proc.stderr.strip() or proc.stdout.strip() or "Unknown command failure"
            if attempt == retries:
                raise RuntimeError(message)
            time.sleep(1.2 * (attempt + 1))
        except Exception as exc:
            last_exc = exc
            if attempt == retries:
                break
            time.sleep(1.2 * (attempt + 1))
    if last_exc:
        raise RuntimeError(str(last_exc))
    raise RuntimeError("Command failed")


def _get_effective_user(user_headers: Optional[dict], forwarded_username: Optional[str]) -> str:
    if forwarded_username:
        return forwarded_username
    if user_headers:
        try:
            resp = requests.get(f"{_host()}{_CURRENT_USER_PATH}", headers=user_headers, timeout=10)
            if resp.status_code < 400:
                payload = resp.json()
                user_name = payload.get("userName")
                if isinstance(user_name, str) and user_name:
                    return user_name
        except Exception:
            pass
    env_fallback = os.getenv("DBX_DEFAULT_USER")
    if env_fallback:
        return env_fallback
    return "naveen.balachandran@databricks.com"


def _workspace_owner_segment(raw_value: Optional[str], default: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return default
    normalized = re.sub(r"[^a-zA-Z0-9._@-]+", "-", value).strip("-")
    return normalized or default


def _app_service_principal_client_id() -> Optional[str]:
    env_candidates = (
        os.getenv("DATABRICKS_CLIENT_ID"),
        os.getenv("DATABRICKS_SERVICE_PRINCIPAL_CLIENT_ID"),
        os.getenv("DB_APP_SERVICE_PRINCIPAL_CLIENT_ID"),
    )
    for candidate in env_candidates:
        normalized = _workspace_owner_segment(candidate, "")
        if normalized:
            return normalized
    cfg_client_id = _workspace_owner_segment(getattr(_cfg, "client_id", None), "")
    if cfg_client_id:
        return cfg_client_id
    return None


def _app_owned_workspace_path(project_slug: str) -> str:
    app_owner = _app_service_principal_client_id()
    if not app_owner:
        # Deterministic fallback stays under app-owned generated root.
        app_owner = _workspace_owner_segment(
            os.getenv("DATABRICKS_APP_NAME") or os.getenv("DBX_APP_NAME"),
            "app-owned",
        )
    return f"/Workspace/Users/{app_owner}/generated/{project_slug}"


def _probe_workspace_write(path: str) -> tuple[bool, Optional[str]]:
    try:
        _ensure_workspace_dir(path)
        return True, None
    except Exception as exc:
        detail = str(exc).strip()[:240]
        return False, f"write_probe_failed:{type(exc).__name__}:{detail}"


def _resolve_workspace_upload_path(
    project_slug: str,
    user_headers: Optional[dict],
    forwarded_username: Optional[str],
) -> dict[str, Optional[str]]:
    app_owned_path = _app_owned_workspace_path(project_slug)
    preferred_mode = (os.getenv("DBX_WORKSPACE_PATH_MODE", "app_owned") or "app_owned").strip().lower()

    if preferred_mode != "user":
        return {
            "requested_workspace_path": app_owned_path,
            "effective_workspace_path": app_owned_path,
            "workspace_path_mode": "app_owned",
            "workspace_path_fallback_reason": None,
        }

    user_name = _workspace_owner_segment(_get_effective_user(user_headers, forwarded_username), "")
    requested_user_path = f"/Workspace/Users/{user_name}/generated/{project_slug}" if user_name else app_owned_path
    if not user_name:
        return {
            "requested_workspace_path": requested_user_path,
            "effective_workspace_path": app_owned_path,
            "workspace_path_mode": "app_owned",
            "workspace_path_fallback_reason": "user_identity_unavailable",
        }

    can_write, probe_reason = _probe_workspace_write(requested_user_path)
    if can_write:
        return {
            "requested_workspace_path": requested_user_path,
            "effective_workspace_path": requested_user_path,
            "workspace_path_mode": "user",
            "workspace_path_fallback_reason": None,
        }

    return {
        "requested_workspace_path": requested_user_path,
        "effective_workspace_path": app_owned_path,
        "workspace_path_mode": "app_owned",
        "workspace_path_fallback_reason": probe_reason,
    }


def _invoke_generation_model(
    model: str, user_request: str, project_name: str, user_headers: Optional[dict]
) -> tuple[dict, dict[str, Any]]:
    skill_block = (
        f"\n\n## Databricks App Development Reference\n\n{_GENERATION_SKILLS}"
        if _GENERATION_SKILLS
        else ""
    )
    system_prompt = (
        "You generate production-ready source code ONLY for Databricks Apps deployment. "
        "Return ONLY strict JSON with keys: project_name, summary, files. "
        "files MUST be an object map path->content. "
        "Allowed file paths: app.py, app.yaml, requirements.txt, README.md, "
        "and any static/* files with extensions: .html .js .css .json .ico .svg .png .jpg .gif .woff .woff2 .ttf. "
        "No markdown fences and no extra keys. "
        "NEVER mention or include deployment steps for Netlify, Vercel, GitHub, or non-Databricks targets. "
        "app.yaml MUST have a 'command' key that is a YAML list of strings, e.g.: "
        "command:\\n  - uvicorn\\n  - app:app\\n  - --host\\n  - 0.0.0.0\\n  - --port\\n  - '8000'\\n"
        "No other top-level keys except optional 'resources' and 'env'. "
        "project_name MUST be a 2-3 word kebab-case slug (lowercase, hyphens only, max 20 chars). "
        "Name the THING, not the request — 'tic-tac-toe' not 'build-tic-tac-toe-game'. "
        "Never start with a verb like build/create/make/please. No 'app' suffix."
        + skill_block
    )
    user_prompt = (
        f"Build a runnable app for this request: {user_request}\n"
        f"Project name (2-3 words max, name the thing not the action"
        f"{f', use: {project_name}' if project_name and project_name != 'generated-app' else ''}): \n"
        "Use FastAPI + static frontend when suitable. "
        "Ensure app.yaml uses valueFrom for resources when needed."
    )
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    }
    strict_payload = {
        **payload,
        "response_format": {"type": "json_object"},
    }
    resp, resolved_model, attempts, invocation_diagnostics = _invoke_generation_with_retries(
        model,
        strict_payload,
        payload,
        user_headers,
    )
    parse_attempts: list[dict[str, Any]] = []

    def _try_parse(candidate_model: str, payload_mode: str, content: str) -> Optional[dict[str, Any]]:
        try:
            parsed, parse_stage = _parse_generation_output_with_salvage(content)
            parse_attempts.append(
                {"model": candidate_model, "mode": payload_mode, "parse_stage": parse_stage, "success": True}
            )
            logger.info(
                "Generation parse success model=%s mode=%s parse_stage=%s",
                candidate_model,
                payload_mode,
                parse_stage,
            )
            return parsed
        except GenerationFormatError as exc:
            parse_attempts.append(
                {
                    "model": candidate_model,
                    "mode": payload_mode,
                    "parse_stage": "failed_local_salvage",
                    "success": False,
                    "error_type": type(exc).__name__,
                }
            )
            logger.warning(
                "Generation parse failed model=%s mode=%s error_class=%s",
                candidate_model,
                payload_mode,
                type(exc).__name__,
            )
            return None

    content = _extract_response_content(resp.json())
    parsed = _try_parse(resolved_model, attempts[-1].get("payload_mode", "unknown"), content)
    if parsed is not None:
        return parsed, {
            "requested_model": model,
            "initial_model": resolved_model,
            "fallback_model": None,
            "timeout_retries": invocation_diagnostics.get("timeout_retries", 0),
            "models_tried": invocation_diagnostics.get("models_tried", [resolved_model]),
            "last_error_class": invocation_diagnostics.get("last_error_class"),
        }

    # Optional best-effort remote repair: never fail solely on repair endpoint errors.
    if (
        os.getenv("GENERATION_REMOTE_REPAIR_ENABLED", "false").lower() == "true"
        and not _is_lower_reliability_generation_model(resolved_model)
    ):
        try:
            repaired_content = _repair_generation_json(resolved_model, content, user_headers)
            repaired = _try_parse(resolved_model, "remote_repair", repaired_content)
            if repaired is not None:
                return repaired, {
                    "requested_model": model,
                    "initial_model": resolved_model,
                    "fallback_model": None,
                    "timeout_retries": invocation_diagnostics.get("timeout_retries", 0),
                    "models_tried": invocation_diagnostics.get("models_tried", [resolved_model]),
                    "last_error_class": invocation_diagnostics.get("last_error_class"),
                }
        except Exception as exc:
            logger.warning("Remote JSON repair skipped after failure: %s", type(exc).__name__)

    candidates = _generation_model_candidates(model)
    remaining = [candidate for candidate in candidates if candidate != resolved_model]
    if _is_lower_reliability_generation_model(resolved_model):
        logger.info("Detected lower-reliability generation model=%s; forcing immediate fallback", resolved_model)
    regen_count = 0
    for candidate in remaining:
        if regen_count >= MAX_GENERATION_REGEN_ATTEMPTS:
            break
        regen_count += 1
        regen_resp = _invoke_generation_with_auth_fallback(candidate, payload, user_headers)
        status_code = int(regen_resp.status_code)
        reason_class = _classify_generation_400_reason(status_code, regen_resp.text)
        attempts.append(
            {
                "attempt": len(attempts) + 1,
                "model": candidate,
                "payload_mode": "minimal_chat",
                "status_code": status_code,
                "reason": reason_class,
                "short_reason": _short_response_reason(regen_resp.text),
            }
        )
        if status_code >= 400:
            logger.warning(
                "Generation regen failed model=%s mode=minimal_chat status=%s reason=%s",
                candidate,
                status_code,
                reason_class,
            )
            continue
        regen_content = _extract_response_content(regen_resp.json())
        regen_parsed = _try_parse(candidate, "minimal_chat", regen_content)
        if regen_parsed is not None:
            return regen_parsed, {
                "requested_model": model,
                "initial_model": resolved_model,
                "fallback_model": candidate,
                "timeout_retries": invocation_diagnostics.get("timeout_retries", 0),
                "models_tried": invocation_diagnostics.get("models_tried", [resolved_model, candidate]),
                "last_error_class": invocation_diagnostics.get("last_error_class"),
            }

    parse_summary = "; ".join(
        f"{item.get('model')}|{item.get('mode')}|{item.get('parse_stage')}"
        for item in parse_attempts[:8]
    )
    raise GenerationFormatError(
        "Model outputs could not be validated after local salvage and bounded fallback regeneration. "
        f"Attempts: {parse_summary or 'none'}",
        attempts=parse_attempts,
    )


def _ensure_workspace_dir(path: str) -> None:
    raw_path = (path or "").strip()
    if not raw_path:
        raise RuntimeError("Could not create workspace path for upload: empty path.")
    canonical_path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    api_path = canonical_path
    if api_path == "/Workspace":
        return
    if api_path.startswith("/Workspace/"):
        api_path = api_path[len("/Workspace") :]

    def _iter_parent_paths(target: str) -> list[str]:
        parts = [segment for segment in target.split("/") if segment]
        parents: list[str] = []
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else f"/{part}"
            parents.append(current)
        return parents

    def _is_already_exists(status_code: int, body_text: str) -> bool:
        if status_code not in {400, 409}:
            return False
        lowered = body_text.lower()
        return any(
            marker in lowered
            for marker in (
                "already exists",
                "resource_already_exists",
                "file already exists",
                "directory already exists",
            )
        )

    headers = _app_auth_headers()
    api_error: Optional[Exception] = None
    try:
        for parent in _iter_parent_paths(api_path):
            resp = requests.post(
                f"{_host()}{_WORKSPACE_MKDIRS_PATH}",
                headers=headers,
                json={"path": parent},
                timeout=20,
            )
            if resp.status_code < 400:
                continue
            if _is_already_exists(resp.status_code, resp.text):
                continue
            raise RuntimeError(f"status={resp.status_code} body={_short_response_reason(resp.text)}")
        return
    except Exception as exc:
        api_error = exc
        logger.warning(
            "Workspace mkdirs API failed path=%s error=%s: %s",
            canonical_path,
            type(exc).__name__,
            exc,
        )

    cli_error: Optional[Exception] = None
    try:
        _run_cmd(["databricks", "workspace", "mkdirs", canonical_path], retries=1, timeout=60)
        return
    except RuntimeError as exc:
        if "already exists" in str(exc).lower():
            return
        cli_error = exc
    except Exception as exc:
        cli_error = exc

    api_diag = (
        f"{type(api_error).__name__}: {api_error}" if api_error is not None else "none"
    )
    cli_diag = (
        f"{type(cli_error).__name__}: {cli_error}" if cli_error is not None else "none"
    )
    raise RuntimeError(
        "Could not create workspace path for upload. "
        f"path={canonical_path} api_error={api_diag} cli_error={cli_diag}"
    )


def _replace_workspace_folder(local_path: Path, workspace_path: str) -> None:
    headers = _app_auth_headers()
    requests.post(
        f"{_host()}{_WORKSPACE_DELETE_PATH}",
        headers=headers,
        json={"path": workspace_path, "recursive": True},
        timeout=20,
    )
    _ensure_workspace_dir(workspace_path)
    _run_cmd(
        [
            "databricks",
            "workspace",
            "import-dir",
            str(local_path),
            workspace_path,
            "--overwrite",
        ],
        retries=2,
        timeout=180,
    )


_VERB_PREFIX = re.compile(
    r'^(please[\s-]*)?(build|create|make|generate|develop|write|add|design|implement|a|an|the)[\s-]+',
    re.IGNORECASE,
)

def _clean_project_name(raw: str) -> str:
    """Strip imperative verbs so 'please build a tic tac toe' → 'tic-tac-toe'."""
    name = raw.strip()
    for _ in range(4):  # remove up to 4 leading verb/article words
        cleaned = _VERB_PREFIX.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    # Keep only the first 4 words to avoid long descriptions becoming names
    words = re.split(r"[\s_-]+", name)[:4]
    return "-".join(w for w in words if w)


def _app_name_for_project(project_slug: str) -> str:
    # Databricks app names: 2-30 chars, kebab-case.
    # Prefix "ds-" = 3 chars, leaving 27 for the slug.
    slug = _slugify(project_slug, default="app")[:27].rstrip("-")
    return f"ds-{slug}"


def _is_app_already_exists_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "already exists",
            "resource_already_exists",
            "app with the same name already exists",
        )
    )


def _is_app_not_found_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "does not exist",
            "not found",
            "resource_does_not_exist",
            "no such app",
        )
    )


def _parse_json_or_raw_output(raw_output: str) -> dict[str, Any]:
    text = (raw_output or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"raw_output": text[:500]}
    return parsed if isinstance(parsed, dict) else {"raw_output": text[:500]}


def _get_existing_app_payload(app_name: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        app_proc = _run_cmd(["databricks", "apps", "get", app_name, "-o", "json"], retries=1, timeout=60)
    except RuntimeError as exc:
        message = str(exc)
        if _is_app_not_found_error(message):
            return None, message
        raise
    return _parse_json_or_raw_output(app_proc.stdout), None


def _deploy_databricks_app(app_name: str, workspace_path: str) -> dict:
    # Deploy always runs as the service principal — the user's OAuth token lacks the
    # `apps` scope required by the Databricks CLI. The SP must own (or have CAN_MANAGE
    # on) the target app; apps created by this SP automatically satisfy that.
    deployment_notes: list[str] = []
    app_payload, get_error = _get_existing_app_payload(app_name)
    app_exists = app_payload is not None
    if app_exists:
        deployment_notes.append("app exists, reused")
    else:
        deployment_notes.append("app not found, creating")
        try:
            create_proc = _run_cmd(["databricks", "apps", "create", app_name], retries=1, timeout=60)
            create_output = f"{create_proc.stdout}\n{create_proc.stderr}"
            if _is_app_already_exists_error(create_output):
                app_exists = True
                deployment_notes.append("app create returned already exists; reused")
            else:
                app_exists = True
                deployment_notes.append("app created")
        except RuntimeError as exc:
            if _is_app_already_exists_error(str(exc)):
                app_exists = True
                deployment_notes.append("app create race detected; reused existing app")
            else:
                raise

    if app_payload is None and app_exists:
        app_payload, get_error = _get_existing_app_payload(app_name)

    # Ensure the app is RUNNING before deploying.
    # Newly created apps can be STOPPED or STARTING; deploy fails unless RUNNING.
    state = ((app_payload or {}).get("compute_status") or {}).get("state", "")
    if state in ("STOPPED", "CRASHED", "ERROR"):
        _run_cmd(["databricks", "apps", "start", app_name], retries=1, timeout=30)
        deployment_notes.append(f"started app from {state} state before deploy")
        time.sleep(3)

    for _wait in range(30):  # up to ~150s
        app_payload, _ = _get_existing_app_payload(app_name)
        state = ((app_payload or {}).get("compute_status") or {}).get("state", "")
        if state == "RUNNING" or state == "":
            break
        time.sleep(5)

    deploy_proc = _run_cmd(
        [
            "databricks",
            "apps",
            "deploy",
            app_name,
            "--source-code-path",
            workspace_path,
            "-o",
            "json",
        ],
        retries=2,
        timeout=240,
    )
    deployment_payload = _parse_json_or_raw_output(deploy_proc.stdout or deploy_proc.stderr)
    latest_app_payload, latest_get_error = _get_existing_app_payload(app_name)
    if latest_app_payload:
        app_payload = latest_app_payload
    if app_payload is None:
        app_payload = {}
    if not app_payload and (latest_get_error or get_error):
        deployment_notes.append("app metadata lookup unavailable after deploy")
    return {
        "created": not any("reused" in note for note in deployment_notes),
        "reused_existing_app": any("reused" in note for note in deployment_notes),
        "deployment_notes": deployment_notes,
        "deployment": deployment_payload,
        "app": app_payload,
    }


def _update_job(job_id: str, step_meta: Optional[dict] = None, **updates: Any) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        step = updates.pop("step", None)
        if step:
            job["current_step"] = step
            step_record: dict[str, Any] = {"step": step, "at": time.time()}
            if step_meta:
                step_record.update(step_meta)
            job["steps"].append(step_record)
        job.update(updates)


def _run_build_and_deploy_job(
    job_id: str,
    req: BuildDeployRequest,
    user_headers: Optional[dict],
    forwarded_username: Optional[str],
    selected_model: str,
    model_selection: dict[str, Any],
) -> None:
    project_hint = req.project_name or "generated-app"
    project_slug = _slugify(project_hint)
    workspace_path_info = _resolve_workspace_upload_path(project_slug, user_headers, forwarded_username)
    workspace_path = workspace_path_info["effective_workspace_path"] or _app_owned_workspace_path(project_slug)
    app_name = _app_name_for_project(project_slug)

    stage_dir: Optional[Path] = None
    try:
        _update_job(job_id, status="running", step="Generating code")
        generated, generation_diagnostics = _invoke_generation_model(
            selected_model, req.user_request, project_hint, user_headers
        )

        # Use the LLM-suggested project name if the user didn't specify one explicitly.
        if not req.project_name:
            llm_name = _clean_project_name((generated.get("project_name") or "").strip())
            if len(llm_name) >= 3:
                project_slug = _slugify(llm_name)
                app_name = _app_name_for_project(project_slug)
                workspace_path_info = _resolve_workspace_upload_path(project_slug, user_headers, forwarded_username)
                workspace_path = (
                    workspace_path_info["effective_workspace_path"]
                    or _app_owned_workspace_path(project_slug)
                )

        generated_files, total_bytes = _validate_generated_files(generated.get("files", {}))
        effective_generation_model = generation_diagnostics.get("fallback_model") or selected_model
        _update_job(
            job_id,
            step="Generated code",
            step_meta={
                "model": effective_generation_model,
                "file_count": len(generated_files),
                "file_names": list(generated_files.keys()),
            },
        )

        _update_job(job_id, step="Materializing files")
        temp_root = Path(tempfile.mkdtemp(prefix="design-studio-build-"))
        stage_dir = temp_root / project_slug
        stage_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in generated_files.items():
            file_path = stage_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        if req.dry_run:
            _update_job(
                job_id,
                status="completed",
                step="Done",
                result={
                    "workspace_source_path": workspace_path,
                    "requested_workspace_path": workspace_path_info["requested_workspace_path"],
                    "effective_workspace_path": workspace_path,
                    "workspace_path_mode": workspace_path_info["workspace_path_mode"],
                    "workspace_path_fallback_reason": workspace_path_info["workspace_path_fallback_reason"],
                    "app_name": app_name,
                    "generation_model": selected_model,
                    "generation_effective_model": generation_diagnostics.get("fallback_model") or selected_model,
                    "generation_diagnostics": generation_diagnostics,
                    "model_selection": model_selection,
                    "dry_run": True,
                    "generated_file_count": len(generated_files),
                    "generated_total_bytes": total_bytes,
                    "generated_files": [
                        {"path": p, "content": c} for p, c in generated_files.items()
                    ],
                },
            )
            return

        _update_job(job_id, step="Uploading files", step_meta={"workspace_path": workspace_path})
        _replace_workspace_folder(stage_dir, workspace_path)

        _update_job(job_id, step="Deploying app", step_meta={"app_name": app_name})
        deployed = _deploy_databricks_app(app_name, workspace_path)
        app_info = deployed.get("app", {})
        deployment = deployed.get("deployment", {})
        deployment_notes = deployed.get("deployment_notes", [])

        _update_job(
            job_id,
            status="completed",
            step="Done",
            step_meta={"app_url": app_info.get("url"), "app_name": app_name},
            result={
                "workspace_source_path": workspace_path,
                "requested_workspace_path": workspace_path_info["requested_workspace_path"],
                "effective_workspace_path": workspace_path,
                "workspace_path_mode": workspace_path_info["workspace_path_mode"],
                "workspace_path_fallback_reason": workspace_path_info["workspace_path_fallback_reason"],
                "app_name": app_name,
                "app_url": app_info.get("url"),
                "generation_model": selected_model,
                "generation_effective_model": generation_diagnostics.get("fallback_model") or selected_model,
                "generation_diagnostics": generation_diagnostics,
                "model_selection": model_selection,
                "app_id": app_info.get("id"),
                "deployment_id": deployment.get("deployment_id") or deployment.get("id"),
                "deployment_status": deployment.get("status") or app_info.get("status"),
                "reused_existing_app": bool(deployed.get("reused_existing_app")),
                "deployment_notes": deployment_notes,
                "created_resource_ids": {
                    "app_id": app_info.get("id"),
                    "deployment_id": deployment.get("deployment_id") or deployment.get("id"),
                },
                "generated_file_count": len(generated_files),
                "generated_total_bytes": total_bytes,
                "generated_files": [
                    {"path": p, "content": c} for p, c in generated_files.items()
                ],
            },
        )
    except Exception as exc:
        logger.exception("Build/deploy pipeline failed: %s", exc)
        error_payload = {
            "message": "Build and deploy failed. Check request and Databricks permissions.",
            "detail": "Review generation diagnostics and retry.",
        }
        if isinstance(exc, GenerationInvocationError):
            invocation_diag = exc.diagnostics or {}
            timeout_retries = int(invocation_diag.get("timeout_retries") or 0)
            models_tried = invocation_diag.get("models_tried") or []
            if timeout_retries > 0:
                message = (
                    "Model generation timed out after "
                    f"{max(1, timeout_retries)} attempts across {max(1, len(models_tried))} models; "
                    "please retry or pick a smaller scope."
                )
                detail = "Generation request exceeded timeout/network retry budget."
            else:
                message = "Model generation failed across allowlisted models; please retry or pick a smaller scope."
                detail = "Generation request exhausted model/payload retry budget."
            error_payload = {
                "message": message,
                "detail": detail,
                "timeout_retries": timeout_retries,
                "models_tried": models_tried,
                "last_error_class": invocation_diag.get("last_error_class"),
            }
            error_payload["generation_attempts"] = [
                {
                    "model": a.get("model"),
                    "payload_mode": a.get("payload_mode"),
                    "status_code": a.get("status_code"),
                    "reason": a.get("reason"),
                    "short_reason": a.get("short_reason"),
                }
                for a in exc.attempts[:MAX_GENERATION_HTTP_ATTEMPTS]
            ]
        if isinstance(exc, GenerationFormatError):
            error_payload = {
                "message": (
                    "Generation output was invalid after local salvage and fallback model retries. "
                    "Try simplifying the prompt or rerunning with a different allowlisted model."
                ),
                "detail": str(exc)[:500],
            }
            if getattr(exc, "attempts", None):
                error_payload["generation_attempts"] = [
                    {
                        "model": a.get("model"),
                        "payload_mode": a.get("mode"),
                        "parse_stage": a.get("parse_stage"),
                        "success": bool(a.get("success")),
                    }
                    for a in exc.attempts[:MAX_GENERATION_HTTP_ATTEMPTS]
                ]
        _update_job(
            job_id,
            status="failed",
            step="Failed",
            error=error_payload,
        )
    finally:
        if stage_dir and stage_dir.parent.exists():
            shutil.rmtree(stage_dir.parent, ignore_errors=True)


def _create_build_job(
    req: BuildDeployRequest, request: Request, initial_status: str = "queued"
) -> tuple[str, dict[str, Any]]:
    job_id = str(uuid.uuid4())
    live_models, _ = _fetch_accessible_models(request)
    selected_model, model_selection = _resolve_allowed_model(req.model, live_models)
    if not selected_model:
        raise HTTPException(
            status_code=400,
            detail="No allowlisted chat-capable models are currently available.",
        )
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": initial_status,
            "current_step": "Queued",
            "steps": [{"step": "Queued", "at": time.time()}],
            "result": None,
            "error": None,
            "requested_project_name": req.project_name,
            "requested_model": req.model,
            "selected_model": selected_model,
            "model_selection": model_selection,
            "workspace_host": _host(),
        }
    user_headers = _user_auth_headers(request)
    forwarded_username = request.headers.get("x-forwarded-preferred-username")
    thread = threading.Thread(
        target=_run_build_and_deploy_job,
        args=(job_id, req, user_headers, forwarded_username, selected_model, model_selection),
        daemon=True,
    )
    thread.start()
    return job_id, _jobs[job_id]


def _is_auth_scope_error(status_code: int, body_text: str) -> bool:
    """Return True when response indicates auth scope/permission issue."""
    if status_code not in (401, 403):
        return False
    text = body_text.lower()
    auth_markers = (
        "invalid scope",
        "required scopes",
        "permission",
        "not authorized",
        "unauthorized",
        "forbidden",
    )
    return any(marker in text for marker in auth_markers)


def _invoke_chat_model(
    model: str, payload: dict, headers: dict, timeout_seconds: int = 120
) -> requests.Response:
    """Invoke a serving endpoint chat model."""
    url = f"{_host()}/serving-endpoints/{model}/invocations"
    request_headers = {"Content-Type": "application/json", **headers}
    return requests.post(url, headers=request_headers, json=payload, timeout=timeout_seconds)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage]


class BuildDeployRequest(BaseModel):
    user_request: str
    model: str
    project_name: Optional[str] = None
    dry_run: bool = False


class PromoteRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    selected_docs: Optional[list[str]] = None
    workspace_path: Optional[str] = None


class IdeateRequest(BaseModel):
    description: str
    model: str


class IdeatePromptRequest(BaseModel):
    idea_title: str
    idea_description: str
    messages: list[ChatMessage]
    model: str


@app.get("/api/models")
def list_models(request: Request) -> dict:
    """Return serving endpoints the caller can access plus chat capability."""
    models, source, diagnostics = _fetch_accessible_models_with_diagnostics(request)
    diagnostics["model_policy"] = "allowlisted_chat_only"
    diagnostics["returned_model_count"] = len(models)
    return {"models": models, "source": source, "diagnostics": diagnostics}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request) -> dict:
    """Proxy a chat completion to the selected serving endpoint."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages provided.")

    # Keep the selected model aligned with currently accessible endpoints.
    try:
        live_models, _ = _fetch_accessible_models(request)
    except Exception:
        live_models = []
    selected_model, model_selection = _resolve_allowed_model(req.model, live_models)
    if not selected_model:
        raise HTTPException(status_code=400, detail="No allowlisted chat-capable models available.")

    latest_user_text = _latest_user_message(req.messages)
    if _is_build_intent(latest_user_text):
        build_req = BuildDeployRequest(
            user_request=latest_user_text,
            model=req.model,
            project_name=_slugify(latest_user_text[:50], default="generated-app"),
        )
        job_id, _ = _create_build_job(build_req, request)
        return {
            "content": "Starting Build & Deploy in your current Databricks workspace.",
            "mode": "build_and_deploy",
            "build_trigger": {
                "job_id": job_id,
                "status": "queued",
                "workspace_host": _host(),
                "model_selection": model_selection,
            },
        }

    messages_with_system = [{"role": "system", "content": DESIGN_STUDIO_SYSTEM_PROMPT}] + [
        m.model_dump() for m in req.messages
    ]
    payload = {"messages": messages_with_system}

    diagnostics = {"auth_modes_attempted": [], "model_selection": model_selection}
    user_headers = _user_auth_headers(request)
    resp: Optional[requests.Response] = None

    if user_headers:
        diagnostics["auth_modes_attempted"].append("user")
        user_resp: Optional[requests.Response] = None
        try:
            user_resp = _invoke_chat_model(selected_model, payload, user_headers)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not reach model: {exc}")
        if user_resp.status_code < 400:
            resp = user_resp
        elif _is_auth_scope_error(user_resp.status_code, user_resp.text):
            diagnostics["auth_modes_attempted"].append("app")
            try:
                resp = _invoke_chat_model(selected_model, payload, _app_auth_headers())
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Could not reach model: {exc}")
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail={
                        "message": f"Model error: {resp.text[:400]}",
                        "diagnostics": diagnostics,
                    },
                )
        else:
            raise HTTPException(
                status_code=user_resp.status_code,
                detail={
                    "message": f"Model error: {user_resp.text[:400]}",
                    "diagnostics": diagnostics,
                },
            )
    else:
        diagnostics["auth_modes_attempted"].append("app")
        try:
            resp = _invoke_chat_model(selected_model, payload, _app_auth_headers())
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not reach model: {exc}")
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=resp.status_code,
                detail={
                    "message": f"Model error: {resp.text[:400]}",
                    "diagnostics": diagnostics,
                },
            )

    if resp is None:
        raise HTTPException(status_code=502, detail="Model invocation did not return a response.")

    data = resp.json()

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise HTTPException(status_code=502, detail="Unexpected model response.")
    if _contains_external_target(content) and not _contains_external_target(latest_user_text):
        content = (
            "I can deploy this in Databricks workspace directly. "
            "Use Build & Deploy or ask me to build and deploy now."
        )
    return {"content": content, "diagnostics": {"model_selection": model_selection}}


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Proxy a chat completion as Server-Sent Events (token-by-token streaming)."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages provided.")

    try:
        live_models, _ = _fetch_accessible_models(request)
    except Exception:
        live_models = []
    selected_model, model_selection = _resolve_allowed_model(req.model, live_models)
    if not selected_model:
        raise HTTPException(status_code=400, detail="No allowlisted chat-capable models available.")

    payload = {
        "messages": [m.model_dump() for m in req.messages],
        "stream": True,
    }

    user_headers = _user_auth_headers(request)
    auth_headers = user_headers or _app_auth_headers()

    def _generate():
        url = f"{_host()}/serving-endpoints/{selected_model}/invocations"
        req_headers = {"Content-Type": "application/json", **auth_headers}
        try:
            with requests.post(
                url,
                headers=req_headers,
                json=payload,
                stream=True,
                timeout=120,
            ) as resp:
                if resp.status_code >= 400:
                    error_text = resp.text[:400]
                    yield f"data: {json.dumps({'error': f'Model error: {error_text}'})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                for raw_line in resp.iter_lines():
                    if not raw_line:
                        continue
                    if isinstance(raw_line, bytes):
                        raw_line = raw_line.decode("utf-8", errors="replace")
                    if raw_line.startswith("data: "):
                        payload_str = raw_line[6:].strip()
                        if payload_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            chunk = json.loads(payload_str)
                            delta = (
                                chunk.get("choices", [{}])[0]
                                .get("delta", {})
                                .get("content") or ""
                            )
                            if delta:
                                yield f"data: {json.dumps({'delta': delta})}\n\n"
                        except (json.JSONDecodeError, IndexError, KeyError):
                            pass
                yield "data: [DONE]\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)[:300]})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/build-and-deploy")
def start_build_and_deploy(req: BuildDeployRequest, request: Request) -> dict:
    if not req.user_request.strip():
        raise HTTPException(status_code=400, detail="user_request is required.")
    if not req.model.strip():
        raise HTTPException(status_code=400, detail="model is required.")

    job_id, _ = _create_build_job(req, request)
    return {"job_id": job_id, "status": "queued"}


_PROMOTE_DOCS = [
    (
        "architecture",
        "You are a software architect. Write clear, detailed technical architecture documentation.",
        "Based on this conversation, write a detailed architecture document for the described application. "
        "Include: system overview, component diagram (as ASCII or text), data flow, technology choices and rationale, "
        "scalability considerations, and Databricks-specific components used (serving endpoints, Unity Catalog, "
        "Lakebase, Apps). Format as a markdown document.",
    ),
    (
        "security",
        "You are a security engineer. Write thorough, practical security documentation.",
        "Based on this conversation, write security documentation for the described application. "
        "Include: authentication and authorisation design, data classification and handling, network security, "
        "secrets management, compliance considerations, and Databricks security controls applied. "
        "Format as a markdown document.",
    ),
    (
        "jira_stories",
        "You are a product manager. Write well-structured Jira stories.",
        "Based on this conversation, write a full set of Jira stories required to build this application, "
        "ordered by execution sequence. For each story include: title, description, acceptance criteria (bullet list), "
        "story points (1/2/3/5/8), and dependencies on other stories. "
        "Format as markdown with each story as a level-2 heading.",
    ),
    (
        "test_cases",
        "You are a QA engineer. Write comprehensive test case documentation.",
        "Based on this conversation, write comprehensive test case documentation. "
        "Include: unit tests, integration tests, end-to-end test scenarios, edge cases, and performance considerations. "
        "For each test case include: test ID, description, preconditions, steps, and expected result. "
        "Format as a markdown document.",
    ),
    (
        "build_prompt",
        "You are a prompt engineer. Write a complete, self-contained AI prompt.",
        "Based on this conversation, write a single comprehensive prompt that a developer could paste into an AI "
        "assistant to build this entire application from scratch. The prompt should be self-contained: include all "
        "requirements, tech stack choices, data models, API contracts, UI design decisions, and deployment target "
        "(Databricks Apps). Write ONLY the prompt text, no preamble.",
    ),
]


@app.post("/api/promote")
def promote(req: PromoteRequest, request: Request) -> dict:
    """Generate selected promotion documents and upload them to the workspace."""
    if not req.messages:
        raise HTTPException(status_code=400, detail="No messages provided — have a conversation first, then promote.")

    live_models, _ = _fetch_accessible_models(request)
    selected_model, _ = _resolve_allowed_model(req.model, live_models)
    # If allowlist resolution fails (e.g. SP can't list endpoints), fall back to trusting
    # the model the user already has selected in the UI — it worked for chat/build.
    if not selected_model:
        selected_model = (req.model or "").strip() or None
    if not selected_model:
        raise HTTPException(status_code=400, detail="No model available — pick a model in the top-right dropdown.")
    logger.info("Promote request: model=%s messages=%d docs=%s", selected_model, len(req.messages), req.selected_docs)

    wanted = set(req.selected_docs) if req.selected_docs else {d[0] for d in _PROMOTE_DOCS}
    user_headers = _user_auth_headers(request)
    conversation = [m.model_dump() for m in req.messages]
    documents: dict[str, Any] = {}

    for doc_key, system_prompt, user_prompt in _PROMOTE_DOCS:
        if doc_key not in wanted:
            continue
        msgs = [
            {"role": "system", "content": system_prompt},
            *conversation,
            {"role": "user", "content": user_prompt},
        ]
        payload = {"messages": msgs}
        try:
            headers = user_headers or _app_auth_headers()
            resp = _invoke_chat_model(selected_model, payload, headers, timeout_seconds=120)
            if resp.status_code >= 400 and user_headers:
                resp = _invoke_chat_model(selected_model, payload, _app_auth_headers(), timeout_seconds=120)
            if resp.status_code >= 400:
                documents[doc_key] = {"error": f"Model returned HTTP {resp.status_code}"}
                continue
            data = resp.json()
            documents[doc_key] = data["choices"][0]["message"]["content"]
        except Exception as exc:
            documents[doc_key] = {"error": str(exc)[:240]}

    uploaded_to: Optional[str] = None
    if req.workspace_path and any(isinstance(v, str) for v in documents.values()):
        try:
            staging = tempfile.mkdtemp()
            docs_dir = os.path.join(staging, "docs")
            os.makedirs(docs_dir)
            for doc_key, content in documents.items():
                if isinstance(content, str):
                    with open(os.path.join(docs_dir, f"{doc_key}.md"), "w", encoding="utf-8") as fh:
                        fh.write(content)
            workspace_docs = req.workspace_path.rstrip("/") + "/docs"
            _run_cmd(
                ["databricks", "workspace", "import-dir", docs_dir, workspace_docs, "--overwrite"],
                retries=1,
                timeout=60,
            )
            uploaded_to = workspace_docs
        except Exception as exc:
            logger.warning("Promote workspace upload failed: %s", exc)

    return {"documents": documents, "uploaded_to": uploaded_to}


@app.post("/api/ideate")
def ideate(req: IdeateRequest, request: Request) -> dict:
    """Generate 5 app ideas from a user description."""
    live_models, _ = _fetch_accessible_models(request)
    selected_model, _ = _resolve_allowed_model(req.model, live_models)
    if not selected_model:
        raise HTTPException(status_code=400, detail="No allowlisted chat-capable models available.")

    system_prompt = (
        "You are a creative product strategist specialising in Databricks applications. "
        "Return ONLY valid JSON — a list of exactly 5 objects, each with keys: "
        "title (string, ≤8 words), description (string, 2-3 sentences), "
        "why (string, 1 sentence on why this works well as a Databricks app)."
    )
    user_prompt = f"Here is the user's problem or job context:\n\n{req.description}\n\nGenerate 5 app ideas."

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    }

    user_headers = _user_auth_headers(request)
    headers = user_headers or _app_auth_headers()
    resp = _invoke_chat_model(selected_model, payload, headers, timeout_seconds=120)
    if resp.status_code >= 400 and user_headers:
        resp = _invoke_chat_model(selected_model, payload, _app_auth_headers(), timeout_seconds=120)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Model error: {resp.text[:400]}")

    content = resp.json()["choices"][0]["message"]["content"]
    # Strip markdown fences if present
    stripped = content.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", stripped, flags=re.IGNORECASE)
    if fence_match:
        stripped = fence_match.group(1).strip()
    # Find JSON array bounds
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start >= 0 and end > start:
        stripped = stripped[start:end + 1]
    try:
        ideas = json.loads(stripped)
        if not isinstance(ideas, list):
            raise ValueError("Not a list")
    except Exception:
        raise HTTPException(status_code=500, detail="Could not parse ideas from model response.")

    return {"ideas": ideas}


@app.post("/api/ideate/prompt")
def ideate_prompt(req: IdeatePromptRequest, request: Request) -> dict:
    """Generate a build prompt for a selected idea."""
    live_models, _ = _fetch_accessible_models(request)
    selected_model, _ = _resolve_allowed_model(req.model, live_models)
    if not selected_model:
        raise HTTPException(status_code=400, detail="No allowlisted chat-capable models available.")

    system_prompt = (
        "You are a senior Databricks app architect. Write a single, self-contained build prompt "
        "that a developer could paste into an AI assistant to build the described app from scratch "
        "on Databricks Apps. Include: what the app does, the tech stack (FastAPI + vanilla JS on "
        "Databricks Apps), data needs, key screens/features, and any Databricks-specific requirements. "
        "Write ONLY the prompt — no preamble, no explanation."
    )
    conversation_context = "\n".join(
        f"{m.role}: {m.content}" for m in req.messages
    )
    user_prompt = (
        f"Selected idea: {req.idea_title}\n\n{req.idea_description}\n\n"
        f"Conversation context:\n{conversation_context}"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
    }

    user_headers = _user_auth_headers(request)
    headers = user_headers or _app_auth_headers()
    resp = _invoke_chat_model(selected_model, payload, headers, timeout_seconds=120)
    if resp.status_code >= 400 and user_headers:
        resp = _invoke_chat_model(selected_model, payload, _app_auth_headers(), timeout_seconds=120)
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Model error: {resp.text[:400]}")

    content = resp.json()["choices"][0]["message"]["content"]
    return {"prompt": content}


@app.get("/api/build-and-deploy/{job_id}")
def get_build_and_deploy_status(job_id: str) -> dict:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found.")
        return job


# ---- Static assets via explicit routes (avoids the websocket health-check
# tripping StaticFiles' http-only assertion). ----

_ALLOWED = {"index.html", "styles.css", "app.js"}
_MIME = {".html": "text/html", ".css": "text/css", ".js": "application/javascript"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{filename}")
def static_file(filename: str) -> FileResponse:
    if filename not in _ALLOWED:
        raise HTTPException(status_code=404, detail="Not found")
    path = STATIC_DIR / filename
    media = _MIME.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media)
