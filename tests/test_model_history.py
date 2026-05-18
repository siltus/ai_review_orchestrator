"""Tests for live Copilot model discovery helpers."""

from __future__ import annotations

import json
import queue
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from aidor.model_history import (
    ModelInfo,
    _acp_request,
    _default_cache_path,
    _discover_models_via_acp,
    _discover_models_via_rest,
    _get_json,
    _load_raw,
    _parse_acp_config_models,
    _parse_acp_session_models,
    _parse_models_payload,
    _read_acp_stdout,
    _resolve_github_token,
    discover_supported_models,
    load_recent_models,
    load_supported_models,
    record_model_use,
    record_supported_models,
)


class _FakeStdin:
    def __init__(self):
        self.writes: list[str] = []

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        return None


class _FakeProc:
    def __init__(self, lines: list[str]):
        self.stdin = _FakeStdin()
        self.stdout = iter(lines)
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, *, timeout: float | None = None):
        return 0


def _json_line(payload: dict[str, object]) -> str:
    return json.dumps(payload) + "\n"


def test_parse_models_payload_uses_all_live_catalog_models():
    payload = {
        "data": [
            {
                "id": "gpt-live",
                "name": "GPT Live",
                "model_picker_enabled": True,
                "model_picker_category": "powerful",
            },
            {
                "id": "text-embedding-3-small",
                "name": "Embedding",
                "model_picker_enabled": False,
            },
            {"id": "missing-picker-flag", "name": "Hidden"},
        ]
    }

    models = _parse_models_payload(payload)

    assert models == [
        ModelInfo("gpt-live", "GPT Live", "powerful"),
        ModelInfo("text-embedding-3-small", "Embedding", ""),
        ModelInfo("missing-picker-flag", "Hidden", ""),
    ]


def test_parse_acp_session_models_uses_structured_model_ids():
    payload = {
        "models": {
            "currentModelId": "alpha-live",
            "availableModels": [
                {"modelId": "alpha-live", "name": "Alpha Live"},
                {"id": "legacy-live", "name": "Legacy Live"},
                {"modelId": "alpha-live", "name": "Duplicate"},
            ],
        }
    }

    assert _parse_acp_session_models(payload) == [
        ModelInfo("alpha-live", "Alpha Live", ""),
        ModelInfo("legacy-live", "Legacy Live", ""),
    ]


def test_parse_acp_config_models_flattens_grouped_select_options():
    payload = [
        {"id": "mode", "type": "select", "options": [{"value": "agent", "name": "Agent"}]},
        {
            "id": "model",
            "category": "model",
            "type": "select",
            "options": [
                {
                    "name": "Available",
                    "options": [
                        {"value": "alpha-live", "label": "Alpha Live"},
                        {"value": "beta-live", "name": "Beta Live"},
                    ],
                },
                {"value": "custom-live"},
            ],
        },
    ]

    assert _parse_acp_config_models(payload) == [
        ModelInfo("alpha-live", "Alpha Live", "Available"),
        ModelInfo("beta-live", "Beta Live", "Available"),
        ModelInfo("custom-live", "custom-live", ""),
    ]


def test_parse_acp_config_models_can_detect_model_option_by_id():
    payload = [
        {
            "id": "available-models",
            "type": "select",
            "options": [{"value": "alpha-live", "name": "Alpha Live"}],
        }
    ]

    assert _parse_acp_config_models(payload) == [ModelInfo("alpha-live", "Alpha Live", "")]


def test_read_acp_stdout_ignores_malformed_lines():
    messages: queue.Queue[dict[str, object]] = queue.Queue()
    proc = _FakeProc(["not json\n", "\n", _json_line({"id": 1, "result": {"ok": True}})])

    _read_acp_stdout(cast(Any, proc), messages)

    assert messages.get_nowait() == {"id": 1, "result": {"ok": True}}
    assert messages.empty()


def test_acp_request_writes_json_rpc_and_ignores_notifications():
    messages: queue.Queue[dict[str, object]] = queue.Queue()
    messages.put({"jsonrpc": "2.0", "method": "session/update", "params": {}})
    messages.put({"jsonrpc": "2.0", "id": 7, "result": {"answer": True}})
    proc = _FakeProc([])

    result = _acp_request(cast(Any, proc), messages, 7, "test/method", {"x": 1}, timeout_s=0.1)

    assert result == {"answer": True}
    assert proc.stdin.writes == [
        '{"jsonrpc":"2.0","headers":[],"id":7,"method":"test/method","params":{"x":1}}\n'
    ]


def test_acp_request_raises_on_rpc_error():
    import pytest

    messages: queue.Queue[dict[str, object]] = queue.Queue()
    messages.put({"jsonrpc": "2.0", "id": 8, "error": {"message": "nope"}})

    with pytest.raises(RuntimeError, match="test/method"):
        _acp_request(cast(Any, _FakeProc([])), messages, 8, "test/method", {}, timeout_s=0.1)


