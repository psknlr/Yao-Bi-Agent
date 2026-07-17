"""Multi-provider model backend tests (v0.13): Poe / MiniMax / Azure OpenAI.

The named providers share the OpenAI-compatible wire path but differ in endpoint
defaults, auth-header conventions and error surfaces. These tests monkeypatch
``urllib.request.urlopen`` to capture the real outgoing request, so the URL
construction, headers and payload are asserted per provider without any network.
"""

from __future__ import annotations

import importlib
import json
from typing import Any, get_args

import pytest

from backend.llm.dao_client import (
    OPENAI_COMPATIBLE_BACKENDS,
    DaoBackend,
    DaoClient,
    DaoGenerationConfig,
    DaoRuntimeError,
)

_OPENAI_REPLY = {"choices": [{"message": {"content": "OK：模型回复（待医师复核）。"}}]}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self, limit: int | None = None) -> bytes:
        raw = json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
        return raw if limit is None else raw[:limit]

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


def _capture_urlopen(monkeypatch, payload: dict[str, Any]) -> dict[str, Any]:
    """Patch urlopen; return a dict that fills with the captured request on call."""

    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        # Request normalizes header capitalization — compare lowercased.
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def _clear_provider_env(monkeypatch) -> None:
    for name in (
        "TAO_BACKEND", "TAO_ENDPOINT_URL", "TAO_API_KEY", "TAO_MODEL_ID",
        "POE_API_KEY", "MINIMAX_API_KEY",
        "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT",
        "AZURE_OPENAI_API_VERSION", "TAO_AZURE_DEPLOYMENT", "TAO_AZURE_API_VERSION",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# from_env resolution
# ---------------------------------------------------------------------------

def test_from_env_poe_defaults(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "poe")
    monkeypatch.setenv("POE_API_KEY", "poe-key")
    config = DaoGenerationConfig.from_env()
    assert config.backend == "poe"
    assert config.api_key == "poe-key"
    assert config.endpoint_url == "https://api.poe.com/v1/chat/completions"


def test_from_env_tao_api_key_wins_over_provider_key(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "poe")
    monkeypatch.setenv("TAO_API_KEY", "unified-key")
    monkeypatch.setenv("POE_API_KEY", "poe-key")
    assert DaoGenerationConfig.from_env().api_key == "unified-key"


def test_from_env_minimax_defaults(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "minimax")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    config = DaoGenerationConfig.from_env()
    assert config.api_key == "mm-key"
    # v0.14: default is the current OpenAI-compatible surface (api.minimax.io); the
    # legacy chatcompletion_v2 endpoint is deprecated upstream but still reachable
    # via TAO_ENDPOINT_URL (as is mainland api.minimaxi.com).
    assert config.endpoint_url == "https://api.minimax.io/v1/chat/completions"


def test_from_env_azure_reads_conventional_variables(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "azure")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://myres.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-prod")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    config = DaoGenerationConfig.from_env()
    assert config.api_key == "az-key"
    assert config.endpoint_url == "https://myres.openai.azure.com"
    assert config.azure_deployment == "gpt-4o-prod"
    assert config.azure_api_version == "2024-10-21"


def test_from_env_explicit_endpoint_overrides_provider_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "poe")
    monkeypatch.setenv("POE_API_KEY", "poe-key")
    monkeypatch.setenv("TAO_ENDPOINT_URL", "https://proxy.internal/v1/chat/completions")
    assert DaoGenerationConfig.from_env().endpoint_url == "https://proxy.internal/v1/chat/completions"


# ---------------------------------------------------------------------------
# Wire shape per provider
# ---------------------------------------------------------------------------

def test_poe_request_shape(monkeypatch):
    captured = _capture_urlopen(monkeypatch, _OPENAI_REPLY)
    client = DaoClient(DaoGenerationConfig(
        backend="poe",
        endpoint_url="https://api.poe.com/v1/chat/completions",
        api_key="poe-key",
        model_id="Claude-Sonnet-4.5",
    ))
    reply = client.chat([], "你好")
    assert "模型回复" in reply
    assert captured["url"] == "https://api.poe.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer poe-key"
    assert captured["payload"]["model"] == "Claude-Sonnet-4.5"
    assert captured["payload"]["messages"][0]["role"] == "system"
    assert captured["payload"]["messages"][-1] == {"role": "user", "content": "你好"}


def test_minimax_request_shape_and_success(monkeypatch):
    captured = _capture_urlopen(monkeypatch, {**_OPENAI_REPLY, "base_resp": {"status_code": 0, "status_msg": "success"}})
    client = DaoClient(DaoGenerationConfig(
        backend="minimax",
        endpoint_url="https://api.minimax.chat/v1/text/chatcompletion_v2",
        api_key="mm-key",
        model_id="MiniMax-Text-01",
    ))
    reply = client.chat([], "你好")
    assert "模型回复" in reply
    assert captured["url"] == "https://api.minimax.chat/v1/text/chatcompletion_v2"
    assert captured["headers"]["authorization"] == "Bearer mm-key"
    assert captured["payload"]["model"] == "MiniMax-Text-01"


