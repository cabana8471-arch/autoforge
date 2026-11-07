"""
Project Detach Endpoint Tests
=============================

Regression tests for the detach/reattach endpoints in server/routers/projects.py.
They unpack the tuple returned by _get_registry_functions(); a stale tuple
length made every call fail with HTTP 500 instead of reaching its own checks.
Run with: python -m pytest test_projects_detach_endpoints.py
"""

import asyncio

import pytest
from fastapi import HTTPException

import registry
from server.routers import projects


@pytest.mark.parametrize(
    "endpoint",
    [projects.get_detach_status, projects.detach_project, projects.reattach_project],
)
def test_unknown_project_returns_404(monkeypatch, endpoint):
    monkeypatch.setattr(registry, "get_project_path", lambda name: None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint("ghost-project"))

    assert exc_info.value.status_code == 404