def test_discover_models_via_acp_reads_session_models(monkeypatch, tmp_path: Path):
    proc = _FakeProc(
        [
            _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
            _json_line({"jsonrpc": "2.0", "method": "session/update", "params": {}}),
            _json_line(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "sessionId": "s1",
                        "models": {
                            "availableModels": [
                                {"modelId": "alpha-live", "name": "Alpha Live"},
                                {"modelId": "beta-live", "name": "Beta Live"},
                            ]
                        },
                    },
                }
            ),
        ]
    )
    calls: list[object] = []
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: "copilot.exe")
    monkeypatch.setattr(
        "aidor.model_history.subprocess.Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or proc,
    )

    assert _discover_models_via_acp(copilot_binary="copilot", timeout_s=0.1, cwd=tmp_path) == [
        ModelInfo("alpha-live", "Alpha Live", ""),
        ModelInfo("beta-live", "Beta Live", ""),
    ]
    assert proc.terminated
    assert calls


def test_discover_models_via_acp_falls_back_to_config_options(monkeypatch, tmp_path: Path):
    proc = _FakeProc(
        [
            _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
            _json_line(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "sessionId": "s1",
                        "configOptions": [
                            {
                                "id": "model",
                                "category": "model",
                                "options": [{"value": "alpha-live", "name": "Alpha Live"}],
                            }
                        ],
                    },
                }
            ),
        ]
    )
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: "copilot.exe")
    monkeypatch.setattr("aidor.model_history.subprocess.Popen", lambda *_, **__: proc)

    assert _discover_models_via_acp(copilot_binary="copilot", timeout_s=0.1, cwd=tmp_path) == [
        ModelInfo("alpha-live", "Alpha Live", "")
    ]


def test_discover_models_via_acp_fails_closed_when_binary_missing(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: None)

    assert _discover_models_via_acp(copilot_binary="copilot", timeout_s=0.1, cwd=tmp_path) == []


def test_discover_models_via_acp_fails_closed_when_process_start_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: "copilot.exe")
    monkeypatch.setattr(
        "aidor.model_history.subprocess.Popen",
        lambda *_, **__: (_ for _ in ()).throw(OSError("nope")),
    )

    assert _discover_models_via_acp(copilot_binary="copilot", timeout_s=0.1, cwd=tmp_path) == []


def test_discover_models_via_acp_fails_closed_on_rpc_error(monkeypatch, tmp_path: Path):
    proc = _FakeProc([_json_line({"jsonrpc": "2.0", "id": 1, "error": {"message": "nope"}})])
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: "copilot.exe")
    monkeypatch.setattr("aidor.model_history.subprocess.Popen", lambda *_, **__: proc)

    assert _discover_models_via_acp(copilot_binary="copilot", timeout_s=0.1, cwd=tmp_path) == []
    assert proc.terminated


def test_discover_models_via_rest_fails_closed_without_token(monkeypatch):
    monkeypatch.setattr("aidor.model_history._resolve_github_token", lambda **_: "")

    assert _discover_models_via_rest(gh_binary="gh", timeout_s=0.1) == []


def test_model_label_includes_name_and_category():
    assert ModelInfo("gpt-live", "GPT Live", "powerful").label == "gpt-live - GPT Live [powerful]"
    assert ModelInfo("gpt-live").label == "gpt-live"


def test_default_cache_path_honors_env_override(monkeypatch, tmp_path: Path):
    cache = tmp_path / "cache.json"
    monkeypatch.setenv("AIDOR_MODELS_CACHE", str(cache))

    assert _default_cache_path() == cache


def test_load_raw_tolerates_malformed_cache(tmp_path: Path):
    cache = tmp_path / "models.json"
    cache.write_text("{not json", encoding="utf-8")

    assert _load_raw(cache) == {}


def test_load_recent_models_rejects_unknown_role(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError):
        load_recent_models("robot", path=tmp_path / "models.json")


def test_supported_model_cache_round_trips_model_metadata(tmp_path: Path):
    cache = tmp_path / "models.json"

    record_supported_models(
        [
            ModelInfo("gpt-live", "GPT Live", "powerful"),
            ModelInfo("claude-live", "Claude Live", "versatile"),
        ],
        path=cache,
    )

    assert load_supported_models(path=cache) == [
        ModelInfo("gpt-live", "GPT Live", "powerful"),
        ModelInfo("claude-live", "Claude Live", "versatile"),
    ]


