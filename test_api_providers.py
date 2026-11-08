"""
API Provider Environment Tests
==============================

Tests for get_effective_sdk_env() in registry.py: GLM (Zhipu AI / Z.ai) and
Gemini (through a LiteLLM proxy).

Run with: python -m pytest test_api_providers.py -v
"""

import unittest
from unittest.mock import patch

import pytest

import registry
from registry import API_PROVIDERS, get_effective_sdk_env


def _env_for(settings: dict[str, str]) -> dict[str, str]:
    with patch("registry.get_all_settings", return_value=settings):
        return get_effective_sdk_env()


class TestGlmProvider(unittest.TestCase):
    def test_glm_4x_models_listed(self):
        model_ids = {m["id"] for m in API_PROVIDERS["glm"]["models"]}
        self.assertTrue({"glm-4.7", "glm-4.6", "glm-4.5-air"} <= model_ids)

    def test_glm_defaults(self):
        env = _env_for({"api_provider": "glm", "api_auth_token": "secret"})
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        for tier in ("OPUS", "SONNET", "HAIKU"):
            self.assertEqual(env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"], "glm-4.7")
        self.assertEqual(env["API_TIMEOUT_MS"], "3000000")

    def test_glm_custom_model_and_china_endpoint(self):
        env = _env_for({
            "api_provider": "glm",
            "api_model": "glm-4.5",
            "api_base_url": "https://open.bigmodel.cn/api/anthropic",
        })
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://open.bigmodel.cn/api/anthropic")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "glm-4.5")

    def test_explicit_timeout_overrides_default(self):
        env = _env_for({"api_provider": "glm", "api_timeout_ms": "600000"})
        self.assertEqual(env["API_TIMEOUT_MS"], "600000")

    def test_other_providers_get_no_default_timeout(self):
        env = _env_for({"api_provider": "kimi"})
        self.assertNotIn("API_TIMEOUT_MS", env)



@pytest.fixture
def settings(monkeypatch):
    """Patch the settings store with an in-memory dict."""
    store: dict[str, str] = {}
    monkeypatch.setattr(registry, "get_all_settings", lambda: dict(store))
    return store


def test_gemini_provider_definition():
    gemini = API_PROVIDERS["gemini"]
    assert gemini["base_url"] == "http://localhost:4000"
    assert gemini["auth_env_var"] == "ANTHROPIC_AUTH_TOKEN"
    assert gemini["default_model"] in {m["id"] for m in gemini["models"]}


def test_gemini_env_defaults(settings):
    settings.update({"api_provider": "gemini", "api_auth_token": "sk-proxy"})
    env = get_effective_sdk_env()

    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-proxy"
    # Real Anthropic key must not leak to the proxy
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["CLAUDE_CODE_USE_VERTEX"] == ""
    for tier in ("OPUS", "SONNET", "HAIKU"):
        assert env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] == "gemini-2.5-pro"


def test_gemini_env_overrides(settings):
    settings.update({
        "api_provider": "gemini",
        "api_base_url": "http://proxy.internal:8080",
        "api_model": "gemini-2.5-flash",
    })
    env = get_effective_sdk_env()

    assert env["ANTHROPIC_BASE_URL"] == "http://proxy.internal:8080"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "gemini-2.5-flash"
    assert "ANTHROPIC_AUTH_TOKEN" not in env


if __name__ == "__main__":
    unittest.main()
