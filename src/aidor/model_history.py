"""Live Copilot model discovery for ``aidor run --interactive``.

Do not hard-code model ids here. Copilot changes its model catalog often,
and the interactive picker must reflect what the *current authenticated
Copilot account* can use.

The primary source of truth is Copilot CLI's structured Agent Client
Protocol (ACP) handshake:

1. Spawn ``copilot --acp --stdio``.
2. Send ``initialize``.
3. Send ``session/new`` and read ``result.models.availableModels``.

The REST ``/models`` endpoint remains a fallback for environments where
the installed Copilot CLI does not expose ACP model metadata.

This is not guessed from human-facing help text. It is corroborated by:

* Copilot CLI's own runtime trace with ``NODE_DEBUG=undici``: immediately
  before logging "Successfully listed 29 models", it calls
  ``GET https://api.enterprise.githubcopilot.com/models``.
* Microsoft VS Code's public ``CopilotApiService`` source, which documents
  the same endpoint discovery flow and exposes ``models(githubToken)`` as
  "List models available to the GitHub user" using ``/copilot_internal/user``
  for ``endpoints.api`` discovery.
* ``copilot login --help``: Copilot CLI accepts tokens from
  ``COPILOT_GITHUB_TOKEN``, ``GH_TOKEN``, and ``GITHUB_TOKEN`` in that
  precedence order, and those token types include GitHub CLI OAuth tokens.

When no usable token is available to aidor, model discovery fails closed:
interactive mode can still ask for a free-form id, but it will not present
a stale hard-coded list as if it were authoritative.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MAX_HISTORY = 12
VALID_ROLES: frozenset[str] = frozenset({"coder", "reviewer"})
DISCOVERY_TIMEOUT_S = 20.0
DEFAULT_SUPPORTED_MODELS_CACHE_TTL_S = 24 * 60 * 60
GITHUB_API = "https://api.github.com"


@dataclass(frozen=True)
class ModelInfo:
    """One Copilot model returned by the live model catalog."""

    model_id: str
    name: str = ""
    category: str = ""

    @property
    def label(self) -> str:
        if self.name and self.name != self.model_id:
            suffix = f" [{self.category}]" if self.category else ""
            return f"{self.model_id} - {self.name}{suffix}"
        return self.model_id


def _default_cache_path() -> Path:
    override = os.environ.get("AIDOR_MODELS_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".copilot" / "aidor_recent_models.json"


def _load_raw(cache_path: Path) -> dict[str, Any]:
    if not cache_path.is_file():
        return {}
    try:
        data: Any = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("recent-models cache unreadable at %s: %s", cache_path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_recent_models(role: str, *, path: Path | None = None) -> list[str]:
    """Return newest-first model ids previously used for ``role``."""
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(VALID_ROLES)}")
    data = _load_raw(path or _default_cache_path())
    return _clean_id_list(data.get(role), limit=MAX_HISTORY)


def load_supported_models(
    *,
    path: Path | None = None,
    max_age_s: float | None = None,
    now: datetime | None = None,
) -> list[ModelInfo]:
    """Return the cached live catalog snapshot, if present and fresh enough.

    ``max_age_s=None`` disables the age check for diagnostics/fallback messages.
    ``max_age_s <= 0`` deliberately bypasses the cache.
    """
    data = _load_raw(path or _default_cache_path())
    supported = data.get("supported")
    if not isinstance(supported, dict):
        return []
    if max_age_s is not None:
        if max_age_s <= 0:
            return []
        fetched_at = _parse_utc_timestamp(supported.get("fetched"))
        if fetched_at is None:
            return []
        reference = now or datetime.now(UTC)
        if (reference - fetched_at).total_seconds() > max_age_s:
            return []
    ids = _clean_id_list(supported.get("ids"))
    names = supported.get("names")
    categories = supported.get("categories")
    out: list[ModelInfo] = []
    for idx, model_id in enumerate(ids):
        out.append(
            ModelInfo(
                model_id=model_id,
                name=_string_list_value(names, idx, default=model_id),
                category=_string_list_value(categories, idx, default=""),
            )
        )
    return out


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def record_model_use(role: str, model: str, *, path: Path | None = None) -> None:
    """Move ``model`` to the front of the recent-models list for ``role``."""
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(VALID_ROLES)}")
    cleaned = (model or "").strip()
    if not cleaned:
        return
    cache_path = path or _default_cache_path()
    data = _ensure_writable(cache_path)
    if data is None:
        return
    existing = _clean_id_list(data.get(role), limit=MAX_HISTORY)
    data[role] = [cleaned, *(x for x in existing if x != cleaned)][:MAX_HISTORY]
    _atomic_write(cache_path, data)


def record_supported_models(models: list[ModelInfo], *, path: Path | None = None) -> None:
    """Persist the latest live catalog for offline visibility."""
    if not models:
        return
    cache_path = path or _default_cache_path()
    data = _ensure_writable(cache_path)
    if data is None:
        return
    data["supported"] = {
        "ids": [m.model_id for m in models],
        "names": [m.name or m.model_id for m in models],
        "categories": [m.category for m in models],
        "fetched": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "copilot-acp:session/new models.availableModels",
    }
    _atomic_write(cache_path, data)


def discover_supported_models(
    *,
    gh_binary: str = "gh",
    copilot_binary: str = "copilot",
    timeout_s: float = DISCOVERY_TIMEOUT_S,
    cwd: Path | None = None,
) -> list[ModelInfo]:
    """Fetch the full current Copilot model catalog from the live API.

    Token precedence mirrors ``copilot login --help`` for headless tokens:
    ``COPILOT_GITHUB_TOKEN``, ``GH_TOKEN``, then ``GITHUB_TOKEN``. If none
    are set, aidor asks ``gh auth token`` for a GitHub CLI OAuth token,
    which Copilot documents as a supported token type.

    ACP is queried first because it is the same structured source Copilot
    exposes to model-picker clients. The returned list is intentionally not
    filtered by ``model_picker_enabled`` when the REST fallback is used:
    Copilot's live model API is still account-specific and current.
    """
    models = _discover_models_via_acp(
        copilot_binary=copilot_binary,
        timeout_s=timeout_s,
        cwd=cwd,
    )
    if models:
        return models
    return _discover_models_via_rest(gh_binary=gh_binary, timeout_s=timeout_s)


def _discover_models_via_acp(
    *,
    copilot_binary: str,
    timeout_s: float,
    cwd: Path | None,
) -> list[ModelInfo]:
    """Return model metadata from ``copilot --acp --stdio`` if available."""
    if not shutil.which(copilot_binary):
        log.debug("Copilot CLI binary %r not found; cannot query ACP models", copilot_binary)
        return []
    workdir = (cwd or Path.cwd()).resolve()
    try:
        proc = subprocess.Popen(
            [copilot_binary, "--acp", "--stdio"],
            cwd=str(workdir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("failed to start Copilot ACP model discovery: %s", exc)
        return []

    messages: queue.Queue[dict[str, Any]] = queue.Queue()
    reader = threading.Thread(
        target=_read_acp_stdout,
        args=(proc, messages),
        name="aidor-copilot-acp-model-reader",
        daemon=True,
    )
    reader.start()

    try:
        _acp_request(
            proc,
            messages,
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "aidor", "version": "1.2.0"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    "_meta": {"parameterizedModelPicker": True},
                },
            },
            timeout_s=timeout_s,
        )
        session = _acp_request(
            proc,
            messages,
            2,
            "session/new",
            {"cwd": str(workdir), "mcpServers": []},
            timeout_s=timeout_s,
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        log.debug("Copilot ACP model discovery failed: %s", exc)
        return []
    finally:
        _terminate_process(proc)

    models = _parse_acp_session_models(session)
    if models:
        return models
    return _parse_acp_config_models(
        session.get("configOptions") if isinstance(session, dict) else None
    )


def _discover_models_via_rest(
    *, gh_binary: str = "gh", timeout_s: float = DISCOVERY_TIMEOUT_S
) -> list[ModelInfo]:
    """Fallback to Copilot's REST model catalog."""
    token = _resolve_github_token(gh_binary=gh_binary, timeout_s=timeout_s)
    if not token:
        return []
    user_info = _get_json(
        f"{GITHUB_API}/copilot_internal/user",
        token=token,
        timeout_s=timeout_s,
    )
    endpoints = user_info.get("endpoints") if isinstance(user_info, dict) else None
    api_base = endpoints.get("api") if isinstance(endpoints, dict) else None
    if not isinstance(api_base, str) or not api_base.startswith("https://"):
        log.debug("copilot_internal/user response did not contain endpoints.api")
        return []
    payload = _get_json(
        f"{api_base.rstrip('/')}/models",
        token=token,
        timeout_s=timeout_s,
    )
    return _parse_models_payload(payload)


