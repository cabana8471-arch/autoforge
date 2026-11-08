"""
Project files sent to an agent prompt or the UI are never read through a link
=============================================================================

The server reads agent-writable project files (prompts, the app spec, reports,
configs, snapshots, exports) and sends their content to an agent prompt or to
the UI. A symlink or hard link the agent planted there would make it send a file
from outside the project. Each test plants such a link to a "secret" outside the
project and checks that the secret never comes back.

Run with: python -m pytest test_project_read_sites.py
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from api import safe_project_io as sio

pytestmark = pytest.mark.skipif(not sio.DIR_FD_SUPPORTED, reason="needs dir_fd support (POSIX)")

SECRET = "s3cr3t-AKIAABCDEFGHIJKLMNOP"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / ".autoforge" / "prompts").mkdir(parents=True)
    return folder


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "secret.txt"
    path.parent.mkdir()
    path.write_text(SECRET, encoding="utf-8")
    return path


def _plant(link: Path, secret: Path, hard: bool = False) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if hard:
        os.link(secret, link)
    else:
        link.symlink_to(secret)


def _no_secret(value: Any) -> bool:
    return SECRET not in json.dumps(value, default=str)


# ---------------------------------------------------------------------------
# Agent prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hard", [False, True])
def test_load_prompt_refuses_linked_prompt_with_clear_error(project: Path, secret: Path, hard: bool) -> None:
    """Neither the secret nor (silently) the generic template: the project's own prompt is refused."""
    import prompts

    _plant(project / ".autoforge" / "prompts" / "coding_prompt.md", secret, hard)
    with pytest.raises(sio.UnsafeProjectPathError, match="Refusing to load .*coding_prompt.md"):
        prompts.load_prompt("coding_prompt", project)


def test_load_prompt_refuses_symlinked_prompts_dir(project: Path, tmp_path: Path) -> None:
    import prompts

    mine = tmp_path / "myprompts"
    mine.mkdir()
    (mine / "coding_prompt.md").write_text("MY CUSTOM prompt")
    (project / ".autoforge" / "prompts").rmdir()
    (project / ".autoforge" / "prompts").symlink_to(mine, target_is_directory=True)

    with pytest.raises(sio.UnsafeProjectPathError, match="replace the link"):
        prompts.load_prompt("coding_prompt", project)


def test_load_prompt_still_falls_back_when_project_has_no_prompt(project: Path) -> None:
    import prompts

    text = prompts.load_prompt("coding_prompt", project)
    assert text == (prompts.TEMPLATES_DIR / "coding_prompt.template.md").read_text(encoding="utf-8")


def test_get_app_spec_refuses_linked_spec(project: Path, secret: Path) -> None:
    import prompts

    _plant(project / ".autoforge" / "prompts" / "app_spec.txt", secret)
    with pytest.raises(FileNotFoundError):
        prompts.get_app_spec(project)


def test_assistant_prompt_skips_linked_app_spec(project: Path, secret: Path) -> None:
    from server.services.assistant_chat_session import get_system_prompt

    _plant(project / ".autoforge" / "prompts" / "app_spec.txt", secret, hard=True)
    assert SECRET not in get_system_prompt("demo", project)


# ---------------------------------------------------------------------------
# UI endpoints
# ---------------------------------------------------------------------------


