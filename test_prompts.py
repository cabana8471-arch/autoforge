"""
Prompt Tests
============

- Scripted regression-test instructions injected into coding and testing
  prompts (issue #157).
- Testing-agent prompt content for browser and command-line verification and
  the v3 -> v4 prompt migration (issue #136).

Migration idempotency across versions lives in test_prompt_migrations.py.

Run with:
    python -m pytest test_prompts.py -v
"""

import tempfile
import unittest
from pathlib import Path

import prompts
from prompts import (
    _CLI_TESTING_STEP2,
    CURRENT_MIGRATION_VERSION,
    REGRESSION_SCRIPT_CODING_SECTION,
    REGRESSION_TESTS_DIR,
    _add_regression_script_section,
    _migrate_testing_prompt_to_v4,
    get_batch_feature_prompt,
    get_coding_prompt,
    get_single_feature_prompt,
    get_testing_prompt,
    migrate_project_to_current,
)


def test_coding_prompt_includes_regression_script_step_before_step_6():
    prompt = get_coding_prompt()
    assert "STEP 5.8: SAVE A REGRESSION TEST SCRIPT" in prompt
    assert prompt.count("STEP 5.8:") == 1
    assert prompt.index("STEP 5.8:") < prompt.index("### STEP 6:")
    assert f"{REGRESSION_TESTS_DIR}/feature-<ID>.spec.ts" in prompt


def test_coding_prompt_yolo_mode_has_no_regression_script_step():
    prompt = get_coding_prompt(yolo_mode=True)
    assert "STEP 5.8:" not in prompt


def test_single_and_batch_prompts_include_regression_script_step():
    assert "STEP 5.8:" in get_single_feature_prompt(7)
    assert "STEP 5.8:" in get_batch_feature_prompt([1, 2])
    assert "STEP 5.8:" not in get_single_feature_prompt(7, yolo_mode=True)
    assert "STEP 5.8:" not in get_batch_feature_prompt([1, 2], yolo_mode=True)


def test_add_section_appends_when_custom_prompt_has_no_step_6():
    custom = "## My custom coding prompt\n\nDo the work."
    result = _add_regression_script_section(custom)
    assert result.startswith(custom)
    assert result.endswith(REGRESSION_SCRIPT_CODING_SECTION)


def test_testing_prompt_runs_scripts_first():
    prompt = get_testing_prompt(testing_feature_ids=[3, 5])
    assert prompt.startswith("## SCRIPTED REGRESSION TESTS (RUN THESE FIRST)")
    assert f"npx playwright test {REGRESSION_TESTS_DIR}/feature-<ID>.spec.ts" in prompt
    # The original testing workflow (manual fallback) is still present
    assert "## YOUR ROLE - TESTING AGENT" in prompt
    assert "3, 5" in prompt
    assert "{{TESTING_FEATURE_IDS}}" not in prompt


def test_testing_prompt_header_applies_to_single_and_unassigned_modes():
    assert get_testing_prompt(testing_feature_id=9).startswith("## SCRIPTED REGRESSION TESTS")
    assert get_testing_prompt().startswith("## SCRIPTED REGRESSION TESTS")


def test_project_specific_prompts_also_get_regression_instructions(tmp_path: Path):
    prompts_dir = tmp_path / ".autoforge" / "prompts"
    prompts_dir.mkdir(parents=True)
    (prompts_dir / "coding_prompt.md").write_text("custom coding\n\n### STEP 6: MARK\n", encoding="utf-8")
    (prompts_dir / "testing_prompt.md").write_text("custom testing {{TESTING_FEATURE_IDS}}", encoding="utf-8")

    coding = get_coding_prompt(tmp_path)
    assert coding.startswith("custom coding")
    assert coding.index("STEP 5.8:") < coding.index("### STEP 6: MARK")

    testing = get_testing_prompt(tmp_path, testing_feature_ids=[4])
    assert testing.startswith("## SCRIPTED REGRESSION TESTS")
    assert testing.endswith("custom testing 4")



def test_regression_step_defers_to_scenario_script_for_non_ui_features():
    """#333's non-UI features keep a bash scenario script; STEP 5.8 must not demand Playwright for them."""
    section = REGRESSION_SCRIPT_CODING_SECTION
    assert "tests/feature_<id>_test.sh" in section
    assert section.index("tests/feature_<id>_test.sh") < section.index("@playwright/test")


