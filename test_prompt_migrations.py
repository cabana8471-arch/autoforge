"""
Prompt Migration Tests
======================

Version migrations in ``prompts.py`` rewrite users' existing project files in
place, once, on the next agent start. Each one must:

- be idempotent (a second run changes nothing and reports nothing),
- leave customized prompts byte-for-byte untouched,
- never write through a symlink planted inside the project.

v2 (#292): scaffold ``.autoforge/allowed_commands.yaml``.
v3 (#289): screenshots go to ``.autoforge/validation/``.
"""

from pathlib import Path

import pytest

import prompts
from prompts import (
    CURRENT_MIGRATION_VERSION,
    TEMPLATES_DIR,
    _get_migration_version,
    _set_migration_version,
    migrate_project_to_current,
    scaffold_project_prompts,
)

CUSTOM_CODING = "# My coding prompt\n\nTake screenshots with `playwright-cli screenshot` whenever you like.\n"
CUSTOM_TESTING = "# My testing prompt\n\n- `playwright-cli screenshot` - my own wording\n"


def _snapshot(project_dir: Path) -> dict[str, bytes]:
    """Every regular file under the project, keyed by relative path."""
    return {
        str(p.relative_to(project_dir)): p.read_bytes()
        for p in sorted(project_dir.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _project_at(tmp_path: Path, version: int, coding: str, testing: str) -> Path:
    prompts_dir = tmp_path / ".autoforge" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "coding_prompt.md").write_text(coding, encoding="utf-8")
    (prompts_dir / "testing_prompt.md").write_text(testing, encoding="utf-8")
    (prompts_dir / "app_spec.txt").write_text("<project_specification/>", encoding="utf-8")
    _set_migration_version(tmp_path, version)
    return tmp_path


def _pre_v3_template(name: str, replacements: list[tuple[str, str]]) -> str:
    """Rebuild the template wording a project scaffolded before v3 has."""
    content = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    for old, new in replacements:
        assert new in content, f"{name} no longer contains a v3 replacement target:\n{new}"
        content = content.replace(new, old)
    return content


# ---------------------------------------------------------------------------
# v2: allowed_commands.yaml
# ---------------------------------------------------------------------------


class TestV2AllowedCommands:
    def test_second_run_changes_nothing(self, tmp_path: Path):
        project = _project_at(tmp_path, 1, CUSTOM_CODING, CUSTOM_TESTING)
        assert "Created .autoforge/allowed_commands.yaml" in prompts._migrate_v1_to_v2(project)
        before = _snapshot(project)

        assert prompts._migrate_v1_to_v2(project) == []
        assert _snapshot(project) == before

    def test_custom_prompts_untouched(self, tmp_path: Path):
        project = _project_at(tmp_path, 1, CUSTOM_CODING, CUSTOM_TESTING)
        prompts._migrate_v1_to_v2(project)

        prompts_dir = project / ".autoforge" / "prompts"
        assert (prompts_dir / "coding_prompt.md").read_text(encoding="utf-8") == CUSTOM_CODING
        assert (prompts_dir / "testing_prompt.md").read_text(encoding="utf-8") == CUSTOM_TESTING


# ---------------------------------------------------------------------------
# v3: screenshots in .autoforge/validation/
# ---------------------------------------------------------------------------


@pytest.fixture
def pre_v3_project(tmp_path: Path) -> Path:
    coding = _pre_v3_template("coding_prompt.template.md", prompts._VALIDATION_CODING_REPLACEMENTS)
    testing = _pre_v3_template("testing_prompt.template.md", prompts._VALIDATION_TESTING_REPLACEMENTS)
    return _project_at(tmp_path, 2, coding, testing)


class TestV3ValidationDir:
    def test_current_version_is_at_least_3(self):
        assert CURRENT_MIGRATION_VERSION >= 3

    def test_default_prompts_converge_on_current_templates(self, pre_v3_project: Path):
        prompts._migrate_v2_to_v3(pre_v3_project)

        prompts_dir = pre_v3_project / ".autoforge" / "prompts"
        for prompt, template in (
            ("coding_prompt.md", "coding_prompt.template.md"),
            ("testing_prompt.md", "testing_prompt.template.md"),
        ):
            migrated = (prompts_dir / prompt).read_text(encoding="utf-8")
            assert migrated == (TEMPLATES_DIR / template).read_text(encoding="utf-8"), prompt

    def test_pre_v3_wording_had_no_validation_dir(self, pre_v3_project: Path):
        """The replacement tables cover every screenshot instruction in the templates."""
        prompts_dir = pre_v3_project / ".autoforge" / "prompts"
        for prompt in ("coding_prompt.md", "testing_prompt.md"):
            content = (prompts_dir / prompt).read_text(encoding="utf-8")
            screenshot_lines = [
                line for line in content.splitlines()
                if "playwright-cli screenshot" in line or "save screenshot" in line.lower()
            ]
            assert screenshot_lines, prompt
            assert not [line for line in screenshot_lines if ".autoforge/validation" in line], prompt

    def test_creates_validation_dir_and_gitignore_entry(self, pre_v3_project: Path):
        prompts._migrate_v2_to_v3(pre_v3_project)

        assert (pre_v3_project / ".autoforge" / "validation").is_dir()
        gitignore = (pre_v3_project / ".autoforge" / ".gitignore").read_text(encoding="utf-8")
        assert "validation/" in gitignore.splitlines()

    def test_second_run_changes_nothing(self, pre_v3_project: Path):
        assert prompts._migrate_v2_to_v3(pre_v3_project)
        before = _snapshot(pre_v3_project)

        assert prompts._migrate_v2_to_v3(pre_v3_project) == []
        assert _snapshot(pre_v3_project) == before

    def test_custom_prompts_untouched(self, tmp_path: Path):
        project = _project_at(tmp_path, 2, CUSTOM_CODING, CUSTOM_TESTING)
        prompts._migrate_v2_to_v3(project)

        prompts_dir = project / ".autoforge" / "prompts"
        assert (prompts_dir / "coding_prompt.md").read_text(encoding="utf-8") == CUSTOM_CODING
        assert (prompts_dir / "testing_prompt.md").read_text(encoding="utf-8") == CUSTOM_TESTING

    def test_symlinked_prompt_is_not_written_through(self, tmp_path: Path, pre_v3_project: Path):
        outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
        coding = pre_v3_project / ".autoforge" / "prompts" / "coding_prompt.md"
        outside.write_text(coding.read_text(encoding="utf-8"), encoding="utf-8")
        original = outside.read_text(encoding="utf-8")
        coding.unlink()
        coding.symlink_to(outside)

        prompts._migrate_v2_to_v3(pre_v3_project)

        assert outside.read_text(encoding="utf-8") == original


class TestMigrationChain:
    def test_v1_project_runs_every_step(self, tmp_path: Path):
        coding = _pre_v3_template("coding_prompt.template.md", prompts._VALIDATION_CODING_REPLACEMENTS)
        testing = _pre_v3_template("testing_prompt.template.md", prompts._VALIDATION_TESTING_REPLACEMENTS)
        project = _project_at(tmp_path, 1, coding, testing)

        migrated = migrate_project_to_current(project)

        assert "Created .autoforge/allowed_commands.yaml" in migrated
        assert (project / ".autoforge" / "validation").is_dir()
        assert _get_migration_version(project) == CURRENT_MIGRATION_VERSION
        assert migrate_project_to_current(project) == []

    def test_new_project_is_stamped_current_with_validation_dir(self, tmp_path: Path):
        scaffold_project_prompts(tmp_path)

        assert _get_migration_version(tmp_path) == CURRENT_MIGRATION_VERSION
        assert (tmp_path / ".autoforge" / "validation").is_dir()
        assert migrate_project_to_current(tmp_path) == []

    def test_v1_cli_text_is_picked_up_by_v3(self):
        """A v0 project gets v1's CLI text, which v3 must then recognize."""
        coding = prompts._CLI_STEP5_CONTENT + prompts._CLI_BROWSER_SECTION
        testing = prompts._CLI_TESTING_STEP2 + prompts._CLI_TESTING_TOOLS
        for old, _ in prompts._VALIDATION_CODING_REPLACEMENTS:
            assert old in coding, old
        for old, _ in prompts._VALIDATION_TESTING_REPLACEMENTS:
            assert old in testing, old