def test_project_prompts_endpoint_skips_linked_prompt(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import projects

    _plant(project / ".autoforge" / "prompts" / "app_spec.txt", secret)
    registry_functions = (None, None, lambda _name: project, None, None, None, None, None, None)
    monkeypatch.setattr(projects, "_get_registry_functions", lambda: registry_functions)

    result = asyncio.run(projects.get_project_prompts("demo"))

    assert result.app_spec == ""


def test_spec_status_endpoint_refuses_linked_status_file(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import spec_creation

    secret.write_text(json.dumps({"status": SECRET}), encoding="utf-8")
    _plant(project / ".autoforge" / "prompts" / ".spec_status.json", secret)
    monkeypatch.setattr(spec_creation, "_get_project_path", lambda _name: project)

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(spec_creation.get_spec_file_status("demo"))
    assert excinfo.value.status_code == 500


def test_doc_content_refuses_hard_link_and_delete_stays_in_project(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import documentation

    monkeypatch.setattr(documentation, "get_project_dir", lambda _name: project)
    _plant(project / "README.md", secret, hard=True)
    outside_doc = secret.parent / "notes.md"
    outside_doc.write_text("keep", encoding="utf-8")
    (project / "docs").symlink_to(secret.parent, target_is_directory=True)

    with pytest.raises(HTTPException):
        asyncio.run(documentation.get_doc_content("demo", "README.md"))
    for name in ("docs/notes.md", str(outside_doc)):
        with pytest.raises(HTTPException):
            asyncio.run(documentation.delete_doc("demo", name))

    assert outside_doc.read_text(encoding="utf-8") == "keep"


def test_workflow_content_refuses_linked_workflow(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import cicd

    _plant(project / ".github" / "workflows" / "ci.yml", secret)
    monkeypatch.setattr(cicd, "_get_project_path", lambda _name: project)

    with pytest.raises(HTTPException):
        asyncio.run(cicd.get_workflow_content("demo", "ci.yml"))


def test_report_endpoints_refuse_linked_reports(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import review, security
    from server.routers import visual_regression as visual

    secret.write_text(json.dumps({"summary": {"note": SECRET}, "review_time": SECRET}), encoding="utf-8")
    _plant(project / ".autocoder" / "review-reports" / "review_1.json", secret)
    _plant(project / ".autocoder" / "security-reports" / "security_scan_1.json", secret)
    _plant(project / ".visual-snapshots" / "reports" / "visual_test_1.json", secret)
    for module in (review, visual):
        monkeypatch.setattr(module, "get_project_dir", lambda _name: project)
    monkeypatch.setattr(security, "_get_project_path", lambda _name: project)

    assert _no_secret(review.list_reports("demo"))
    assert _no_secret(asyncio.run(visual.list_reports("demo")))
    calls = [
        lambda: review.get_report("demo", "review_1.json"),
        lambda: asyncio.run(security.get_report("demo", "security_scan_1.json")),
        lambda: asyncio.run(security.get_latest_report("demo")),
        lambda: asyncio.run(visual.get_report("demo", "visual_test_1.json")),
    ]
    for call in calls:
        with pytest.raises(HTTPException):
            call()


def test_report_deletes_do_not_follow_symlinked_report_dirs(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import review
    from server.routers import visual_regression as visual

    outside = secret.parent
    (outside / "review_1.json").write_text("keep")
    (outside / "visual_test_1.json").write_text("keep")
    (project / ".autocoder").mkdir()
    (project / ".autocoder" / "review-reports").symlink_to(outside, target_is_directory=True)
    (project / ".visual-snapshots").mkdir()
    (project / ".visual-snapshots" / "reports").symlink_to(outside, target_is_directory=True)
    for module in (review, visual):
        monkeypatch.setattr(module, "get_project_dir", lambda _name: project)

    with pytest.raises(HTTPException):
        review.delete_report("demo", "review_1.json")
    with pytest.raises(HTTPException):
        asyncio.run(visual.delete_report("demo", "visual_test_1.json"))

    assert (outside / "review_1.json").exists() and (outside / "visual_test_1.json").exists()


def test_snapshot_and_log_downloads_refuse_links(
    project: Path, secret: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.routers import logs
    from server.routers import visual_regression as visual

    _plant(project / ".visual-snapshots" / "baselines" / "home_desktop.png", secret)
    _plant(project / ".autocoder" / "exports" / "logs_demo.jsonl", secret, hard=True)
    monkeypatch.setattr(visual, "get_project_dir", lambda _name: project)
    monkeypatch.setattr(logs, "_get_project_path", lambda _name: project)

    with pytest.raises(HTTPException):
        asyncio.run(visual.get_snapshot("demo", "baselines", "home_desktop.png"))
    with pytest.raises(HTTPException):
        asyncio.run(logs.download_export("demo", "logs_demo.jsonl"))


def test_log_download_streams_a_real_export(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Streamed from the safely opened descriptor (exports can be large), not loaded whole."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    from server.routers import logs

    export = project / ".autocoder" / "exports" / "logs_demo.jsonl"
    export.parent.mkdir(parents=True)
    content = b'{"m": 1}\n' * 20_000  # several chunks
    export.write_bytes(content)
    monkeypatch.setattr(logs, "_get_project_path", lambda _name: project)

    assert isinstance(asyncio.run(logs.download_export("demo", "logs_demo.jsonl")), StreamingResponse)
    app = FastAPI()
    app.include_router(logs.router)
    response = TestClient(app).get("/api/logs/demo/download/logs_demo.jsonl")

    assert response.status_code == 200 and response.content == content
    assert response.headers["content-disposition"] == 'attachment; filename="logs_demo.jsonl"'


def test_browser_view_screenshot_lives_outside_the_project(
    project: Path, secret: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """playwright-cli writes the capture into a private server folder, not into the
    project, so a link planted in .playwright-cli/ redirects neither the write nor the read."""
    import base64
    import sys

    from server.services.browser_view_service import BrowserViewService, SessionInfo

    # Stand-in for playwright-cli: records where it was asked to write and writes a PNG there
    seen = tmp_path / "seen.txt"
    cli = tmp_path / "fake-playwright-cli"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "target = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--filename='))\n"
        f"open({str(seen)!r}, 'w').write(target)\n"
        "open(target, 'wb').write(b'PNGDATA')\n"
    )
    cli.chmod(0o755)
    _plant(project / ".playwright-cli" / "_view_s1.png", secret)  # must play no part

    service = BrowserViewService("demo", project)
    sent: list[Any] = []

    async def collect(screenshot: Any) -> None:
        sent.append(screenshot)

    service._screenshot_callbacks.add(collect)
    monkeypatch.setattr(service, "_get_playwright_cli", lambda: str(cli))
    session = SessionInfo(session_name="s1", agent_index=0, agent_type="coding", feature_id=1, feature_name="f")

    asyncio.run(service._capture_and_deliver(session))

    target = Path(seen.read_text())
    assert not target.resolve().is_relative_to(project.resolve())
    assert len(sent) == 1 and base64.b64decode(sent[0].image_base64) == b"PNGDATA"
    assert not target.exists()  # removed after delivery
    assert secret.read_text(encoding="utf-8") == SECRET
    private_dir = service._screenshot_dir
    assert private_dir is not None and private_dir.is_dir()
    asyncio.run(service.stop())
    assert not private_dir.exists()


# ---------------------------------------------------------------------------
# Content that ends up in the features database, configs, manifests, reports, docs
# ---------------------------------------------------------------------------


def test_json_feature_migration_refuses_linked_file(project: Path, secret: Path) -> None:
    from api.database import Feature, create_database
    from api.migration import migrate_json_to_sqlite

    secret.write_text(json.dumps([{"category": "c", "name": SECRET, "description": "d", "steps": []}]))
    _plant(project / "feature_list.json", secret)
    _engine, session_maker = create_database(project)

    assert migrate_json_to_sqlite(project, session_maker) is False
    session = session_maker()
    try:
        assert session.query(Feature).count() == 0
    finally:
        session.close()


def test_configs_and_tokens_ignore_linked_files(project: Path, secret: Path) -> None:
    import design_tokens
    from server.services import autocoder_config, project_config

    secret.write_text(json.dumps({"dev_command": SECRET, "colors": {"primary": SECRET}}))
    _plant(project / ".autoforge" / "config.json", secret)
    _plant(project / ".autoforge" / "design-tokens.json", secret, hard=True)

    assert project_config._load_config(project) == {}
    assert _no_secret(autocoder_config.load_autocoder_config(project))
    assert _no_secret(design_tokens.DesignTokensManager(project).load().colors)


def test_detach_status_ignores_linked_manifest(project: Path, secret: Path) -> None:
    import detach

    secret.write_text(json.dumps({"project_name": SECRET, "files": []}))
    _plant(project / detach.BACKUP_DIR / detach.MANIFEST_FILE, secret)

    assert detach.get_backup_info(project) is None


def test_security_scan_does_not_read_linked_source(project: Path, secret: Path) -> None:
    from security_scanner import SecurityScanner

    secret.write_text(f'api_key = "{SECRET}"\n')
    _plant(project / "config.py", secret)
    (project / "real.py").write_text('api_key = "abcdefghijklmnopqrstuvwxyz123"\n')

    result = SecurityScanner(project).scan(scan_dependencies=False, save_report=False)

    files = {v.file_path for v in result.vulnerabilities}
    assert "real.py" in files and "config.py" not in files
    assert _no_secret(result.to_dict())


def test_security_scan_reports_skipped_linked_files(project: Path, secret: Path) -> None:
    """A skipped file must show in the report: a 'clean' scan would be a false result."""
    from security_scanner import SecurityScanner

    _plant(project / "config.py", secret)
    _plant(project / "hard.py", secret, hard=True)

    result = SecurityScanner(project).scan(scan_dependencies=False, save_report=False)

    assert sorted(result.skipped_files) == ["config.py", "hard.py"]
    assert result.summary["skipped_files"] == 2
    assert result.to_dict()["skipped_files"] == result.skipped_files


def test_review_does_not_read_linked_source(project: Path, secret: Path) -> None:
    from review_agent import ReviewAgent

    secret.write_text(f'x = eval("{SECRET}")\n')  # findings quote the offending line
    _plant(project / "app.py", secret)
    (project / "real.py").write_text('y = eval("1")\n')

    report = ReviewAgent(project).review()

    assert {issue.file_path for issue in report.issues} == {"real.py"}
    assert _no_secret(report.to_dict())
    # Reported as skipped, not as reviewed
    assert report.skipped_files == ["app.py"] and "app.py" not in report.files_reviewed
    assert report.to_dict()["summary"]["skipped_files"] == 1


def test_generated_docs_skip_linked_env_file(project: Path, secret: Path) -> None:
    from auto_documentation import DocumentationGenerator

    secret.write_text(f"{SECRET}=1\n")
    _plant(project / ".env.example", secret)

    assert _no_secret(DocumentationGenerator(project).generate().environment_vars)