def test_minimax_http200_error_surface_raises(monkeypatch):
    # MiniMax reports failures as HTTP 200 + base_resp.status_code != 0 — this must
    # become a DaoRuntimeError (deterministic fallback), never an empty completion.
    _capture_urlopen(monkeypatch, {"base_resp": {"status_code": 1004, "status_msg": "鉴权失败"}})
    client = DaoClient(DaoGenerationConfig(
        backend="minimax",
        endpoint_url="https://api.minimax.chat/v1/text/chatcompletion_v2",
        api_key="bad-key",
    ))
    with pytest.raises(DaoRuntimeError, match="1004"):
        client.chat([], "你好")


def test_azure_builds_deployment_url_and_api_key_header(monkeypatch):
    captured = _capture_urlopen(monkeypatch, _OPENAI_REPLY)
    client = DaoClient(DaoGenerationConfig(
        backend="azure",
        endpoint_url="https://myres.openai.azure.com",
        api_key="az-key",
        azure_deployment="gpt-4o-prod",
        azure_api_version="2024-06-01",
    ))
    client.chat([], "你好")
    assert captured["url"] == (
        "https://myres.openai.azure.com/openai/deployments/gpt-4o-prod/chat/completions?api-version=2024-06-01"
    )
    assert captured["headers"]["api-key"] == "az-key"
    assert "authorization" not in captured["headers"]
    # Azure selects the model via the deployment in the URL, not the payload.
    assert "model" not in captured["payload"]


def test_azure_full_completions_url_passthrough(monkeypatch):
    captured = _capture_urlopen(monkeypatch, _OPENAI_REPLY)
    url = "https://myres.openai.azure.com/openai/deployments/d1/chat/completions?api-version=2024-02-01"
    client = DaoClient(DaoGenerationConfig(backend="azure", endpoint_url=url, api_key="az-key"))
    client.chat([], "你好")
    assert captured["url"] == url  # no rebuilt path, no duplicated api-version


def test_azure_deployment_falls_back_to_model_id(monkeypatch):
    captured = _capture_urlopen(monkeypatch, _OPENAI_REPLY)
    client = DaoClient(DaoGenerationConfig(
        backend="azure", endpoint_url="https://myres.openai.azure.com/", api_key="az-key", model_id="gpt-4o",
    ))
    client.chat([], "你好")
    assert "/openai/deployments/gpt-4o/chat/completions" in captured["url"]


def test_generic_http_backend_still_supports_keyless_endpoints(monkeypatch):
    captured = _capture_urlopen(monkeypatch, _OPENAI_REPLY)
    client = DaoClient(DaoGenerationConfig(backend="http", endpoint_url="http://localhost:8000/v1/chat/completions"))
    client.chat([], "你好")
    assert "authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# Fail-fast configuration errors (clear remediation in the message)
# ---------------------------------------------------------------------------

def test_hosted_providers_require_api_key():
    poe = DaoClient(DaoGenerationConfig(backend="poe", endpoint_url="https://api.poe.com/v1/chat/completions"))
    with pytest.raises(DaoRuntimeError, match="POE_API_KEY"):
        poe.chat([], "你好")
    minimax = DaoClient(DaoGenerationConfig(backend="minimax", endpoint_url="https://api.minimax.chat/v1/text/chatcompletion_v2"))
    with pytest.raises(DaoRuntimeError, match="MINIMAX_API_KEY"):
        minimax.chat([], "你好")
    azure_no_key = DaoClient(DaoGenerationConfig(backend="azure", endpoint_url="https://myres.openai.azure.com"))
    with pytest.raises(DaoRuntimeError, match="AZURE_OPENAI_API_KEY"):
        azure_no_key.chat([], "你好")


def test_azure_requires_endpoint():
    client = DaoClient(DaoGenerationConfig(backend="azure", api_key="az-key"))
    with pytest.raises(DaoRuntimeError, match="AZURE_OPENAI_ENDPOINT"):
        client.chat([], "你好")


# ---------------------------------------------------------------------------
# Lifecycle + manifest + server integration
# ---------------------------------------------------------------------------

def test_provider_backends_report_ready_without_local_weights():
    for backend in ("poe", "minimax", "azure"):
        client = DaoClient(DaoGenerationConfig(backend=backend, endpoint_url="https://x", api_key="k"))  # type: ignore[arg-type]
        assert client.load_status()["state"] == "ready"
        assert client.preload()["ok"] is True


def test_manifest_backend_lists_match_code(monkeypatch):
    import yaml

    from backend import provenance

    expected = set(get_args(DaoBackend))
    assert expected == {"disabled", "mock", "transformers"} | OPENAI_COMPATIBLE_BACKENDS
    root = provenance.ROOT
    hermes = yaml.safe_load((root / "config" / "hermes_agent.yaml").read_text(encoding="utf-8"))
    model_cfg = yaml.safe_load((root / "config" / "model_config.yaml").read_text(encoding="utf-8"))
    assert set(hermes["tao_runtime"]["supported_backends"]) == expected
    assert set(model_cfg["runtime"]["supported_backends"]) == expected


def test_server_enables_tao_for_provider_backends(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("TAO_BACKEND", "poe")
    monkeypatch.setenv("POE_API_KEY", "poe-key")
    import backend.server as server_module

    server = importlib.reload(server_module)
    try:
        assert server.TAO_ENABLED is True
        info = server.tao_info()
        assert info["backend"] == "poe"
        assert info["load_state"] == "ready"
    finally:
        # Leave the module in the env-default state so later tests reloading with
        # their own TAO_BACKEND start from the same baseline as a fresh import.
        _clear_provider_env(monkeypatch)
        importlib.reload(server_module)
