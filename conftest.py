"""
Suite-wide test guards.

Tests must never write to the real ``~/.autoforge`` (the user's project
registry and settings). Many code paths open the registry on first use, so
the whole session runs with HOME pointed at a temporary directory; this is
set here, at import, before any test module (or a module-level path in it)
is loaded. The guard below still checks the real directory: if it did not
exist when the session started and exists at the end, something bypassed HOME.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_REAL_AUTOFORGE_HOME = Path.home() / ".autoforge"
_TEST_HOME = tempfile.mkdtemp(prefix="autoforge-test-home-")
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME  # what Path.home() reads on Windows


@pytest.fixture(autouse=True, scope="session")
def _real_autoforge_home_untouched():
    existed = _REAL_AUTOFORGE_HOME.exists()
    yield
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
    if not existed and _REAL_AUTOFORGE_HOME.exists():
        pytest.fail(
            f"A test created {_REAL_AUTOFORGE_HOME}; stub the registry or set HOME to a temp dir in that test",
            pytrace=False,
        )
