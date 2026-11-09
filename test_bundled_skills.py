"""
Bundled Skills Tests
====================

The third-party skills shipped in ``.claude/skills/`` (ui-ux-pro-max and three
superpowers skills) are adapted for AutoForge agents, and every agent prompt
names the installed skills and when to invoke them.

Run with: python -m pytest test_bundled_skills.py -v
"""

import subprocess
import sys
from pathlib import Path

import pytest

import project_skills
from project_skills import install_skills, list_library_skills
from prompts import with_project_skills
from security import ALLOWED_COMMANDS

THIRD_PARTY_SKILLS = [
    "ui-ux-pro-max",
    "test-driven-development",
    "systematic-debugging",
    "verification-before-completion",
]
BUNDLED_DIR = Path(__file__).parent / ".claude" / "skills"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(project_skills.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project


def test_third_party_skills_are_in_the_library(fake_home):
    library = {s["name"]: s for s in list_library_skills()}
    for name in THIRD_PARTY_SKILLS:
        assert library[name]["source"] == "bundled"
        assert library[name]["description"]


@pytest.mark.parametrize("name", THIRD_PARTY_SKILLS)
def test_third_party_skill_ships_its_license(name):
    assert "MIT License" in (BUNDLED_DIR / name / "LICENSE").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", THIRD_PARTY_SKILLS)
def test_skill_text_fits_an_autonomous_project_agent(name):
    """No plugin-only paths, no plugin-prefixed skill names, no human to ask."""
    for md in (BUNDLED_DIR / name).rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        assert "CLAUDE_PLUGIN_ROOT" not in text, md
        assert "superpowers:" not in text, md
        assert "human partner" not in text, md


def test_ui_ux_search_script_runs_from_an_installed_project(fake_home, project_dir):
    """The command the skill documents works in a project that installed it."""
    installed, _ = install_skills(project_dir, ["ui-ux-pro-max"])
    assert installed == ["ui-ux-pro-max"]
    assert "python3" in ALLOWED_COMMANDS

    result = subprocess.run(
        [sys.executable, ".claude/skills/ui-ux-pro-max/scripts/search.py", "keyboard focus modal", "--domain", "ux"],
        cwd=project_dir, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "ux-guidelines.csv" in result.stdout


def test_ui_ux_skill_documents_the_project_relative_command():
    text = (BUNDLED_DIR / "ui-ux-pro-max" / "SKILL.md").read_text(encoding="utf-8")
    assert "python3 .claude/skills/ui-ux-pro-max/scripts/search.py" in text


class TestPromptSkillPointer:
    def test_no_project_or_no_skills_leaves_prompt_unchanged(self, project_dir):
        assert with_project_skills("BASE", None) == "BASE"
        assert with_project_skills("BASE", project_dir) == "BASE"

    def test_required_playwright_skill_alone_adds_nothing(self, fake_home, project_dir):
        install_skills(project_dir, ["playwright-cli"])
        assert with_project_skills("BASE", project_dir) == "BASE"

    def test_installed_skills_are_named_with_when_to_use_them(self, fake_home, project_dir):
        install_skills(project_dir, ["ui-ux-pro-max", "systematic-debugging"])
        prompt = with_project_skills("BASE", project_dir)
        assert prompt.endswith("BASE")
        assert "## PROJECT SKILLS" in prompt
        assert "`ui-ux-pro-max`: before building or changing any user interface" in prompt
        assert "`systematic-debugging`: when a test fails" in prompt

    def test_unknown_skill_is_listed_by_name(self, project_dir):
        skill = project_dir / ".claude" / "skills" / "my-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: my-skill\ndescription: x\n---\n", encoding="utf-8")
        assert "- `my-skill`\n" in with_project_skills("BASE", project_dir)

    def test_yolo_mode_drops_test_driven_development(self, fake_home, project_dir):
        install_skills(project_dir, ["test-driven-development", "verification-before-completion"])
        assert "`test-driven-development`" in with_project_skills("BASE", project_dir)
        yolo = with_project_skills("BASE", project_dir, yolo_mode=True)
        assert "`test-driven-development`" not in yolo
        assert "`verification-before-completion`" in yolo

    def test_planted_skills_link_is_not_followed(self, fake_home, project_dir, tmp_path):
        outside = tmp_path / "outside" / "skills"
        (outside / "evil").mkdir(parents=True)
        (outside / "evil" / "SKILL.md").write_text("---\nname: evil\n---\n", encoding="utf-8")
        (project_dir / ".claude").mkdir()
        (project_dir / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
        assert with_project_skills("BASE", project_dir) == "BASE"
