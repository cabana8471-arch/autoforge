"""
autocoder_config reads the same config.json as project_config (#179).

Run with: python -m pytest test_autocoder_config_path.py
"""

import json

import pytest

from server.services import autocoder_config, project_config


@pytest.mark.parametrize("new_exists, legacy_exists", [(False, False), (True, False), (False, True), (True, True)])
def test_config_path_matches_project_config(tmp_path, new_exists, legacy_exists):
    for exists, folder in ((new_exists, ".autoforge"), (legacy_exists, ".autocoder")):
        if exists:
            (tmp_path / folder).mkdir()
            (tmp_path / folder / "config.json").write_text("{}", encoding="utf-8")

    assert autocoder_config._get_config_path(tmp_path) == project_config._get_config_path(tmp_path)


def test_new_project_config_lands_in_autoforge(tmp_path):
    assert autocoder_config._get_config_path(tmp_path) == tmp_path / ".autoforge" / "config.json"


def test_legacy_config_is_still_read(tmp_path):
    (tmp_path / ".autocoder").mkdir()
    (tmp_path / ".autocoder" / "config.json").write_text(
        json.dumps({"git_workflow": {"mode": "feature_branches"}}), encoding="utf-8"
    )

    assert autocoder_config.load_autocoder_config(tmp_path)["git_workflow"]["mode"] == "feature_branches"


def test_saving_keeps_project_config_keys(tmp_path):
    (tmp_path / ".autoforge").mkdir()
    (tmp_path / ".autoforge" / "config.json").write_text(json.dumps({"dev_command": "npm run dev"}), encoding="utf-8")

    autocoder_config.update_autocoder_config(tmp_path, {"git_workflow": {"mode": "feature_branches"}})

    saved = json.loads((tmp_path / ".autoforge" / "config.json").read_text(encoding="utf-8"))
    assert saved["dev_command"] == "npm run dev"
    assert saved["git_workflow"]["mode"] == "feature_branches"
