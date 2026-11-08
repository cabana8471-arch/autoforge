"""
npm Package Whitelist Guard
===========================

The npm package (``autoforge-ai``) ships only the paths listed in
``package.json`` ``files``. A repo-root Python module that shipped code imports
(often lazily, inside a function) but that is missing from the list works from
a source checkout and fails with ``ModuleNotFoundError`` for npm users.

This test collects every import in the shipped Python files and asserts that
each repo-root module or package it names is shipped too.

Run with: python -m pytest test_package_files.py -v
"""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _package_files() -> list[str]:
    files: list[str] = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["files"]
    return files


def _shipped_python_files(entries: list[str]) -> list[Path]:
    files = []
    for entry in entries:
        if entry.startswith("!"):
            continue
        path = ROOT / entry
        if entry.endswith("/") and path.is_dir():
            files += [p for p in path.rglob("*.py") if "__pycache__" not in p.parts]
        elif entry.endswith(".py") and path.is_file():
            files.append(path)
    # .npmignore drops test files from the tarball
    return [p for p in files if not p.name.startswith("test_")]


def _imported_top_level_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _missing_root_modules() -> list[str]:
    """Return repo-root modules/packages imported by shipped code but not shipped (transitively)."""
    entries = _package_files()
    shipped = set(entries)
    to_scan = _shipped_python_files(entries)
    scanned: set[Path] = set()
    missing: set[str] = set()

    while to_scan:
        path = to_scan.pop()
        if path in scanned:
            continue
        scanned.add(path)
        for name in _imported_top_level_names(path):
            module = ROOT / f"{name}.py"
            package = ROOT / name / "__init__.py"
            if module.is_file() and f"{name}.py" not in shipped:
                missing.add(f"{name}.py")
                to_scan.append(module)
            elif package.is_file() and f"{name}/" not in shipped:
                missing.add(f"{name}/")
                to_scan += [p for p in (ROOT / name).rglob("*.py") if "__pycache__" not in p.parts]

    return sorted(missing)


def test_imported_root_modules_are_in_package_files() -> None:
    missing = _missing_root_modules()

    assert not missing, (
        "Shipped code imports repo-root modules that package.json 'files' omits "
        f"(npm users would get ModuleNotFoundError): {missing}"
    )


def test_guard_detects_imports_inside_functions(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "def handler():\n"
        "    from structured_logging import get_log_query\n"
        "    import api.database\n"
        "    from . import sibling\n",
        encoding="utf-8",
    )

    assert _imported_top_level_names(source) == {"os", "pathlib", "structured_logging", "api"}


def test_playwright_cli_install_is_pinned_everywhere() -> None:
    """playwright-cli warns when the project's skill differs from the one its version ships.

    The repo's ``.claude/skills/playwright-cli`` (copied into every project) matches one
    release, so every installer must install that release, not the latest.
    """
    import re

    pinned = "@playwright/cli@0.1.23"
    for name in ("lib/cli.js", "start.sh", "start.bat", "start_ui.sh", "start_ui.bat"):
        text = (ROOT / name).read_text(encoding="utf-8")
        mentions = re.findall(r"@playwright/cli(?:@[\w.]+)?", text)
        assert mentions, name
        assert all(m == pinned for m in mentions), (name, mentions)
