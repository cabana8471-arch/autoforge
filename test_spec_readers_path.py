"""
Spec Reader Path Tests
======================

The app spec lives in ``.autoforge/prompts/app_spec.txt`` (legacy projects:
``prompts/app_spec.txt``). Every module that reads it must resolve the prompts
directory through ``autoforge_paths`` instead of hard-coding ``prompts/``.
"""

from pathlib import Path

import pytest

from app_spec_parser import get_ui_config_from_spec
from auto_documentation import DocumentationGenerator
from design_tokens import DesignTokensManager

SPEC = """<project_specification>
  <app_name>Spec Path App</app_name>
  <description>Reads the spec from the right place.</description>
  <ui_components>
    <library>shadcn-ui</library>
  </ui_components>
  <visual_style>
    <style>neobrutalism</style>
  </visual_style>
  <design_tokens>
    <colors>
      <primary>#123456</primary>
    </colors>
  </design_tokens>
</project_specification>
"""


@pytest.fixture(params=[".autoforge/prompts", "prompts"], ids=["autoforge-layout", "legacy-layout"])
def project(request, tmp_path: Path) -> Path:
    prompts_dir = tmp_path / request.param
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "app_spec.txt").write_text(SPEC, encoding="utf-8")
    return tmp_path


def test_ui_config_reads_spec(project: Path):
    config = get_ui_config_from_spec(project)
    assert config is not None
    assert config["library"] == "shadcn-ui"
    assert config["style"] == "neobrutalism"


def test_documentation_reads_spec(project: Path):
    docs = DocumentationGenerator(project).generate()
    assert docs.project_name == "Spec Path App"
    assert docs.description == "Reads the spec from the right place."


def test_design_tokens_read_spec(project: Path):
    tokens = DesignTokensManager(project).load()
    assert tokens.colors["primary"] == "#123456"


def test_template_apply_writes_spec_where_it_is_read(tmp_path: Path):
    """POST /api/templates/apply must write into the resolved prompts dir, not a stray prompts/."""
    import asyncio

    from autoforge_paths import get_prompts_dir
    from server.routers.templates import ApplyRequest, apply_template
    from templates import list_templates

    (tmp_path / ".autoforge" / "prompts").mkdir(parents=True)
    template_id = list_templates()[0].id

    response = asyncio.run(apply_template(ApplyRequest(
        template_id=template_id, project_name="tpl", project_dir=str(tmp_path), customizations=None,
    )))

    expected = get_prompts_dir(tmp_path) / "app_spec.txt"
    assert Path(response.app_spec_path) == expected
    assert expected.exists()
    assert not (tmp_path / "prompts").exists()