def test_testing_header_runs_scenario_scripts_for_non_ui_features():
    header = get_testing_prompt(testing_feature_ids=[3]).split("---", 1)[0]
    assert "bash tests/feature_<ID>_test.sh" in header
    assert f"npx playwright test {REGRESSION_TESTS_DIR}/feature-<ID>.spec.ts" in header

# Minimal reproduction of a project testing_prompt.md scaffolded from the v1 template
V1_TESTING_PROMPT = (
    "## YOUR ROLE - TESTING AGENT\n\n"
    "### Workflow for EACH feature:\n"
    "1. Call `feature_get_by_id` with the feature ID\n"
    "2. Read the feature's verification steps\n"
    "3. Test the feature in the browser\n"
    "4. Call `feature_mark_passing` or `feature_mark_failing`\n\n"
    "---\n\n"
    + _CLI_TESTING_STEP2
    + "### STEP 3: HANDLE RESULTS\n\n"
    "**Quality Bar:**\n- Zero console errors\n- All verification steps pass\n"
    "- Visual appearance correct\n- API calls succeed\n\n"
    "**If you find a regression:**\n"
    "3. Verify the fix with browser automation\n"
)


class TestTestingPromptTemplate(unittest.TestCase):
    def test_template_covers_browser_and_cli_verification(self):
        prompt = get_testing_prompt(testing_feature_ids=[1, 2])
        self.assertIn("1, 2", prompt)
        self.assertIn("#### 2A: Browser Automation", prompt)
        self.assertIn("#### 2B: Command-Line Verification", prompt)
        # Key steps requested in issue #136
        self.assertIn("Plan the success criteria BEFORE running anything", prompt)
        self.assertIn("simulated user input", prompt)
        self.assertIn("exit code", prompt)
        self.assertIn("bash tests/feature_<id>_test.sh", prompt)

    def test_template_step2_is_bounded_by_step3(self):
        section = prompts._get_template_section(
            "testing_prompt", r"### STEP 2: VERIFY THE FEATURE", r"### STEP 3:"
        )
        self.assertIsNotNone(section)
        assert section is not None
        self.assertIn("#### 2B:", section)
        self.assertNotIn("### STEP 3:", section)


class TestTestingPromptMigrationV4(unittest.TestCase):
    def test_migrates_unmodified_v1_prompt(self):
        updated = _migrate_testing_prompt_to_v4(V1_TESTING_PROMPT)
        self.assertNotIn(_CLI_TESTING_STEP2, updated)
        self.assertIn("#### 2B: Command-Line Verification", updated)
        self.assertIn("### STEP 3: HANDLE RESULTS", updated)
        self.assertNotIn("3. Test the feature in the browser\n", updated)
        self.assertIn("CLI / non-UI: expected exit codes", updated)
        self.assertIn("or your scenario script (CLI / non-UI)", updated)

    def test_migration_is_idempotent(self):
        once = _migrate_testing_prompt_to_v4(V1_TESTING_PROMPT)
        self.assertEqual(once, _migrate_testing_prompt_to_v4(once))

    def test_current_template_is_unchanged_by_migration(self):
        template = (prompts.TEMPLATES_DIR / "testing_prompt.template.md").read_text(encoding="utf-8")
        self.assertEqual(template, _migrate_testing_prompt_to_v4(template))

    def test_customized_step2_is_preserved(self):
        custom = V1_TESTING_PROMPT.replace(
            "**CRITICAL:** You MUST verify the feature through the actual UI using browser automation.",
            "**CRITICAL:** Run `make e2e` to verify.",
        )
        updated = _migrate_testing_prompt_to_v4(custom)
        self.assertIn("Run `make e2e` to verify.", updated)
        self.assertNotIn("#### 2B:", updated)

    def test_migrate_project_to_current_updates_testing_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            prompts_dir = project_dir / ".autoforge" / "prompts"
            prompts_dir.mkdir(parents=True)
            # v4 is the only pending step (v1-v3 ran on an earlier start)
            (project_dir / ".autoforge" / ".migration_version").write_text("3")
            testing_prompt = prompts_dir / "testing_prompt.md"
            testing_prompt.write_text(V1_TESTING_PROMPT, encoding="utf-8")

            migrated = migrate_project_to_current(project_dir)

            self.assertEqual(len(migrated), 1)
            self.assertIn("#### 2B:", testing_prompt.read_text(encoding="utf-8"))
            version = (project_dir / ".autoforge" / ".migration_version").read_text()
            self.assertEqual(version, str(CURRENT_MIGRATION_VERSION))
            # Second run is a no-op
            self.assertEqual(migrate_project_to_current(project_dir), [])


if __name__ == "__main__":
    unittest.main()
