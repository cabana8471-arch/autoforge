"""
Unit tests for the spec creation conversation transcript (spec_input_summary.md).
"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from server.services.spec_chat_session import (
    SPEC_CONVERSATION_FILENAME,
    SpecChatSession,
    render_spec_conversation_markdown,
)

STARTED = datetime(2026, 1, 2, 3, 4, 5)


class TestRenderSpecConversationMarkdown(unittest.TestCase):
    def test_merges_streamed_assistant_chunks(self):
        messages = [
            {"role": "assistant", "content": "Hi! ", "timestamp": "t1"},
            {"role": "assistant", "content": "What are you building?", "timestamp": "t2"},
            {"role": "user", "content": "A todo app", "timestamp": "t3"},
            {"role": "assistant", "content": "Pick one:\n1. Quick\n2. Detailed", "timestamp": "t4"},
            {"role": "user", "content": "Quick", "timestamp": "t5"},
        ]
        md = render_spec_conversation_markdown("todo", messages, STARTED, complete=False)

        assert md.startswith("# Spec Creation Conversation: todo\n")
        assert "**Status:** In progress" in md
        assert "**Started:** 2026-01-02T03:04:05" in md
        assert "Hi! What are you building?" in md
        assert md.count("### Claude") == 2
        assert md.count("### You") == 2
        assert "1. Quick\n2. Detailed" in md
        # Initial prompt is the user's first message
        initial = md.split("## Initial Prompt")[1].split("## Conversation")[0]
        assert initial.strip() == "A todo app"

    def test_complete_status_and_attachments(self):
        messages = [
            {"role": "user", "content": "See mockup", "attachments": ["mock.png"], "timestamp": "t1"},
        ]
        md = render_spec_conversation_markdown("p", messages, STARTED, complete=True)
        assert "**Status:** Complete" in md
        assert "_Attachments: `mock.png`_" in md

    def test_no_user_messages_omits_initial_prompt(self):
        messages = [{"role": "assistant", "content": "Hello", "timestamp": "t1"}]
        md = render_spec_conversation_markdown("p", messages, STARTED, complete=False)
        assert "## Initial Prompt" not in md
        assert "Hello" in md


class TestSaveConversationSummary(unittest.TestCase):
    def test_writes_file_to_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp)
            session = SpecChatSession("p", project_dir)
            # Not started yet: nothing is written
            session._save_conversation_summary()
            assert not any(project_dir.rglob(SPEC_CONVERSATION_FILENAME))

            session._summary_path = project_dir / ".autoforge" / "prompts" / SPEC_CONVERSATION_FILENAME
            session.messages.append({"role": "user", "content": "Build a blog"})
            session._save_conversation_summary()

            content = session._summary_path.read_text(encoding="utf-8")
            assert "Build a blog" in content

    def test_does_not_write_through_symlink(self):
        # The coding agent can write inside the project, including symlinks; the
        # server must not follow one out of the project (same rule as the
        # .autocoder migration and prompt migrations).
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            prompts = project_dir / ".autoforge" / "prompts"
            prompts.mkdir(parents=True)
            outside = Path(tmp) / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            (prompts / SPEC_CONVERSATION_FILENAME).symlink_to(outside)

            session = SpecChatSession("p", project_dir)
            session._summary_path = prompts / SPEC_CONVERSATION_FILENAME
            session.messages.append({"role": "user", "content": "Build a blog"})
            session._save_conversation_summary()

            assert outside.read_text(encoding="utf-8") == "keep"

    def test_does_not_write_through_symlinked_prompts_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "project"
            (project_dir / ".autoforge").mkdir(parents=True)
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (project_dir / ".autoforge" / "prompts").symlink_to(outside, target_is_directory=True)

            session = SpecChatSession("p", project_dir)
            session._summary_path = project_dir / ".autoforge" / "prompts" / SPEC_CONVERSATION_FILENAME
            session.messages.append({"role": "user", "content": "Build a blog"})
            session._save_conversation_summary()

            assert list(outside.iterdir()) == []


if __name__ == "__main__":
    unittest.main()
