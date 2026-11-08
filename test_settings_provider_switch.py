"""
Switching the API provider in Settings
======================================

Base URL and auth token are single global settings shared by every provider.
On a provider switch both reset, so neither the previous provider's URL nor
its secret key is ever sent to the new provider's server.

Run with: python -m pytest test_settings_provider_switch.py
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.routers import settings as settings_router


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    data: dict[str, str] = {}
    monkeypatch.setattr(settings_router, "get_setting", lambda key, default=None: data.get(key, default))
    monkeypatch.setattr(settings_router, "set_setting", lambda key, value: data.__setitem__(key, value))
    monkeypatch.setattr(settings_router, "get_all_settings", lambda: dict(data))
    return data


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(settings_router.router)
    return TestClient(app)


def test_switch_resets_base_url_and_clears_the_old_key(store: dict[str, str], client: TestClient) -> None:
    store.update({"api_provider": "glm", "api_base_url": "https://open.bigmodel.cn/api/anthropic",
                  "api_auth_token": "glm-secret"})

    response = client.patch("/api/settings", json={"api_provider": "moonshot"})

    assert response.status_code == 200
    assert store["api_base_url"] == "https://api.moonshot.ai/anthropic"
    assert store["api_auth_token"] == ""
    assert response.json()["api_has_auth_token"] is False


def test_switch_to_custom_clears_the_previous_url(store: dict[str, str], client: TestClient) -> None:
    store.update({"api_provider": "glm", "api_base_url": "https://api.z.ai/api/anthropic"})
    client.patch("/api/settings", json={"api_provider": "custom"})
    assert store["api_base_url"] == ""


def test_same_provider_keeps_the_key(store: dict[str, str], client: TestClient) -> None:
    store.update({"api_provider": "glm", "api_auth_token": "glm-secret"})
    client.patch("/api/settings", json={"api_provider": "glm"})
    assert store["api_auth_token"] == "glm-secret"


def test_key_sent_with_the_switch_is_kept(store: dict[str, str], client: TestClient) -> None:
    store.update({"api_provider": "glm", "api_auth_token": "glm-secret"})
    client.patch("/api/settings", json={"api_provider": "moonshot", "api_auth_token": "  sk-moon\n"})
    assert store["api_auth_token"] == "sk-moon"


def test_pasted_base_url_is_trimmed(store: dict[str, str], client: TestClient) -> None:
    store.update({"api_provider": "moonshot"})
    client.patch("/api/settings", json={"api_base_url": " https://api.moonshot.cn/anthropic \n"})
    assert store["api_base_url"] == "https://api.moonshot.cn/anthropic"