def test_supported_model_cache_honors_max_age(tmp_path: Path):
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps(
            {
                "supported": {
                    "ids": ["alpha-live"],
                    "names": ["Alpha Live"],
                    "categories": [""],
                    "fetched": "2026-05-16T00:00:00Z",
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_supported_models(
        path=cache,
        max_age_s=86_400,
        now=datetime(2026, 5, 16, 23, 59, tzinfo=UTC),
    ) == [ModelInfo("alpha-live", "Alpha Live", "")]
    assert (
        load_supported_models(
            path=cache,
            max_age_s=86_400,
            now=datetime(2026, 5, 17, 0, 0, 1, tzinfo=UTC),
        )
        == []
    )
    assert load_supported_models(
        path=cache,
        max_age_s=None,
        now=datetime(2026, 5, 20, tzinfo=UTC),
    ) == [ModelInfo("alpha-live", "Alpha Live", "")]


def test_supported_model_cache_zero_ttl_bypasses_cache(tmp_path: Path):
    cache = tmp_path / "models.json"
    record_supported_models([ModelInfo("alpha-live", "Alpha Live")], path=cache)

    assert load_supported_models(path=cache, max_age_s=0) == []


def test_supported_model_cache_rejects_invalid_fetched_timestamp(tmp_path: Path):
    cache = tmp_path / "models.json"
    cache.write_text(
        json.dumps(
            {
                "supported": {
                    "ids": ["alpha-live"],
                    "names": ["Alpha Live"],
                    "categories": [""],
                    "fetched": "not-a-timestamp",
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_supported_models(path=cache, max_age_s=86_400) == []
    assert load_supported_models(path=cache, max_age_s=None) == [
        ModelInfo("alpha-live", "Alpha Live", "")
    ]


def test_supported_model_cache_ignores_non_mapping_snapshot(tmp_path: Path):
    cache = tmp_path / "models.json"
    cache.write_text(json.dumps({"supported": ["alpha-live"]}), encoding="utf-8")

    assert load_supported_models(path=cache, max_age_s=86_400) == []


def test_recent_model_cache_moves_existing_choice_to_front(tmp_path: Path):
    cache = tmp_path / "models.json"

    record_model_use("coder", "gpt-live", path=cache)
    record_model_use("coder", "claude-live", path=cache)
    record_model_use("coder", "gpt-live", path=cache)

    import json

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["coder"] == ["gpt-live", "claude-live"]


def test_empty_model_records_are_noops(tmp_path: Path):
    cache = tmp_path / "models.json"

    record_model_use("coder", "", path=cache)
    record_supported_models([], path=cache)

    assert not cache.exists()


def test_resolve_github_token_prefers_copilot_env(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    assert _resolve_github_token(gh_binary="gh", timeout_s=0.1) == "copilot-token"


def test_resolve_github_token_falls_back_to_gh(monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: "gh.exe")
    monkeypatch.setattr(
        "aidor.model_history.subprocess.run",
        lambda *_, **__: SimpleNamespace(returncode=0, stdout="gh-token\n"),
    )

    assert _resolve_github_token(gh_binary="gh", timeout_s=0.1) == "gh-token"


def test_resolve_github_token_returns_empty_when_gh_missing(monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("aidor.model_history.shutil.which", lambda name: None)

    assert _resolve_github_token(gh_binary="gh", timeout_s=0.1) == ""


def test_discover_supported_models_prefers_acp_catalog(monkeypatch):
    monkeypatch.setattr(
        "aidor.model_history._discover_models_via_acp",
        lambda **_: [ModelInfo("alpha-live", "Alpha Live")],
    )
    monkeypatch.setattr(
        "aidor.model_history._discover_models_via_rest",
        lambda **_: (_ for _ in ()).throw(AssertionError("REST fallback should not run")),
    )

    assert discover_supported_models() == [ModelInfo("alpha-live", "Alpha Live")]


def test_discover_supported_models_falls_back_to_rest_endpoint_chain(monkeypatch):
    calls: list[str] = []

    def fake_get_json(url: str, *, token: str, timeout_s: float):
        calls.append(url)
        assert token == "tok"
        if url.endswith("/copilot_internal/user"):
            return {"endpoints": {"api": "https://api.enterprise.githubcopilot.com"}}
        if url == "https://api.enterprise.githubcopilot.com/models":
            return {
                "data": [
                    {
                        "id": "gpt-live",
                        "name": "GPT Live",
                        "model_picker_enabled": True,
                        "model_picker_category": "powerful",
                    }
                ]
            }
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("aidor.model_history._discover_models_via_acp", lambda **_: [])
    monkeypatch.setattr("aidor.model_history._resolve_github_token", lambda **_: "tok")
    monkeypatch.setattr("aidor.model_history._get_json", fake_get_json)

    assert discover_supported_models() == [ModelInfo("gpt-live", "GPT Live", "powerful")]
    assert calls == [
        "https://api.github.com/copilot_internal/user",
        "https://api.enterprise.githubcopilot.com/models",
    ]


def test_discover_supported_models_fails_closed_without_endpoint(monkeypatch):
    monkeypatch.setattr("aidor.model_history._discover_models_via_acp", lambda **_: [])
    monkeypatch.setattr("aidor.model_history._resolve_github_token", lambda **_: "tok")
    monkeypatch.setattr("aidor.model_history._get_json", lambda *_, **__: {"endpoints": {}})

    assert discover_supported_models() == []


def test_get_json_success(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(
        "aidor.model_history.urllib.request.urlopen", lambda *_, **__: FakeResponse()
    )

    assert _get_json("https://example.invalid/models", token="tok", timeout_s=0.1) == {"ok": True}


def test_get_json_fails_closed_on_invalid_json(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b"{not json"

    monkeypatch.setattr(
        "aidor.model_history.urllib.request.urlopen", lambda *_, **__: FakeResponse()
    )

    assert _get_json("https://example.invalid/models", token="tok", timeout_s=0.1) == {}
