"""
Suite-wide test guards.

Tests must never write to the real ``~/.autoforge`` (the user's project
registry and settings). If the directory did not exist when the session
started and exists at the end, some test touched the real home directory.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _real_autoforge_home_untouched():
    home_dir = Path.home() / ".autoforge"
    existed = home_dir.exists()
    yield
    if not existed and home_dir.exists():
        pytest.fail(
            f"A test created {home_dir}; stub the registry or set HOME to a temp dir in that test",
            pytrace=False,
        )