def _read_acp_stdout(proc: subprocess.Popen[str], messages: queue.Queue[dict[str, Any]]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload: Any = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            messages.put(payload)


def _acp_request(
    proc: subprocess.Popen[str],
    messages: queue.Queue[dict[str, Any]],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    timeout_s: float,
) -> dict[str, Any]:
    if proc.stdin is None:
        raise RuntimeError("Copilot ACP stdin is unavailable")
    request = {
        "jsonrpc": "2.0",
        "headers": [],
        "id": request_id,
        "method": method,
        "params": params,
    }
    proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    proc.stdin.flush()

    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Copilot ACP {method} timed out")
        try:
            message = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError(f"Copilot ACP {method} timed out") from exc
        if message.get("id") != request_id or "method" in message:
            continue
        if "error" in message:
            raise RuntimeError(f"Copilot ACP {method} failed: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _resolve_github_token(*, gh_binary: str, timeout_s: float) -> str:
    for var_name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        token = os.environ.get(var_name, "").strip()
        if token:
            return token
    if not shutil.which(gh_binary):
        log.debug("GitHub CLI binary %r not found; cannot fetch Copilot models", gh_binary)
        return ""
    try:
        proc = subprocess.run(
            [gh_binary, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("gh auth token failed: %s", exc)
        return ""
    if proc.returncode != 0:
        log.debug("gh auth token exited with code %s", proc.returncode)
        return ""
    return proc.stdout.strip()


def _get_json(url: str, *, token: str, timeout_s: float) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "aidor (copilot model discovery)",
            "Copilot-Integration-Id": "copilot-developer-cli",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        log.debug("GET %s failed during Copilot model discovery: %s", url, exc)
        return {}
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.debug("GET %s returned malformed JSON during Copilot model discovery: %s", url, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _parse_models_payload(payload: dict[str, Any]) -> list[ModelInfo]:
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        cleaned = model_id.strip()
        if cleaned in seen:
            continue
        seen.add(cleaned)
        name = row.get("name")
        category = row.get("model_picker_category")
        out.append(
            ModelInfo(
                model_id=cleaned,
                name=name.strip() if isinstance(name, str) and name.strip() else cleaned,
                category=category.strip() if isinstance(category, str) and category.strip() else "",
            )
        )
    return out


def _parse_acp_session_models(payload: dict[str, Any]) -> list[ModelInfo]:
    models = payload.get("models")
    if not isinstance(models, dict):
        return []
    rows = models.get("availableModels")
    if not isinstance(rows, list):
        return []
    out: list[ModelInfo] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        _append_acp_model(out, seen, row, category="")
    return out


def _parse_acp_config_models(value: Any) -> list[ModelInfo]:
    if not isinstance(value, list):
        return []
    for option in value:
        if not isinstance(option, dict):
            continue
        if option.get("category") != "model" and not _option_mentions(option, "model"):
            continue
        rows = option.get("options")
        if not isinstance(rows, list):
            continue
        out: list[ModelInfo] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("options"), list):
                group_name = _display_text(row.get("name")) or _display_text(row.get("label"))
                for nested in row["options"]:
                    if isinstance(nested, dict):
                        _append_acp_model(out, seen, nested, category=group_name)
            else:
                _append_acp_model(out, seen, row, category="")
        if out:
            return out
    return []


def _append_acp_model(
    out: list[ModelInfo],
    seen: set[str],
    row: dict[str, Any],
    *,
    category: str,
) -> None:
    raw_id = row.get("modelId")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raw_id = row.get("value")
    if not isinstance(raw_id, str) or not raw_id.strip():
        raw_id = row.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip():
        return
    model_id = raw_id.strip()
    if model_id in seen:
        return
    seen.add(model_id)
    name = _display_text(row.get("name")) or _display_text(row.get("label")) or model_id
    out.append(ModelInfo(model_id=model_id, name=name, category=category))


def _display_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _option_mentions(option: dict[str, Any], keyword: str) -> bool:
    haystack = " ".join(
        value
        for value in (_display_text(option.get("id")), _display_text(option.get("name")))
        if value
    )
    return keyword.casefold() in haystack.casefold()


def _clean_id_list(value: Any, *, limit: int | None = None) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
        if limit is not None and len(out) >= limit:
            break
    return out


def _string_list_value(value: Any, idx: int, *, default: str) -> str:
    if isinstance(value, list) and idx < len(value) and isinstance(value[idx], str):
        return value[idx].strip() or default
    return default


def _ensure_writable(cache_path: Path) -> dict[str, Any] | None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("recent-models cache dir unwritable at %s: %s", cache_path.parent, exc)
        return None
    data = _load_raw(cache_path)
    for role in VALID_ROLES:
        if not isinstance(data.get(role), list):
            data[role] = []
    if not isinstance(data.get("supported"), dict):
        data["supported"] = {}
    data["version"] = 2
    return data


def _atomic_write(cache_path: Path, data: dict[str, Any]) -> None:
    try:
        cache_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.debug("recent-models cache unwritable at %s: %s", cache_path, exc)
