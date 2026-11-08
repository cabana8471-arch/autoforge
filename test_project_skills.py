"""
Project Skills Tests
====================

Tests for the skill library and per-project skill installation. The user's
global ``~/.claude/skills`` is not a source (user decision 2A), and links the
agent planted in the project's ``.claude/`` are never followed.

Run with: python -m pytest test_project_skills.py -v
"""

from pathlib import Path

import pytest

import project_skills
from api import safe_project_io as sio
from project_skills import (
    install_skills,
    is_valid_skill_name,
    list_library_skills,
    list_project_skills,
    parse_skill_metadata,
    remove_skill,
)


def _make_skill(root: Path, name: str, description: str = "A test skill") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point the user skill libraries at a temp home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(project_skills.Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return project


class TestSkillNames:
    @pytest.mark.parametrize("name", ["frontend-design", "my_skill", "a", "skill.v2"])
    def test_valid(self, name):
        assert is_valid_skill_name(name)

    @pytest.mark.parametrize("name", ["", "..", "../etc", "a/b", "a\\b", ".hidden", "-x", "a..b", "x" * 65])
    def test_invalid(self, name):
        assert not is_valid_skill_name(name)


class TestParseMetadata:
    def test_parses_frontmatter(self, tmp_path):
        skill = _make_skill(tmp_path, "demo", "Does demo things")
        assert parse_skill_metadata(skill / "SKILL.md") == {"name": "demo", "description": "Does demo things"}

    def test_missing_frontmatter(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("# no frontmatter\n", encoding="utf-8")
        assert parse_skill_metadata(md) == {}

    def test_invalid_yaml(self, tmp_path):
        md = tmp_path / "SKILL.md"
        md.write_text("---\nname: [unclosed\n---\n", encoding="utf-8")
        assert parse_skill_metadata(md) == {}


class TestLibrary:
    def test_bundled_skills_listed(self, fake_home):
        names = {s["name"] for s in list_library_skills()}
        assert "playwright-cli" in names
        assert "frontend-design" in names
        # AutoForge-internal skill is not offered to generated projects
        assert "gsd-to-autoforge-spec" not in names

    def test_flags(self, fake_home):
        by_name = {s["name"]: s for s in list_library_skills()}
        assert by_name["playwright-cli"]["required"] is True
        assert by_name["frontend-design"]["source"] == "bundled"

    def test_user_skills_listed(self, fake_home):
        _make_skill(fake_home / ".autoforge" / "skills", "my-autoforge-skill")
        by_name = {s["name"]: s for s in list_library_skills()}
        assert by_name["my-autoforge-skill"]["source"] == "user"

    def test_global_claude_skills_are_not_a_source(self, fake_home, project_dir):
        """~/.claude/skills holds the user's own skills, which may carry private data (user decision 2A)."""
        _make_skill(fake_home / ".claude" / "skills", "my-claude-skill")
        assert "my-claude-skill" not in {s["name"] for s in list_library_skills()}
        assert install_skills(project_dir, ["my-claude-skill"]) == ([], ["my-claude-skill"])

    def test_bundled_takes_precedence(self, fake_home):
        _make_skill(fake_home / ".autoforge" / "skills", "frontend-design", "user copy")
        matches = [s for s in list_library_skills() if s["name"] == "frontend-design"]
        assert len(matches) == 1
        assert matches[0]["source"] == "bundled"

    def test_dirs_without_skill_md_ignored(self, fake_home):
        (fake_home / ".autoforge" / "skills" / "not-a-skill").mkdir(parents=True)
        assert "not-a-skill" not in {s["name"] for s in list_library_skills()}


class TestInstallRemove:
    def test_install_and_list(self, fake_home, project_dir):
        installed, skipped = install_skills(project_dir, ["frontend-design", "unknown-skill"])
        assert installed == ["frontend-design"]
        assert skipped == ["unknown-skill"]
        assert (project_dir / ".claude" / "skills" / "frontend-design" / "SKILL.md").is_file()
        assert [s["name"] for s in list_project_skills(project_dir)] == ["frontend-design"]

    def test_install_preserves_existing(self, fake_home, project_dir):
        install_skills(project_dir, ["frontend-design"])
        skill_md = project_dir / ".claude" / "skills" / "frontend-design" / "SKILL.md"
        skill_md.write_text("---\nname: frontend-design\n---\nlocal changes\n", encoding="utf-8")
        installed, skipped = install_skills(project_dir, ["frontend-design"])
        assert installed == []
        assert skipped == ["frontend-design"]
        assert "local changes" in skill_md.read_text(encoding="utf-8")

    def test_install_rejects_traversal(self, fake_home, project_dir):
        with pytest.raises(ValueError):
            install_skills(project_dir, ["../../etc"])

    def test_install_skips_symlinks(self, fake_home, project_dir, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("top secret", encoding="utf-8")
        skill = _make_skill(fake_home / ".autoforge" / "skills", "linky")
        try:
            (skill / "leak.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        install_skills(project_dir, ["linky"])
        dest = project_dir / ".claude" / "skills" / "linky"
        assert (dest / "SKILL.md").is_file()
        assert not (dest / "leak.txt").exists()

    def test_remove(self, fake_home, project_dir):
        install_skills(project_dir, ["frontend-design"])
        assert remove_skill(project_dir, "frontend-design") is True
        assert not (project_dir / ".claude" / "skills" / "frontend-design").exists()
        assert remove_skill(project_dir, "frontend-design") is False

    def test_remove_required_refused(self, project_dir):
        with pytest.raises(ValueError):
            remove_skill(project_dir, "playwright-cli")

    def test_remove_rejects_traversal(self, project_dir):
        with pytest.raises(ValueError):
            remove_skill(project_dir, "..")


needs_dir_fd = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")


@needs_dir_fd
class TestPlantedLinks:
    """The agent can write the project's .claude/: the server never follows a link planted there."""

    @pytest.fixture
    def outside(self, tmp_path):
        folder = tmp_path / "outside"
        _make_skill(folder, "frontend-design", "outside copy")
        return folder

    def test_install_does_not_write_through_a_symlinked_skills_dir(self, fake_home, project_dir, outside):
        (project_dir / ".claude").mkdir()
        (project_dir / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            install_skills(project_dir, ["playwright-cli"])
        assert not (outside / "playwright-cli").exists()

    def test_remove_does_not_delete_through_a_symlinked_skills_dir(self, project_dir, outside):
        (project_dir / ".claude").mkdir()
        (project_dir / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
        with pytest.raises(OSError):
            remove_skill(project_dir, "frontend-design")
        assert (outside / "frontend-design" / "SKILL.md").is_file()

    def test_remove_drops_a_symlinked_skill_not_its_target(self, project_dir, outside):
        skills = project_dir / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "frontend-design").symlink_to(outside / "frontend-design", target_is_directory=True)
        assert remove_skill(project_dir, "frontend-design") is True
        assert not (skills / "frontend-design").exists()
        assert (outside / "frontend-design" / "SKILL.md").is_file()

    def test_project_listing_does_not_read_through_links(self, project_dir, outside, tmp_path):
        skills = project_dir / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / "frontend-design").symlink_to(outside / "frontend-design", target_is_directory=True)
        hard = skills / "hard"
        hard.mkdir()
        secret = tmp_path / "secret.md"
        secret.write_text("---\nname: hard\ndescription: TOP SECRET\n---\n", encoding="utf-8")
        (hard / "SKILL.md").hardlink_to(secret)
        listed = list_project_skills(project_dir)
        assert "outside copy" not in str(listed)
        assert "TOP SECRET" not in str(listed)


def test_agents_can_invoke_installed_skills(tmp_path):
    """Skills in the project's .claude/skills/ need the Skill tool (allowed tools + permission rule)."""
    import json
    from unittest.mock import patch

    import client

    class _Abort:
        captured: list = []

        def __init__(self, options=None, **_kwargs):
            _Abort.captured.append(options)

        async def __aenter__(self):
            raise RuntimeError("stop")

        async def __aexit__(self, *args):
            return None

    project = tmp_path / "p"
    (project / ".autoforge").mkdir(parents=True)
    with (
        patch.object(client, "ClaudeSDKClient", _Abort),
        patch("registry.get_effective_sdk_env", return_value={}),
        patch("registry.get_effort_setting", return_value=None),
    ):
        client.create_client(project, "claude-opus-4-6")
    assert "Skill" in _Abort.captured[0].allowed_tools
    settings = json.loads((project / ".autoforge" / ".claude_settings.json").read_text(encoding="utf-8"))
    assert "Skill" in settings["permissions"]["allow"]


@needs_dir_fd
def test_router_answers_409_for_a_planted_link(fake_home, project_dir, tmp_path, monkeypatch):
    """A refused path is 409 (server-wide UnsafeProjectPathError handler), never a 500."""
    from fastapi.testclient import TestClient

    from server.main import app
    from server.routers import skills as skills_router

    outside = tmp_path / "outside"
    outside.mkdir()
    (project_dir / ".claude").mkdir()
    (project_dir / ".claude" / "skills").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(skills_router, "_get_project_path", lambda _name: project_dir)
    client = TestClient(app, client=("127.0.0.1", 50000))
    res = client.post("/api/projects/demo/skills", json={"names": ["frontend-design"]})
    assert res.status_code == 409
    assert client.delete("/api/projects/demo/skills/frontend-design").status_code == 409
    assert list(outside.iterdir()) == []
