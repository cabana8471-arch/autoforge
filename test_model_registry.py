#!/usr/bin/env python3
"""
Model Registry Tests
====================

The Claude model list offered in Settings, the default model, and the
migration of stored legacy model IDs to current ones.

Run with: python -m pytest test_model_registry.py -v
"""

import os
import re
from pathlib import Path

import pytest

import registry

CURRENT_CLAUDE_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"}
SUPERSEDED_IDS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5-20251101",
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-sonnet-4-5-20250929",
    "claude-haiku-4-5",
]


def test_available_models_are_current_generation():
    assert {m["id"] for m in registry.AVAILABLE_MODELS} == CURRENT_CLAUDE_MODELS


def test_display_names_carry_the_version():
    # "Claude Opus" alone hid which generation was selected.
    for model in registry.AVAILABLE_MODELS:
        assert re.search(r"\d", model["name"]), model


@pytest.mark.skipif(bool(os.getenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "").strip()), reason="env pins the default")
def test_default_model_is_opus_5_5():
    assert registry.DEFAULT_MODEL == "claude-opus-5-5"


def test_claude_provider_offers_the_same_models():
    claude = registry.API_PROVIDERS["claude"]
    assert claude["models"] == registry.AVAILABLE_MODELS
    assert claude["default_model"] == "claude-opus-5-5"


def test_azure_provider_models_are_valid():
    azure = registry.API_PROVIDERS["azure"]
    assert {m["id"] for m in azure["models"]} <= CURRENT_CLAUDE_MODELS
    assert azure["default_model"] == "claude-opus-5-5"


@pytest.mark.parametrize("old_id", SUPERSEDED_IDS)
def test_superseded_ids_migrate_to_a_valid_model(old_id):
    assert registry.LEGACY_MODEL_MAP[old_id] in CURRENT_CLAUDE_MODELS


def test_legacy_targets_are_never_legacy_themselves():
    # A target that is itself legacy would leave settings on a removed model.
    for old_id, new_id in registry.LEGACY_MODEL_MAP.items():
        assert new_id not in registry.LEGACY_MODEL_MAP, (old_id, new_id)
        assert new_id in registry.VALID_MODELS, (old_id, new_id)


def test_tier_is_kept_when_migrating():
    for old_id, new_id in registry.LEGACY_MODEL_MAP.items():
        tier = old_id.split("-")[1]
        assert new_id.split("-")[1] == tier, (old_id, new_id)


def test_stored_settings_are_migrated_on_read():
    registry.set_setting("model", "claude-opus-4-7")
    registry.set_setting("testing_model", "claude-haiku-4-5")
    settings = registry.get_all_settings()
    assert settings["model"] == "claude-opus-5-5"
    assert settings["testing_model"] == "claude-haiku-5-5"
    # Persisted, not only rewritten in the returned dict.
    assert registry.get_setting("model") == "claude-opus-5-5"


def test_ui_placeholder_defaults_match_the_backend():
    """The UI shows these until /api/settings answers; they drifted before."""
    source = (Path(__file__).parent / "ui" / "src" / "hooks" / "useProjects.ts").read_text(encoding="utf-8")
    placeholder_ids = set(re.findall(r"id: '(claude-[a-z0-9-]+)'", source))
    assert placeholder_ids == CURRENT_CLAUDE_MODELS
    assert "'claude-opus-4" not in source and "'claude-sonnet-4" not in source
    assert "default: 'claude-opus-5-5'" in source
